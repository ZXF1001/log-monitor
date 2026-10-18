"use strict";

const $ = (id) => document.getElementById(id);
const ui = Object.fromEntries(
  [
    "log-list",
    "log-count",
    "list-empty",
    "list-status",
    "filter",
    "file-name",
    "file-path",
    "refresh",
    "config-error",
    "auto-refresh",
    "follow",
    "read-state",
    "read-dot",
    "read-error",
    "memory-note",
    "content",
    "history-bar",
    "history-description",
    "load-earlier",
    "latest",
    "empty-state",
    "empty-title",
    "empty-description",
    "empty-add",
    "file-size",
    "file-modified",
    "last-read",
    "add-dialog",
    "add-form",
    "add-name",
    "add-path",
    "add-error",
    "save-add",
  ].map((id) => [id, $(id)]),
);

// Keep the displayed text bounded; cursors remain opaque values owned by the backend.
const TEXT_LIMIT = 1024 * 1024;
let logs = [];
let selectedId = null;
let cursor = null;
let beforeCursor = null;
let hasEarlier = false;
let contentRequest = null;
let listing = false;
let listSignature = "";
let historyCropped = false;
let adding = false;
let listRevision = 0;
let textChunks = [];
let catchUpTimer = null;

async function api(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options });
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error(
      response.ok
        ? "服务器返回的数据无法解析"
        : `服务器返回错误（${response.status}）`,
    );
  }
  if (!response.ok) {
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : `请求失败（${response.status}）`,
    );
  }
  return data;
}

function errorText(error) {
  return error instanceof TypeError
    ? "无法连接后端，请检查服务或网络后重试"
    : error.message;
}

function showNotice(element, message) {
  element.textContent = message || "";
  element.hidden = !message;
}

function setReadState(text, status = "") {
  ui["read-state"].textContent = text;
  ui["read-dot"].className = `live-dot ${status}`;
}

function selectedLog() {
  return logs.find((log) => log.id === selectedId);
}

function updateHeader() {
  const log = selectedLog();
  ui["file-name"].textContent = log ? log.name : "开始查看日志";
  ui["file-path"].textContent = log
    ? log.path
    : "添加服务器上的日志文件，即可在这里查看";
  ui["file-path"].title = log ? log.path : "";
  ui.content.setAttribute(
    "aria-label",
    log ? `${log.name}的日志文本` : "日志文本",
  );
  document.title = log ? `${log.name} · 日志查看器` : "日志查看器";
}

function renderLogs(force = false) {
  const query = ui.filter.value.trim().toLocaleLowerCase();
  const signature = JSON.stringify([logs, query, selectedId]);
  if (!force && signature === listSignature) return;
  listSignature = signature;
  const visible = logs.filter((log) =>
    `${log.name}\n${log.path}`.toLocaleLowerCase().includes(query),
  );
  const fragment = document.createDocumentFragment();
  for (const log of visible) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `log-item${log.id === selectedId ? " active" : ""}`;
    button.setAttribute(
      "aria-current",
      log.id === selectedId ? "true" : "false",
    );
    button.title = `${log.name}\n${log.path}`;
    // The SVG is fixed markup; all configured values are assigned as plain text.
    button.innerHTML =
      '<svg class="log-icon" viewBox="0 0 20 24" aria-hidden="true"><path d="M4 2h8l5 5v15H3V2h1ZM12 2v6h5M7 12h6M7 16h6"/></svg>';
    const details = document.createElement("div");
    details.className = "log-details";
    const name = document.createElement("div");
    name.className = "log-name";
    name.textContent = log.name;
    const path = document.createElement("div");
    path.className = "log-path";
    path.textContent = log.path;
    details.append(name, path);
    button.append(details);
    button.addEventListener("click", () => selectLog(log.id));
    fragment.append(button);
  }
  ui["log-list"].replaceChildren(fragment);
  ui["log-count"].textContent = String(logs.length);
  ui["list-empty"].hidden = visible.length > 0;
  ui["list-empty"].textContent = logs.length
    ? "没有匹配的日志文件"
    : "还没有日志文件";
}

async function refreshLogs() {
  if (listing) return;
  listing = true;
  const revision = listRevision;
  try {
    const data = await api("/api/logs");
    if (revision !== listRevision) return;
    logs = data.logs;
    showNotice(
      ui["config-error"],
      data.config_error
        ? `配置读取异常：${data.config_error}。正在保留上一份有效列表。`
        : "",
    );
    ui["list-status"].textContent = "配置每 5 秒自动同步";
    ui["list-status"].parentElement.classList.remove("failed");
    if (!selectedLog()) {
      selectLog(logs[0]?.id ?? null);
    } else {
      updateHeader();
      renderLogs();
    }
  } catch (error) {
    ui["list-status"].textContent = "配置同步失败，稍后自动重试";
    ui["list-status"].parentElement.classList.add("failed");
    showNotice(ui["config-error"], errorText(error));
  } finally {
    listing = false;
    if (revision !== listRevision) refreshLogs();
  }
}

function updateControls() {
  const busy = contentRequest !== null;
  ui.refresh.disabled = !selectedId || busy;
  ui["load-earlier"].disabled = !hasEarlier || busy;
  ui.latest.disabled = !selectedId || busy;
  ui["history-bar"].hidden = !selectedId || (!hasEarlier && !historyCropped);
}

function showEmpty(title, description, addButton = false) {
  ui["empty-state"].hidden = false;
  ui["empty-title"].textContent = title;
  ui["empty-description"].textContent = description;
  ui["empty-add"].hidden = !addButton;
}

function selectLog(id) {
  if (id === selectedId && id !== null) return;
  contentRequest?.controller.abort();
  contentRequest = null;
  selectedId = id;
  cursor = null;
  beforeCursor = null;
  hasEarlier = false;
  historyCropped = false;
  textChunks = [];
  clearTimeout(catchUpTimer);
  ui.content.value = "";
  ui.follow.checked = true;
  ui["file-size"].textContent = "—";
  ui["file-modified"].textContent = "";
  ui["last-read"].textContent = "所有操作仅查看，不修改日志文件";
  showNotice(ui["read-error"], "");
  showNotice(ui["memory-note"], "");
  updateHeader();
  renderLogs();
  updateControls();
  if (id) {
    showEmpty("正在读取日志", "首次读取文件末尾内容，请稍候。");
    readContent("initial");
  } else {
    setReadState("等待选择文件");
    showEmpty(
      "让日志触手可及",
      "添加日志路径，开始查看文件内容。也可以直接修改后端的 config.json。",
      true,
    );
  }
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024)
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

function formatDate(value) {
  if (value == null) return "";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime())
    ? ""
    : date.toLocaleString("zh-CN", { hour12: false });
}

function newlineCount(text) {
  let count = 0;
  for (let i = 0; i < text.length; i++) if (text.charCodeAt(i) === 10) count++;
  return count;
}

function renderContent(data, direction) {
  const oldTop = ui.content.scrollTop;
  const lineHeight = parseFloat(getComputedStyle(ui.content).lineHeight);
  const replace = direction === "initial" || data.reset;
  let scrollDelta = 0;
  const chunk = {
    text: data.text,
    before: data.before_cursor,
    hasEarlier: data.has_more,
  };

  if (replace) {
    textChunks = [chunk];
    historyCropped = false;
    showNotice(ui["memory-note"], "");
  } else if (direction === "backward") {
    const first = textChunks[0];
    if (first && first.text.length + data.text.length <= 256 * 1024) {
      chunk.text += first.text;
      textChunks[0] = chunk;
    } else textChunks.unshift(chunk);
    scrollDelta = newlineCount(data.text) * lineHeight;
  } else if (data.text) {
    const last = textChunks.at(-1);
    if (last && last.text.length + data.text.length <= 256 * 1024)
      last.text += data.text;
    else textChunks.push(chunk);
  }

  let textLength = textChunks.reduce((sum, item) => sum + item.text.length, 0);
  if (textLength > TEXT_LIMIT) {
    if (direction === "backward" && !replace) {
      // A history window cannot append at the old end cursor after its tail is removed.
      while (textLength > TEXT_LIMIT && textChunks.length > 1)
        textLength -= textChunks.pop().text.length;
      historyCropped = true;
      ui["auto-refresh"].checked = false;
      showNotice(
        ui["memory-note"],
        "历史内容已达到页面保留上限，已暂停自动刷新。点击“回到最新”可重新查看文件末尾。",
      );
    } else {
      // Drop complete chunks so the earliest cursor still matches displayed text.
      while (textLength > TEXT_LIMIT && textChunks.length > 1) {
        const removed = textChunks.shift().text;
        textLength -= removed.length;
        scrollDelta -= newlineCount(removed) * lineHeight;
      }
      showNotice(
        ui["memory-note"],
        "为保持页面流畅，已清理较早的显示内容。可点击“加载更早”重新查看历史，日志文件未被修改。",
      );
    }
  }
  // Textareas normalize line endings; join first in case a CRLF crosses chunks.
  const text = textChunks
    .map((item) => item.text)
    .join("")
    .replace(/\r\n?/g, "\n");

  // Avoid resetting the textarea on empty polls so selection and scroll remain stable.
  if (ui.content.value !== text) ui.content.value = text;
  ui["empty-state"].hidden = text.length > 0;
  if (!text.length) showEmpty("日志文件暂时为空", "新内容写入后将在这里显示。");

  if (direction === "backward" && !replace) {
    ui.content.scrollTop = oldTop + scrollDelta;
  } else if (ui.follow.checked) {
    ui.content.scrollTop = ui.content.scrollHeight;
  } else if (!replace) {
    ui.content.scrollTop = Math.max(0, oldTop + scrollDelta);
  }

  if (direction !== "backward" || replace) cursor = data.cursor;
  beforeCursor = textChunks[0]?.before ?? data.before_cursor;
  hasEarlier = textChunks[0]?.hasEarlier ?? data.has_more;
  ui["history-description"].textContent = historyCropped
    ? "正在查看历史内容"
    : "向上加载可查看更早的日志";
  ui["file-size"].textContent = `文件 ${formatSize(data.size)}`;
  const modified = formatDate(data.modified_at);
  ui["file-modified"].textContent = modified ? `修改于 ${modified}` : "";
  ui["last-read"].textContent =
    `更新于 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;
}

async function readContent(direction = "forward") {
  if (!selectedId || contentRequest) return;
  if (direction === "forward" && (cursor === null || historyCropped))
    direction = "initial";
  if (direction === "backward" && (!hasEarlier || beforeCursor === null))
    return;
  const request = { id: selectedId, controller: new AbortController() };
  clearTimeout(catchUpTimer);
  contentRequest = request;
  updateControls();
  setReadState(
    direction === "backward" ? "正在加载历史…" : "正在读取…",
    "loading",
  );
  const params = new URLSearchParams();
  if (direction !== "initial") {
    params.set("cursor", direction === "backward" ? beforeCursor : cursor);
    params.set("direction", direction);
  }
  try {
    const data = await api(
      `/api/logs/${encodeURIComponent(request.id)}/content?${params}`,
      { signal: request.controller.signal },
    );
    if (contentRequest !== request || selectedId !== request.id) return;
    renderContent(data, direction);
    showNotice(ui["read-error"], "");
    setReadState(
      historyCropped
        ? "历史内容"
        : ui["auto-refresh"].checked
          ? "正在追踪日志"
          : "已读取",
      "ready",
    );
    // Catch up with large bursts, but never spin on an unfinished UTF-8 character.
    if (
      direction !== "backward" &&
      data.has_newer &&
      data.text &&
      ui["auto-refresh"].checked
    ) {
      catchUpTimer = setTimeout(() => {
        if (
          selectedId === request.id &&
          !document.hidden &&
          ui["auto-refresh"].checked
        )
          readContent();
      }, 100);
    }
  } catch (error) {
    if (error.name === "AbortError" || contentRequest !== request) return;
    showNotice(ui["read-error"], errorText(error));
    setReadState("读取失败，可刷新重试", "error");
    if (!ui.content.value)
      showEmpty(
        "暂时无法读取日志",
        "请检查文件路径和读取权限，然后点击刷新重试。",
      );
  } finally {
    if (contentRequest === request) {
      contentRequest = null;
      updateControls();
    }
  }
}

function openAdd() {
  if (ui["add-dialog"].open) return;
  ui["add-form"].reset();
  showNotice(ui["add-error"], "");
  ui["add-dialog"].showModal();
  ui["add-name"].focus();
}

function closeAdd() {
  if (!adding) ui["add-dialog"].close();
}

$("open-add").addEventListener("click", openAdd);
ui["empty-add"].addEventListener("click", openAdd);
$("close-add").addEventListener("click", closeAdd);
$("cancel-add").addEventListener("click", closeAdd);
ui["add-dialog"].addEventListener("cancel", (event) => {
  if (adding) event.preventDefault();
});
ui["add-form"].addEventListener("submit", async (event) => {
  event.preventDefault();
  if (adding) return;
  const name = ui["add-name"].value.trim();
  const path = ui["add-path"].value.trim();
  if (!name || !path) {
    showNotice(ui["add-error"], "请填写显示名称和日志路径。");
    return;
  }
  adding = true;
  ui["save-add"].disabled = true;
  ui["save-add"].textContent = "正在添加…";
  showNotice(ui["add-error"], "");
  try {
    const log = await api("/api/logs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, path }),
    });
    // Update immediately, even if a scheduled list refresh is already in flight.
    listRevision++;
    if (!logs.some((item) => item.id === log.id)) logs.push(log);
    ui.filter.value = "";
    selectLog(log.id);
    ui["add-dialog"].close();
    refreshLogs();
  } catch (error) {
    showNotice(ui["add-error"], errorText(error));
  } finally {
    adding = false;
    ui["save-add"].disabled = false;
    ui["save-add"].textContent = "添加日志";
  }
});

ui.filter.addEventListener("input", () => renderLogs());
ui.refresh.addEventListener("click", () => readContent());
ui["load-earlier"].addEventListener("click", () => {
  ui.follow.checked = false;
  readContent("backward");
});
ui.latest.addEventListener("click", () => {
  if (historyCropped) ui["auto-refresh"].checked = true;
  ui.follow.checked = true;
  readContent("initial");
});
ui.follow.addEventListener("change", () => {
  if (ui.follow.checked) ui.content.scrollTop = ui.content.scrollHeight;
});
ui.content.addEventListener(
  "scroll",
  () => {
    if (
      ui.content.scrollHeight - ui.content.clientHeight - ui.content.scrollTop >
      32
    )
      ui.follow.checked = false;
  },
  { passive: true },
);
ui["auto-refresh"].addEventListener("change", () => {
  if (ui["auto-refresh"].checked) readContent();
  else if (selectedId && !contentRequest)
    setReadState("自动刷新已暂停", "ready");
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    refreshLogs();
    if (ui["auto-refresh"].checked) readContent();
  }
});

refreshLogs();
setInterval(() => {
  if (!document.hidden) refreshLogs();
}, 5000);
setInterval(() => {
  if (!document.hidden && ui["auto-refresh"].checked) readContent();
}, 2000);
