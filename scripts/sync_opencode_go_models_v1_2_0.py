#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OpenCode Go 訂閱可用模型清單同步工具 v1.2.0

用途
----
查詢 OpenCode Go 訂閱「目前實際可用」的模型，並把差異同步到兩份清單：

1. Pi 自訂模型清單：``~/.pi/agent/models.json`` 的 ``providers.opencode-go.models``
2. LiteLLM 路由：``M:\\AI\\ai-router-2026\\litellm\\config.yaml`` 的 ``opencode-go/*`` routes

資料來源（依可信度排序）
------------------------
1. OpenCode Go 即時 catalog：``https://opencode.ai/zen/go/v1/models``
   （官方 API，權威的「現在存在哪些模型 ID」來源）
2. models.dev：``https://models.dev/api.json``
   （**模型能力的權威來源**：limit.context / limit.output、reasoning 與 reasoning_options、
   modalities、cost、tool_call、structured_output。已驗證它與 Pi 官方目錄對 29 個共用模型的
   contextWindow / maxTokens / cost / input 完全一致，因此直接寫入設定檔。）
3. 人工覆寫：``metadata-overrides.json``（優先於一切自動來源）
4. OpenCode Go 官方文件：``https://opencode.ai/docs/go/``
   （權威的「每個模型該用哪個 API 協定」來源，另有每 1M token 價格）
5. pi.dev 遠端模型目錄：``https://pi.dev/api/models/providers/opencode-go``
6. 實際最小生成請求（probe）：確認模型「當下真的可呼叫」與協定
7. 能力實測（capability probe）：對所需的模型實際測試 system messages、function calling、
   reasoning、tool_choice、strict JSON schema、vision、native streaming、prompt caching，
   結果直接寫入 LiteLLM model_info 與 Pi models.json（邏輯合併自 opencode-go-monitor v1.0.0）
8. 上一次成功探測的協定快取：``reports/protocol-cache.json``

models.dev 查無該模型時，依序退用 pi.dev、官方文件、實測結果與保守預設值，
並在報告與 models.json 註解中標示實際採用的來源。

安全設計
--------
- API Key 只從環境變數或 ``auth.json`` 讀取，永不輸出、永不寫入報告。
- 所有寫入前都會建立時間戳備份。
- 只有「通過實際呼叫驗證」的模型才會被寫入設定檔。
- LiteLLM 候選設定會先在容器內用 PyYAML 驗證，套用後重新啟動並驗證 /v1/models。
- 預設套用變更；``--dry-run`` 只預覽（LiteLLM 仍會在容器內驗證候選設定，但不寫入）。
- 上游已消失的 route 預設只報告，需 ``--prune`` 才移除。
- 上游已消失、且經實測確認無法呼叫的殘留項目（Pi 與 LiteLLM）會自動移除，
  ``--keep-stale`` 可改為只報告。
- 能力實測會消耗少量訂閱額度；預設 ``--capabilities auto`` 只測真正需要的模型
  （新增模型、以及 models.dev 查無能力資料的管理項目），結果另有快取。
- 只會自動重建「本工具先前寫入」的 models.json 項目（有 ``[自動同步 v…]`` 註解者），
  人工手寫的項目不會被改寫。

版本：v1.2.0（變更：合併 opencode-go-monitor v1.0.0 的能力實測程式，支援 chat / responses /
anthropic 三種協定，實測結果優先寫入設定；上游已消失且無法呼叫的殘留項目自動移除）
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
VERSION = "1.2.0"
USER_AGENT = f"opencode-go-model-sync/{VERSION}"

CATALOG_URL = "https://opencode.ai/zen/go/v1/models"
DOCS_URL = "https://opencode.ai/docs/go/"
REMOTE_CATALOG_URL = "https://pi.dev/api/models/providers/opencode-go"
MODELS_DEV_URL = "https://models.dev/api.json"

# models.dev 的 provider.npm → OpenCode Go 的 API 協定（作為官方文件與實測之外的輔助線索）。
NPM_TO_PROTOCOL = {
    "@ai-sdk/openai-compatible": "chat",
    "@ai-sdk/openai": "responses",
    "@ai-sdk/anthropic": "anthropic",
}

# models.dev 的 api.json 約 5MB，因此本機快取後重複使用（預設 6 小時）。
# 快取不涵蓋 catalog 新模型或逾時，就會重新下載。
MODELS_DEV_CACHE_TTL_HOURS = 6.0
# 跨 provider 推定時的優先 provider（同廠商的 OpenCode Zen 與 OpenCode Go 同源）。
MODELS_DEV_CROSS_PROVIDER_PRIORITY = ("opencode",)

# Pi 的思考等級順序（用於依 models.dev reasoning_options 產生 thinkingLevelMap）。
PI_THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")

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

# ────────────────────── 能力實測設定（v1.2.0） ──────────────────────
# 下列設定與函式移植自 C:\op\OpenCodeGo\opencode-go-monitor-v1.0.0 的
# validate_and_update_opencode_capabilities_v1_0_0.py，改寫為同時支援 chat / responses /
# anthropic 三種協定，並以純資料回傳，供 LiteLLM model_info 與 Pi models.json 使用。
CAPABILITY_NAMES = (
    "system_messages",
    "function_calling",
    "reasoning",
    "tool_choice",
    "native_structured_output",
    "vision",
    "native_streaming",
    "prompt_caching",
)

# 實測可信度政策：
#   strong  = 實測 True / False 都直接採用（測試具決定性，失敗即代表當下不支援）
#   confirm = 只採用實測 True；False 時保留 models.dev 等既有來源，
#             避免因時序（快取暖機）或偵測方式造成的偽陰性把能力降級。
CAPABILITY_TRUST: dict[str, str] = {
    "system_messages": "strong",
    "function_calling": "strong",
    "tool_choice": "strong",
    "native_structured_output": "strong",
    "native_streaming": "strong",
    "vision": "confirm",
    "prompt_caching": "confirm",
    "reasoning": "confirm",
}

# 各協定可測試的能力：Anthropic Messages API 沒有標準的 json_schema response_format，
# 因此 native_structured_output 不列入該協定的測試項目。
PROTOCOL_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "chat": CAPABILITY_NAMES,
    "responses": CAPABILITY_NAMES,
    "anthropic": tuple(name for name in CAPABILITY_NAMES if name != "native_structured_output"),
}

# 實測結果 → LiteLLM model_info 欄位名稱。
LITELLM_CAPABILITY_FIELDS: dict[str, str] = {
    "system_messages": "supports_system_messages",
    "function_calling": "supports_function_calling",
    "reasoning": "supports_reasoning",
    "tool_choice": "supports_tool_choice",
    "native_structured_output": "supports_native_structured_output",
    "vision": "supports_vision",
    "native_streaming": "supports_native_streaming",
    "prompt_caching": "supports_prompt_caching",
}

# prompt caching 實測用的固定長前綴（與 validator v1.0.0 相同，約 25 KB）。
CACHE_VALIDATION_PREFIX = "CACHE VALIDATION " + ("stable coding context. " * 900)
DEFAULT_CAPABILITY_TTL_DAYS = 30.0


@dataclass
class HttpResult:
    """單一 HTTP 請求結果。"""

    status: int
    payload: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    raw: str = ""


@dataclass
class CapabilityResult:
    """單一能力實測結果（純資料，可序列化到報告與快取）。"""

    supported: bool | None = None
    http_status: int = 0
    error: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "http_status": self.http_status,
            "error": self.error,
            "details": self.details,
        }


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
    raw: bool = False,
) -> HttpResult:
    """送出 HTTP 請求並回傳解析後的 JSON；錯誤一律轉成 HttpResult。

    ``raw=True`` 時不解析 JSON，原始內容放在 ``HttpResult.raw``（SSE 串流測試用）。
    """
    request_headers = {
        "Accept": "text/event-stream" if raw else "application/json",
        "User-Agent": USER_AGENT,
        **headers,
    }
    data = None
    method = "GET"
    if body is not None:
        request_headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
        method = "POST"

    request = Request(url, headers=request_headers, data=data, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
            if raw:
                return HttpResult(response.status, {}, "", text)
            return HttpResult(response.status, json.loads(text) if text.strip() else {})
    except HTTPError as error:
        text = error.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError:
            payload = {"error": {"message": text or str(error)}}
        return HttpResult(error.code, payload, extract_error(payload) or str(error), text)
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


# ───────────────────────── models.dev 能力資料 ─────────────────────────
def fetch_models_dev(timeout: float) -> dict[str, Any]:
    """下載 models.dev 全量資料（約 5MB JSON）。"""
    result = http_json(MODELS_DEV_URL, {}, timeout)
    if result.status != 200 or not isinstance(result.payload, dict):
        raise RuntimeError(f"models.dev 查詢失敗：HTTP {result.status} {result.error}")
    return result.payload


def build_models_dev_index(
    raw: dict[str, Any],
    catalog_ids: Sequence[str],
    provider_id: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, dict[str, Any]]]]:
    """抽出指定 provider 的模型，並為 catalog 中缺少的 ID 建立跨 provider 對照。"""
    provider_models: dict[str, dict[str, Any]] = {}
    provider = raw.get(provider_id)
    if isinstance(provider, dict) and isinstance(provider.get("models"), dict):
        provider_models = {
            str(model_id): definition
            for model_id, definition in provider["models"].items()
            if isinstance(definition, dict)
        }

    fallback: dict[str, dict[str, dict[str, Any]]] = {}
    for model_id in catalog_ids:
        if model_id in provider_models:
            continue
        matches: dict[str, dict[str, Any]] = {}
        for other_id, other in raw.items():
            if not isinstance(other, dict) or not isinstance(other.get("models"), dict):
                continue
            entry = other["models"].get(model_id)
            if isinstance(entry, dict):
                matches[str(other_id)] = entry
        if matches:
            fallback[model_id] = matches
    return provider_models, fallback


def load_models_dev(
    catalog_ids: Sequence[str],
    provider_id: str,
    cache_path: Path,
    ttl_hours: float,
    refresh: bool,
    timeout: float,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, dict[str, Any]]], str]:
    """取得 models.dev 能力資料，優先使用未逾期且涵蓋全部 catalog 模型的快取。"""
    cached = read_json_file(cache_path)
    if cached and not refresh and cached.get("provider") == provider_id:
        try:
            age_hours = (time.time() - float(cached.get("fetched_at_epoch", 0))) / 3600.0
        except (TypeError, ValueError):
            age_hours = ttl_hours + 1
        cached_models = cached.get("provider_models")
        cached_fallback = cached.get("fallback")
        if (
            age_hours < ttl_hours
            and isinstance(cached_models, dict)
            and set(catalog_ids) <= set(cached_models)
        ):
            return cached_models, cached_fallback or {}, ""

    try:
        raw = fetch_models_dev(timeout)
    except Exception as error:  # noqa: BLE001 - 能力來源失敗時退用快取或其他來源
        if isinstance(cached, dict) and isinstance(cached.get("provider_models"), dict):
            return (
                cached["provider_models"],
                cached.get("fallback") or {},
                f"{type(error).__name__}: {error}（改用本機快取）",
            )
        return {}, {}, f"{type(error).__name__}: {error}"

    provider_models, fallback = build_models_dev_index(raw, catalog_ids, provider_id)
    atomic_write(
        cache_path,
        json.dumps(
            {
                "version": VERSION,
                "provider": provider_id,
                "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "fetched_at_epoch": time.time(),
                "provider_models": provider_models,
                "fallback": fallback,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    return provider_models, fallback, ""


def read_json_file(path: Path) -> dict[str, Any]:
    """讀取一般 JSON 檔（失敗時回傳空 dict）。"""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def most_common_value(values: Sequence[Any]) -> Any:
    """取眾數；平手時取較小值（保守）。"""
    counts: dict[Any, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    if not counts:
        return None
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def merge_cross_provider(matches: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """將不同 provider 對同一模型 ID 的描述合併（眾數、平手取保守值）。"""

    def limits(key: str) -> Any:
        return most_common_value(
            [
                (entry.get("limit") or {}).get(key)
                for entry in matches.values()
                if (entry.get("limit") or {}).get(key) is not None
            ]
        )

    def costs(key: str) -> float:
        return float(
            most_common_value(
                [
                    (entry.get("cost") or {}).get(key)
                    for entry in matches.values()
                    if (entry.get("cost") or {}).get(key) is not None
                ]
            )
            or 0.0
        )

    option_sets = [json.dumps(entry.get("reasoning_options") or [], sort_keys=True) for entry in matches.values()]
    merged_options = json.loads(most_common_value(option_sets) or "[]")
    modality_sets = [
        json.dumps(sorted(entry.get("modalities", {}).get("input") or [])) for entry in matches.values()
    ]
    merged_modalities = json.loads(most_common_value(modality_sets) or "[]")
    reasoning = most_common_value([bool(entry.get("reasoning")) for entry in matches.values()])
    npm = most_common_value(
        [
            (entry.get("provider") or {}).get("npm")
            for entry in matches.values()
            if (entry.get("provider") or {}).get("npm")
        ]
    )
    return {
        "name": most_common_value([entry.get("name") for entry in matches.values() if entry.get("name")]),
        "family": None,
        "reasoning": bool(reasoning),
        "reasoning_options": merged_options,
        "limit": {"context": limits("context"), "input": limits("input"), "output": limits("output")},
        "modalities": {"input": merged_modalities},
        "cost": {
            "input": costs("input"),
            "output": costs("output"),
            "cache_read": costs("cache_read"),
            "cache_write": costs("cache_write"),
        },
        "tool_call": most_common_value([entry.get("tool_call") for entry in matches.values()]),
        "structured_output": most_common_value(
            [entry.get("structured_output") for entry in matches.values()]
        ),
        "interleaved": {
            "field": most_common_value(
                [
                    (entry.get("interleaved") or {}).get("field")
                    for entry in matches.values()
                    if (entry.get("interleaved") or {}).get("field")
                ]
            )
        },
        "provider": {"npm": npm},
    }


def models_dev_meta(
    model_id: str,
    provider_models: dict[str, dict[str, Any]],
    fallback: dict[str, dict[str, dict[str, Any]]],
    provider_id: str,
) -> tuple[dict[str, Any] | None, str]:
    """把 models.dev 的模型資料整理成本工具內部使用的能力欄位。

    回傳 ``(meta, source)``；查無資料時回傳 ``(None, "")``。
    跨 provider 推定時會標示資料筆數，方便人工複核。
    """
    entry = provider_models.get(model_id)
    source = f"models.dev({provider_id})"
    if entry is None:
        matches = fallback.get(model_id) or {}
        preferred = next(
            (matches[pid] for pid in MODELS_DEV_CROSS_PROVIDER_PRIORITY if pid in matches), None
        )
        if preferred is not None:
            entry = preferred
            source = "models.dev(opencode)"
        elif matches:
            entry = merge_cross_provider(matches)
            source = f"models.dev(跨 provider 推定 {len(matches)} 筆)"
    if entry is None:
        return None, ""

    limit = entry.get("limit") or {}
    cost = entry.get("cost") or {}
    modalities = (entry.get("modalities") or {}).get("input") or []
    effort_values: list[str] = []
    for option in entry.get("reasoning_options") or []:
        if isinstance(option, dict) and option.get("type") == "effort":
            effort_values = [str(value) for value in (option.get("values") or [])]
    npm = str((entry.get("provider") or {}).get("npm") or "")
    interleaved = entry.get("interleaved") or {}

    # 只有出現在 models.dev 的 cost 欄位才可信；否則交回官方文件處理。
    has_cost = any(cost.get(key) is not None for key in ("input", "output", "cache_read", "cache_write"))
    return (
        {
            "source": source,
            "name": entry.get("name"),
            "family": entry.get("family"),
            "context": limit.get("context"),
            "input_window": limit.get("input"),
            "output": limit.get("output"),
            "reasoning": bool(entry.get("reasoning")),
            "effort_values": effort_values,
            "modalities": [str(value) for value in modalities],
            "has_cost": has_cost,
            "cost": {
                "input": float(cost.get("input") or 0.0),
                "output": float(cost.get("output") or 0.0),
                "cacheRead": float(cost.get("cache_read") or 0.0),
                "cacheWrite": float(cost.get("cache_write") or 0.0),
            },
            "tool_call": entry.get("tool_call"),
            "structured_output": entry.get("structured_output"),
            "interleaved_field": interleaved.get("field"),
            "protocol": NPM_TO_PROTOCOL.get(npm),
        },
        source,
    )


def thinking_level_map(meta: dict[str, Any] | None) -> dict[str, str | None] | None:
    """依 models.dev 的 effort 值產生 Pi thinkingLevelMap。

    規則（與 Pi 官方目錄的既有定義一致）：
    - 只有 effort 類型的值才會產生對應；Pi 等級同名值存在就對應，否則為 null。
    - “off” 對應 “none”（當上游提供時）；上游無法關閉思考時為 null。
    - 上游沒有 effort 選項時回傳 None（不寫入 thinkingLevelMap）。
    """
    if not meta:
        return None
    values = set(meta.get("effort_values") or [])
    if not values:
        return None
    mapping: dict[str, str | None] = {}
    for level in PI_THINKING_LEVELS:
        if level == "off":
            mapping["off"] = "none" if "none" in values else None
        else:
            mapping[level] = level if level in values else None
    return mapping


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


# ───────────────────────── 能力實測（v1.2.0） ─────────────────────────
# 移植自 opencode-go-monitor v1.0.0 的 validate_and_update_opencode_capabilities_v1_0_0.py。
# 差異：同一個測試套件同時支援 chat / responses / anthropic 三種協定，且只回傳純資料，
# 由本工具決定要寫入 LiteLLM model_info 或 Pi models.json 的哪些欄位。

def parse_sse_events(raw_text: str) -> list[dict[str, Any]]:
    """解析 SSE 串流內容（``data: {...}`` 行），忽略註解與 [DONE]。"""
    events: list[dict[str, Any]] = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def strict_answer_schema() -> dict[str, Any]:
    """strict JSON schema：只有完全符合 schema 的原始 JSON 才算支援。"""
    return {
        "type": "object",
        "properties": {"answer": {"type": "string", "const": "OK"}},
        "required": ["answer"],
        "additionalProperties": False,
    }


def capability_tool_definition(protocol: str) -> dict[str, Any]:
    """function calling / tool_choice 測試用的工具定義（依協定調整格式）。"""
    parameters = {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
        "additionalProperties": False,
    }
    if protocol == "anthropic":
        return {"name": "get_weather", "description": "Get weather for a city", "input_schema": parameters}
    if protocol == "responses":
        return {
            "type": "function",
            "name": "get_weather",
            "description": "Get weather for a city",
            "parameters": parameters,
        }
    return {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather for a city",
            "parameters": parameters,
        },
    }


def capability_forced_tool_choice(protocol: str) -> dict[str, Any]:
    """指定名稱的強制 tool_choice（各協定語法不同）。"""
    if protocol == "anthropic":
        return {"type": "tool", "name": "get_weather"}
    if protocol == "responses":
        return {"type": "function", "name": "get_weather"}
    return {"type": "function", "function": {"name": "get_weather"}}


def capability_vision_content(protocol: str) -> Any:
    """vision 測試的訊息內容（64x64 PNG；1x1 會被 Anthropic 拒絕而誤判）。"""
    question = "What is the dominant color of this image?"
    if protocol == "anthropic":
        return [
            {"type": "text", "text": question},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": TINY_PNG_BASE64},
            },
        ]
    if protocol == "responses":
        return [
            {"type": "input_text", "text": question},
            {"type": "input_image", "image_url": f"data:image/png;base64,{TINY_PNG_BASE64}"},
        ]
    return [
        {"type": "text", "text": question},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{TINY_PNG_BASE64}"}},
    ]


def chat_capability_body(
    capability: str,
    model_id: str,
    cache_prefix: str | None = None,
) -> dict[str, Any]:
    """Chat Completions 的單一能力測試請求主體。"""
    base: dict[str, Any] = {"model": model_id, "max_tokens": 512, "stream": False}
    if capability == "system_messages":
        return {
            **base,
            "messages": [
                {"role": "system", "content": "Follow the user's output format exactly."},
                {"role": "user", "content": "Reply exactly: OK"},
            ],
        }
    if capability in {"function_calling", "tool_choice"}:
        body = {
            **base,
            "messages": [{"role": "user", "content": "Use get_weather for Taipei."}],
            "tools": [capability_tool_definition("chat")],
        }
        if capability == "tool_choice":
            body["tool_choice"] = capability_forced_tool_choice("chat")
        return body
    if capability == "reasoning":
        return {
            **base,
            "messages": [
                {"role": "user", "content": "Solve 2+2 step by step, then reply exactly: OK"}
            ],
        }
    if capability == "native_structured_output":
        return {
            **base,
            "messages": [{"role": "user", "content": "Return answer OK as JSON."}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "answer", "strict": True, "schema": strict_answer_schema()},
            },
        }
    if capability == "vision":
        return {
            **base,
            "messages": [{"role": "user", "content": capability_vision_content("chat")}],
        }
    if capability == "native_streaming":
        return {
            **base,
            "stream": True,
            "messages": [{"role": "user", "content": "Reply exactly: OK"}],
        }
    if capability == "prompt_caching":
        return {
            **base,
            "messages": [
                {"role": "system", "content": cache_prefix or CACHE_VALIDATION_PREFIX},
                {"role": "user", "content": "Reply exactly: OK"},
            ],
        }
    raise ValueError(f"未知能力：{capability}")


def responses_capability_body(
    capability: str,
    model_id: str,
    cache_prefix: str | None = None,
) -> dict[str, Any]:
    """OpenAI Responses API 的單一能力測試請求主體。"""
    base: dict[str, Any] = {"model": model_id, "max_output_tokens": 512}
    if capability == "system_messages":
        return {
            **base,
            "instructions": "Follow the user's output format exactly.",
            "input": "Reply exactly: OK",
        }
    if capability in {"function_calling", "tool_choice"}:
        body = {
            **base,
            "input": "Use get_weather for Taipei.",
            "tools": [capability_tool_definition("responses")],
        }
        if capability == "tool_choice":
            body["tool_choice"] = capability_forced_tool_choice("responses")
        return body
    if capability == "reasoning":
        return {**base, "input": "Solve 2+2 step by step, then reply exactly: OK"}
    if capability == "native_structured_output":
        return {
            **base,
            "input": "Return answer OK as JSON.",
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "answer",
                    "strict": True,
                    "schema": strict_answer_schema(),
                }
            },
        }
    if capability == "vision":
        return {
            **base,
            "input": [{"role": "user", "content": capability_vision_content("responses")}],
        }
    if capability == "native_streaming":
        return {**base, "stream": True, "input": "Reply exactly: OK"}
    if capability == "prompt_caching":
        return {
            **base,
            "instructions": cache_prefix or CACHE_VALIDATION_PREFIX,
            "input": "Reply exactly: OK",
        }
    raise ValueError(f"未知能力：{capability}")


def anthropic_capability_body(
    capability: str,
    model_id: str,
    cache_prefix: str | None = None,
) -> dict[str, Any]:
    """Anthropic Messages API 的單一能力測試請求主體。"""
    # max_tokens 必須留足夠空間：思考型模型可能先把預算花在 thinking 上，
    # 導致回答被截断而誤判成「不支援」。
    base: dict[str, Any] = {"model": model_id, "max_tokens": 1024}
    if capability == "system_messages":
        return {
            **base,
            "system": "Follow the user's output format exactly.",
            "messages": [{"role": "user", "content": "Reply exactly: OK"}],
        }
    if capability in {"function_calling", "tool_choice"}:
        body = {
            **base,
            "messages": [{"role": "user", "content": "Use get_weather for Taipei."}],
            "tools": [capability_tool_definition("anthropic")],
        }
        if capability == "tool_choice":
            body["tool_choice"] = capability_forced_tool_choice("anthropic")
        return body
    if capability == "reasoning":
        return {
            **base,
            "messages": [
                {"role": "user", "content": "Solve 2+2 step by step, then reply exactly: OK"}
            ],
        }
    if capability == "vision":
        return {
            **base,
            "messages": [{"role": "user", "content": capability_vision_content("anthropic")}],
        }
    if capability == "native_streaming":
        return {
            **base,
            "stream": True,
            "messages": [{"role": "user", "content": "Reply exactly: OK"}],
        }
    if capability == "prompt_caching":
        # Anthropic 必須在內容區塊明確標示 cache_control 才會建立快取。
        return {
            **base,
            "system": [
                {
                    "type": "text",
                    "text": cache_prefix or CACHE_VALIDATION_PREFIX,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": "Reply exactly: OK"}],
        }
    raise ValueError(f"未知能力：{capability}（Anthropic 不測試 native_structured_output）")


def capability_request(
    protocol: str,
    capability: str,
    model_id: str,
    cache_prefix: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """依協定與能力產生實測請求（回傳 URL 與 request body）。"""
    if protocol == "chat":
        return CHAT_URL, chat_capability_body(capability, model_id, cache_prefix)
    if protocol == "responses":
        return RESPONSES_URL, responses_capability_body(capability, model_id, cache_prefix)
    if protocol == "anthropic":
        return ANTHROPIC_URL, anthropic_capability_body(capability, model_id, cache_prefix)
    raise ValueError(f"未知協定：{protocol}")


def chat_message_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message") or {}
    if not isinstance(message, dict):
        return ""
    return str(message.get("content") or "")


def chat_tool_calls(payload: dict[str, Any]) -> list[dict[str, Any]]:
    choices = payload.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return []
    message = choices[0].get("message") or {}
    calls = message.get("tool_calls") if isinstance(message, dict) else None
    return [item for item in (calls or []) if isinstance(item, dict)]


def responses_output_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    output = payload.get("output")
    return [item for item in (output or []) if isinstance(item, dict)]


def responses_output_text(payload: dict[str, Any]) -> str:
    """取出 Responses API 的 output_text（message → content → output_text）。"""
    collected: list[str] = []
    for item in responses_output_items(payload):
        if item.get("type") not in (None, "message"):
            continue
        for block in item.get("content") or []:
            if isinstance(block, dict) and block.get("type") in ("output_text", "text"):
                collected.append(str(block.get("text") or ""))
    return "".join(collected)


def responses_stream_text(events: Sequence[dict[str, Any]]) -> str:
    """串流事件中的文字片段（response.output_text.delta）。"""
    return "".join(
        str(event.get("delta") or "")
        for event in events
        if str(event.get("type") or "") == "response.output_text.delta"
    )


def chat_stream_text(events: Sequence[dict[str, Any]]) -> str:
    """Chat Completions SSE 的文字片段（choices[0].delta.content）。"""
    collected: list[str] = []
    for event in events:
        choices = event.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            continue
        delta = choices[0].get("delta") or {}
        if isinstance(delta, dict):
            collected.append(str(delta.get("content") or ""))
    return "".join(collected)


def anthropic_blocks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    content = payload.get("content")
    return [item for item in (content or []) if isinstance(item, dict)]


def anthropic_text(payload: dict[str, Any]) -> str:
    return "".join(
        str(block.get("text") or "")
        for block in anthropic_blocks(payload)
        if block.get("type") == "text"
    )


def anthropic_stream_text(events: Sequence[dict[str, Any]]) -> str:
    """Anthropic SSE 的文字片段（content_block_delta / text_delta）。"""
    collected: list[str] = []
    for event in events:
        if str(event.get("type") or "") != "content_block_delta":
            continue
        delta = event.get("delta") or {}
        if isinstance(delta, dict) and delta.get("type") == "text_delta":
            collected.append(str(delta.get("text") or ""))
    return "".join(collected)


def reasoning_tokens(protocol: str, payload: dict[str, Any]) -> int:
    """從 usage 取得 reasoning token 數（各協定欄位不同）。"""
    usage = payload.get("usage") or {}
    if not isinstance(usage, dict):
        return 0
    if protocol == "anthropic":
        details = usage.get("output_tokens_details") or {}
        return int((details or {}).get("thinking_tokens") or 0)
    if protocol == "responses":
        details = usage.get("output_tokens_details") or {}
        return int((details or {}).get("reasoning_tokens") or 0)
    details = usage.get("completion_tokens_details") or {}
    return int((details or {}).get("reasoning_tokens") or 0)


def cached_tokens(protocol: str, payload: dict[str, Any]) -> int:
    """從 usage 取得本次請求命中的快取 token 數（各協定欄位不同）。"""
    usage = payload.get("usage") or {}
    if not isinstance(usage, dict):
        return 0
    if protocol == "anthropic":
        return int(usage.get("cache_read_input_tokens") or 0)
    if protocol == "responses":
        details = usage.get("input_tokens_details") or {}
        return int((details or {}).get("cached_tokens") or 0)
    details = usage.get("prompt_tokens_details") or {}
    return int((details or {}).get("cached_tokens") or 0)


def parse_json_exact(text: str) -> Any:
    """嚴格解析：不接受 Markdown fence 包裏（native structured output 的判定依據）。"""
    try:
        return json.loads(text.strip())
    except (json.JSONDecodeError, TypeError, AttributeError):
        return None


def capability_supported(
    protocol: str,
    capability: str,
    status: int,
    payload: dict[str, Any],
    raw_text: str = "",
) -> bool | None:
    """判定單一能力是否支援。

    回傳 ``None`` 表示「此協定無法判定該能力」（例如 Anthropic 的 native_structured_output），
    呼叫端應用既有來源（models.dev）而不應該把它當成「不支援」。
    """
    if capability not in PROTOCOL_CAPABILITIES.get(protocol, ()):
        return None
    if status != 200:
        return False
    events = parse_sse_events(raw_text) if raw_text else []
    tool_name = "get_weather"

    if protocol == "chat":
        text = chat_message_text(payload).strip()
        calls = chat_tool_calls(payload)
        message = (payload.get("choices") or [{}])[0].get("message") or {}
        has_reasoning = reasoning_tokens(protocol, payload) > 0 or bool(
            isinstance(message, dict) and (message.get("reasoning_content") or message.get("reasoning"))
        )
        # 無法判定：模型只吐 thinking 就被 token 上限切掉，不能當成「不支援」。
        if capability in {"system_messages", "native_structured_output", "vision"} and not text and has_reasoning:
            return None
        if capability == "system_messages":
            return text == "OK"
        if capability in {"function_calling", "tool_choice"}:
            return bool(calls) and str(calls[0].get("function", {}).get("name")) == tool_name
        if capability == "reasoning":
            return has_reasoning
        if capability == "native_structured_output":
            return parse_json_exact(text) == {"answer": "OK"}
        if capability == "vision":
            return bool(text)
        if capability == "native_streaming":
            return len(events) > 1 and chat_stream_text(events).strip() == "OK"
        return None

    if protocol == "responses":
        text = responses_output_text(payload).strip()
        calls = [item for item in responses_output_items(payload) if item.get("type") == "function_call"]
        has_reasoning = reasoning_tokens(protocol, payload) > 0 or any(
            item.get("type") == "reasoning" for item in responses_output_items(payload)
        )
        # 無法判定：只有 reasoning 輸出、沒有文字回答（token 上限造成截断）。
        if capability in {"system_messages", "native_structured_output", "vision"} and not text and has_reasoning:
            return None
        if capability == "system_messages":
            return text == "OK"
        if capability in {"function_calling", "tool_choice"}:
            return bool(calls) and str(calls[0].get("name")) == tool_name
        if capability == "reasoning":
            return has_reasoning
        if capability == "native_structured_output":
            return parse_json_exact(text) == {"answer": "OK"}
        if capability == "vision":
            return bool(text)
        if capability == "native_streaming":
            return len(events) > 1 and responses_stream_text(events).strip() == "OK"
        return None

    if protocol == "anthropic":
        text = anthropic_text(payload).strip()
        blocks = anthropic_blocks(payload)
        has_reasoning = any(block.get("type") == "thinking" for block in blocks) or (
            reasoning_tokens(protocol, payload) > 0
        )
        # 無法判定：只有 thinking 區塊、沒有文字回答（token 上限造成截断）。
        if capability in {"system_messages", "vision"} and not text and has_reasoning:
            return None
        if capability == "system_messages":
            return text == "OK"
        if capability in {"function_calling", "tool_choice"}:
            return any(
                block.get("type") == "tool_use" and str(block.get("name")) == tool_name
                for block in blocks
            )
        if capability == "reasoning":
            return has_reasoning
        if capability == "vision":
            return bool(text)
        if capability == "native_streaming":
            return len(events) > 1 and anthropic_stream_text(events).strip() == "OK"
        return None

    return None


def run_prompt_caching_test(
    protocol: str,
    model_id: str,
    api_key: str,
    timeout: float,
    session_id: str,
) -> CapabilityResult:
    """prompt caching 需多送幾次相同前綴，以第二次之後的 cached_tokens 判定。"""
    url, body = capability_request(protocol, "prompt_caching", model_id)
    counts: list[int] = []
    status = 0
    error = ""
    for _ in range(4):
        result = http_json(
            url,
            opencode_headers(api_key, session_id, anthropic=(protocol == "anthropic")),
            timeout,
            body,
        )
        status = result.status
        error = result.error
        counts.append(cached_tokens(protocol, result.payload))
        if status != 200:
            break
    supported = any(count > 0 for count in counts[1:])
    return CapabilityResult(
        supported=supported,
        http_status=status,
        error=error or ("" if supported else "重複請求均未回報 cached_tokens"),
        details={"cached_tokens_per_request": counts},
    )


def run_capability_tests(
    protocol: str,
    model_id: str,
    api_key: str,
    timeout: float,
    capabilities: Sequence[str] | None = None,
) -> dict[str, Any]:
    """對單一模型執行能力實測，回傳 ``{capability: {...}}`` 純資料。

    遇到 401/403/429/502/503/504 等會直接中止其餘測試，避免浪費配額或誤判能力。
    """
    session_id = str(uuid.uuid4())
    names = list(capabilities or PROTOCOL_CAPABILITIES.get(protocol, ()))
    results: dict[str, Any] = {}
    for name in names:
        if name == "prompt_caching":
            result = run_prompt_caching_test(protocol, model_id, api_key, timeout, session_id)
        else:
            url, body = capability_request(protocol, name, model_id)
            streaming = name == "native_streaming"
            response = http_json(
                url,
                opencode_headers(api_key, session_id, anthropic=(protocol == "anthropic")),
                timeout,
                body,
                raw=streaming,
            )
            supported = capability_supported(
                protocol,
                name,
                response.status,
                response.payload,
                response.raw if streaming else "",
            )
            details: dict[str, Any] = {}
            if name == "reasoning":
                details["reasoning_tokens"] = reasoning_tokens(protocol, response.payload)
            elif streaming:
                details["event_count"] = len(parse_sse_events(response.raw))
            result = CapabilityResult(
                supported=supported,
                http_status=response.status,
                error=response.error,
                details=details,
            )
        results[name] = result.to_dict()
        log(
            f"  [能力] {model_id} {name}="
            f"{'yes' if result.supported else ('no' if result.supported is False else 'n/a')}"
            f" http={result.http_status}"
        )
        if result.http_status in STOP_PROTOCOL_FALLBACK_STATUSES:
            # 權限、配額或上游故障：保留未測試的能力為「未判定」，不要寫入不正確的能力值。
            log(f"  [能力] {model_id} 因 HTTP {result.http_status} 中止其餘能力測試")
            break
    return results


def capability_cache_fresh(entry: dict[str, Any], ttl_days: float) -> bool:
    """判斷能力快取是否仍在有效期限內。"""
    if not entry or not entry.get("results"):
        return False
    stamp = str(entry.get("checked_at") or "")
    if not stamp:
        return False
    try:
        checked = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=datetime.now().astimezone().tzinfo)
    age = (datetime.now().astimezone() - checked).total_seconds()
    return age < max(0.0, ttl_days) * 86400


def apply_capability_overrides(
    capability_results: dict[str, dict[str, Any]],
    overrides: dict[str, Any],
) -> list[str]:
    """把 metadata-overrides.json 的 capabilities 人工覆寫併入實測結果。

    回傳被人工覆寫的模型 ID 清單。
    """
    applied_ids: list[str] = []
    for model_id, override in overrides.items():
        wanted = override.get("capabilities") if isinstance(override, dict) else None
        if not isinstance(wanted, dict):
            continue
        merged = dict(capability_results.get(model_id) or {})
        applied = False
        for name, value in wanted.items():
            if name in CAPABILITY_NAMES and isinstance(value, bool):
                merged[name] = {
                    "supported": value,
                    "http_status": 0,
                    "error": "",
                    "details": {"source": "人工覆寫 metadata-overrides.json"},
                }
                applied = True
        if applied:
            capability_results[model_id] = merged
            applied_ids.append(model_id)
    return applied_ids


def capability_targets(
    mode: str,
    catalog_ids: Sequence[str],
    callable_ids: set[str],
    auto_ids: set[str],
) -> list[str]:
    """決定要執行能力實測的模型清單。

    mode 可以是 ``off``、``all``、``auto`` 或逗號分隔的模型 ID 清單。
    """
    if mode == "off":
        return []
    if mode == "all":
        return [model_id for model_id in catalog_ids if model_id in callable_ids]
    if mode == "auto":
        return sorted(auto_ids & callable_ids)
    explicit = {part.strip() for part in mode.split(",") if part.strip()}
    return [model_id for model_id in catalog_ids if model_id in explicit and model_id in callable_ids]


def apply_capabilities_to_updates(
    updates: dict[str, Any],
    capabilities: dict[str, Any] | None,
) -> list[str]:
    """把實測能力合併進 LiteLLM model_info 更新內容。

    回傳「實測結果與 models.dev 不同」的能力名稱清單，供報告列為需人工確認事項。
    """
    conflicts: list[str] = []
    for name, key in LITELLM_CAPABILITY_FIELDS.items():
        result = (capabilities or {}).get(name) or {}
        value = result.get("supported") if isinstance(result, dict) else None
        if value is None:
            continue
        if CAPABILITY_TRUST.get(name) == "confirm" and not value:
            # 只採用陽性結果：例如快取暖機未生效不代表上游真的沒有 prompt caching。
            continue
        previous = updates.get(key)
        if previous is not None and bool(previous) != bool(value):
            conflicts.append(name)
        updates[key] = bool(value)
        if name == "native_structured_output":
            updates["supports_response_schema"] = bool(value)
        if name == "vision" and value:
            # 一定保留 text；沒有 models.dev 資料時基礎清單就是 ["text"]。
            modalities = list(updates.get("supported_modalities") or ["text"])
            if "text" not in modalities:
                modalities.insert(0, "text")
            if "image" not in modalities:
                modalities.append("image")
            updates["supported_modalities"] = modalities
    return conflicts


def pi_capability_inputs(inputs: list[str], capabilities: dict[str, Any] | None) -> list[str]:
    """實測确认可讀圖片時，把 image 加入 Pi 的 input 清單。"""
    result = (capabilities or {}).get("vision") or {}
    if result.get("supported") is True and "image" not in inputs:
        return [*inputs, "image"]
    return inputs


def pi_capability_reasoning(reasoning: bool, capabilities: dict[str, Any] | None) -> bool:
    """實測看到思考內容時，將 Pi 的 reasoning 設為 true。"""
    result = (capabilities or {}).get("reasoning") or {}
    if result.get("supported") is True:
        return True
    return bool(reasoning)


def capability_conflicts(
    meta: dict[str, Any] | None,
    capabilities: dict[str, Any] | None,
) -> list[str]:
    """比對實測結果與 models.dev 的差異，回傳不一致的能力說明（供人工確認）。"""
    if not meta or not capabilities:
        return []
    expected = {
        "function_calling": bool(meta.get("tool_call")),
        "native_structured_output": bool(meta.get("structured_output")),
        "reasoning": bool(meta.get("reasoning")),
        "vision": "image" in (meta.get("modalities") or []),
        "prompt_caching": float((meta.get("cost") or {}).get("cacheRead") or 0.0) > 0,
    }
    conflicts: list[str] = []
    for name, want in expected.items():
        result = (capabilities.get(name) or {}).get("supported")
        if result is None:
            continue
        if CAPABILITY_TRUST.get(name) == "confirm" and not result:
            # confirm 類能力的陰性結果不會寫入設定，因此不需列為差異。
            continue
        if bool(result) != want:
            conflicts.append(
                f"{name}（實測 {'yes' if result else 'no'}／models.dev {'yes' if want else 'no'}）"
            )
    return conflicts


def capability_summary(capabilities: dict[str, Any] | None) -> str:
    """產生給報告與 models.json 註解用的能力摘要（僅列已判定為支援的項目）。"""
    supported = [
        name
        for name, result in (capabilities or {}).items()
        if isinstance(result, dict) and result.get("supported") is True
    ]
    return "、".join(supported)


def model_protocol(
    model_id: str,
    overrides: dict[str, Any],
    docs_entries: dict[str, Any],
    model_meta: dict[str, Any],
    probe_results: dict[str, Any],
    protocol_cache: dict[str, Any],
) -> str:
    """決定模型實際使用的 API 協定（與寫入設定的優先序一致）。"""
    return (
        (overrides.get(model_id) or {}).get("protocol")
        or (docs_entries.get(model_id) or {}).get("protocol")
        or (model_meta.get(model_id) or {}).get("protocol")
        or (probe_results.get(model_id) or {}).get("protocol")
        or (protocol_cache.get(model_id) or {}).get("protocol")
        or "chat"
    )


def write_capability_reports(
    directory: Path,
    model_id: str,
    protocol: str,
    results: dict[str, Any],
    checked_at: str,
) -> None:
    """寫出單一模型的能力實測報告（與原 validator 的報告格式對齊）。"""
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "validator_version": VERSION,
        "model": model_id,
        "protocol": protocol,
        "checked_at": checked_at,
        "tests": results,
    }
    atomic_write(
        directory / f"{model_id}-latest.json",
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )
    lines = [
        f"# {model_id} 能力實測報告",
        "",
        f"- 工具版本：`v{VERSION}`（能力實測程式合併自 opencode-go-monitor v1.0.0）",
        f"- API 協定：`{protocol}`",
        f"- 執行時間：`{checked_at}`",
        "",
        "| Capability | 支援 | HTTP | 證據／錯誤 |",
        "|---|---:|---:|---|",
    ]
    for name, result in results.items():
        supported = result.get("supported")
        mark = "yes" if supported is True else ("no" if supported is False else "n/a")
        detail = result.get("error") or json.dumps(result.get("details") or {}, ensure_ascii=False)
        detail = str(detail).replace("|", "\\|").replace("\n", " ")[:600]
        lines.append(f"| `{name}` | {mark} | {result.get('http_status', '-')} | {detail} |")
    lines.extend(
        [
            "",
            "> contextWindow / maxTokens 無法用小型請求證明，仍取自 models.dev 或人工覆寫值。",
            "> 實測政策：strong 類能力（system messages、function calling、tool_choice、strict JSON、streaming）",
            "> 的失敗與成功都直接採用；confirm 類能力（reasoning、vision、prompt caching）只採用陽性結果。",
            "",
        ]
    )
    atomic_write(directory / f"{model_id}-latest.md", "\n".join(lines))


# ───────────────────────────── 變更內容建立 ─────────────────────────────
def build_pi_entry(
    model_id: str,
    probe: dict[str, Any],
    docs_entry: dict[str, Any],
    remote_entry: dict[str, Any],
    override: dict[str, Any],
    meta: dict[str, Any] | None = None,
    capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """建立可寫入 Pi models.json 的模型定義。

    能力優先序：人工覆寫 > models.dev > pi.dev 遠端定義 > 官方文件 > 實測 > 保守預設。
    寫入前已由 probe 驗證模型可呼叫，因此 models.dev 的 contextWindow / maxTokens 可直接信任。
    """
    meta = meta or {}
    protocol = (
        override.get("protocol")
        or docs_entry.get("protocol")
        or meta.get("protocol")
        or probe.get("protocol")
        or "chat"
    )
    api, base_url = PI_API_BY_PROTOCOL.get(protocol, PI_API_BY_PROTOCOL["chat"])

    reasoning = override.get("reasoning")
    if reasoning is None:
        if meta:
            reasoning = bool(meta.get("reasoning"))
        elif probe.get("reasoning") is not None:
            reasoning = bool(probe.get("reasoning"))
        else:
            reasoning = False
    # v1.2.0：實測看到思考內容時直接採用（models.dev 查無資料時的重要補位）。
    reasoning = pi_capability_reasoning(reasoning, capabilities)

    inputs = override.get("input")
    if inputs is None:
        if meta.get("modalities"):
            inputs = ["text", "image"] if "image" in meta["modalities"] else ["text"]
        elif probe.get("vision"):
            inputs = ["text", "image"]
        else:
            inputs = ["text"]
    # 圖片輸入有兩項獨立證據：models.dev modalities 與能力實測，任一確認就寫入 image。
    inputs = pi_capability_inputs(list(inputs), capabilities)

    if override.get("cost"):
        cost = override["cost"]
    elif meta.get("has_cost"):
        cost = meta["cost"]
    elif docs_entry.get("cost"):
        cost = docs_entry["cost"]
    else:
        cost = {"input": 0.0, "output": 0.0, "cacheRead": 0.0, "cacheWrite": 0.0}

    entry: dict[str, Any] = {
        "id": model_id,
        "name": override.get("name")
        or meta.get("name")
        or docs_entry.get("name")
        or remote_entry.get("name")
        or model_id,
        "api": api,
        "baseUrl": base_url,
        "reasoning": bool(reasoning),
        "input": inputs,
        "contextWindow": int(
            override.get("contextWindow")
            or meta.get("context")
            or remote_entry.get("contextWindow")
            or DEFAULT_PI_CONTEXT_WINDOW
        ),
        "maxTokens": int(
            override.get("maxTokens")
            or meta.get("output")
            or remote_entry.get("maxTokens")
            or DEFAULT_PI_MAX_TOKENS
        ),
        "cost": {
            "input": float(cost.get("input", 0.0)),
            "output": float(cost.get("output", 0.0)),
            "cacheRead": float(cost.get("cacheRead", 0.0)),
            "cacheWrite": float(cost.get("cacheWrite", 0.0)),
        },
    }

    level_map = thinking_level_map(meta) if meta else None
    if level_map is None and entry["reasoning"]:
        # models.dev 沒有 effort 選項時，退用「不送出思考參數」的全 null 對應。
        # 實測各模型接受的思考參數差異極大（部分拒絕 disabled、部分拒絕 minimal）。
        level_map = {level: None for level in PI_THINKING_LEVELS}
    # 只有至少一個非 null 等級時，Pi 才會送出思考參數。
    sends_thinking_params = bool(level_map) and any(
        value is not None for value in level_map.values()
    )

    if api == "openai-completions":
        entry["compat"] = {
            "supportsStore": False,
            "supportsDeveloperRole": False,
            "supportsStrictMode": True,
            "maxTokensField": "max_tokens",
        }
        if not sends_thinking_params:
            # 沒有可用的思考等級對應時，不讓 Pi 送出 reasoning_effort。
            entry["compat"]["supportsReasoningEffort"] = False
        if meta.get("interleaved_field") == "reasoning_content" and model_id.startswith("deepseek"):
            # DeepSeek 系列上游要求在回放 assistant 訊息時帶上 reasoning_content
            # （與 Pi 官方目錄對 deepseek-v4-* 的設定一致）。
            entry["compat"]["requiresReasoningContentOnAssistantMessages"] = True
            entry["compat"]["thinkingFormat"] = "deepseek"
    elif api == "openai-responses":
        entry["compat"] = {"sessionAffinityFormat": "openai-nosession"}
    else:
        # Anthropic Messages：上游可能回傳空 signature，允許 Pi 接受（同 qwen3.8-flash）。
        entry["compat"] = {"allowEmptySignature": True}

    if level_map is not None:
        entry["thinkingLevelMap"] = level_map
    return entry


def pi_entry_comment(
    model_id: str,
    probe: dict[str, Any],
    docs_entry: dict[str, Any],
    override: dict[str, Any],
    timestamp: str,
    meta: dict[str, Any] | None = None,
    capabilities: dict[str, Any] | None = None,
) -> str:
    """產生寫入 models.json 的說明註解（含來源與待人工確認事項）。"""
    meta = meta or {}
    # 這行標記是「本工具管理」的識別依據，重新執行時會依最新資料重建此模型。
    lines = [f"        // [自動同步 v{VERSION} {timestamp}] {model_id}"]
    if meta.get("source"):
        lines.append(f"        // 能力來源：{meta['source']}。")
    else:
        lines.append("        // 能力來源：models.dev 查無資料（官方文件／實測／保守預設）。")
    if docs_entry.get("protocol"):
        lines.append("        // API 協定來源：OpenCode Go 官方文件 Endpoints 表。")
    elif meta.get("protocol"):
        lines.append("        // API 協定來源：models.dev provider.npm 對應。")
    else:
        lines.append("        // 注意：官方文件未收錄此模型，API 協定由實際請求探測判定。")
    if meta.get("context") and meta.get("output"):
        lines.append("        // contextWindow / maxTokens 直接取自 models.dev limit 欄位。")
    elif "contextWindow" not in override or "maxTokens" not in override:
        lines.append(
            "        // TODO：contextWindow / maxTokens 尚無 models.dev 或官方規格，目前為保守預設值，請人工校對。"
        )
    if meta:
        if meta.get("effort_values"):
            lines.append(
                "        // thinkingLevelMap 依 models.dev reasoning_options 的 effort 值產生。"
            )
        elif meta.get("reasoning"):
            lines.append("        // models.dev 未提供 effort 選項，思考層級參數全部不送出。")
    elif probe.get("reasoning") is None:
        lines.append("        // TODO：思考（reasoning）能力未經確認，目前設為 false 以免送出無效參數。")
    if capability_summary(capabilities):
        # v1.2.0：能力實測結果（直接測試本訂閱當下行為）
        lines.append(f"        // 能力實測（v{VERSION}）：{capability_summary(capabilities)}。")
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

    # 先移除指定模型（例如上游 catalog 已消失的自訂模型，或即將重建的管理項目）。
    for model_id in remove_ids:
        pattern = re.compile(r'"id"\s*:\s*"' + re.escape(model_id) + r'"')
        removed_any = False
        while True:
            open_index, close_index = find_opencode_models_array(text)
            matched = False
            for span_start, span_end in iter_top_level_objects(text, open_index, close_index):
                if pattern.search(text[span_start:span_end]):
                    text = remove_object_span(text, span_start, span_end)
                    matched = True
                    removed_any = True
                    break
            if not matched:
                break
        if not removed_any:
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
            payload.get("meta"),
            payload.get("capabilities"),
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
def find_managed_model_ids(models_text: str) -> set[str]:
    """找出「本工具先前寫入」的模型 ID（models.json 內的 ``[自動同步 v…]`` 註解）。

    只有這些項目會被重新同步覆寫；人工手寫的模型定義不會被本工具更動。
    """
    try:
        open_index, close_index = find_opencode_models_array(models_text)
        spans = iter_top_level_objects(models_text, open_index, close_index)
    except RuntimeError:
        return set()

    managed: set[str] = set()
    for start, end in spans:
        head_lines = models_text[:start].split("\n")[-6:]
        if not any("自動同步 v" in line for line in head_lines):
            continue
        match = re.search(r'"id"\s*:\s*"([^"]+)"', models_text[start:end])
        if match:
            managed.add(match.group(1))
    return managed


def litellm_block_text(text: str, route_name: str, manage: Any) -> str:
    """取回指定 LiteLLM route 的 YAML 原文（找不到時回傳空字串）。"""
    lines = text.splitlines(keepends=True)
    try:
        blocks = manage.find_model_blocks(lines)
    except ValueError:
        return ""
    span = blocks.get(route_name)
    if not span:
        return ""
    start, end = span
    return "".join(lines[start:end])


def patch_litellm_block(
    text: str,
    route_name: str,
    manage: Any,
    meta: dict[str, Any] | None,
    capabilities: dict[str, Any] | None = None,
) -> str:
    """把能力資料寫入指定 LiteLLM route 的 model_info / 成本欄位。

    能力來源優先序：能力實測（v1.2.0）> models.dev > 原有保守預設值。
    保留既有管理工具產生的 YAML 結構與註解，只改寫能力相關的純量欄位。
    """
    if not meta and not capabilities:
        return text
    meta = meta or {}
    lines = text.splitlines(keepends=True)
    try:
        blocks = manage.find_model_blocks(lines)
    except ValueError:
        return text
    span = blocks.get(route_name)
    if not span:
        return text
    start, end = span
    block = "".join(lines[start:end])

    cost = meta.get("cost") or {}
    modalities = list(meta.get("modalities") or [])
    supports_vision = "image" in modalities
    context_window = int(meta.get("context") or 0)
    input_window = int(meta.get("input_window") or context_window or 0)
    output_tokens = int(meta.get("output") or 0)

    updates: dict[str, Any] = {}
    if meta:
        if output_tokens:
            updates["max_tokens"] = output_tokens
            updates["max_output_tokens"] = output_tokens
        if input_window:
            updates["max_input_tokens"] = input_window
        updates["supports_vision"] = supports_vision
        updates["supports_pdf_input"] = "pdf" in modalities
        updates["supports_audio_input"] = "audio" in modalities
        updates["supports_function_calling"] = bool(meta.get("tool_call"))
        updates["supports_reasoning"] = bool(meta.get("reasoning"))
        updates["supports_response_schema"] = bool(meta.get("structured_output"))
        updates["supports_native_structured_output"] = bool(meta.get("structured_output"))
        updates["supported_modalities"] = ["text"] + (["image"] if supports_vision else [])
        # 有 cache 讀取價格就表示上游有 prompt caching（官方價目表列 Cached Read 的模型）。
        updates["supports_prompt_caching"] = float(cost.get("cacheRead") or 0.0) > 0
        updates["input_cost_per_token"] = round(float(cost.get("input") or 0.0) / 1_000_000, 12)
        updates["output_cost_per_token"] = round(float(cost.get("output") or 0.0) / 1_000_000, 12)

    # 能力實測結果優先於 models.dev（那是本訂閱當下實際行為的直接證據）。
    conflicts = apply_capabilities_to_updates(updates, capabilities)
    if conflicts:
        log(f"  [能力] {route_name} 實測與 models.dev 不一致：{', '.join(conflicts)}（已採用實測值）")

    for key, value in updates.items():
        block = set_yaml_scalar(block, key, yaml_scalar(value))
    # 若管理工具的 block 沒有某個 key（例如日後改版移除），就在 model_info 尾端補上，
    # 避免默默地保留錯誤的預設值。
    for key, value in updates.items():
        if not has_yaml_key(block, key):
            block = append_to_model_info(block, key, yaml_scalar(value))
    return "".join(lines[:start]) + block + "".join(lines[end:])


def has_yaml_key(block: str, key: str) -> bool:
    """檢查 YAML 區塊內是否有指定的 key。"""
    return re.search(rf"^\s*{re.escape(key)}:\s*", block, re.MULTILINE) is not None


def append_to_model_info(block: str, key: str, value: str) -> str:
    """把缺少的 key 補在 model_info 區塊尾端（保留原換行風格與縮排）。"""
    newline = "\r\n" if "\r\n" in block else "\n"
    marker = re.search(r"^\s*litellm_params:\s*$", block, re.MULTILINE)
    if marker is None:
        return block
    insert_at = marker.start()
    # 縮排與 model_info 內其他欄位對齊。
    lines = block[:insert_at].splitlines()
    indent = "      "
    for line in reversed(lines):
        match = re.match(r"^(\s*)\S", line)
        if match:
            indent = match.group(1)
            break
    return f"{block[:insert_at]}{indent}{key}: {value}{newline}{block[insert_at:]}"


def yaml_scalar(value: Any) -> str:
    """把 Python 值轉成 YAML 純量（布林用小寫，list 用 flow 格式）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value), ensure_ascii=False)
    if isinstance(value, float):
        return f"{value:.12g}"
    return str(value)


def set_yaml_scalar(block: str, key: str, value: str, count: int = 0) -> str:
    """改寫 YAML 區塊中指定 key 的值（count=0 表示改寫全部同名 key）。"""
    pattern = re.compile(rf"^(\s*{re.escape(key)}:\s*)[^\r\n]*", re.MULTILINE)
    return pattern.sub(lambda match: f"{match.group(1)}{value}", block, count=count)


# ───────────────────────── Pi 顯示清單驗証（v1.2.0） ─────────────────────
# 目的：技能負責「資料正確」，Pi extension（pi-ext-opencode-go-catalog）負責
# 「Pi 看得見什麼」。本區塊把 extension 的裁切結果納入同一份報告，讓兩者一致效。
PI_CATALOG_PROVIDER = "opencode-go"
PI_EXTENSION_PREFIX = "pi-ext-opencode-go-catalog"
PI_CATALOG_EXPECT_PRESENT = ("space-bunny",)
PI_CATALOG_EXPECT_ABSENT = ("space-bunny-free",)


def decode_process_output(raw: bytes) -> str:
    """解碼子行程輸出（Windows 上可能是 UTF-8、CP950 或 CP1252）。"""
    for encoding in ("utf-8", "cp950", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_pi_list_output(text: str, provider: str = PI_CATALOG_PROVIDER) -> list[str]:
    """解析 `pi --list-models <provider>` 的表格輸出，回傳模型 ID 清單。

    表格格式：provider  model  context  max-out  thinking  images
    非表格行（表頭、提示訊息）會被忽略。
    """
    models: list[str] = []
    for raw_line in text.replace("\r\n", "\n").split("\n"):
        tokens = raw_line.strip().split()
        if len(tokens) < 2:
            continue
        if tokens[0].lower() == "provider":  # 表頭
            continue
        if tokens[0].lower() != provider.lower():  # 其他 provider 或說明文字
            continue
        model_id = tokens[1]
        if model_id and model_id not in models:
            models.append(model_id)
    return models


def pi_catalog_extension_state(agent_dir: Path) -> list[str]:
    """列出本專案的 Pi extension 目錄名稱與啟用狀態（唯讀）。"""
    state: list[str] = []
    try:
        extensions_dir = agent_dir / "extensions"
        if not extensions_dir.is_dir():
            return state
        for entry in sorted(extensions_dir.iterdir()):
            if not entry.is_dir() or not entry.name.startswith(PI_EXTENSION_PREFIX):
                continue
            state.append(f"{entry.name}（{'已啟用' if (entry / 'index.ts').is_file() else '已停用'}）")
    except OSError as error:
        state.append(f"無法讀取 extensions 目錄：{type(error).__name__}")
    return state


def check_pi_catalog(
    agent_dir: Path,
    pi_command: str,
    timeout: float,
    provider: str = PI_CATALOG_PROVIDER,
) -> dict[str, Any]:
    """實際執行 `pi --list-models <provider>` 並回傳清單（唯讀，失敗不抛錯）。"""
    command = f"{pi_command} --list-models {provider}"
    result: dict[str, Any] = {
        "command": command,
        "provider": provider,
        "ok": False,
        "models": [],
        "count": 0,
        "extensions": pi_catalog_extension_state(agent_dir),
        "error": "",
    }
    try:
        completed = subprocess.run(  # noqa: S602 - 指令由使用者控制，且為唯讀查詢
            command,
            shell=True,
            capture_output=True,
            timeout=timeout,
        )
    except Exception as error:  # noqa: BLE001 - 驗證失敗不應中断同步流程
        result["error"] = f"{type(error).__name__}: {error}"
        return result
    stdout = decode_process_output(completed.stdout)
    stderr = decode_process_output(completed.stderr)
    models = parse_pi_list_output(stdout, provider)
    result["models"] = models
    result["count"] = len(models)
    result["ok"] = bool(models)
    if not models:
        detail = (stderr or stdout).strip().splitlines()
        result["error"] = detail[-1][:200] if detail else f"exit code {completed.returncode}"
    return result


def evaluate_pi_catalog(
    check: dict[str, Any],
    expected_ids: Sequence[str],
    required_present: Sequence[str] = PI_CATALOG_EXPECT_PRESENT,
    required_absent: Sequence[str] = PI_CATALOG_EXPECT_ABSENT,
) -> dict[str, Any]:
    """比對 Pi 顯示清單與預期結果。

    - required_present：必須出現（例如上游改名後的新 ID space-bunny）
    - required_absent：必須被 extension 裁切隱藏（例如已下架的 space-bunny-free）
    - expected_ids：本工具管理、應出現在清單中的自訂模型
    """
    models = list(check.get("models") or [])
    visible = set(models)
    missing = [model_id for model_id in required_present if model_id not in visible]
    still_visible = [model_id for model_id in required_absent if model_id in visible]
    custom_missing = [model_id for model_id in expected_ids if model_id not in visible]
    return {
        "ok": bool(check.get("ok")) and not missing and not still_visible and not custom_missing,
        "present": [model_id for model_id in required_present if model_id in visible],
        "missing": missing,
        "hidden": [model_id for model_id in required_absent if model_id not in visible],
        "still_visible": still_visible,
        "custom_missing": custom_missing,
        "custom_expected": list(expected_ids),
    }


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
        f"- LiteLLM routes：**{report['litellm_route_count']}**（新增 {len(report['litellm_added'])}、移除 {len(report['litellm_removed'])}、能力更新 {len(report.get('litellm_refreshed', []))}）",
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
        [
            f"`{item['route']}`（protocol={item['protocol']}，已實測可呼叫"
            + (f"，能力來源={item['capability_source']}" if item.get("capability_source") else "")
            + "）"
            for item in report["litellm_added"]
        ],
    )
    section("LiteLLM 移除 route", [f"`{route}`" for route in report["litellm_removed"]])
    section(
        "LiteLLM 既有 route 能力／成本更新（models.dev＋能力實測）",
        [f"`{route}`" for route in report.get("litellm_refreshed", [])],
    )
    section(
        "Pi models.json 新增模型",
        [
            f"`{item['id']}`（api={item['entry']['api']}，contextWindow={item['entry']['contextWindow']}，maxTokens={item['entry']['maxTokens']}）"
            for item in report["pi_added"]
        ],
    )
    # 重建（先移除後重寫的同一個 ID）會同時出現在新增清單，這裡只列真正不再管理的項目。
    added_ids = {item["id"] for item in report["pi_added"]}
    section(
        "Pi models.json 移除模型",
        [f"`{model_id}`" for model_id in report["pi_removed"] if model_id not in added_ids],
    )
    section(
        "已由 pi.dev 官方目錄接管（移除本工具重複定義）",
        [f"`{model_id}`" for model_id in report.get("pi_superseded", [])],
    )
    section("上游已消失但 LiteLLM 仍設定（未移除）", [f"`{route}`" for route in report["litellm_orphan_routes"]])
    section(
        "Pi 遠端目錄尚未收錄（等待 Pi 自行刷新）",
        [f"`{model_id}`" for model_id in report["pi_waiting_remote"]],
    )
    section("catalog 未收錄但 Pi 仍有定義", [f"`{model_id}`" for model_id in report["pi_stale_models"]])
    # v1.2.0：上游已消失項目的實測結果與是否已自動移除。
    section(
        "上游已消失項目（實測判定）",
        [
            "`{model}`：{state}{pi}{litellm}".format(
                model=row["model"],
                state=(
                    "實測仍可呼叫"
                    if row["available"]
                    else ("實測無法呼叫" if row["probed"] else "本次未實測")
                ),
                pi="，已從 Pi models.json 移除" if row["removed_from_pi"] else "",
                litellm="，已從 LiteLLM 移除" if row["removed_from_litellm"] else "",
            )
            for row in report.get("stale_items", [])
        ],
    )

    # v1.2.0：能力實測矩陣。
    capabilities = report.get("capabilities") or {}
    lines.extend(["## 能力實測結果", ""])
    if capabilities:
        header = ["Model", "協定", "來源", *[f"`{name}`" for name in CAPABILITY_NAMES]]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "---|" * len(header))
        for model_id, item in capabilities.items():
            results = item.get("results") or {}
            cells = []
            for name in CAPABILITY_NAMES:
                value = (results.get(name) or {}).get("supported")
                cells.append("yes" if value is True else ("no" if value is False else "-"))
            lines.append(
                f"| `{model_id}` | {item.get('protocol') or '-'} | {item.get('source') or '-'} | "
                + " | ".join(cells)
                + " |"
            )
        lines.extend(
            [
                "",
                "> 能力實測程式合併自 opencode-go-monitor v1.0.0（validator），改寫為支援三種協定。",
                "> strong 類能力（system_messages、function_calling、tool_choice、native_structured_output、",
                "> native_streaming）的實測結果直接採用；confirm 類能力（reasoning、vision、prompt_caching）",
                "> 只採用陽性結果，陰性時保留 models.dev 值。",
                "",
            ]
        )
    else:
        note = report.get("capability_note") or "--capabilities auto（本次沒有需要實測的模型）"
        lines.extend([f"- 本次未執行或無結果（{note}）。", ""])

    # v1.2.0：Pi 顯示清單驗證（技能＋extension 的分工結果一併檢查）。
    lines.extend(["## Pi 顯示清單驗證", ""])
    catalog_check = report.get("pi_catalog") or {}
    if catalog_check:
        verdict = catalog_check.get("verdict") or {}
        lines.append(f"- 指令：`{catalog_check.get('command', '-')}`")
        if catalog_check["extensions"]:
            lines.extend(f"- Pi extension：{item}" for item in catalog_check["extensions"])
        else:
            lines.append(
                f"- Pi extension：找不到 `{PI_EXTENSION_PREFIX}-*`"
                "（Pi 會用核心 overlay：`space-bunny` 會出現，但已下架 ID 不會被隱藏）"
            )
        if not catalog_check["ok"]:
            lines.append(f"- 結果：**無法驗證**（{catalog_check.get('error') or '沒有回應'}）")
        else:
            lines.append(f"- Pi 清單模型數：**{catalog_check['count']}**")
            lines.append(
                "- 上游正確 ID："
                + (", ".join(f"`{item}` ✅" for item in verdict.get("present", [])) or "-")
            )
            for model_id in verdict.get("missing", []):
                lines.append(f"- ⚠️ `{model_id}` 未出現在 Pi 清單（需重啟 Pi，或 extension 未載入）")
            for model_id in verdict.get("hidden", []):
                lines.append(f"- 已下架 ID `{model_id}` 已正確隱藏 ✅")
            for model_id in verdict.get("still_visible", []):
                lines.append(
                    f"- ⚠️ 已下架 ID `{model_id}` 仍顯示（extension 未啟用，"
                    "或與其他 extension 同時註冊 `opencode-go` 而衝突）"
                )
            for model_id in verdict.get("custom_missing", []):
                lines.append(f"- ⚠️ 本工具管理的自訂模型 `{model_id}` 未出現在 Pi 清單")
            if verdict.get("ok"):
                lines.append("- 判定：**一致**（技能寫入的模型都在，已下架項目已隱藏）")
        lines.append("")
    else:
        lines.extend(["- 未執行（已使用 `--no-check-pi-catalog`）", ""])

    lines.extend(
        [
            "## 完整模型清單",
            "",
            "| Model | 協定 | 可用 | Pi | LiteLLM | 官方文件 | 能力來源 | context | max out | 失敗次數 | 錯誤 |",
            "|---|---|---:|---:|---:|---:|---|---:|---:|---:|---|",
        ]
    )
    for item in report["models"]:
        error = str(item.get("error", "")).replace("|", "\\|").replace("\n", " ")[:80]
        available = "-" if not report.get("probe_enabled", True) else (
            "yes" if item.get("available") else "no"
        )
        context = item.get("context_window")
        output = item.get("max_output_tokens")
        lines.append(
            "| `{model}` | {protocol} | {available} | {pi} | {litellm} | {docs} | {source} | {context} | {output} | {failures} | {error} |".format(
                model=item["model"],
                protocol=item.get("protocol") or "-",
                available=available,
                pi="yes" if item.get("pi_configured") else "no",
                litellm="yes" if item.get("litellm_configured") else "no",
                docs="yes" if item.get("documented") else "no",
                source=item.get("metadata_source") or "-",
                context=context if context else "-",
                output=output if output else "-",
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
    parser.add_argument(
        "--no-refresh-litellm",
        dest="no_refresh_litellm",
        action="store_true",
        help="不依 models.dev 更新既有 LiteLLM route 的能力與成本",
    )
    parser.add_argument("--prune-pi", action="store_true", help="移除 models.json 內上游已消失的自訂模型")
    parser.add_argument(
        "--keep-stale",
        action="store_true",
        help="不要自動移除「上游已消失且實測確認無法呼叫」的殘留項目（預設會自動移除）",
    )
    parser.add_argument(
        "--capabilities",
        default=os.environ.get("OCG_CAPABILITIES", "auto"),
        help=("能力實測範圍：off、auto（預設，只測新增與無 models.dev 資料者）、"
              "all（全部可呼叫模型）、或逗號分隔的模型 ID 清單"),
    )
    parser.add_argument(
        "--capability-ttl-days",
        type=float,
        default=float(os.environ.get("OCG_CAPABILITY_TTL_DAYS", DEFAULT_CAPABILITY_TTL_DAYS)),
        help="能力實測快取的有效天數；預設 30",
    )
    parser.add_argument(
        "--refresh-capabilities",
        action="store_true",
        help="忽略能力實測快取，強制重新測試",
    )
    parser.add_argument(
        "--include-remote",
        action="store_true",
        help=("連 pi.dev 遠端目錄已收錄的模型也寫入 models.json"
              "（預設交給 Pi 自己的 overlay，避免重複定義）"),
    )
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
        help="不抓 models.dev、官方文件與 pi.dev 目錄，只用探測結果",
    )
    parser.add_argument(
        "--models-dev-provider",
        default=os.environ.get("MODELS_DEV_PROVIDER", "opencode-go"),
        help="models.dev 的 provider id；預設 opencode-go",
    )
    parser.add_argument(
        "--models-dev-ttl",
        type=float,
        default=float(os.environ.get("MODELS_DEV_TTL_HOURS", MODELS_DEV_CACHE_TTL_HOURS)),
        help="models.dev 本機快取的有效時數；預設 6",
    )
    parser.add_argument(
        "--refresh-models-dev",
        action="store_true",
        help="忽略 models.dev 本機快取，重新下載能力資料",
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
    parser.add_argument(
        "--check-pi-catalog",
        dest="check_pi_catalog",
        action="store_true",
        default=True,
        help="同步後執行 `pi --list-models` 驗證 Pi 顯示清單（唯讀，預設開啟）",
    )
    parser.add_argument(
        "--no-check-pi-catalog",
        dest="check_pi_catalog",
        action="store_false",
        help="不做 Pi 顯示清單驗證",
    )
    parser.add_argument(
        "--pi-command",
        default=os.environ.get("OCG_PI_COMMAND", "pi"),
        help="Pi 顯示清單驗證使用的指令（預設 pi，可用環境變數 OCG_PI_COMMAND）",
    )
    parser.add_argument(
        "--check-pi-timeout",
        type=float,
        default=float(os.environ.get("OCG_CHECK_PI_TIMEOUT", "120")),
        help="Pi 顯示清單驗證的逾時秒數（預設 120）",
    )
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

    # 2) 能力資料來源：models.dev（主要）、官方文件、pi.dev 遠端目錄。
    #    任一來源失敗時降級但仍可運作，錯誤會列在報告的「需人工確認」。
    docs_entries: dict[str, dict[str, Any]] = {}
    remote_entries: dict[str, dict[str, Any]] = {}
    models_dev_models: dict[str, dict[str, Any]] = {}
    models_dev_fallback: dict[str, dict[str, dict[str, Any]]] = {}
    if not args.offline_metadata:
        try:
            models_dev_models, models_dev_fallback, models_dev_error = load_models_dev(
                catalog_ids,
                args.models_dev_provider,
                report_dir / "models-dev-cache.json",
                args.models_dev_ttl,
                args.refresh_models_dev,
                args.timeout,
            )
            if models_dev_error:
                source_errors["models.dev"] = models_dev_error
        except Exception as error:  # noqa: BLE001 - 能力來源失敗不應中斷同步
            source_errors["models.dev"] = f"{type(error).__name__}: {error}"
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

    # 每個 catalog 模型的能力資料（models.dev 為主，不足時由其他來源補）。
    model_meta: dict[str, dict[str, Any] | None] = {
        model_id: models_dev_meta(
            model_id,
            models_dev_models,
            models_dev_fallback,
            args.models_dev_provider,
        )[0]
        for model_id in catalog_ids
    }

    log(
        f"catalog={len(catalog_ids)} models.dev={sum(1 for v in model_meta.values() if v)} "
        f"官方文件={len(docs_entries)} pi.dev={len(remote_entries)}"
    )

    # 3) Pi 目前狀態。
    pi = pi_state(args.pi_agent_dir)
    for message in pi["errors"]:
        source_errors.setdefault("Pi models.json", message)
    pi_effective = set(pi["store_ids"]) | set(pi["models_ids"])
    # 本工具先前寫入的項目（models.json 內有 [自動同步 v…] 註解）。
    managed_ids = find_managed_model_ids(pi["models_text"])

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
    #    另実測「上游已消失但 Pi / LiteLLM 仍保留」的項目，
    #    因為只有實測確認無法呼叫才能安全地自動移除（--keep-stale 可關閉）。
    stale_candidate_ids = sorted((set(pi["models_ids"]) | configured_ids) - set(catalog_ids))
    probe_targets = [*catalog_ids, *stale_candidate_ids]
    probe_results: dict[str, dict[str, Any]] = {}
    if args.no_probe:
        log("略過模型實測（--no-probe），可用性以既有狀態為準")
    else:
        if stale_candidate_ids:
            log(f"額外實測上游已消失的殘留項目：{', '.join(stale_candidate_ids)}")
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
                    # 只在「Pi 尚未收錄」與「本工具管理但沒有 models.dev 能力資料」時
                    # 額外驗證圖片輸入，避免不必要的請求，也避免重建時遺失既有能力。
                    args.probe_vision
                    and (
                        model_id not in pi_effective
                        or (model_id in managed_ids and not model_meta.get(model_id))
                    ),
                ): model_id
                for model_id in probe_targets
            }
            for future in as_completed(futures):
                result = future.result()
                probe_results[result["model"]] = result
                log(
                    f"  [{result['model']}] protocol={result['protocol']} "
                    f"http={result['http_status']} available={result['available']}"
                )

    # 合併本次探測與快取中的上一次成功結果（例如圖片能力只在需要時才重新探測）。
    effective_probe: dict[str, dict[str, Any]] = {}
    for model_id in probe_targets:
        merged = dict(probe_results.get(model_id) or {})
        cached = protocol_cache.get(model_id) or {}
        for key in ("reasoning", "vision"):
            if merged.get(key) is None and cached.get(key) is not None:
                merged[key] = cached[key]
        effective_probe[model_id] = merged

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
    # 上游已消失的殘留項目：只有「實測確認無法呼叫」才自動移除，
    # 避免上游短暫故障或被重新上架時誤刪仍可用的設定。
    stale_unreachable = {
        model_id
        for model_id in stale_candidate_ids
        if probe_results.get(model_id) and not probe_results[model_id].get("available")
    }
    if args.prune:
        litellm_orphan_prune_routes = list(litellm_orphan_routes)
    elif args.keep_stale:
        litellm_orphan_prune_routes = []
    else:
        litellm_orphan_prune_routes = [
            route
            for route in litellm_orphan_routes
            if route[len(ROUTE_PREFIX) :] in stale_unreachable
        ]
    pi_missing_ids = [model_id for model_id in catalog_ids if model_id not in pi_effective]
    pi_new_ids = [model_id for model_id in pi_missing_ids if model_id in callable_ids]
    pi_waiting_remote = [model_id for model_id in pi_new_ids if model_id in remote_entries]
    # pi.dev 遠端目錄已收錄者，預設交給 Pi 自己的 overlay（不重複寫入 models.json）；
    # --include-remote 時仍寫入，用於 overlay 被其他 extension 取代或 Pi 顯示不正常的情況。
    pi_add_ids = [
        model_id
        for model_id in pi_new_ids
        if args.include_remote or model_id not in remote_entries
    ]
    pi_stale_models = sorted(model_id for model_id in pi["models_ids"] if model_id not in catalog_ids)
    if args.prune_pi:
        pi_removed_ids = pi_stale_models
    elif args.keep_stale:
        pi_removed_ids = []
    else:
        pi_removed_ids = [model_id for model_id in pi_stale_models if model_id in stale_unreachable]

    # 本工具先前寫入的項目（models.json 內有 [自動同步 v…] 註解）會依最新 models.dev
    # 資料重建；人工手寫的模型定義不會被更動。
    pi_refresh_ids = [
        model_id for model_id in catalog_ids if model_id in managed_ids and model_id in callable_ids
    ]
    # 上游已在 pi.dev 遠端目錄收錄時，Pi 官方定義較新，故移除本工具的重複項目。
    pi_superseded_ids = [
        model_id
        for model_id in catalog_ids
        if model_id in managed_ids and model_id in remote_entries and model_id not in pi_add_ids
    ]
    pi_rebuild_ids = sorted(set(pi_refresh_ids) | set(pi_add_ids))
    pi_remove_ids = sorted(set(pi_removed_ids) | set(pi_superseded_ids))
    # --no-probe 且 models.dev 也查無資料時，不重建既有管理項目，
    # 避免把先前實測得來的 reasoning / 圖片能力降級成保守預設值。
    pi_kept_ids: list[str] = []
    if args.no_probe:
        pi_kept_ids = [
            model_id
            for model_id in pi_rebuild_ids
            if model_id in managed_ids
            and not model_meta.get(model_id)
            and not overrides.get(model_id)
        ]
        pi_rebuild_ids = [model_id for model_id in pi_rebuild_ids if model_id not in pi_kept_ids]
    # 重建之前必須先移除舊的管理項目（可能還有多份舊版殘留），否則會產生重複 ID。
    pi_rebuild_remove_ids = sorted(set(pi_remove_ids) | (set(pi_rebuild_ids) & managed_ids))

    # 既有 LiteLLM route 的能力與成本也依 models.dev 更新（可用 --no-refresh-litellm 關閉）。
    litellm_refresh_ids = (
        []
        if args.no_refresh_litellm or args.skip_litellm
        else sorted(configured_ids & set(catalog_ids))
    )

    # 6.5) 能力實測（v1.2.0）：對「需要能力證據」的模型實際測試，結果優先寫入兩份設定。
    #      範圍由 --capabilities 決定，預設 auto = 新增模型 + 本工具管理但 models.dev 無資料者。
    capability_results: dict[str, dict[str, Any]] = {}
    capability_sources: dict[str, str] = {}
    capability_cache_path = report_dir / "capabilities-cache.json"
    capability_cache = read_state_file(capability_cache_path)
    capability_needed = set(litellm_add_ids) | set(pi_add_ids) | {
        model_id for model_id in managed_ids if not model_meta.get(model_id)
    }
    capability_mode = str(args.capabilities).strip().lower()
    capability_planned = (
        []
        if args.no_probe or capability_mode == "off"
        else capability_targets(args.capabilities, catalog_ids, callable_ids, capability_needed)
    )
    capability_note = "--no-probe" if args.no_probe else ("--capabilities off" if capability_mode == "off" else "")
    if capability_planned:
        pending: list[str] = []
        for model_id in capability_planned:
            entry = capability_cache.get(model_id) or {}
            if not args.refresh_capabilities and capability_cache_fresh(entry, args.capability_ttl_days):
                capability_results[model_id] = entry.get("results") or {}
                capability_sources[model_id] = f"快取 {str(entry.get('checked_at') or '')[:10]}"
            else:
                pending.append(model_id)
        log(
            f"能力實測：{len(capability_planned)} 個模型"
            f"（快取命中 {len(capability_planned) - len(pending)}、待測 {len(pending)}）"
        )
        if pending:
            planned_protocols = {
                model_id: model_protocol(
                    model_id, overrides, docs_entries, model_meta, effective_probe, protocol_cache
                )
                for model_id in pending
            }
            workers = max(1, min(args.workers, 8))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(
                        run_capability_tests,
                        planned_protocols[model_id],
                        model_id,
                        api_key,
                        args.timeout,
                    ): model_id
                    for model_id in pending
                }
                for future in as_completed(futures):
                    model_id = futures[future]
                    try:
                        capability_results[model_id] = future.result()
                        capability_sources[model_id] = "實測"
                    except Exception as error:  # noqa: BLE001 - 單一模型能力測試失敗不應中斷同步
                        capability_sources[model_id] = f"實測失敗：{type(error).__name__}: {error}"
                        log(f"[警告] {model_id} 能力實測失敗：{error}")
            checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
            for model_id in pending:
                results = capability_results.get(model_id)
                if not results:
                    continue
                capability_cache[model_id] = {
                    "protocol": planned_protocols[model_id],
                    "checked_at": checked_at,
                    "results": results,
                }
                write_capability_reports(
                    report_dir / "capabilities",
                    model_id,
                    planned_protocols[model_id],
                    results,
                    checked_at,
                )
            atomic_write(
                capability_cache_path,
                json.dumps(
                    {"version": VERSION, "models": capability_cache},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
            )

    # 人工覆寫的能力值（metadata-overrides.json 的 capabilities）優先於實測結果。
    for model_id in apply_capability_overrides(capability_results, overrides):
        capability_sources[model_id] = "人工覆寫"

    # 7) LiteLLM：以既有管理工具產生候選設定（單一交易、單次重啟）。
    litellm_added_report: list[dict[str, Any]] = []
    litellm_removed_report: list[str] = []
    litellm_refreshed_report: list[str] = []
    litellm_skipped: list[str] = []
    if litellm_add_ids or litellm_refresh_ids or litellm_orphan_prune_routes:
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
                    protocol = model_protocol(
                        model_id,
                        overrides,
                        docs_entries,
                        model_meta,
                        effective_probe,
                        protocol_cache,
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
                    # 以 models.dev 能力與能力實測改寫剛產生的 route block（token 上限、能力、成本）。
                    candidate = patch_litellm_block(
                        candidate,
                        route,
                        manage,
                        model_meta.get(model_id),
                        capability_results.get(model_id),
                    )
                    added_routes.append(route)
                    litellm_added_report.append(
                        {
                            "route": route,
                            "protocol": protocol,
                            "metadata_source": (model_meta.get(model_id) or {}).get("source", "保守預設值"),
                            "capability_source": capability_sources.get(model_id, ""),
                        }
                    )

                for route in litellm_orphan_prune_routes:
                    candidate = manage.remove_route(candidate, route)
                    removed_routes.append(route)
                    litellm_removed_report.append(route)

                # 既有 route：把 token 上限、能力旗標與成本寫回去（能力實測優先於 models.dev）。
                for model_id in litellm_refresh_ids:
                    meta = model_meta.get(model_id)
                    capabilities = capability_results.get(model_id)
                    if not meta and not capabilities:
                        continue
                    route = f"{ROUTE_PREFIX}{model_id}"
                    patched = patch_litellm_block(candidate, route, manage, meta, capabilities)
                    if patched != candidate:
                        candidate = patched
                        litellm_refreshed_report.append(route)

                if candidate == original:
                    log("LiteLLM 沒有需要套用的變更")
                elif args.dry_run:
                    # dry-run：印出 unified diff，並在容器內用 PyYAML 驗證候選設定（不寫入、不重啟）。
                    try:
                        manage.show_diff(original, candidate, args.config)
                    except AttributeError:  # 舊版管理工具沒有 show_diff 時只輸出摘要
                        log("（無法輸出 diff：管理工具版本不支援 show_diff）")
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
                        added_routes + litellm_refreshed_report,
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
                litellm_refreshed_report = []

    # 8) Pi models.json：重建本工具管理的項目，並補上 models.dev 尚未收錄於遠端目錄的模型。
    pi_added_report: list[dict[str, Any]] = []
    pi_removed_report: list[str] = []
    if (pi_rebuild_ids or pi_remove_ids) and not args.skip_pi and args.pi_mode == "auto":
        entries: list[tuple[str, dict[str, Any]]] = []
        for model_id in pi_rebuild_ids:
            entry = build_pi_entry(
                model_id,
                effective_probe.get(model_id, {}),
                docs_entries.get(model_id, {}),
                remote_entries.get(model_id, {}),
                overrides.get(model_id, {}),
                model_meta.get(model_id),
                capability_results.get(model_id),
            )
            entries.append(
                (
                    model_id,
                    {
                        "entry": entry,
                        "probe": effective_probe.get(model_id, {}),
                        "docs": docs_entries.get(model_id, {}),
                        "override": overrides.get(model_id, {}),
                        "meta": model_meta.get(model_id) or {},
                        "capabilities": capability_results.get(model_id) or {},
                    },
                )
            )
        try:
            new_text = insert_pi_entries(pi["models_text"], entries, pi_rebuild_remove_ids)
            # 語法與重複 ID 自我檢查：出現重複就中止，不寫入設定檔。
            parsed = json.loads(strip_json_comments(new_text))
            result_ids = [
                str(item.get("id"))
                for item in parsed["providers"]["opencode-go"]["models"]
            ]
            duplicates = sorted({value for value in result_ids if result_ids.count(value) > 1})
            if duplicates:
                raise RuntimeError(f"重建後出現重複模型 ID：{duplicates}")
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
            pi_added_report = [
                {
                    "id": model_id,
                    "entry": payload["entry"],
                    "metadata_source": (payload.get("meta") or {}).get("source", "官方文件／實測／保守預設"),
                    "capability_source": capability_sources.get(model_id, ""),
                }
                for model_id, payload in entries
            ]
            pi_removed_report = list(pi_remove_ids)
        except Exception as error:  # noqa: BLE001
            source_errors["Pi models.json 同步"] = f"{type(error).__name__}: {error}"
            log(f"[錯誤] Pi models.json 同步失敗：{error}")
            pi_added_report = []
            pi_removed_report = []

    # 9) 更新協定快取。
    cache_models: dict[str, Any] = {}
    for model_id in catalog_ids:
        probe = probe_results.get(model_id)
        previous = protocol_cache.get(model_id) or {}
        if probe and probe.get("available"):
            # 只有真的探測到的值才覆寫，避免把先前確認過的 reasoning / 圖片能力洗掉。
            cache_models[model_id] = {
                "protocol": probe["protocol"],
                "reasoning": probe.get("reasoning")
                if probe.get("reasoning") is not None
                else previous.get("reasoning"),
                "vision": probe.get("vision")
                if probe.get("vision") is not None
                else previous.get("vision"),
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
                "protocol": model_protocol(
                    model_id, overrides, docs_entries, model_meta, probe_results, protocol_cache
                ),
                "capabilities": capability_results.get(model_id) or {},
                "capability_source": capability_sources.get(model_id, ""),
                "available": bool((probe or {}).get("available")),
                "error": (probe or {}).get("error", ""),
                "consecutive_failures": failures,
                "pi_configured": model_id in pi_effective,
                "litellm_configured": model_id in configured_ids,
                "documented": model_id in docs_entries,
                "metadata_source": (model_meta.get(model_id) or {}).get("source", ""),
                "context_window": (model_meta.get(model_id) or {}).get("context"),
                "max_output_tokens": (model_meta.get(model_id) or {}).get("output"),
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

    # 上游已消失項目的實測結果（供報告說明自動移除的依據）。
    stale_rows = [
        {
            "model": model_id,
            "probed": probe_results.get(model_id) is not None,
            "available": bool((probe_results.get(model_id) or {}).get("available")),
            "error": (probe_results.get(model_id) or {}).get("error", ""),
            "removed_from_pi": model_id in pi_removed_ids,
            "removed_from_litellm": f"{ROUTE_PREFIX}{model_id}" in litellm_removed_report,
        }
        for model_id in stale_candidate_ids
    ]

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
        elif str(item.get("metadata_source", "")).startswith("models.dev(跨 provider"):
            manual_attention.append(
                f"`{item['id']}` 的能力取自 {item['metadata_source']}，建議人工複核後填入 metadata-overrides.json。"
            )
    for model_id in pi_superseded_ids:
        manual_attention.append(
            f"`{model_id}` 已由 pi.dev 官方目錄收錄，已移除本工具的重複定義（Pi 會使用官方定義）。"
        )
    for model_id in pi_kept_ids:
        manual_attention.append(
            f"`{model_id}` 在 --no-probe 下沒有能力資料來源，保留既有 models.json 定義（未降級）。"
        )
    for item in pi_waiting_remote:
        manual_attention.append(f"`{item}` 已由 pi.dev 遠端目錄收錄，等待 Pi 重新整理模型清單。")
    for route in litellm_orphan_routes:
        if not args.prune:
            manual_attention.append(f"`{route}` 上游 catalog 已消失，尚未移除（可用 --prune）。")
    for model_id in sorted(configured_ids & set(catalog_ids)):
        if not model_meta.get(model_id):
            if capability_results.get(model_id):
                manual_attention.append(
                    f"`{ROUTE_PREFIX}{model_id}` 在 models.dev 查無能力資料，已改用能力實測結果寫入設定。"
                )
            else:
                manual_attention.append(
                    f"`{ROUTE_PREFIX}{model_id}` 在 models.dev 查無能力資料，保留原設定"
                    "（可寫入 metadata-overrides.json，或用 --capabilities <id> 實測）。"
                )
    for model_id in pi_stale_models:
        if not args.prune_pi and model_id not in pi_removed_ids:
            manual_attention.append(
                f"`{model_id}` 已不在上游 catalog（可能已改名），Pi models.json 仍保留（可用 --prune-pi 移除）。"
            )
    # v1.2.0：能力實測與殘留項目的說明。
    if capability_note and catalog_ids:
        manual_attention.append(
            f"本次未執行能力實測（{capability_note}），能力值仍來自 models.dev 或既有設定。"
        )
    for model_id in sorted(capability_sources):
        source = capability_sources[model_id]
        if source.startswith("實測失敗"):
            manual_attention.append(f"`{model_id}` 能力實測失敗（{source}）。")
    for model_id in sorted(capability_results):
        conflicts = capability_conflicts(model_meta.get(model_id), capability_results.get(model_id))
        if conflicts:
            manual_attention.append(
                f"`{model_id}` 能力實測與 models.dev 不一致：{'、'.join(conflicts)}（已採用實測值，"
                "如不認同可寫入 metadata-overrides.json）。"
            )
    for row in stale_rows:
        if row["removed_from_pi"] or row["removed_from_litellm"]:
            where = "、".join(
                name
                for name, flag in (
                    ("Pi models.json", row["removed_from_pi"]),
                    ("LiteLLM", row["removed_from_litellm"]),
                )
                if flag
            )
            manual_attention.append(
                f"`{row['model']}` 已不在上游 catalog 且實測無法呼叫，已從 {where} 移除。"
            )
        elif row["probed"] and row["available"]:
            manual_attention.append(
                f"`{row['model']}` 已不在上游 catalog 但實測仍可呼叫，保留設定不予移除。"
            )
        elif not row["probed"] and not args.keep_stale:
            manual_attention.append(
                f"`{row['model']}` 已不在上游 catalog 且本次未實測，尚未自動移除（可加 --prune-pi 或 --prune）。"
            )

    # v1.2.0：驗證 Pi 顯示清單（技能寫入的模型是否可見、已下架項目是否被 extension 隱藏）。
    pi_catalog: dict[str, Any] = {}
    if args.check_pi_catalog:
        expected_custom_ids = sorted(
            (set(managed_ids) | {item["id"] for item in pi_added_report}) - set(pi_removed_report)
        )
        check = check_pi_catalog(args.pi_agent_dir, args.pi_command, args.check_pi_timeout)
        verdict = evaluate_pi_catalog(check, expected_custom_ids)
        pi_catalog = {**check, "verdict": verdict}
        log(
            f"Pi 顯示清單驗証：{'通過' if verdict['ok'] else '需確認'}"
            f"（模型數={check['count']} 缺少={verdict['missing'] + verdict['custom_missing']}"
            f" 已下架仍顯示={verdict['still_visible']}）"
        )
        if not check["ok"]:
            manual_attention.append(
                f"Pi 顯示清單驗証無法完成（{check['error'] or 'pi 指令沒有回應'}），"
                "請先確認 `pi --list-models opencode-go` 可正常執行。"
            )
        else:
            if verdict["missing"]:
                manual_attention.append(
                    "下列模型未出現在 Pi 清單："
                    + "、".join(f"`{model_id}`" for model_id in verdict["missing"])
                    + "（重新啟動 Pi 讓 extension 載入，或確認沒有其他 extension 取代同一個 provider）。"
                )
            if verdict["still_visible"]:
                manual_attention.append(
                    "下列上游已下架模型仍出現在 Pi 清單："
                    + "、".join(f"`{model_id}`" for model_id in verdict["still_visible"])
                    + "（請確認 `pi-ext-opencode-go-catalog` 已啟用，且沒有第二個 extension 註冊 `opencode-go`）。"
                )
            if verdict["custom_missing"]:
                manual_attention.append(
                    "下列本工具管理的自訂模型未出現在 Pi 清單："
                    + "、".join(f"`{model_id}`" for model_id in verdict["custom_missing"])
                    + "（重新啟動 Pi 後 models.json 才會生效）。"
                )
    else:
        log("略過 Pi 顯示清單驗證（--no-check-pi-catalog）")

    report = {
        "version": VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "pi_catalog": pi_catalog,
        "dry_run": args.dry_run,
        "probe_enabled": not args.no_probe,
        "catalog_count": len(catalog_ids),
        "available_count": sum(1 for row in model_rows if row["available"]),
        "failed_count": sum(1 for row in model_rows if not row["available"] and not args.no_probe),
        "litellm_route_count": len(litellm_routes),
        "litellm_added": litellm_added_report,
        "litellm_removed": litellm_removed_report,
        "litellm_refreshed": litellm_refreshed_report,
        "litellm_orphan_routes": litellm_orphan_routes,
        "pi_models_count": len(pi_effective),
        "pi_added": pi_added_report,
        "pi_removed": pi_removed_report,
        "pi_rebuild_ids": pi_rebuild_ids,
        "pi_kept": pi_kept_ids,
        "pi_superseded": pi_superseded_ids,
        "pi_waiting_remote": pi_waiting_remote,
        "pi_stale_models": pi_stale_models,
        "stale_items": stale_rows,
        "stale_unreachable": sorted(stale_unreachable),
        "capabilities": {
            model_id: {
                "source": capability_sources.get(model_id, ""),
                "protocol": model_protocol(
                    model_id, overrides, docs_entries, model_meta, probe_results, protocol_cache
                ),
                "results": capability_results.get(model_id) or {},
            }
            for model_id in sorted(capability_results)
        },
        "capability_targets": capability_planned,
        "capability_note": capability_note,
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
        f"LiteLLM 新增={len(litellm_added_report)} 移除={len(litellm_removed_report)} "
        f"能力更新={len(litellm_refreshed_report)}；Pi 新增={len(pi_added_report)} 移除={len(pi_removed_report)}"
    )
    log(f"報告：{report_dir / 'models-sync-latest.md'}")
    if args.fail_on_unavailable and report["failed_count"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
