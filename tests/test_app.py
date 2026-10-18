"""Exercise the viewer through HTTP using only temporary configuration and logs."""

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import create_app


@pytest.fixture
def viewer(tmp_path):
    config = tmp_path / "config.json"
    config.write_text('{"logs": []}', encoding="utf-8")
    with TestClient(create_app(config)) as client:
        yield client, config, tmp_path


def write_config(config: Path, *logs: Path):
    config.write_text(
        json.dumps(
            {"logs": [{"name": log.name, "path": str(log)} for log in logs]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def register(viewer, text="hello\n", name="application.log"):
    client, config, directory = viewer
    log = directory / name
    log.write_text(text, encoding="utf-8", newline="")
    write_config(config, log)
    response = client.get("/api/logs")
    assert response.status_code == 200
    return log, response.json()["logs"][0]["id"]


def read(client, log_id, **params):
    response = client.get(f"/api/logs/{log_id}/content", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def assert_rejected(response):
    assert 400 <= response.status_code < 500, response.text
    assert isinstance(response.json()["detail"], str)


def test_external_config_changes_are_loaded_and_bad_edits_keep_last_valid_list(viewer):
    client, config, directory = viewer
    first, first_id = register(viewer)
    second = directory / "second.log"
    second.write_text("another service\n", encoding="utf-8", newline="")
    write_config(config, first, second)

    current = client.get("/api/logs").json()
    assert {entry["path"] for entry in current["logs"]} == {str(first), str(second)}
    assert current["config_error"] is None

    config.write_text('{"logs": [', encoding="utf-8")
    invalid = client.get("/api/logs").json()
    assert invalid["logs"] == current["logs"]
    assert invalid["config_error"]
    assert read(client, first_id)["text"] == "hello\n"

    write_config(config, second)
    recovered = client.get("/api/logs").json()
    assert [entry["path"] for entry in recovered["logs"]] == [str(second)]
    assert recovered["config_error"] is None


def test_browser_addition_is_persisted_and_available_to_a_new_app(viewer):
    client, config, directory = viewer
    log = directory / "新增日志.log"
    log.write_text("服务已启动\n", encoding="utf-8", newline="")

    response = client.post("/api/logs", json={"name": "应用日志", "path": str(log)})
    assert response.status_code == 201, response.text
    entry = response.json()
    assert entry["name"] == "应用日志"
    assert entry["path"] == str(log)
    assert read(client, entry["id"])["text"] == "服务已启动\n"
    assert json.loads(config.read_text(encoding="utf-8"))["logs"] == [
        {"name": "应用日志", "path": str(log)}
    ]

    with TestClient(create_app(config)) as reopened:
        assert reopened.get("/api/logs").json()["logs"] == [entry]


def test_same_file_cannot_be_added_twice_under_a_different_name(viewer):
    client, config, _ = viewer
    log, _ = register(viewer)
    before = config.read_bytes()

    response = client.post("/api/logs", json={"name": "另一个名称", "path": str(log)})

    assert_rejected(response)
    assert config.read_bytes() == before
    assert len(client.get("/api/logs").json()["logs"]) == 1


def test_browser_addition_does_not_overwrite_an_invalid_external_config(viewer):
    client, config, directory = viewer
    register(viewer)
    log = directory / "new.log"
    log.write_text("new\n", encoding="utf-8", newline="")
    broken = b'{"logs": [\n  unfinished external edit'
    config.write_bytes(broken)

    response = client.post("/api/logs", json={"name": "新增", "path": str(log)})

    assert_rejected(response)
    assert config.read_bytes() == broken


@pytest.mark.parametrize("kind", ["directory", "missing", "relative"])
def test_adding_a_path_requires_an_existing_absolute_regular_file(viewer, kind):
    client, _, directory = viewer
    paths = {
        "directory": str(directory),
        "missing": str(directory / "missing.log"),
        "relative": "relative.log",
    }

    response = client.post("/api/logs", json={"name": "日志", "path": paths[kind]})

    assert_rejected(response)
    assert client.get("/api/logs").json()["logs"] == []


@pytest.mark.parametrize("kind", ["directory", "missing"])
def test_unavailable_files_from_external_config_report_a_read_error(viewer, kind):
    client, config, directory = viewer
    log = directory if kind == "directory" else directory / "gone.log"
    write_config(config, log)
    entries = client.get("/api/logs").json()["logs"]
    assert len(entries) == 1

    response = client.get(f"/api/logs/{entries[0]['id']}/content")

    assert_rejected(response)


def test_initial_read_and_forward_refresh_return_literal_text_and_only_new_bytes(viewer):
    client, _, _ = viewer
    original = '<script>alert("log text")</script>\n中文日志\n'
    log, log_id = register(viewer, original)

    initial = read(client, log_id)
    assert initial["text"] == original
    assert initial["reset"] is True
    assert initial["has_more"] is False
    assert initial["size"] == log.stat().st_size

    unchanged = read(client, log_id, cursor=initial["cursor"])
    assert unchanged["text"] == ""
    assert unchanged["reset"] is False

    with log.open("a", encoding="utf-8", newline="") as stream:
        stream.write("new entry\n")
    appended = read(client, log_id, cursor=unchanged["cursor"])
    assert appended["text"] == "new entry\n"
    assert appended["reset"] is False


def test_empty_file_can_be_followed_when_content_arrives(viewer):
    client, _, _ = viewer
    log, log_id = register(viewer, "")

    initial = read(client, log_id)
    assert initial["text"] == ""
    assert initial["size"] == 0
    assert initial["has_more"] is False

    log.write_bytes(b"first line\n")
    appended = read(client, log_id, cursor=initial["cursor"])
    assert appended["text"] == "first line\n"
    assert appended["reset"] is False


def test_large_file_starts_at_tail_and_backward_pages_reconstruct_the_file(viewer):
    client, _, _ = viewer
    expected = "".join(f"{number:06d}: a sample log line\n" for number in range(35000))
    _, log_id = register(viewer, expected)

    page = read(client, log_id)
    assert page["text"]
    assert len(page["text"].encode("utf-8")) <= 300 * 1024
    assert expected.endswith(page["text"])
    assert page["has_more"] is True
    combined = page["text"]

    for _ in range(10):
        if not page["has_more"]:
            break
        page = read(client, log_id, cursor=page["before_cursor"], direction="backward")
        assert page["text"], "Backward paging must always make progress"
        assert len(page["text"].encode("utf-8")) <= 300 * 1024
        combined = page["text"] + combined

    assert page["has_more"] is False
    assert combined == expected


def test_utf8_character_split_between_appends_is_emitted_once_when_complete(viewer):
    client, _, _ = viewer
    log, log_id = register(viewer, "start\n")
    initial = read(client, log_id)
    encoded = "中".encode("utf-8")

    with log.open("ab") as stream:
        stream.write(encoded[:2])
    partial = read(client, log_id, cursor=initial["cursor"])
    assert partial["text"] == ""

    with log.open("ab") as stream:
        stream.write(encoded[2:] + "文\n".encode("utf-8"))
    completed = read(client, log_id, cursor=partial["cursor"])
    assert completed["text"] == "中文\n"
    assert completed["reset"] is False
    assert read(client, log_id, cursor=completed["cursor"])["text"] == ""


def test_unicode_tail_and_backward_pages_do_not_split_chinese_or_emoji(viewer):
    client, _, _ = viewer
    expected = "".join(f"{number:05d} 日志检查🙂完成🚀\n" for number in range(30000))
    _, log_id = register(viewer, expected)

    page = read(client, log_id)
    assert page["has_more"] is True
    assert expected.endswith(page["text"])
    assert "\ufffd" not in page["text"]
    combined = page["text"]

    for _ in range(12):
        if not page["has_more"]:
            break
        page = read(client, log_id, cursor=page["before_cursor"], direction="backward")
        assert page["text"]
        assert "\ufffd" not in page["text"]
        combined = page["text"] + combined

    assert page["has_more"] is False
    assert combined == expected


def test_truncation_resets_to_the_new_file_contents(viewer):
    client, _, _ = viewer
    log, log_id = register(viewer, "old entry\n" * 20)
    initial = read(client, log_id)

    log.write_bytes(b"new\n")
    refreshed = read(client, log_id, cursor=initial["cursor"])

    assert refreshed["reset"] is True
    assert refreshed["text"] == "new\n"


def test_truncation_and_immediate_regrowth_on_same_file_resets(viewer):
    client, _, _ = viewer
    log, log_id = register(viewer, "old entry\n" * 20)
    initial = read(client, log_id)
    original_identity = log.stat().st_ino
    replacement = "new content after truncate\n" * 30

    with log.open("wb") as stream:
        stream.write(replacement.encode("utf-8"))
    assert log.stat().st_ino == original_identity
    assert log.stat().st_size > initial["size"]

    refreshed = read(client, log_id, cursor=initial["cursor"])

    assert refreshed["reset"] is True
    assert refreshed["text"] == replacement


def test_replacing_log_during_rotation_resets_even_when_replacement_is_larger(viewer):
    client, _, directory = viewer
    log, log_id = register(viewer, "old\n")
    initial = read(client, log_id)
    replacement = directory / "replacement.log"
    replacement.write_text("replacement file is longer\n", encoding="utf-8", newline="")
    log.replace(directory / "rotated.log")
    replacement.replace(log)

    refreshed = read(client, log_id, cursor=initial["cursor"])

    assert refreshed["reset"] is True
    assert refreshed["text"] == "replacement file is longer\n"


def test_invalid_utf8_bytes_are_replaced_without_losing_surrounding_text(viewer):
    client, _, _ = viewer
    log, log_id = register(viewer, "")
    raw = b"before\xff\xfeafter\n" + "中文🙂\n".encode("utf-8")
    log.write_bytes(raw)

    initial = read(client, log_id)

    assert initial["text"] == raw.decode("utf-8", errors="replace")
    with log.open("ab") as stream:
        stream.write(b"next\xffentry\n")
    appended = read(client, log_id, cursor=initial["cursor"])
    assert appended["text"] == "next\ufffdentry\n"
    assert appended["reset"] is False


def test_crlf_split_across_refreshes_is_preserved_exactly(viewer):
    client, _, _ = viewer
    log, log_id = register(viewer, "")
    chunks = [b"first\r", b"\nsecond\r", b"\nthird\r\n"]
    log.write_bytes(chunks[0])
    page = read(client, log_id)
    combined = page["text"]

    for chunk in chunks[1:]:
        with log.open("ab") as stream:
            stream.write(chunk)
        page = read(client, log_id, cursor=page["cursor"])
        assert page["reset"] is False
        combined += page["text"]

    assert combined == b"".join(chunks).decode("utf-8")


def test_unknown_log_ids_and_invalid_cursors_are_rejected(viewer):
    client, _, _ = viewer
    _, log_id = register(viewer)

    assert_rejected(client.get("/api/logs/unconfigured-file/content"))
    assert_rejected(
        client.get(f"/api/logs/{log_id}/content", params={"cursor": "invalid-cursor"})
    )
    assert_rejected(
        client.get(f"/api/logs/{log_id}/content", params={"direction": "invalid"})
    )


@pytest.mark.skipif(os.name != "posix", reason="chmod does not remove Windows read access")
def test_files_without_read_permission_are_rejected(viewer):
    client, _, _ = viewer
    log, log_id = register(viewer)
    old_mode = log.stat().st_mode
    log.chmod(0)
    try:
        if os.access(log, os.R_OK):
            pytest.skip("The current user bypasses file permissions")
        assert_rejected(client.get(f"/api/logs/{log_id}/content"))
        assert_rejected(client.post("/api/logs", json={"name": "无权限", "path": str(log)}))
    finally:
        log.chmod(old_mode)


def test_invalid_initial_config_can_be_corrected_without_restarting(viewer):
    _, config, directory = viewer
    config.write_text('{"logs": "not a list"}', encoding="utf-8")

    with TestClient(create_app(config)) as client:
        initial = client.get("/api/logs").json()
        assert initial["logs"] == []
        assert initial["config_error"]

        log = directory / "recovered.log"
        log.write_text("recovered\n", encoding="utf-8", newline="")
        write_config(config, log)
        recovered = client.get("/api/logs").json()
        assert recovered["config_error"] is None
        assert len(recovered["logs"]) == 1
        assert read(client, recovered["logs"][0]["id"])["text"] == "recovered\n"


def test_unresolvable_home_path_is_validation_error_and_keeps_last_valid_config(viewer, monkeypatch):
    client, config, _ = viewer
    register(viewer)
    previous = client.get("/api/logs").json()["logs"]
    original_expanduser = Path.expanduser

    def expanduser(path):
        if str(path).startswith("~missing-log-monitor-user"):
            raise RuntimeError("Could not determine home directory")
        return original_expanduser(path)

    monkeypatch.setattr(Path, "expanduser", expanduser)
    invalid_path = "~missing-log-monitor-user/app.log"
    assert_rejected(client.post("/api/logs", json={"name": "invalid", "path": invalid_path}))
    config.write_text(json.dumps({"logs": [{"name": "invalid", "path": invalid_path}]}), encoding="utf-8")
    response = client.get("/api/logs")
    assert response.status_code == 200
    assert response.json()["logs"] == previous
    assert response.json()["config_error"]
