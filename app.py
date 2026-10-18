"""Read-only log viewer. Start with: uv run python app.py"""

import argparse
import base64
import codecs
import hashlib
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator

BASE_DIR = Path(__file__).resolve().parent
READ_SIZE = 256 * 1024


class LogEntry(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=4096)

    @field_validator("name", "path", mode="before")
    @classmethod
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("path")
    @classmethod
    def absolute_path(cls, value: str) -> str:
        try:
            path = Path(value).expanduser()
        except (RuntimeError, OSError):
            raise ValueError("无法展开用户目录，请填写有效的绝对路径") from None
        if not path.is_absolute() or "\0" in value:
            raise ValueError("日志路径必须是当前服务器上的绝对路径")
        return str(path)


class Configuration(BaseModel):
    logs: list[LogEntry] = Field(default_factory=list)


def log_id(path: str) -> str:
    # Preserve the configured path so replacing a rotated file keeps its menu ID.
    normalized = os.path.normcase(os.path.normpath(path))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def public_entry(entry: LogEntry) -> dict:
    return {"id": log_id(entry.path), **entry.model_dump()}


class ConfigStore:
    """Read the small configuration on demand; keep the last valid snapshot."""

    def __init__(self, path: Path):
        self.path = path
        self.entries: list[LogEntry] = []
        self.error: str | None = None
        self.lock = threading.RLock()

    def load(self) -> list[LogEntry]:
        with self.lock:
            try:
                config = Configuration.model_validate_json(
                    self.path.read_text(encoding="utf-8-sig")
                )
                ids = [log_id(entry.path) for entry in config.logs]
                if len(set(ids)) != len(ids):
                    raise ValueError("配置中包含重复的日志路径")
            except (OSError, UnicodeError, ValidationError, ValueError) as exc:
                # Never put configuration contents or validation input in a response.
                if isinstance(exc, FileNotFoundError):
                    self.error = "配置文件不存在，请创建配置文件后重试"
                elif isinstance(exc, PermissionError):
                    self.error = "没有读取配置文件的权限"
                else:
                    self.error = "配置文件无效，请检查 JSON 格式、名称和绝对路径是否正确且不重复"
            else:
                self.entries = config.logs
                self.error = None
            return list(self.entries)

    def find(self, entry_id: str) -> LogEntry:
        for entry in self.load():
            if log_id(entry.path) == entry_id:
                return entry
        raise HTTPException(404, "日志未登记或已从配置中移除")

    def add(self, entry: LogEntry) -> dict:
        with self.lock:
            entries = self.load()
            if self.error:
                raise HTTPException(409, "请先修复配置文件，再添加日志")
            if any(log_id(item.path) == log_id(entry.path) for item in entries):
                raise HTTPException(409, "该日志路径已经添加")
            # An open check gives the same permission result as reading the log.
            with open_log(entry.path):
                pass
            updated = [*entries, entry]
            payload = Configuration(logs=updated).model_dump_json(indent=2) + "\n"
            temporary = None
            try:
                # Same-directory replacement is atomic on Windows and Linux.
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.path.parent,
                    prefix=f".{self.path.name}.", suffix=".tmp", delete=False,
                ) as output:
                    temporary = Path(output.name)
                    output.write(payload)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.path)
            except OSError:
                raise HTTPException(500, "配置保存失败，请检查配置目录的写入权限") from None
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
            self.entries = updated
            return public_entry(entry)


def open_log(path: str) -> BinaryIO:
    try:
        # Refuse directories, devices and pipes (which could block a worker).
        if not Path(path).is_file():
            raise HTTPException(404, "文件不存在或不是普通文件")
        return open(path, "rb")
    except PermissionError:
        raise HTTPException(403, "没有读取该日志文件的权限") from None
    except FileNotFoundError:
        raise HTTPException(404, "日志文件不存在，可能正在轮转，请稍后重试") from None
    except OSError:
        raise HTTPException(400, "无法打开日志，请检查路径和文件状态") from None


def anchor(file: BinaryIO, offset: int) -> str:
    file.seek(max(0, offset - 64))
    return hashlib.sha256(file.read(min(64, offset))).hexdigest()[:20]


def encode_cursor(file: BinaryIO, entry_id: str, stat: os.stat_result, offset: int) -> str:
    data = {
        "id": entry_id,
        "file": f"{stat.st_dev}:{stat.st_ino}",
        "offset": offset,
        "anchor": anchor(file, offset),
    }
    raw = json.dumps(data, separators=(",", ":")).encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(token: str, entry_id: str) -> dict:
    try:
        if len(token) > 1024:
            raise ValueError
        raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        data = json.loads(raw)
        if (
            not isinstance(data, dict)
            or data.get("id") != entry_id
            or type(data.get("offset")) is not int
            or not 0 <= data["offset"] <= 2**63 - 1
            or not isinstance(data.get("file"), str)
            or not isinstance(data.get("anchor"), str)
        ):
            raise ValueError
        return data
    except (ValueError, TypeError, UnicodeError):
        raise HTTPException(400, "读取位置无效，请重新选择日志") from None


def read_text(file: BinaryIO, start: int, end: int, align_start: bool) -> tuple[str, int, int]:
    file.seek(start)
    data = file.read(end - start)
    if align_start and start > 0:
        # A tail/page may begin in the middle of a UTF-8 character.
        skipped = 0
        while skipped < min(3, len(data)) and data[skipped] & 0xC0 == 0x80:
            skipped += 1
        data = data[skipped:]
        start += skipped
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    text = decoder.decode(data, final=False)
    pending = decoder.getstate()[0]
    # Leave an incomplete final character for the next incremental request.
    next_offset = start + len(data) - len(pending)
    return text, start, next_offset


def read_content(entry: LogEntry, token: str | None, direction: str) -> dict:
    entry_id = log_id(entry.path)
    cursor = decode_cursor(token, entry_id) if token is not None else None
    if direction == "backward" and cursor is None:
        raise HTTPException(400, "加载更早内容需要有效的读取位置")
    with open_log(entry.path) as file:
        stat = os.fstat(file.fileno())
        reset = cursor is None
        if cursor is not None:
            offset = cursor["offset"]
            reset = (
                cursor["file"] != f"{stat.st_dev}:{stat.st_ino}"
                or offset > stat.st_size
                or cursor["anchor"] != anchor(file, offset)
            )
        if reset:
            end = stat.st_size
            start = max(0, end - READ_SIZE)
        elif direction == "backward":
            end = cursor["offset"]
            start = max(0, end - READ_SIZE)
        else:
            start = cursor["offset"]
            end = min(stat.st_size, start + READ_SIZE)
        try:
            text, start, next_offset = read_text(
                file, start, end, align_start=reset or direction == "backward"
            )
            return {
                "text": text,
                "cursor": encode_cursor(file, entry_id, stat, next_offset),
                "before_cursor": encode_cursor(file, entry_id, stat, start),
                "reset": reset,
                "has_more": start > 0,
                "has_newer": next_offset < stat.st_size,
                "truncated": reset and start > 0,
                "size": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            }
        except OSError:
            raise HTTPException(409, "日志正在变化，读取失败，请刷新重试") from None


def create_app(config_path: str | Path | None = None) -> FastAPI:
    config_path = Path(config_path or os.environ.get("LOG_MONITOR_CONFIG", BASE_DIR / "config.json"))
    store = ConfigStore(config_path.expanduser().resolve())
    application = FastAPI(title="日志查看器", docs_url=None, redoc_url=None, openapi_url=None)

    @application.exception_handler(RequestValidationError)
    async def invalid_parameters(request: Request, exc: RequestValidationError):
        # FastAPI's default validation response includes the submitted input.
        return JSONResponse(
            status_code=422,
            content={"detail": "请求参数无效，请检查名称、服务器绝对路径和读取参数"},
        )

    @application.middleware("http")
    async def private_responses(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @application.get("/", include_in_schema=False)
    def index():
        return FileResponse(BASE_DIR / "static" / "index.html")

    @application.get("/api/logs")
    def list_logs():
        with store.lock:
            entries = store.load()
            return {"logs": [public_entry(entry) for entry in entries], "config_error": store.error}

    @application.post("/api/logs", status_code=201)
    def add_log(entry: LogEntry, request: Request):
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            raise HTTPException(403, "请从日志查看器页面添加日志")
        return store.add(entry)

    @application.get("/api/logs/{entry_id}/content")
    def content(entry_id: str, cursor: str | None = None, direction: Literal["forward", "backward"] = "forward"):
        return read_content(store.find(entry_id), cursor, direction)

    application.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    return application


app = create_app()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="浏览器日志查看器")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=8000, help="监听端口，默认 8000")
    parser.add_argument(
        "--config", type=Path,
        default=Path(os.environ.get("LOG_MONITOR_CONFIG", BASE_DIR / "config.json")),
        help="JSON 配置文件路径",
    )
    options = parser.parse_args()
    uvicorn.run(create_app(options.config), host=options.host, port=options.port, log_level="info", access_log=False)
