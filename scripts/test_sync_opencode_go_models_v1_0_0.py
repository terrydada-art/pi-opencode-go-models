#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sync_opencode_go_models_v1_0_0.py 的單元測試 v1.0.0

只測試純函式（不連網、不改設定檔），確保：
1. JSONC 註解處理與 models.json 文字操作正確。
2. 官方文件表格解析（Endpoints 表 + 價目表）正確。
3. 模型定義建立的保守預設值與協定對應正確。

執行：
    py -3.12 -m unittest -v .\\test_sync_opencode_go_models_v1_0_0.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from typing import Any

MODULE_PATH = Path(__file__).resolve().parent / "sync_opencode_go_models_v1_0_0.py"


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


class JsonCommentTests(unittest.TestCase):
    def test_strip_json_comments_keeps_strings(self) -> None:
        text = '{"a": "http://x//y", // 註解\n "b": /* 區塊 */ 1}'
        cleaned = sync.strip_json_comments(text)
        self.assertEqual(json.loads(cleaned), {"a": "http://x//y", "b": 1})

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

    def test_names_are_kept(self) -> None:
        self.assertEqual(self.entries["claude-haiku-5-5"]["name"], "Claude Haiku 5.5")

    def test_parse_money_variants(self) -> None:
        self.assertIsNone(sync.parse_money("-"))
        self.assertEqual(sync.parse_money("Free"), 0.0)
        self.assertEqual(sync.parse_money("$1.40"), 1.40)

    def test_normalize_model_name_removes_notes(self) -> None:
        self.assertEqual(sync.normalize_model_name("Qwen3.7 Plus (\u2264 256K tokens)"), "qwen37plus")
        self.assertEqual(sync.normalize_model_name("DeepSeek V4.1 Flash (Off-Peak)"), "deepseekv41flash")


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

    def test_insert_adds_todo_comment_when_tokens_unknown(self) -> None:
        entry = sync.build_pi_entry("gamma", {"protocol": "chat"}, {}, {}, {})
        text = sync.insert_pi_entries(
            SAMPLE_MODELS_JSON,
            [("gamma", {"entry": entry, "probe": {}, "docs": {}, "override": {}})],
            [],
        )
        self.assertIn("TODO", text)
        self.assertIn(f"v{sync.VERSION}", text)

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

    def test_conservative_defaults(self) -> None:
        entry = sync.build_pi_entry("m", {"protocol": "chat"}, {}, {}, {})
        self.assertEqual(entry["contextWindow"], sync.DEFAULT_PI_CONTEXT_WINDOW)
        self.assertEqual(entry["maxTokens"], sync.DEFAULT_PI_MAX_TOKENS)
        self.assertFalse(entry["reasoning"])

    def test_docs_name_and_cost_are_used(self) -> None:
        docs = {"name": "Omen Alpha", "cost": {"input": 1.0, "output": 2.0, "cacheRead": 0, "cacheWrite": 0}}
        entry = sync.build_pi_entry("omen-alpha", {"protocol": "chat"}, docs, {}, {})
        self.assertEqual(entry["name"], "Omen Alpha")
        self.assertEqual(entry["cost"]["output"], 2.0)

    def test_override_wins(self) -> None:
        override = {"protocol": "responses", "contextWindow": 500000, "reasoning": True}
        entry = sync.build_pi_entry("m", {"protocol": "chat"}, {}, {}, override)
        self.assertEqual(entry["api"], "openai-responses")
        self.assertEqual(entry["contextWindow"], 500000)
        self.assertTrue(entry["reasoning"])

    def test_vision_result_adds_image_input(self) -> None:
        entry = sync.build_pi_entry("m", {"protocol": "chat", "vision": True}, {}, {}, {})
        self.assertEqual(entry["input"], ["text", "image"])

    def test_reasoning_chat_entry_disables_thinking_levels(self) -> None:
        entry = sync.build_pi_entry("m", {"protocol": "chat", "reasoning": True}, {}, {}, {})
        self.assertFalse(entry["compat"]["supportsReasoningEffort"])
        self.assertTrue(entry["compat"]["requiresReasoningContentOnAssistantMessages"])
        self.assertEqual(entry["compat"]["thinkingFormat"], "deepseek")
        self.assertTrue(all(value is None for value in entry["thinkingLevelMap"].values()))

    def test_non_reasoning_chat_entry_has_no_thinking_map(self) -> None:
        entry = sync.build_pi_entry("m", {"protocol": "chat", "reasoning": False}, {}, {}, {})
        self.assertNotIn("thinkingLevelMap", entry)
        self.assertNotIn("thinkingFormat", entry["compat"])

    def test_comment_mentions_disabled_thinking_levels(self) -> None:
        comment = sync.pi_entry_comment("m", {"protocol": "chat", "reasoning": True}, {}, {}, "2026-10-08")
        self.assertIn("thinkingLevelMap", comment)


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


if __name__ == "__main__":
    unittest.main()
