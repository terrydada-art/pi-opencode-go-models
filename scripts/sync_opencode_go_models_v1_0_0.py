#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OpenCode Go 訂閱可用模型清單同步工具 v1.0.0

用途
----
查詢 OpenCode Go 訂閱「目前實際可用」的模型，並把差異同步到兩份清單：

1. Pi 自訂模型清單：``~/.pi/agent/models.json`` 的 ``providers.opencode-go.models``
2. LiteLLM 路由：``M:\\AI\\ai-router-2026\\litellm\\config.yaml`` 的 ``opencode-go/*`` routes

資料來源（依可信度排序）
------------------------
1. OpenCode Go 即時 catalog：``https://opencode.ai/zen/go/v1/models``
   （官方 API，權威的「現在存在哪些模型 ID」來源）
2. OpenCode Go 官方文件：``https://opencode.ai/docs/go/``
   （權威的「每個模型該用哪個 API 協定」與「每 1M token 價格」來源）
3. pi.dev 遠端模型目錄：``https://pi.dev/api/models/providers/opencode-go``
   （Pi 官方維護的完整模型定義，含 contextWindow / maxTokens）
4. 實際最小生成請求（probe）：確認模型「當下真的可呼叫」
5. 上一次成功探測的協定快取：``reports/protocol-cache.json``

安全設計
--------
- API Key 只從環境變數或 ``auth.json`` 讀取，永不輸出、永不寫入報告。
- 所有寫入前都會建立時間戳備份。
- 只有「通過實際呼叫驗證」的模型才會被寫入設定檔。
- LiteLLM 候選設定會先在容器內用 PyYAML 驗證，套用後重新啟動並驗證 /v1/models。
- 預設套用變更；``--dry-run`` 只預覽（LiteLLM 仍會在容器內驗證候選設定，但不寫入）。
- 上游已消失的 route 預設只報告，需 ``--prune`` 才移除。

版本：v1.0.0
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# ───────────────────────────── 版本與端點 ─────────────────────────────
VERSION = "1.0.0"
USER_AGENT = f"opencode-go-model-sync/{VERSION}"

CATALOG_URL = "https://opencode.ai/zen/go/v1/models"
DOCS_URL = "https://opencode.ai/docs/go/"
REMOTE_CATALOG_URL = "https://pi.dev/api/models/providers/opencode-go"

CHAT_URL = "https://opencode.ai/zen/go/v1/chat/completions"
RESPONSES_URL = "https://opencode.ai/zen/go/v1/responses"
ANTHROPIC_URL = "https://opencode.ai/zen/go/v1/messages"

OPENCODE_BASE_CHAT = "https://opencode.ai/zen/go/v1"
OPENCODE_BASE_ANTHROPIC = "https://opencode.ai/zen/go"

ROUTE_PREFIX = "opencode-go/"
MAIN_ROUTE_NAME = "opencode-go-main"

# ───────────────────────────── 預設路徑 ─────────────────────────────
DEFAULT_PI_AGENT_DIR = Path.home() / ".pi" / "agent"
DEFAULT_LITELLM_CONFIG = Path(r"M:\AI\ai-router-2026\litellm\config.yaml")
DEFAULT_LITELLM_BACKUP_DIR = Path(r"M:\AI\ai-router-2026\backups")
DEFAULT_MANAGE_TOOL = Path(
    r"C:\op\OpenCodeGo\opencode-go-monitor-v1.0.0\manage_litellm_opencode_models_v1_0_0.py"
)
SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_REPORT_DIR = SKILL_DIR / "reports"
DEFAULT_OVERRIDES = SKILL_DIR / "scripts" / "metadata-overrides.json"

# 遇到這些狀態就不再嘗試其他協定，避免把權限、配額或暫時故障誤判成協定問題。
STOP_PROTOCOL_FALLBACK_STATUSES = {401, 403, 429, 502, 503, 504}

# 協定 → Pi 的 api 名稱與 baseUrl
PI_API_BY_PROTOCOL = {
    "chat": ("openai-completions", OPENCODE_BASE_CHAT),
    "responses": ("openai-responses", OPENCODE_BASE_CHAT),
    "anthropic": ("anthropic-messages", OPENCODE_BASE_ANTHROPIC),
}

# 保守預設值：官方未提供規格時使用，並在 models.json 留下 TODO 註解。
DEFAULT_PI_CONTEXT_WINDOW = 131072
DEFAULT_PI_MAX_TOKENS = 32768
DEFAULT_LITELLM_MAX_INPUT_TOKENS = 262144
DEFAULT_LITELLM_MAX_OUTPUT_TOKENS = 65536

# 測試用圖片：64x64 單色 PNG 的 base64（240 字元）。
# 注意：1x1 影像會被 Anthropic 端拒絕（Could not process image），故使用 64x64。
TINY_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAeklEQVR4nO3PUQkAIBTAwBfbTCYx"
    "liH8OITBAtxm7fN1wwUNaEEDWtCAFjSgBQ1oQQNa0IAWNKAFDWhBA1rQgBY0oAUNaEEDWtCAFjSg"
    "BQ1oQQNa0IAWNKAFDWhBA1rQgBY0oAUNaEEDWtCAFjSgBQ1oQQNa0IAWPHYBfOahw/tyiYoAAAAA"
    "SUVORK5CYII="
)


@dataclass
class HttpResult:
    """單一 HTTP 請求結果。"""

    status: int
    payload: dict[str, Any] = field(default_factory=dict)
    error: str = ""


# ───────────────────────────── 基礎工具 ─────────────────────────────
def log(message: str) -> None:
    """輸出進度訊息（立即 flush，方便長時間執行時觀察）。"""
    print(message, flush=True)


def env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def read_text_raw(path: Path) -> str:
    """以原始換行讀取文字，避免破壞 CRLF 排版。"""
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return handle.read()


def write_text_raw(path: Path, text: str) -> None:
    """以原始換行寫入文字。"""
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def atomic_write(path: Path, content: str) -> None:
    """先寫暫存檔再取代，避免中途中斷留下半份檔案。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def strip_json_comments(text: str) -> str:
    """移除 JSONC 的 // 與 /* */ 註解（字串內容不動）。"""
    result: list[str] = []
    index = 0
    length = len(text)
    in_string = False
    escape = False
    while index < length:
        char = text[index]
        nxt = text[index + 1] if index + 1 < length else ""
        if in_string:
            result.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            result.append(char)
            index += 1
            continue
        if char == "/" and nxt == "/":
            while index < length and text[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and nxt == "*":
            index += 2
            while index + 1 < length and not (text[index] == "*" and text[index + 1] == "/"):
                index += 1
            index += 2
            continue
        result.append(char)
        index += 1
    return "".join(result)


def read_jsonc(path: Path) -> dict[str, Any]:
    """讀取含註解的 JSON（models.json）。"""
    return json.loads(strip_json_comments(read_text_raw(path)))


def http_json(
    url: str,
    headers: dict[str, str],
    timeout: float,
    body: dict[str, Any] | None = None,
) -> HttpResult:
    """送出 HTTP 請求並回傳解析後的 JSON；錯誤一律轉成 HttpResult。"""
    request_headers = {"Accept": "application/json", "User-Agent": USER_AGENT, **headers}
    data = None
    method = "GET"
    if body is not None:
        request_headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
        method = "POST"

    request = Request(url, headers=request_headers, data=data, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return HttpResult(response.status, json.loads(raw) if raw.strip() else {})
    except HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            payload = {"error": {"message": raw or str(error)}}
        return HttpResult(error.code, payload, extract_error(payload) or str(error))
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        return HttpResult(0, {}, f"{type(error).__name__}: {error}")


def extract_error(payload: dict[str, Any]) -> str:
    """從錯誤回應取出可讀訊息。"""
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("type") or error)
    if error:
        return str(error)
    return ""


def opencode_headers(api_key: str, session_id: str, anthropic: bool = False) -> dict[str, str]:
    """建立 OpenCode Go 需要的請求標頭（包含穩定的 session 識別）。"""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "x-opencode-session": session_id,
    }
    if anthropic:
        headers.update({"x-api-key": api_key, "anthropic-version": "2023-06-01"})
    return headers


# ───────────────────────────── 資料來源 ─────────────────────────────
def load_api_key(agent_dir: Path) -> str:
    """依序從環境變數、Pi auth.json 取得 OpenCode Go API Key。"""
    key = os.environ.get("OPENCODE_GO_API_KEY", "").strip()
    if key:
        return key
    auth_path = agent_dir / "auth.json"
    if auth_path.is_file():
        try:
            auth = json.loads(read_text_raw(auth_path))
            key = str(auth.get("opencode-go", {}).get("key", "")).strip()
        except (json.JSONDecodeError, OSError):
            key = ""
    if not key:
        raise RuntimeError(
            "找不到 OpenCode Go API Key：請設定環境變數 OPENCODE_GO_API_KEY，"
            f"或確認 {auth_path} 內含 opencode-go.key"
        )
    return key


def fetch_catalog(api_key: str, timeout: float) -> list[str]:
    """取得 OpenCode Go 即時 catalog 的模型 ID（權威來源）。"""
    result = http_json(
        CATALOG_URL,
        opencode_headers(api_key, str(uuid.uuid4())),
        timeout,
    )
    if result.status != 200:
        raise RuntimeError(f"catalog 查詢失敗：HTTP {result.status} {result.error}")
    data = result.payload.get("data", [])
    if not isinstance(data, list):
        raise RuntimeError("catalog 回應缺少 data 陣列")
    ids = {str(item.get("id")) for item in data if isinstance(item, dict) and item.get("id")}
    if not ids:
        raise RuntimeError("catalog 回應沒有任何模型 ID")
    return sorted(ids)


def html_tables(html: str) -> list[list[list[str]]]:
    """把 HTML 中的所有表格轉成「列 → 欄 → 純文字」結構。"""
    tables: list[list[list[str]]] = []
    for table_html in re.findall(r"<table[^>]*>([\s\S]*?)</table>", html):
        rows: list[list[str]] = []
        for row_html in re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", table_html):
            cells = []
            for cell_html in re.findall(r"<t[dh][^>]*>([\s\S]*?)</t[dh]>", row_html):
                text = re.sub(r"<[^>]+>", "", cell_html)
                text = (
                    text.replace("&nbsp;", " ")
                    .replace("&amp;", "&")
                    .replace("&lt;", "<")
                    .replace("&gt;", ">")
                    .replace("&#39;", "'")
                    .replace("&quot;", '"')
                )
                cells.append(re.sub(r"\s+", " ", text).strip())
            if cells:
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


def parse_money(value: str) -> float | None:
    """解析官方價目表的金額欄位（例如 $1.40、Free、-）。"""
    text = value.strip().replace("**", "")
    if not text or text in {"-", "N/A", "?"}:
        return None
    if text.lower().startswith("free"):
        return 0.0
    match = re.search(r"\$?\s*([0-9]+(?:\.[0-9]+)?)", text)
    return float(match.group(1)) if match else None


def parse_docs(html: str) -> dict[str, dict[str, Any]]:
    """從官方文件解析每個模型 ID 的協定、顯示名稱與價格。

    回傳：``{model_id: {"name", "protocol", "cost"}}``

    注意：官方文件把「價目表」排在「Endpoints 表」之前，因此必須分兩輪掃描，
    先建立 model ID ↔ 顯示名稱對照，才不會漏掉價格。
    """
    entries: dict[str, dict[str, Any]] = {}
    name_to_id: dict[str, str] = {}
    tables = html_tables(html)

    # 第一輪：Endpoints 表（Model | Model ID | Endpoint | AI SDK Package）
    for rows in tables:
        for cells in rows:
            if len(cells) < 3 or "opencode.ai/zen/go" not in cells[2]:
                continue
            model_id, name, endpoint = cells[1], cells[0], cells[2]
            if endpoint.endswith("/chat/completions"):
                protocol = "chat"
            elif endpoint.endswith("/responses"):
                protocol = "responses"
            elif endpoint.endswith("/messages"):
                protocol = "anthropic"
            else:
                continue
            entries.setdefault(model_id, {})["name"] = name
            entries[model_id]["protocol"] = protocol
            name_to_id[normalize_model_name(name)] = model_id

    # 第二輪：價目表（Model | Input | Output | Cached Read | Cached Write | Monthly limit）
    for rows in tables:
        for cells in rows:
            if len(cells) < 5:
                continue
            price_text = cells[1].strip()
            # 金額欄位必須是「$金額」或「Free」，用來排除「預估請求數」等其他表格。
            if "$" not in price_text and not price_text.lower().startswith("free"):
                continue
            model_id = name_to_id.get(normalize_model_name(cells[0]))
            if not model_id or "cost" in entries.get(model_id, {}):
                continue
            input_cost = parse_money(cells[1])
            output_cost = parse_money(cells[2])
            if input_cost is None and output_cost is None:
                continue
            entries[model_id]["cost"] = {
                "input": input_cost or 0.0,
                "output": output_cost or 0.0,
                "cacheRead": parse_money(cells[3]) or 0.0,
                "cacheWrite": parse_money(cells[4]) or 0.0,
            }
    return entries


def normalize_model_name(name: str) -> str:
    """正規化官方文件中的模型顯示名稱，用於對照 ID。"""
    text = re.sub(r"\((?:≤|>=|\u2264)[^)]*\)", "", name)  # 移除價格分級註記
    text = re.sub(r"\([^)]*\)", "", text)  # 移除 (Off-Peak) / (Peak) 等註記
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def fetch_docs(timeout: float) -> tuple[dict[str, dict[str, Any]], str]:
    """取得官方文件（HTML）並解析模型 metadata。"""
    try:
        request = Request(DOCS_URL, headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=timeout) as response:
            html = response.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError) as error:
        return {}, f"{type(error).__name__}: {error}"
    entries = parse_docs(html)
    if not entries:
        return {}, "官方文件解析不到任何模型資料（版面可能已變更）"
    return entries, ""


def fetch_remote_catalog(timeout: float) -> dict[str, dict[str, Any]]:
    """取得 pi.dev 遠端模型目錄（Pi 官方維護的完整定義）。"""
    result = http_json(REMOTE_CATALOG_URL, {}, timeout)
    if result.status != 200 or not isinstance(result.payload, dict):
        return {}
    return {
        str(model_id): definition
        for model_id, definition in result.payload.items()
        if isinstance(definition, dict)
    }


def pi_state(agent_dir: Path) -> dict[str, Any]:
    """讀取 Pi 目前的 opencode-go 模型狀態。"""
    models_path = agent_dir / "models.json"
    store_path = agent_dir / "models-store.json"
    state: dict[str, Any] = {
        "models_path": models_path,
        "store_path": store_path,
        "models_text": "",
        "models_ids": set(),
        "store_ids": set(),
        "errors": [],
    }
    if models_path.is_file():
        state["models_text"] = read_text_raw(models_path)
        try:
            data = json.loads(strip_json_comments(state["models_text"]))
            models = data.get("providers", {}).get("opencode-go", {}).get("models", [])
            state["models_ids"] = {
                str(item.get("id")) for item in models if isinstance(item, dict) and item.get("id")
            }
        except (json.JSONDecodeError, AttributeError) as error:
            state["errors"].append(f"models.json 解析失敗：{error}")
    else:
        state["errors"].append(f"找不到 {models_path}")
    if store_path.is_file():
        try:
            store = json.loads(read_text_raw(store_path))
            models = store.get("opencode-go", {}).get("models", [])
            state["store_ids"] = {
                str(item.get("id")) for item in models if isinstance(item, dict) and item.get("id")
            }
        except (json.JSONDecodeError, AttributeError) as error:
            state["errors"].append(f"models-store.json 解析失敗：{error}")
    return state


def read_litellm_routes(config_path: Path) -> tuple[set[str], str]:
    """從 LiteLLM config.yaml 讀出 opencode-go 相關 route 名稱。"""
    if not config_path.is_file():
        return set(), f"找不到設定檔 {config_path}"
    text = read_text_raw(config_path)
    routes = set(
        re.findall(r"^\s*-\s*model_name:\s*([^\s#]+)", text, flags=re.MULTILINE)
    )
    return {route for route in routes if route == MAIN_ROUTE_NAME or route.startswith(ROUTE_PREFIX)}, ""


# ───────────────────────────── 模型探測 ─────────────────────────────
def probe_body(protocol: str, vision: bool) -> tuple[str, dict[str, Any]]:
    """依協定產生最小測試請求。"""
    if protocol == "responses":
        if vision:
            content = [
                {"type": "input_text", "text": "Reply exactly: OK"},
                {"type": "input_image", "image_url": f"data:image/png;base64,{TINY_PNG_BASE64}"},
            ]
        else:
            content = "Reply exactly: OK"
        return RESPONSES_URL, {
            "model": None,
            "input": [{"role": "user", "content": content}],
            "max_output_tokens": 256,
        }
    if protocol == "anthropic":
        content = [{"type": "text", "text": "Reply exactly: OK"}]
        if vision:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": TINY_PNG_BASE64,
                    },
                }
            )
        return ANTHROPIC_URL, {
            "model": None,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 64,
        }
    content = [{"type": "text", "text": "Reply exactly: OK"}]
    if vision:
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{TINY_PNG_BASE64}"}}
        )
    return CHAT_URL, {
        "model": None,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 64,
        "stream": False,
    }


def probe_once(
    model_id: str,
    protocol: str,
    api_key: str,
    timeout: float,
    vision: bool = False,
) -> HttpResult:
    """以指定協定送出一次最小請求。"""
    url, body = probe_body(protocol, vision)
    body["model"] = model_id
    return http_json(
        url,
        opencode_headers(api_key, str(uuid.uuid4()), anthropic=(protocol == "anthropic")),
        timeout,
        body,
    )


def detect_reasoning(protocol: str, payload: dict[str, Any]) -> bool:
    """從回應內容判斷模型是否輸出思考內容。"""
    if protocol == "chat":
        for choice in payload.get("choices", []) or []:
            message = choice.get("message", {}) if isinstance(choice, dict) else {}
            if isinstance(message, dict) and (
                message.get("reasoning_content") or message.get("reasoning")
            ):
                return True
        return False
    if protocol == "anthropic":
        for block in payload.get("content", []) or []:
            if isinstance(block, dict) and block.get("type") == "thinking":
                return True
        return False
    for item in payload.get("output", []) or []:
        if isinstance(item, dict) and item.get("type") == "reasoning":
            return True
    return False


def probe_model(
    model_id: str,
    protocols: Sequence[str],
    api_key: str,
    timeout: float,
    probe_vision: bool,
) -> dict[str, Any]:
    """依序嘗試協定，回傳可用性、實際協定、思考與視覺能力。"""
    attempts: list[dict[str, Any]] = []
    for protocol in protocols:
        started = time.monotonic()
        result = probe_once(model_id, protocol, api_key, timeout)
        elapsed = round(time.monotonic() - started, 2)
        failed_response = result.payload.get("status") == "failed"
        available = 200 <= result.status < 300 and not result.error and not failed_response
        attempts.append(
            {
                "protocol": protocol,
                "http_status": result.status,
                "seconds": elapsed,
                "available": available,
                "error": result.error,
            }
        )
        if available:
            vision: bool | None = None
            if probe_vision:
                vision_result = probe_once(model_id, protocol, api_key, timeout, vision=True)
                vision = 200 <= vision_result.status < 300 and not vision_result.error
            return {
                "model": model_id,
                "available": True,
                "protocol": protocol,
                "http_status": result.status,
                "error": "",
                "reasoning": detect_reasoning(protocol, result.payload),
                "vision": vision,
                "attempts": attempts,
            }
        if result.status in STOP_PROTOCOL_FALLBACK_STATUSES:
            break

    last = attempts[-1]
    return {
        "model": model_id,
        "available": False,
        "protocol": last["protocol"],
        "http_status": last["http_status"],
        "error": last["error"] or f"HTTP {last['http_status']}",
        "reasoning": None,
        "vision": None,
        "attempts": attempts,
    }


def protocol_candidates(
    model_id: str,
    docs_entry: dict[str, Any],
    cached: dict[str, Any],
    overrides: dict[str, Any],
) -> list[str]:
    """決定探測協定順序：人工覆寫 → 官方文件 → 上次成功快取 → 全部嘗試。"""
    override = overrides.get(model_id, {})
    if override.get("protocol") in PI_API_BY_PROTOCOL:
        return [override["protocol"]]
    if docs_entry.get("protocol") in PI_API_BY_PROTOCOL:
        return [docs_entry["protocol"]]
    cached_protocol = cached.get("protocol")
    if cached_protocol in PI_API_BY_PROTOCOL:
        return [cached_protocol]
    return ["chat", "responses", "anthropic"]


# ───────────────────────────── 變更內容建立 ─────────────────────────────
def build_pi_entry(
    model_id: str,
    probe: dict[str, Any],
    docs_entry: dict[str, Any],
    remote_entry: dict[str, Any],
    override: dict[str, Any],
) -> dict[str, Any]:
    """建立可寫入 Pi models.json 的模型定義（採保守設定）。"""
    protocol = override.get("protocol") or probe.get("protocol") or docs_entry.get("protocol") or "chat"
    api, base_url = PI_API_BY_PROTOCOL.get(protocol, PI_API_BY_PROTOCOL["chat"])

    reasoning = override.get("reasoning")
    if reasoning is None:
        reasoning = probe.get("reasoning") if probe.get("reasoning") is not None else False

    inputs = override.get("input")
    if inputs is None:
        inputs = ["text", "image"] if probe.get("vision") else ["text"]

    cost = override.get("cost") or docs_entry.get("cost") or {
        "input": 0.0,
        "output": 0.0,
        "cacheRead": 0.0,
        "cacheWrite": 0.0,
    }

    entry: dict[str, Any] = {
        "id": model_id,
        "name": override.get("name")
        or docs_entry.get("name")
        or remote_entry.get("name")
        or model_id,
        "api": api,
        "baseUrl": base_url,
        "reasoning": bool(reasoning),
        "input": inputs,
        "contextWindow": int(
            override.get("contextWindow")
            or remote_entry.get("contextWindow")
            or DEFAULT_PI_CONTEXT_WINDOW
        ),
        "maxTokens": int(
            override.get("maxTokens") or remote_entry.get("maxTokens") or DEFAULT_PI_MAX_TOKENS
        ),
        "cost": {
            "input": float(cost.get("input", 0.0)),
            "output": float(cost.get("output", 0.0)),
            "cacheRead": float(cost.get("cacheRead", 0.0)),
            "cacheWrite": float(cost.get("cacheWrite", 0.0)),
        },
    }
    if api == "openai-completions":
        entry["compat"] = {
            "supportsStore": False,
            "supportsDeveloperRole": False,
            "maxTokensField": "max_tokens",
            "supportsReasoningEffort": False,
        }
        if entry["reasoning"]:
            # 實測：這些端點會回傳 reasoning_content，回放 assistant 訊息時必須帶上。
            entry["compat"]["requiresReasoningContentOnAssistantMessages"] = True
            entry["compat"]["thinkingFormat"] = "deepseek"
            # 實測：各模型接受的思考參數差異極大（部分拒絕 disabled、部分拒絕 minimal），
            # 因此自動產生的定義一律不送出思考層級參數，全部設 null；
            # 待人工確認該模型支援的參數後再透過 metadata-overrides.json 開放。
            entry["thinkingLevelMap"] = {
                "off": None,
                "minimal": None,
                "low": None,
                "medium": None,
                "high": None,
                "xhigh": None,
                "max": None,
            }
    elif api == "openai-responses":
        entry["compat"] = {"sessionAffinityFormat": "openai-nosession"}
    return entry


def pi_entry_comment(
    model_id: str,
    probe: dict[str, Any],
    docs_entry: dict[str, Any],
    override: dict[str, Any],
    timestamp: str,
) -> str:
    """產生寫入 models.json 的說明註解（含待人工確認事項）。"""
    lines = [
        f"        // [自動同步 v{VERSION} {timestamp}] {model_id}",
    ]
    if docs_entry.get("protocol"):
        lines.append("        // 協定來源：OpenCode Go 官方文件 Endpoints 表。")
    else:
        lines.append("        // 注意：官方文件未收錄此模型，API 協定由實際請求探測判定。")
    if "contextWindow" not in override and "maxTokens" not in override:
        lines.append(
            "        // TODO：contextWindow / maxTokens 尚無官方規格，目前為保守預設值，請人工校對。"
        )
    if probe.get("reasoning") is None:
        lines.append("        // TODO：思考（reasoning）能力未經確認，目前設為 false 以免送出無效參數。")
    elif probe.get("reasoning"):
        lines.append(
            "        // 思考層級參數未啟用（thinkingLevelMap 全為 null），避免上游拒絕未知參數；"
            "確認支援後可於 metadata-overrides.json 覆寫。"
        )
    return "\n".join(lines)


def inline_json_block(body: str, key: str, opener: str, closer: str) -> str:
    """把只有純量元素的 input 陣列與 cost 物件壓成單行，貼近 models.json 既有排版。"""
    pattern = re.compile(
        r'("' + key + r'"\s*:\s*' + re.escape(opener) + r")\s*\n(.*?)\n\s*" + re.escape(closer),
        flags=re.DOTALL,
    )

    def repl(match: re.Match[str]) -> str:
        items = [
            line.strip().rstrip(",")
            for line in match.group(2).split("\n")
            if line.strip()
        ]
        return f"{match.group(1)} {', '.join(items)} {closer}"

    return pattern.sub(repl, body)


def jsonc_entry_text(entry: dict[str, Any], indent: str = "        ") -> str:
    """把模型定義轉成 models.json 內的 JSON 文字區塊（含縮排）。"""
    body = json.dumps(entry, ensure_ascii=False, indent=2)
    body = inline_json_block(body, "input", "[", "]")
    body = inline_json_block(body, "cost", "{", "}")
    return "\n".join(indent + line for line in body.split("\n"))


def find_opencode_models_array(text: str) -> tuple[int, int]:
    """找出 providers.opencode-go.models 陣列的 [ 與 ] 位置。

    掃描時略過字串與 JSONC 註解，因此不會誤判註解中的括號。
    """
    match = re.search(r'"opencode-go"\s*:\s*\{', text)
    if not match:
        raise RuntimeError("models.json 找不到 providers.opencode-go 區塊")
    models_match = re.compile(r'"models"\s*:\s*\[').search(text, match.end())
    if not models_match:
        raise RuntimeError("models.json 找不到 providers.opencode-go.models 陣列")
    open_index = text.index("[", models_match.start())
    depth = 0
    in_string = False
    escape = False
    index = open_index
    while index < len(text):
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            index += 1
            continue
        if char == "/" and nxt == "/":
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and nxt == "*":
            index += 2
            while index + 1 < len(text) and not (text[index] == "*" and text[index + 1] == "/"):
                index += 1
            index += 2
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return open_index, index
        index += 1
    raise RuntimeError("models.json 的 models 陣列沒有對應的結尾 ]")


def iter_top_level_objects(text: str, open_index: int, close_index: int) -> list[tuple[int, int]]:
    """列出陣列內最外層物件的 [起, 訖) 位置（略過字串與註解）。"""
    spans: list[tuple[int, int]] = []
    depth = 0
    start: int | None = None
    in_string = False
    escape = False
    index = open_index + 1
    while index < close_index:
        char = text[index]
        nxt = text[index + 1] if index + 1 < len(text) else ""
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            index += 1
            continue
        if char == "/" and nxt == "/":
            while index < close_index and text[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and nxt == "*":
            index += 2
            while index + 1 < close_index and not (text[index] == "*" and text[index + 1] == "/"):
                index += 1
            index += 2
            continue
        if char in "[{":
            if depth == 0 and char == "{":
                start = index
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth == 0 and char == "}" and start is not None:
                spans.append((start, index + 1))
                start = None
            elif depth < 0:
                break
        index += 1
    return spans


def remove_object_span(text: str, start: int, end: int) -> str:
    """移除單一物件，並一併清掉它的前置註解行與多餘逗號。"""
    # 往前延伸移除緊接在上方的 // 註解行（避免留下指向已移除模型的說明）。
    new_start = start
    lines = text[:start].split("\n")
    keep = len(lines)
    while keep > 1:
        stripped = lines[keep - 1].strip()
        if stripped == "" or stripped.startswith("//"):
            keep -= 1
            continue
        break
    if keep < len(lines):
        new_start = len("\n".join(lines[:keep])) + 1

    head, tail = text[:new_start], text[end:]
    tail_stripped = tail.lstrip()
    if tail_stripped.startswith(","):
        # 後面還有其他模型：刪掉後方的逗號
        offset = len(tail) - len(tail_stripped)
        tail = tail[:offset] + tail_stripped[1:]
    else:
        head_stripped = head.rstrip()
        if head_stripped.endswith(","):
            # 這是最後一個模型：刪掉前方的逗號（含其後的空白）。
            head = head_stripped[:-1]
    return head + tail


def insert_pi_entries(
    models_text: str,
    entries: list[tuple[str, dict[str, Any]]],
    remove_ids: Sequence[str],
) -> str:
    """把新模型插入 models.json，並可移除指定模型（純文字操作，保留原註解與排版）。"""
    text = models_text

    # 先移除指定模型（例如上游 catalog 已消失的自訂模型）。
    for model_id in remove_ids:
        open_index, close_index = find_opencode_models_array(text)
        pattern = re.compile(r'"id"\s*:\s*"' + re.escape(model_id) + r'"')
        for span_start, span_end in iter_top_level_objects(text, open_index, close_index):
            if pattern.search(text[span_start:span_end]):
                text = remove_object_span(text, span_start, span_end)
                break
        else:
            log(f"[警告] models.json 找不到可移除的模型：{model_id}")

    if not entries:
        return text

    open_index, close_index = find_opencode_models_array(text)
    newline = "\r\n" if "\r\n" in models_text else "\n"
    has_items = bool(text[open_index + 1 : close_index].strip())
    timestamp = datetime.now().strftime("%Y-%m-%d")

    blocks: list[str] = []
    for model_id, payload in entries:
        comment = pi_entry_comment(
            model_id,
            payload["probe"],
            payload["docs"],
            payload["override"],
            timestamp,
        ).replace("\n", newline)
        blocks.append(comment + newline + jsonc_entry_text(payload["entry"]))
    body = ("," + newline + newline).join(blocks)

    head = text[:close_index].rstrip()
    tail = text[close_index:]
    if has_items:
        return head + "," + newline + newline + body + newline + "      " + tail
    return head + newline + "        " + body.lstrip() + newline + "      " + tail


# ───────────────────────────── LiteLLM 套用 ─────────────────────────────
def load_manage_tool(path: Path) -> Any:
    """載入既有的 LiteLLM 模型管理工具（不修改原檔，只重用其安全邏輯）。"""
    if not path.is_file():
        raise RuntimeError(f"找不到 LiteLLM 管理工具：{path}")
    spec = importlib.util.spec_from_file_location("ocg_manage_tool", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"無法載入 LiteLLM 管理工具：{path}")
    module = importlib.util.module_from_spec(spec)
    # 先註冊到 sys.modules，避免模組內使用 dataclass 等需要模組資訊的功能失敗。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def container_models(container: str, timeout: int) -> set[str]:
    """從 LiteLLM 容器內查詢 /v1/models，避免把 Master Key 帶到命令列。"""
    code = (
        "import os,json,urllib.request; "
        "r=urllib.request.Request('http://127.0.0.1:4000/v1/models',"
        "headers={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY']}); "
        "d=json.load(urllib.request.urlopen(r,timeout=20)); "
        "print('\\n'.join(x.get('id','') for x in d.get('data',[])))"
    )
    result = subprocess.run(
        ["docker", "exec", container, "python", "-c", code],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"容器內查詢 /v1/models 失敗：{result.stderr.strip()}")
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def apply_litellm_transaction(
    manage: Any,
    config: Path,
    backup_dir: Path,
    candidate_text: str,
    container: str,
    added_routes: Sequence[str],
    removed_routes: Sequence[str],
    restart: bool,
    health_timeout: int,
) -> Path:
    """單一交易套用 LiteLLM 變更：備份 → 容器內驗證 → 原子取代 → 重啟驗證。"""
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"litellm-config-before-opencode-go-sync-{timestamp}.yaml"
    shutil.copy2(config, backup)

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        prefix="litellm-candidate-",
        suffix=".yaml",
        dir=config.parent,
        delete=False,
    ) as handle:
        handle.write(candidate_text)
        candidate_path = Path(handle.name)

    try:
        manage.validate_candidate(candidate_path, container)
        os.replace(candidate_path, config)
        log(f"已套用 LiteLLM 設定：{config}")
        log(f"備份：{backup}")
        if not restart:
            return backup
        try:
            subprocess.run(
                ["docker", "restart", container],
                check=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if not manage.wait_healthy(container, health_timeout):
                raise RuntimeError("容器未在時限內恢復 healthy")
            active = container_models(container, health_timeout)
            missing = [route for route in added_routes if route not in active]
            leftover = [route for route in removed_routes if route in active]
            if missing or leftover:
                raise RuntimeError(
                    f"重啟後 /v1/models 驗證失敗：缺少 {missing}；仍在 {leftover}"
                )
            log(f"LiteLLM 驗證成功：新增 {len(added_routes)} 條、移除 {len(removed_routes)} 條 route")
        except Exception:
            log("LiteLLM 驗證失敗，正在自動還原備份……")
            restore_path = config.with_suffix(config.suffix + ".restore")
            shutil.copy2(backup, restore_path)
            os.replace(restore_path, config)
            subprocess.run(
                ["docker", "restart", container],
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            manage.wait_healthy(container, health_timeout)
            raise
    finally:
        if candidate_path.exists():
            candidate_path.unlink()
    return backup


# ───────────────────────────── 報告 ─────────────────────────────
def build_markdown(report: dict[str, Any]) -> str:
    """產生 Markdown 報告。"""
    lines = [
        "# OpenCode Go 模型清單同步報告",
        "",
        f"- 工具版本：`v{VERSION}`",
        f"- 執行時間：`{report['generated_at']}`",
        f"- 模式：`{'dry-run（未寫入）' if report['dry_run'] else '套用'}`",
        f"- 官方 catalog 模型數：**{report['catalog_count']}**",
        f"- 官方可呼叫：**{report['available_count']}** / 失敗：**{report['failed_count']}**"
        if report.get("probe_enabled", True)
        else "- 實測：**未執行**（`--no-probe`，可用性未確認）",
        f"- LiteLLM routes：**{report['litellm_route_count']}**（新增 {len(report['litellm_added'])}、移除 {len(report['litellm_removed'])}）",
        f"- Pi 自訂模型：**{report['pi_models_count']}**（新增 {len(report['pi_added'])}、移除 {len(report['pi_removed'])}）",
        "",
    ]
    for source, message in report.get("source_errors", {}).items():
        lines.append(f"> {source}：{message}")
    if report.get("source_errors"):
        lines.append("")

    def section(title: str, items: Sequence[str]) -> None:
        lines.append(f"### {title}")
        lines.append("")
        if items:
            lines.extend(f"- {item}" for item in items)
        else:
            lines.append("- 無")
        lines.append("")

    lines.extend(["## 需人工確認", ""])
    if report["manual_attention"]:
        lines.extend(f"- {item}" for item in report["manual_attention"])
    else:
        lines.append("- 無")
    lines.append("")

    lines.extend(["## 變更摘要", ""])
    section(
        "LiteLLM 新增 route",
        [f"`{item['route']}`（protocol={item['protocol']}，已實測可呼叫）" for item in report["litellm_added"]],
    )
    section("LiteLLM 移除 route", [f"`{route}`" for route in report["litellm_removed"]])
    section(
        "Pi models.json 新增模型",
        [
            f"`{item['id']}`（api={item['entry']['api']}，contextWindow={item['entry']['contextWindow']}，maxTokens={item['entry']['maxTokens']}）"
            for item in report["pi_added"]
        ],
    )
    section("Pi models.json 移除模型", [f"`{model_id}`" for model_id in report["pi_removed"]])
    section("上游已消失但 LiteLLM 仍設定（未移除）", [f"`{route}`" for route in report["litellm_orphan_routes"]])
    section(
        "Pi 遠端目錄尚未收錄（等待 Pi 自行刷新）",
        [f"`{model_id}`" for model_id in report["pi_waiting_remote"]],
    )
    section("catalog 未收錄但 Pi 仍有定義", [f"`{model_id}`" for model_id in report["pi_stale_models"]])

    lines.extend(
        [
            "## 完整模型清單",
            "",
            "| Model | 協定 | 可用 | Pi | LiteLLM | 官方文件 | 失敗次數 | 錯誤 |",
            "|---|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    for item in report["models"]:
        error = str(item.get("error", "")).replace("|", "\\|").replace("\n", " ")[:80]
        available = "-" if not report.get("probe_enabled", True) else (
            "yes" if item.get("available") else "no"
        )
        lines.append(
            "| `{model}` | {protocol} | {available} | {pi} | {litellm} | {docs} | {failures} | {error} |".format(
                model=item["model"],
                protocol=item.get("protocol") or "-",
                available=available,
                pi="yes" if item.get("pi_configured") else "no",
                litellm="yes" if item.get("litellm_configured") else "no",
                docs="yes" if item.get("documented") else "no",
                failures=item.get("consecutive_failures", 0),
                error=error,
            )
        )
    lines.extend(
        [
            "",
            "> 可用性以實際最小生成請求判定；catalog 列出模型不代表供應商端點當下可服務。",
            "> 本工具只新增已驗證模型，不會因為單次失敗就自動移除設定。",
            "",
        ]
    )
    return "\n".join(lines)


# ───────────────────────────── 主流程 ─────────────────────────────
def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="同步 OpenCode Go 訂閱目前可用模型到 Pi models.json 與 LiteLLM config.yaml",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true", help="只預覽差異，不寫入任何設定檔")
    parser.add_argument("--no-probe", action="store_true", help="只比較清單，不實際呼叫模型")
    parser.add_argument(
        "--probe-vision",
        default=env_flag("OCG_SYNC_PROBE_VISION", True),
        action="store_true",
        help="對即將新增的模型額外驗證圖片輸入能力",
    )
    parser.add_argument("--no-probe-vision", dest="probe_vision", action="store_false")
    parser.add_argument("--prune", action="store_true", help="移除 LiteLLM 上游已消失的 route")
    parser.add_argument("--prune-pi", action="store_true", help="移除 models.json 內上游已消失的自訂模型")
    parser.add_argument("--skip-litellm", action="store_true", help="不同步 LiteLLM")
    parser.add_argument("--skip-pi", action="store_true", help="不同步 Pi models.json")
    parser.add_argument(
        "--pi-mode",
        choices=("auto", "report"),
        default="auto",
        help="auto=自動寫入 models.json；report=只報告 Pi 差異",
    )
    parser.add_argument(
        "--offline-metadata",
        action="store_true",
        help="不抓官方文件與 pi.dev 目錄，只用探測結果",
    )
    parser.add_argument("--pi-agent-dir", type=Path, default=DEFAULT_PI_AGENT_DIR)
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("LITELLM_CONFIG", DEFAULT_LITELLM_CONFIG)))
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_LITELLM_BACKUP_DIR)
    parser.add_argument("--manage-tool", type=Path, default=DEFAULT_MANAGE_TOOL)
    parser.add_argument("--container", default=os.environ.get("LITELLM_CONTAINER", "litellm"))
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--overrides", type=Path, default=DEFAULT_OVERRIDES)
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("OCG_SYNC_TIMEOUT", "120")))
    parser.add_argument("--workers", type=int, default=int(os.environ.get("OCG_SYNC_WORKERS", "3")))
    parser.add_argument("--quarantine-after", type=int, default=3)
    parser.add_argument("--health-timeout", type=int, default=180)
    parser.add_argument("--no-restart", action="store_true", help="LiteLLM 套用後不重啟（不建議）")
    parser.add_argument("--fail-on-unavailable", action="store_true", help="有模型不可呼叫時以 exit code 2 結束")
    return parser.parse_args(argv)


def load_overrides(path: Path) -> dict[str, Any]:
    """讀取人工覆寫設定（模型能力、token 上限、協定）。"""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data.get("models", {}) if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as error:
        log(f"[警告] 讀取 {path} 失敗，忽略覆寫設定：{error}")
        return {}


def read_state_file(path: Path) -> dict[str, Any]:
    """讀取狀態檔（protocol-cache.json / failures.json）中的 models 區塊。"""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data.get("models", {}) if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report_dir = args.report_dir.resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    overrides = load_overrides(args.overrides)
    protocol_cache = read_state_file(report_dir / "protocol-cache.json")

    source_errors: dict[str, str] = {}

    # 1) 官方即時 catalog：權威的可用模型 ID 來源。
    try:
        api_key = load_api_key(args.pi_agent_dir)
        catalog_ids = fetch_catalog(api_key, args.timeout)
    except RuntimeError as error:
        log(f"[錯誤] {error}")
        return 1

    # 2) 官方文件與 pi.dev 遠端目錄：metadata 來源，失敗時降級但仍可運作。
    docs_entries: dict[str, dict[str, Any]] = {}
    remote_entries: dict[str, dict[str, Any]] = {}
    if not args.offline_metadata:
        try:
            docs_entries, docs_error = fetch_docs(args.timeout)
            if docs_error:
                source_errors["官方文件"] = docs_error
        except Exception as error:  # noqa: BLE001 - metadata 來源失敗不應中斷同步
            source_errors["官方文件"] = f"{type(error).__name__}: {error}"
        try:
            remote_entries = fetch_remote_catalog(args.timeout)
            if not remote_entries:
                source_errors["pi.dev 遠端目錄"] = "查詢失敗或回傳空資料"
        except Exception as error:  # noqa: BLE001
            source_errors["pi.dev 遠端目錄"] = f"{type(error).__name__}: {error}"

    log(
        f"catalog={len(catalog_ids)} 官方文件={len(docs_entries)} "
        f"pi.dev={len(remote_entries)}"
    )

    # 3) Pi 目前狀態。
    pi = pi_state(args.pi_agent_dir)
    for message in pi["errors"]:
        source_errors.setdefault("Pi models.json", message)
    pi_effective = set(pi["store_ids"]) | set(pi["models_ids"])

    # 4) LiteLLM 目前 routes。
    litellm_routes, litellm_error = read_litellm_routes(args.config)
    if litellm_error and not args.skip_litellm:
        source_errors["LiteLLM 設定"] = litellm_error
    configured_ids = {
        route[len(ROUTE_PREFIX) :]
        for route in litellm_routes
        if route.startswith(ROUTE_PREFIX)
    }

    # 5) 實測每個 catalog 模型是否真的可呼叫。
    probe_results: dict[str, dict[str, Any]] = {}
    if args.no_probe:
        log("略過模型實測（--no-probe），可用性以既有狀態為準")
    else:
        workers = max(1, min(args.workers, 8))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    probe_model,
                    model_id,
                    protocol_candidates(
                        model_id, docs_entries.get(model_id, {}), protocol_cache.get(model_id, {}), overrides
                    ),
                    api_key,
                    args.timeout,
                    # 只有「Pi 尚未收錄」的候選模型才需要額外驗證圖片輸入，減少不必要的請求。
                    args.probe_vision and model_id not in pi_effective,
                ): model_id
                for model_id in catalog_ids
            }
            for future in as_completed(futures):
                result = future.result()
                probe_results[result["model"]] = result
                log(
                    f"  [{result['model']}] protocol={result['protocol']} "
                    f"http={result['http_status']} available={result['available']}"
                )

    # 6) 計算變更集合。
    callable_ids = {
        model_id
        for model_id in catalog_ids
        if args.no_probe or probe_results.get(model_id, {}).get("available")
    }
    litellm_add_ids = sorted(callable_ids - configured_ids)
    litellm_orphan_routes = sorted(
        f"{ROUTE_PREFIX}{model_id}" for model_id in configured_ids - set(catalog_ids)
    )
    pi_missing_ids = [model_id for model_id in catalog_ids if model_id not in pi_effective]
    pi_new_ids = [model_id for model_id in pi_missing_ids if model_id in callable_ids]
    pi_waiting_remote = [model_id for model_id in pi_new_ids if model_id in remote_entries]
    pi_add_ids = [model_id for model_id in pi_new_ids if model_id not in remote_entries]
    pi_stale_models = sorted(model_id for model_id in pi["models_ids"] if model_id not in catalog_ids)
    pi_removed_ids = pi_stale_models if args.prune_pi else []

    # 7) LiteLLM：以既有管理工具產生候選設定（單一交易、單次重啟）。
    litellm_added_report: list[dict[str, Any]] = []
    litellm_removed_report: list[str] = []
    litellm_skipped: list[str] = []
    if litellm_add_ids or (args.prune and litellm_orphan_routes):
        if args.skip_litellm:
            litellm_skipped = list(litellm_add_ids)
        else:
            try:
                manage = load_manage_tool(args.manage_tool)
                original = read_text_raw(args.config)
                candidate = original
                added_routes: list[str] = []
                removed_routes: list[str] = []

                for model_id in litellm_add_ids:
                    protocol = (
                        overrides.get(model_id, {}).get("protocol")
                        or probe_results.get(model_id, {}).get("protocol")
                        or docs_entries.get(model_id, {}).get("protocol")
                        or "chat"
                    )
                    model_id_norm, route = manage.normalize_model_id(model_id)
                    candidate = manage.add_route(
                        candidate,
                        model_id_norm,
                        route,
                        protocol,
                        int(overrides.get(model_id, {}).get("maxInputTokens", DEFAULT_LITELLM_MAX_INPUT_TOKENS)),
                        int(overrides.get(model_id, {}).get("maxOutputTokens", DEFAULT_LITELLM_MAX_OUTPUT_TOKENS)),
                    )
                    added_routes.append(route)
                    litellm_added_report.append({"route": route, "protocol": protocol})

                for route in litellm_orphan_routes if args.prune else []:
                    candidate = manage.remove_route(candidate, route)
                    removed_routes.append(route)
                    litellm_removed_report.append(route)

                if candidate == original:
                    log("LiteLLM 沒有需要套用的變更")
                elif args.dry_run:
                    # dry-run：只在容器內用 PyYAML 驗證候選設定，不寫入也不重啟。
                    if args.no_restart:
                        log("LiteLLM dry-run：跳過容器內驗證（--no-restart）")
                    else:
                        log("LiteLLM dry-run：僅在容器內驗證候選設定，不寫入")
                        with tempfile.NamedTemporaryFile(
                            mode="w",
                            encoding="utf-8",
                            newline="",
                            prefix="litellm-dryrun-",
                            suffix=".yaml",
                            dir=args.config.parent,
                            delete=False,
                        ) as handle:
                            handle.write(candidate)
                            dry_path = Path(handle.name)
                        try:
                            manage.validate_candidate(dry_path, args.container)
                        except Exception as error:  # noqa: BLE001 - dry-run 驗證失敗只警告
                            source_errors["LiteLLM 候選驗證"] = f"{type(error).__name__}: {error}"
                            log(f"[警告] LiteLLM 候選設定驗證失敗：{error}")
                        finally:
                            dry_path.unlink()
                else:
                    apply_litellm_transaction(
                        manage,
                        args.config,
                        args.backup_dir,
                        candidate,
                        args.container,
                        added_routes,
                        removed_routes,
                        restart=not args.no_restart,
                        health_timeout=args.health_timeout,
                    )
            except Exception as error:  # noqa: BLE001 - 統一輸出可讀錯誤
                source_errors["LiteLLM 同步"] = f"{type(error).__name__}: {error}"
                log(f"[錯誤] LiteLLM 同步失敗：{error}")
                litellm_skipped = list(litellm_add_ids)
                litellm_added_report = []
                litellm_removed_report = []

    # 8) Pi models.json：只寫入通過驗證且 pi.dev 尚未收錄的模型。
    pi_added_report: list[dict[str, Any]] = []
    pi_removed_report: list[str] = []
    if (pi_add_ids or pi_removed_ids) and not args.skip_pi and args.pi_mode == "auto":
        entries: list[tuple[str, dict[str, Any]]] = []
        for model_id in pi_add_ids:
            entry = build_pi_entry(
                model_id,
                probe_results.get(model_id, {}),
                docs_entries.get(model_id, {}),
                remote_entries.get(model_id, {}),
                overrides.get(model_id, {}),
            )
            entries.append(
                (
                    model_id,
                    {
                        "entry": entry,
                        "probe": probe_results.get(model_id, {}),
                        "docs": docs_entries.get(model_id, {}),
                        "override": overrides.get(model_id, {}),
                    },
                )
            )
        try:
            new_text = insert_pi_entries(pi["models_text"], entries, pi_removed_ids)
            json.loads(strip_json_comments(new_text))  # 語法自我檢查
            if args.dry_run:
                log("Pi models.json dry-run：不寫入")
            else:
                timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                backup = pi["models_path"].with_name(f"models.json.bak-before-ocg-sync-{timestamp}")
                shutil.copy2(pi["models_path"], backup)
                temporary = pi["models_path"].with_name("models.json.tmp")
                write_text_raw(temporary, new_text)
                os.replace(temporary, pi["models_path"])
                log(f"已套用 Pi models.json：{pi['models_path']}")
                log(f"備份：{backup}")
            pi_added_report = [{"id": model_id, "entry": payload["entry"]} for model_id, payload in entries]
            pi_removed_report = list(pi_removed_ids)
        except Exception as error:  # noqa: BLE001
            source_errors["Pi models.json 同步"] = f"{type(error).__name__}: {error}"
            log(f"[錯誤] Pi models.json 同步失敗：{error}")
            pi_added_report = []
            pi_removed_report = []

    # 9) 更新協定快取。
    cache_models: dict[str, Any] = {}
    for model_id in catalog_ids:
        probe = probe_results.get(model_id)
        if probe and probe.get("available"):
            cache_models[model_id] = {
                "protocol": probe["protocol"],
                "reasoning": probe.get("reasoning"),
                "vision": probe.get("vision"),
                "updated": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
        elif model_id in protocol_cache:
            cache_models[model_id] = protocol_cache[model_id]
    atomic_write(
        report_dir / "protocol-cache.json",
        json.dumps({"version": VERSION, "models": cache_models}, ensure_ascii=False, indent=2) + "\n",
    )

    # 10) 組報告。
    model_rows: list[dict[str, Any]] = []
    previous_failures = read_state_file(report_dir / "failures.json")
    failure_state: dict[str, int] = {}
    for model_id in catalog_ids:
        probe = probe_results.get(model_id)
        if probe is None:
            failures = int(previous_failures.get(model_id, {}).get("consecutive_failures", 0))
        elif probe["available"]:
            failures = 0
        else:
            failures = int(previous_failures.get(model_id, {}).get("consecutive_failures", 0)) + 1
        failure_state[model_id] = failures
        model_rows.append(
            {
                "model": model_id,
                "protocol": (probe or {}).get("protocol")
                or docs_entries.get(model_id, {}).get("protocol")
                or protocol_cache.get(model_id, {}).get("protocol")
                or "unknown",
                "available": bool((probe or {}).get("available")),
                "error": (probe or {}).get("error", ""),
                "consecutive_failures": failures,
                "pi_configured": model_id in pi_effective,
                "litellm_configured": model_id in configured_ids,
                "documented": model_id in docs_entries,
            }
        )
    atomic_write(
        report_dir / "failures.json",
        json.dumps(
            {
                "version": VERSION,
                "models": {
                    model_id: {
                        "consecutive_failures": count,
                        "updated": datetime.now().astimezone().isoformat(timespec="seconds"),
                    }
                    for model_id, count in failure_state.items()
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )

    manual_attention: list[str] = []
    for item in source_errors.items():
        manual_attention.append(f"{item[0]}：{item[1]}")
    for item in litellm_skipped:
        manual_attention.append(f"`{item}` 可呼叫但未寫入 LiteLLM，請確認後重跑。")
    for row in model_rows:
        if row["available"] or row["consecutive_failures"] < args.quarantine_after:
            continue
        manual_attention.append(
            f"`{row['model']}` 連續失敗 {row['consecutive_failures']} 次（{row['error'][:60]}），暫不納入同步。"
        )
    for item in pi_added_report:
        if item["entry"]["contextWindow"] == DEFAULT_PI_CONTEXT_WINDOW:
            manual_attention.append(
                f"`{item['id']}` 的 contextWindow / maxTokens 為保守預設值，建議人工校對 models.json。"
            )
    for item in pi_waiting_remote:
        manual_attention.append(f"`{item}` 已由 pi.dev 遠端目錄收錄，等待 Pi 重新整理模型清單。")
    for route in litellm_orphan_routes:
        if not args.prune:
            manual_attention.append(f"`{route}` 上游 catalog 已消失，尚未移除（可用 --prune）。")
    for model_id in pi_stale_models:
        if not args.prune_pi:
            manual_attention.append(
                f"`{model_id}` 已不在上游 catalog（可能已改名），Pi models.json 仍保留（可用 --prune-pi 移除）。"
            )

    report = {
        "version": VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "dry_run": args.dry_run,
        "probe_enabled": not args.no_probe,
        "catalog_count": len(catalog_ids),
        "available_count": sum(1 for row in model_rows if row["available"]),
        "failed_count": sum(1 for row in model_rows if not row["available"] and not args.no_probe),
        "litellm_route_count": len(litellm_routes),
        "litellm_added": litellm_added_report,
        "litellm_removed": litellm_removed_report,
        "litellm_orphan_routes": litellm_orphan_routes,
        "pi_models_count": len(pi_effective),
        "pi_added": pi_added_report,
        "pi_removed": pi_removed_report,
        "pi_waiting_remote": pi_waiting_remote,
        "pi_stale_models": pi_stale_models,
        "manual_attention": manual_attention,
        "source_errors": source_errors,
        "models": model_rows,
    }
    atomic_write(report_dir / "models-sync-latest.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    atomic_write(report_dir / "models-sync-latest.md", build_markdown(report))
    date_name = datetime.now().astimezone().strftime("%Y-%m-%d")
    atomic_write(report_dir / f"models-sync-{date_name}.md", build_markdown(report))

    log("")
    if args.no_probe:
        log(f"catalog={report['catalog_count']}（未執行實測 --no-probe）")
    else:
        log(f"catalog={report['catalog_count']} 可呼叫={report['available_count']} 失敗={report['failed_count']}")
    log(
        f"LiteLLM 新增={len(litellm_added_report)} 移除={len(litellm_removed_report)}；"
        f"Pi 新增={len(pi_added_report)} 移除={len(pi_removed_report)}"
    )
    log(f"報告：{report_dir / 'models-sync-latest.md'}")
    if args.fail_on_unavailable and report["failed_count"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
