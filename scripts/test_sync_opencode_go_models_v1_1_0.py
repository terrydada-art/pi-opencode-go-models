#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sync_opencode_go_models_v1_1_0.py 的單元測試 v1.1.0

只測試純函式（不連網、不改設定檔），確保：
1. JSONC 註解處理與 models.json 文字操作正確（含「本工具管理項目」的識別與重建）。
2. 官方文件表格解析（Endpoints 表 + 價目表）正確。
3. models.dev 能力對應正確（context/output、reasoning_options → thinkingLevelMap、co害、modalities）。
4. LiteLLM route block 的能力欄位改寫正確。

執行：
    py -3.12 -m unittest -v .\\test_sync_opencode_go_models_v1_1_0.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from typing import Any

MODULE_PATH = Path(__file__).resolve().parent / "sync_opencode_go_models_v1_1_0.py"


def load_module() -> Any:
    """以檔案路徑載入待測模組（檔名含版號，無法直接 import）。"""
    spec = importlib.util.spec_from_file_location("ocg_sync", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sync = load_module()

# 測試用 models.json：模擬實際檔案的縮排、註解與兩個既有模型。
SAMPLE_MODELS_JSON = """{
  // 測試用註解
  "providers": {
    "opencode-go": {
      "models": [
        {
          "id": "alpha-free",
          "name": "Alpha Free",
          "api": "openai-completions",
          "baseUrl": "https://opencode.ai/zen/go/v1",
          "input": ["text"],
          "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0 }
        },
        {
          "id": "beta-free",
          "name": "Beta Free",
          "api": "openai-completions",
          "baseUrl": "https://opencode.ai/zen/go/v1",
          "input": ["text"],
          "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0 }
        }
      ]
    }
  }
}
"""

SAMPLE_DOCS_HTML = """
<table><thead><tr><th>Model</th><th>Input</th><th>Output</th><th>Cached Read</th><th>Cached Write</th><th>Monthly limit</th></tr></thead>
<tbody>
<tr><td>Grok 4.7</td><td>$2.00</td><td>$6.00</td><td>$0.50</td><td>-</td><td><strong>$15</strong></td></tr>
<tr><td>Grok 4.7 (&gt; 200K tokens)</td><td>$4.00</td><td>$12.00</td><td>$1.00</td><td>-</td><td><strong>$15</strong></td></tr>
<tr><td>LongCat 2.5 Preview Free</td><td>Free</td><td>Free</td><td>Free</td><td>-</td><td><strong>Unlimited</strong></td></tr>
</tbody></table>
<table><tbody>
<tr><td>Grok 4.7</td><td>169</td><td>423</td><td>845</td><td>-</td><td>x</td></tr>
</tbody></table>
<table><thead><tr><th>Model</th><th>Model ID</th><th>Endpoint</th><th>AI SDK Package</th></tr></thead>
<tbody>
<tr><td>Grok 4.7</td><td>grok-4.7</td><td><code dir="auto">https://opencode.ai/zen/go/v1/responses</code></td><td><code dir="auto">@ai-sdk/openai</code></td></tr>
<tr><td>LongCat 2.5 Preview Free</td><td>longcat-2.5-preview-free</td><td><code dir="auto">https://opencode.ai/zen/go/v1/chat/completions</code></td><td><code>@ai-sdk/openai-compatible</code></td></tr>
<tr><td>Claude Haiku 5.5</td><td>claude-haiku-5-5</td><td><code>https://opencode.ai/zen/go/v1/messages</code></td><td><code>@ai-sdk/anthropic</code></td></tr>
</tbody></table>
"""

# 測試用 models.dev provider 資料（結構與 models.dev api.json 相同）。
SAMPLE_MODELS_DEV = {
    "claude-haiku-5-5": {
        "name": "Claude Haiku 5.5",
        "reasoning": True,
        "reasoning_options": [{"type": "toggle"}, {"type": "effort", "values": ["low", "high"]}],
        "modalities": {"input": ["text", "image", "pdf"], "output": ["text"]},
        "limit": {"context": 1000000, "output": 128000},
        "cost": {"input": 0.1, "output": 0.5, "cache_read": 0.01, "cache_write": 0.125},
        "tool_call": True,
        "structured_output": True,
        "provider": {"npm": "@ai-sdk/anthropic"},
    },
    "gpt-6-luna": {
        "name": "GPT-6 Luna",
        "reasoning": True,
        "reasoning_options": [{"type": "effort", "values": ["none", "low", "high", "max"]}],
        "modalities": {"input": ["text", "image"]},
        "limit": {"context": 1050000, "input": 922000, "output": 128000},
        "cost": {"input": 0.1, "output": 0.5},
        "tool_call": True,
        "structured_output": True,
        "provider": {"npm": "@ai-sdk/openai"},
    },
    "deepseek-v4-flash": {
        "name": "DeepSeek V4 Flash",
        "reasoning": True,
        "reasoning_options": [{"type": "effort", "values": ["low", "high", "max"]}],
        "modalities": {"input": ["text"]},
        "limit": {"context": 1000000, "output": 384000},
        "cost": {"input": 0.15, "output": 0.6, "cache_read": 0.003},
        "interleaved": {"field": "reasoning_content"},
        "tool_call": True,
        "provider": {"npm": "@ai-sdk/openai-compatible"},
    },
    "kimi-k2.6": {
        "name": "Kimi K2.6",
        "reasoning": True,
        "reasoning_options": [],
        "modalities": {"input": ["text", "image", "video"]},
        "limit": {"context": 262144, "output": 65536},
        "cost": {"input": 0.95, "output": 4.0},
        "tool_call": True,
    },
}

EVALUATED_MODELS_DEV = {
    "claude-haiku-5-5",
    "gpt-6-luna",
    "deepseek-v4-flash",
    "kimi-k2.6",
}


class FakeManageTool:
    """模擬既有的 LiteLLM 管理工具，只提供 find_model_blocks。"""

    def __init__(self, blocks: dict[str, tuple[int, int]]) -> None:
        self._blocks = blocks

    def find_model_blocks(self, lines: list[str]) -> dict[str, tuple[int, int]]:
        return self._blocks


class JsonCommentTests(unittest.TestCase):
    def test_strip_json_comments_keeps_strings(self) -> None:
        text = '{"a": "http://x//y", // 註解\n "b": /* 區塊 */ 1}'
        self.assertEqual(json.loads(sync.strip_json_comments(text)), {"a": "http://x//y", "b": 1})

    def test_strip_json_comments_keeps_escaped_quote(self) -> None:
        text = '{"a": "say \\"// not a comment\\"", "b": 2}'
        self.assertEqual(json.loads(sync.strip_json_comments(text))["b"], 2)


class DocsParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.entries = sync.parse_docs(SAMPLE_DOCS_HTML)

    def test_protocol_mapping(self) -> None:
        self.assertEqual(self.entries["grok-4.7"]["protocol"], "responses")
        self.assertEqual(self.entries["longcat-2.5-preview-free"]["protocol"], "chat")
        self.assertEqual(self.entries["claude-haiku-5-5"]["protocol"], "anthropic")

    def test_first_price_tier_wins_and_estimate_table_is_ignored(self) -> None:
        self.assertEqual(self.entries["grok-4.7"]["cost"]["input"], 2.0)
        self.assertEqual(self.entries["grok-4.7"]["cost"]["output"], 6.0)

    def test_free_model_cost_is_zero(self) -> None:
        self.assertEqual(self.entries["longcat-2.5-preview-free"]["cost"]["input"], 0.0)

    def test_parse_money_variants(self) -> None:
        self.assertIsNone(sync.parse_money("-"))
        self.assertEqual(sync.parse_money("Free"), 0.0)
        self.assertEqual(sync.parse_money("$1.40"), 1.40)

    def test_normalize_model_name_removes_notes(self) -> None:
        self.assertEqual(sync.normalize_model_name("Qwen3.7 Plus (\u2264 256K tokens)"), "qwen37plus")
        self.assertEqual(sync.normalize_model_name("DeepSeek V4.1 Flash (Off-Peak)"), "deepseekv41flash")


class ModelsDevTests(unittest.TestCase):
    def meta(self, model_id: str) -> Any:
        meta, source = sync.models_dev_meta(
            model_id,
            {mid: value for mid, value in SAMPLE_MODELS_DEV.items()},
            {},
            "opencode-go",
        )
        self.assertTrue(source.startswith("models.dev"))
        return meta

    def test_same_provider_lookup(self) -> None:
        meta, source = sync.models_dev_meta(
            "gpt-6-luna", SAMPLE_MODELS_DEV, {}, "opencode-go"
        )
        self.assertEqual(source, "models.dev(opencode-go)")
        self.assertEqual(meta["context"], 1050000)
        self.assertEqual(meta["output"], 128000)
        self.assertEqual(meta["protocol"], "responses")
        self.assertTrue(meta["has_cost"])

    def test_missing_model_returns_none(self) -> None:
        meta, source = sync.models_dev_meta("omen-alpha", SAMPLE_MODELS_DEV, {}, "opencode-go")
        self.assertIsNone(meta)
        self.assertEqual(source, "")

    def test_opencode_provider_preferred_for_fallback(self) -> None:
        fallback = {
            "glm-5.1": {
                "zai": {"limit": {"context": 200000, "output": 131072}},
                "opencode": {"limit": {"context": 204800, "output": 131072}},
            }
        }
        meta, source = sync.models_dev_meta("glm-5.1", {}, fallback, "opencode-go")
        self.assertEqual(source, "models.dev(opencode)")
        self.assertEqual(meta["context"], 204800)

    def test_cross_provider_uses_mode_with_conservative_tiebreak(self) -> None:
        fallback = {
            "deepseek-flash": {
                "deepseek": {"limit": {"context": 1000000, "output": 393216}},
                "302ai": {"limit": {"context": 1000000, "output": 384000}},
            }
        }
        meta, source = sync.models_dev_meta("deepseek-flash", {}, fallback, "opencode-go")
        self.assertIn("跨 provider", source)
        self.assertEqual(meta["context"], 1000000)
        self.assertEqual(meta["output"], 384000)  # 平手取較小值

    def test_thinking_level_map_from_effort_values(self) -> None:
        mapping = sync.thinking_level_map(self.meta("deepseek-v4-flash"))
        self.assertEqual(mapping["low"], "low")
        self.assertEqual(mapping["high"], "high")
        self.assertEqual(mapping["max"], "max")
        self.assertIsNone(mapping["medium"])
        self.assertIsNone(mapping["off"])  # 沒有 none 可用

    def test_thinking_level_map_off_maps_to_none(self) -> None:
        mapping = sync.thinking_level_map(self.meta("gpt-6-luna"))
        self.assertEqual(mapping["off"], "none")

    def test_thinking_level_map_absent_without_effort_options(self) -> None:
        self.assertIsNone(sync.thinking_level_map(self.meta("kimi-k2.6")))
        self.assertIsNone(sync.thinking_level_map(None))

    def test_most_common_value_tie_break(self) -> None:
        self.assertEqual(sync.most_common_value([2, 2, 1]), 2)
        self.assertEqual(sync.most_common_value([3, 1]), 1)
        self.assertIsNone(sync.most_common_value([]))

    def test_build_index_collects_fallback(self) -> None:
        raw = {
            "opencode-go": {"models": {"kimi-k2.6": SAMPLE_MODELS_DEV["kimi-k2.6"]}},
            "opencode": {"models": {"glm-5.1": {"name": "GLM-5.1"}}},
        }
        provider_models, fallback = sync.build_models_dev_index(raw, ["kimi-k2.6", "glm-5.1"], "opencode-go")
        self.assertIn("kimi-k2.6", provider_models)
        self.assertEqual(fallback["glm-5.1"]["opencode"]["name"], "GLM-5.1")


class ModelsJsonTests(unittest.TestCase):
    def test_find_array_bounds(self) -> None:
        open_index, close_index = sync.find_opencode_models_array(SAMPLE_MODELS_JSON)
        self.assertEqual(SAMPLE_MODELS_JSON[open_index], "[")
        self.assertEqual(SAMPLE_MODELS_JSON[close_index], "]")

    def test_iter_top_level_objects(self) -> None:
        open_index, close_index = sync.find_opencode_models_array(SAMPLE_MODELS_JSON)
        spans = sync.iter_top_level_objects(SAMPLE_MODELS_JSON, open_index, close_index)
        self.assertEqual(len(spans), 2)
        self.assertIn('"id": "alpha-free"', SAMPLE_MODELS_JSON[spans[0][0] : spans[0][1]])

    def test_insert_keeps_existing_models_and_comments(self) -> None:
        entry = sync.build_pi_entry("gamma", {"protocol": "chat"}, {}, {}, {})
        text = sync.insert_pi_entries(
            SAMPLE_MODELS_JSON,
            [("gamma", {"entry": entry, "probe": {}, "docs": {}, "override": {}})],
            [],
        )
        data = json.loads(sync.strip_json_comments(text))
        ids = [item["id"] for item in data["providers"]["opencode-go"]["models"]]
        self.assertEqual(ids, ["alpha-free", "beta-free", "gamma"])
        self.assertIn("// 測試用註解", text)

    def test_insert_adds_source_comment(self) -> None:
        meta = {"source": "models.dev(opencode-go)", "context": 1000000, "output": 128000}
        entry = sync.build_pi_entry("gamma", {"protocol": "chat"}, {}, {}, {}, meta)
        text = sync.insert_pi_entries(
            SAMPLE_MODELS_JSON,
            [("gamma", {"entry": entry, "probe": {}, "docs": {}, "override": {}, "meta": meta})],
            [],
        )
        self.assertIn("能力來源：models.dev(opencode-go)", text)

    def test_managed_ids_are_detected(self) -> None:
        self.assertEqual(sync.find_managed_model_ids(SAMPLE_MODELS_JSON), set())
        meta = {"source": "models.dev(opencode-go)", "context": 1000000, "output": 128000}
        entry = sync.build_pi_entry("gamma", {"protocol": "chat"}, {}, {}, {}, meta)
        text = sync.insert_pi_entries(
            SAMPLE_MODELS_JSON,
            [("gamma", {"entry": entry, "probe": {}, "docs": {}, "override": {}, "meta": meta})],
            [],
        )
        self.assertEqual(sync.find_managed_model_ids(text), {"gamma"})

    def test_managed_entry_is_rebuilt_without_duplication(self) -> None:
        """重複執行時，本工具管理的項目應被重建而不是重複插入。"""
        meta = {"source": "models.dev(opencode-go)", "context": 1000000, "output": 128000}
        entry = sync.build_pi_entry("gamma", {"protocol": "chat"}, {}, {}, {}, meta)
        first = sync.insert_pi_entries(
            SAMPLE_MODELS_JSON,
            [("gamma", {"entry": entry, "probe": {}, "docs": {}, "override": {}, "meta": meta})],
            [],
        )
        second = sync.insert_pi_entries(
            first,
            [("gamma", {"entry": entry, "probe": {}, "docs": {}, "override": {}, "meta": meta})],
            sorted(sync.find_managed_model_ids(first)),
        )
        ids = [
            item["id"]
            for item in json.loads(sync.strip_json_comments(second))["providers"]["opencode-go"]["models"]
        ]
        self.assertEqual(ids, ["alpha-free", "beta-free", "gamma"])

    def test_duplicate_managed_entries_are_all_removed(self) -> None:
        """舊版殘留的重複管理項目必須一次全部移除（真實回歸：v1.1.0 首次套用曾重複）。"""
        meta = {"source": "models.dev(opencode-go)", "context": 1000, "output": 100}
        entry = sync.build_pi_entry("gamma", {"protocol": "chat"}, {}, {}, {}, meta)
        payload = ("gamma", {"entry": entry, "probe": {}, "docs": {}, "override": {}, "meta": meta})
        once = sync.insert_pi_entries(SAMPLE_MODELS_JSON, [payload], [])
        twice = sync.insert_pi_entries(once, [payload], [])  # 未先移除 → 產生兩份
        ids_before = [
            item["id"]
            for item in json.loads(sync.strip_json_comments(twice))["providers"]["opencode-go"]["models"]
        ]
        self.assertEqual(ids_before.count("gamma"), 2)

        fixed = sync.insert_pi_entries(twice, [payload], ["gamma"])
        ids_after = [
            item["id"]
            for item in json.loads(sync.strip_json_comments(fixed))["providers"]["opencode-go"]["models"]
        ]
        self.assertEqual(ids_after, ["alpha-free", "beta-free", "gamma"])

    def test_remove_middle_model(self) -> None:
        text = sync.insert_pi_entries(SAMPLE_MODELS_JSON, [], ["alpha-free"])
        data = json.loads(sync.strip_json_comments(text))
        ids = [item["id"] for item in data["providers"]["opencode-go"]["models"]]
        self.assertEqual(ids, ["beta-free"])

    def test_remove_last_model_keeps_valid_json(self) -> None:
        text = sync.insert_pi_entries(SAMPLE_MODELS_JSON, [], ["beta-free"])
        data = json.loads(sync.strip_json_comments(text))
        ids = [item["id"] for item in data["providers"]["opencode-go"]["models"]]
        self.assertEqual(ids, ["alpha-free"])

    def test_remove_all_models_then_insert(self) -> None:
        text = sync.insert_pi_entries(SAMPLE_MODELS_JSON, [], ["alpha-free", "beta-free"])
        self.assertEqual(
            json.loads(sync.strip_json_comments(text))["providers"]["opencode-go"]["models"], []
        )
        entry = sync.build_pi_entry("gamma", {"protocol": "chat"}, {}, {}, {})
        text = sync.insert_pi_entries(
            text, [("gamma", {"entry": entry, "probe": {}, "docs": {}, "override": {}})], []
        )
        ids = [
            item["id"]
            for item in json.loads(sync.strip_json_comments(text))["providers"]["opencode-go"]["models"]
        ]
        self.assertEqual(ids, ["gamma"])

    def test_jsonc_entry_text_is_inline_for_scalars(self) -> None:
        entry = sync.build_pi_entry("gamma", {"protocol": "chat"}, {}, {}, {})
        text = sync.jsonc_entry_text(entry)
        self.assertIn('"input": [ "text" ]', text)
        self.assertTrue(text.startswith("        {"))


class EntryBuildingTests(unittest.TestCase):
    def test_protocol_mapping_to_pi_api(self) -> None:
        self.assertEqual(
            sync.build_pi_entry("m", {"protocol": "chat"}, {}, {}, {})["api"], "openai-completions"
        )
        self.assertEqual(
            sync.build_pi_entry("m", {"protocol": "responses"}, {}, {}, {})["api"],
            "openai-responses",
        )
        anthropic = sync.build_pi_entry("m", {"protocol": "anthropic"}, {}, {}, {})
        self.assertEqual(anthropic["api"], "anthropic-messages")
        self.assertEqual(anthropic["baseUrl"], sync.OPENCODE_BASE_ANTHROPIC)

    def test_docs_protocol_wins_over_models_dev_npm(self) -> None:
        meta = {"protocol": "anthropic"}
        entry = sync.build_pi_entry("m", {}, {"protocol": "chat"}, {}, {}, meta)
        self.assertEqual(entry["api"], "openai-completions")

    def test_conservative_defaults_without_metadata(self) -> None:
        entry = sync.build_pi_entry("m", {"protocol": "chat"}, {}, {}, {})
        self.assertEqual(entry["contextWindow"], sync.DEFAULT_PI_CONTEXT_WINDOW)
        self.assertEqual(entry["maxTokens"], sync.DEFAULT_PI_MAX_TOKENS)
        self.assertFalse(entry["reasoning"])

    def test_models_dev_values_are_written(self) -> None:
        meta = {
            "source": "models.dev(opencode-go)",
            "context": 1050000,
            "output": 128000,
            "reasoning": True,
            "effort_values": ["none", "low", "high", "max"],
            "modalities": ["text", "image", "pdf"],
            "has_cost": True,
            "cost": {"input": 0.1, "output": 0.5, "cacheRead": 0.01, "cacheWrite": 0.125},
            "protocol": "responses",
            "name": "GPT-6 Luna",
        }
        entry = sync.build_pi_entry("gpt-6-luna", {"protocol": "responses"}, {}, {}, {}, meta)
        self.assertEqual(entry["contextWindow"], 1050000)
        self.assertEqual(entry["maxTokens"], 128000)
        self.assertEqual(entry["input"], ["text", "image"])
        self.assertEqual(entry["cost"]["cacheWrite"], 0.125)
        self.assertEqual(entry["name"], "GPT-6 Luna")
        self.assertEqual(entry["thinkingLevelMap"]["off"], "none")
        self.assertEqual(entry["thinkingLevelMap"]["max"], "max")
        self.assertIsNone(entry["thinkingLevelMap"]["medium"])
        self.assertIn("sessionAffinityFormat", entry["compat"])

    def test_deepseek_interleaved_compat(self) -> None:
        meta = {
            "reasoning": True,
            "effort_values": ["low", "high"],
            "interleaved_field": "reasoning_content",
            "protocol": "chat",
        }
        entry = sync.build_pi_entry("deepseek-v4-flash", {}, {}, {}, {}, meta)
        self.assertTrue(entry["compat"]["requiresReasoningContentOnAssistantMessages"])
        self.assertEqual(entry["compat"]["thinkingFormat"], "deepseek")
        self.assertTrue(entry["compat"]["supportsStrictMode"])

    def test_other_interleaved_model_has_no_deepseek_compat(self) -> None:
        meta = {
            "reasoning": True,
            "effort_values": ["low", "high"],
            "interleaved_field": "reasoning_content",
            "protocol": "chat",
        }
        entry = sync.build_pi_entry("glm-5.3", {}, {}, {}, {}, meta)
        self.assertNotIn("thinkingFormat", entry["compat"])
        self.assertNotIn("requiresReasoningContentOnAssistantMessages", entry["compat"])

    def test_reasoning_without_effort_options_disables_thinking_params(self) -> None:
        meta = {"reasoning": True, "effort_values": [], "protocol": "chat"}
        entry = sync.build_pi_entry("kimi-k2.6", {}, {}, {}, {}, meta)
        self.assertFalse(entry["compat"]["supportsReasoningEffort"])
        self.assertTrue(all(value is None for value in entry["thinkingLevelMap"].values()))

    def test_override_wins_over_models_dev(self) -> None:
        meta = {"context": 1000000, "output": 128000, "reasoning": True, "protocol": "chat"}
        override = {"contextWindow": 200000, "reasoning": False}
        entry = sync.build_pi_entry("m", {}, {}, {}, override, meta)
        self.assertEqual(entry["contextWindow"], 200000)
        self.assertFalse(entry["reasoning"])

    def test_probe_vision_used_without_models_dev(self) -> None:
        entry = sync.build_pi_entry("m", {"protocol": "chat", "vision": True}, {}, {}, {})
        self.assertEqual(entry["input"], ["text", "image"])


class ProtocolCandidateTests(unittest.TestCase):
    def test_override_then_docs_then_cache_then_all(self) -> None:
        self.assertEqual(
            sync.protocol_candidates("m", {"protocol": "chat"}, {}, {"protocol": "responses"}),
            ["chat"],
        )
        self.assertEqual(
            sync.protocol_candidates("m", {"protocol": "anthropic"}, {"protocol": "chat"}, {}),
            ["anthropic"],
        )
        self.assertEqual(sync.protocol_candidates("m", {}, {"protocol": "responses"}, {}), ["responses"])
        self.assertEqual(
            sync.protocol_candidates("m", {}, {}, {}), ["chat", "responses", "anthropic"]
        )


class ReasoningDetectionTests(unittest.TestCase):
    def test_chat_reasoning_content(self) -> None:
        payload = {"choices": [{"message": {"reasoning_content": "..."}}]}
        self.assertTrue(sync.detect_reasoning("chat", payload))
        self.assertFalse(sync.detect_reasoning("chat", {"choices": [{"message": {"content": "OK"}}]}))

    def test_anthropic_thinking_block(self) -> None:
        self.assertTrue(sync.detect_reasoning("anthropic", {"content": [{"type": "thinking"}]}))

    def test_responses_reasoning_item(self) -> None:
        self.assertTrue(sync.detect_reasoning("responses", {"output": [{"type": "reasoning"}]}))


class LitellmPatchTests(unittest.TestCase):
    BLOCK_TEXT = (
        "  - model_name: opencode-go/demo\r\n"
        "    model_info:\r\n"
        "      mode: chat\r\n"
        "      max_tokens: 65536\r\n"
        "      max_input_tokens: 262144\r\n"
        "      max_output_tokens: 65536\r\n"
        "      supports_vision: false\r\n"
        "      supports_function_calling: true\r\n"
        "      supports_response_schema: false\r\n"
        "      supports_reasoning: true\r\n"
        "      supported_modalities: [\"text\"]\r\n"
        "      input_cost_per_token: 0\r\n"
        "      output_cost_per_token: 0\r\n"
        "    litellm_params:\r\n"
        "      model: openai/demo\r\n"
        "      input_cost_per_token: 0\r\n"
        "      output_cost_per_token: 0\r\n"
        "  - model_name: opencode-go/other\r\n"
        "    model_info:\r\n"
        "      max_tokens: 111\r\n"
    )

    def patch_with(self, meta: dict[str, Any]) -> str:
        lines = self.BLOCK_TEXT.splitlines(keepends=True)
        span = (0, 17)
        manage = FakeManageTool({"opencode-go/demo": span})
        return sync.patch_litellm_block(self.BLOCK_TEXT, "opencode-go/demo", manage, meta)

    def test_metadata_is_written_into_block(self) -> None:
        meta = {
            "context": 1000000,
            "input_window": 922000,
            "output": 128000,
            "reasoning": True,
            "modalities": ["text", "image", "pdf"],
            "tool_call": True,
            "structured_output": True,
            "cost": {"input": 0.1, "output": 0.5},
        }
        patched = self.patch_with(meta)
        demo_block = patched.split("  - model_name: opencode-go/other")[0]
        self.assertIn("max_tokens: 128000", demo_block)
        self.assertIn("max_input_tokens: 922000", demo_block)
        self.assertIn("supports_vision: true", demo_block)
        self.assertIn("supports_pdf_input: true", demo_block)
        self.assertIn("supports_response_schema: true", demo_block)
        self.assertIn('supported_modalities: ["text", "image"]', demo_block)
        self.assertIn("input_cost_per_token: 1e-07", demo_block)
        self.assertIn("output_cost_per_token: 5e-07", demo_block)

    def test_other_blocks_are_untouched(self) -> None:
        meta = {"context": 1000, "output": 500, "modalities": ["text"], "cost": {}}
        patched = self.patch_with(meta)
        self.assertIn("      max_tokens: 111", patched)

    def test_crlf_is_preserved(self) -> None:
        meta = {"context": 1000, "output": 500, "modalities": ["text"], "cost": {}}
        patched = self.patch_with(meta)
        self.assertNotIn("\n", patched.replace("\r\n", ""))

    def test_missing_meta_leaves_text_unchanged(self) -> None:
        self.assertEqual(self.patch_with(None), self.BLOCK_TEXT)

    def test_unknown_route_leaves_text_unchanged(self) -> None:
        manage = FakeManageTool({"opencode-go/other": (18, 20)})
        self.assertEqual(
            sync.patch_litellm_block(self.BLOCK_TEXT, "opencode-go/demo", manage, {"context": 1}),
            self.BLOCK_TEXT,
        )

    def test_yaml_scalar_formatting(self) -> None:
        self.assertEqual(sync.yaml_scalar(True), "true")
        self.assertEqual(sync.yaml_scalar(False), "false")
        self.assertEqual(sync.yaml_scalar(["text", "image"]), '["text", "image"]')
        self.assertEqual(sync.yaml_scalar(128000), "128000")
        self.assertEqual(sync.yaml_scalar(1e-7), "1e-07")


if __name__ == "__main__":
    unittest.main()
