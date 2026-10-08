---
name: opencode-go-models
description: 同步 OpenCode Go 訂閱「目前實際可用」的模型清單與模型能力到 Pi models.json 與 LiteLLM config.yaml。模型能力以 models.dev 為主要來源，並可用實際請求實測 system messages、function calling、reasoning、tool_choice、strict JSON、vision、streaming、prompt caching 後直接寫入；上游已消失且實測無法呼叫的殘留模型會自動移除。當使用者要求更新或查詢 OpenCode Go 可用模型、檢查訂閱新增了哪些模型、模型改名或無法呼叫後設定檔沒有更新、OpenCode Go 模型在 Pi / pi-web 看不到、LiteLLM 缺少 opencode-go 路由或能力/成本欄位不正確、或要驗證某個 OpenCode Go 模型是否真的可呼叫時使用。
metadata:
  version: 1.2.0
  maintainer: 99018398
---

# OpenCode Go 模型清單與能力同步

把 OpenCode Go 訂閱「當下真的可呼叫」的模型，以及**模型能力**，同步到兩份清單：

1. Pi：`~/.pi/agent/models.json` 的 `providers.opencode-go.models`
2. LiteLLM：`M:\AI\ai-router-2026\litellm\config.yaml` 的 `opencode-go/*` 路由（新增缺少的路由、更新既有路由的 token 上限／能力旗標／成本）

## 能力資料優先序（v1.2.0 起）

1. `metadata-overrides.json`（人工覆寫，最高）
2. **能力實測**（v1.2.0 新增，合併自 opencode-go-monitor v1.0.0 的 validator）
3. **models.dev**：`https://models.dev/api.json` 的 `opencode-go` provider
   - `limit.context` → `contextWindow` / `max_input_tokens`
   - `limit.output` → `maxTokens` / `max_output_tokens`
   - `reasoning` + `reasoning_options`（effort 值）→ `reasoning` 與 `thinkingLevelMap`
   - `modalities.input` → `input`（text / image）；`pdf`、`audio` 寫入 LiteLLM 能力旗標
   - `cost` → Pi `cost` 與 LiteLLM `input_cost_per_token` / `output_cost_per_token`
   - `tool_call` / `structured_output` → `supports_function_calling`、`supports_response_schema`
   - `provider.npm` → API 協定線索（`@ai-sdk/openai-compatible`=chat、`@ai-sdk/openai`=responses、`@ai-sdk/anthropic`=anthropic）
   - 同 provider 查無時依序用 `opencode`（同廠商）、跨 provider 眾數推定（報告會標示資料筆數）
4. OpenCode Go 官方文件 `https://opencode.ai/docs/go/`：API 協定（Endpoints 表）與每 1M token 價格
5. pi.dev 遠端目錄、實際探測結果、保守預設值（131072 / 32768）

已驗證：models.dev 對 OpenCode Go 29 個模型的 `limit` / `cost` / `modalities` 與 Pi 官方目錄完全一致。

## 能力實測（v1.2.0）

實際送出請求測試 8 項能力，支援 chat / responses / anthropic 三種協定：

| 能力 | LiteLLM 欄位 | 可信度 |
| --- | --- | --- |
| `system_messages` | `supports_system_messages` | strong（成功與失敗都採用） |
| `function_calling` | `supports_function_calling` | strong |
| `tool_choice` | `supports_tool_choice` | strong |
| `native_structured_output` | `supports_native_structured_output` / `supports_response_schema` | strong |
| `native_streaming` | `supports_native_streaming` | strong |
| `vision` | `supports_vision` / `supported_modalities` | confirm（只採用陽性結果） |
| `prompt_caching` | `supports_prompt_caching` | confirm |
| `reasoning` | `supports_reasoning` / Pi `reasoning` | confirm |

- 實測範圍由 `--capabilities` 決定，預設 `auto`：只測**新增模型**與**本工具管理但 models.dev 查無能力資料**的模型，
  避免不必要的額度消耗；`all` 測全部可呼叫模型，也可直接指定 `--capabilities omen-alpha,glm-5.1`。
- 結果有快取（`reports/capabilities-cache.json`，預設 30 天）與逐模型報告（`reports/capabilities/<id>-latest.{md,json}`）。
- 遇到 401/403/429/5xx 會中止該模型其餘能力測試，未測到的能力不會被寫成 false。
- Anthropic 協定沒有標準 json_schema `response_format`，因此不測試 `native_structured_output`。

## 殘留項目處理（v1.2.0）

上游 catalog 已不存在的模型（例如改名後的 `space-bunny-free`）會被**額外實測**：

- 實測確認無法呼叫 → **自動移除**（Pi `models.json` 與 LiteLLM route）
- 實測仍可呼叫 → 保留並在報告提醒
- 想完全不自動移除：`--keep-stale`；想無條件移除：`--prune-pi` / `--prune`

## Pi 顯示清單驗證（v1.2.0）

技能負責「資料正確」，Pi extension `pi-ext-opencode-go-catalog` 負責「Pi 看得見什麼」（接管 provider
註冊以隱藏上游已下架的 ID，例如 `space-bunny-free`）。兩者版本號各自獨立：技能腳本 **v1.2.0**、
extension **v1.1.0**（以 `pi-ext-` 前綴辨識）。

同步完成後（預設）會執行 `pi --list-models opencode-go`（唯讀）確認：

- 本工具管理的自訂模型都出現在清單中
- 上游已下架的 ID 已被 extension 隱藏（若仍顯示，通常是 extension 未啟用或與其他 extension 衝突）

結果寫入報告的「Pi 顯示清單驗證」段落；不需要時用 `--no-check-pi-catalog`。

## 檔案

```text
scripts/sync_opencode_go_models_v1_2_0.py       主程式（本版本）
scripts/sync_opencode_go_models_v1_1_0.py       前版（保留備查）
scripts/sync_opencode_go_models_v1_0_0.py       初版（保留備查）
scripts/test_sync_opencode_go_models_v1_2_0.py  單元測試（89 tests）
scripts/metadata-overrides.json                 人工覆寫
reports/models-sync-latest.md                   最近一次結果
reports/capabilities/<id>-latest.md             各模型能力實測報告
reports/capabilities-cache.json                 能力實測快取（預設 30 天）
reports/models-dev-cache.json                   models.dev 能力資料快取（預設 6 小時）
```

## 執行方式

先做 dry-run 預覽（不寫入設定檔，會印出 LiteLLM unified diff）：

```bash
cd C:/Users/99018398/.pi/agent/skills/opencode-go-models/scripts
python sync_opencode_go_models_v1_2_0.py --dry-run
```

確認差異後正式套用（預設就是套用）：

```bash
python sync_opencode_go_models_v1_2_0.py
```

常用參數：

| 參數 | 用途 |
| --- | --- |
| `--dry-run` | 只預覽與驗證，不寫入 |
| `--no-probe` | 只比對清單與能力，不對每個模型送出測試請求（省額度） |
| `--capabilities auto\|all\|off\|<id,id>` | 能力實測範圍（預設 `auto`） |
| `--refresh-capabilities` / `--capability-ttl-days N` | 能力實測快取控制（預設 30 天） |
| `--keep-stale` | 不自動移除上游已消失且無法呼叫的殘留項目 |
| `--include-remote` | 連 pi.dev 遠端目錄已收錄的模型也寫入 `models.json` |
| `--check-pi-catalog` / `--no-check-pi-catalog` | 同步後以 `pi --list-models` 驗證顯示清單（預設開啟，唯讀） |
| `--pi-command` / `--check-pi-timeout` | 驗證用的 Pi 指令（預設 `pi`）與逾時秒數（預設 120） |
| `--check-pi-catalog` / `--no-check-pi-catalog` | 同步後以 `pi --list-models` 驗證顯示清單（**預設開啟**，唯讀） |
| `--pi-command` / `--check-pi-timeout` | 驗證用的 Pi 指令（預設 `pi`）與逾時秒數（預設 120） |
| `--skip-litellm` / `--skip-pi` | 只同步其中一邊 |
| `--no-refresh-litellm` | 不更新既有 LiteLLM 路由的能力與成本 |
| `--pi-mode report` | 只報告 Pi 差異，不寫 models.json |
| `--refresh-models-dev` | 忽略快取，重新下載 models.dev 能力資料 |
| `--models-dev-provider` | models.dev provider id（預設 `opencode-go`） |
| `--prune` / `--prune-pi` | 無條件移除上游已消失的 LiteLLM 路由／Pi 自訂模型 |

## 執行前檢查

- LiteLLM 容器必須在跑（容器內 PyYAML 驗證 + 套用後重啟驗證）：`docker ps --filter name=litellm`
- 金鑰來源：`OPENCODE_GO_API_KEY` 或 `~/.pi/agent/auth.json` 的 `opencode-go.key`。
  嚴禁把金鑰印出、貼到對話或寫入任何檔案。

## 判讀結果

報告：`reports/models-sync-latest.md`。重點：

- **需人工確認**：models.dev 查無資料、跨 provider 推定、能力實測與 models.dev 不一致、連續失敗被隔離、
  上游已消失項目的處置、等待 Pi 刷新的模型。
- **能力實測結果**：矩陣表列出每個受測模型的能力與來源（實測／快取）。
- **變更摘要**：LiteLLM 新增／移除／能力更新，以及 Pi 新增／移除／重建。
- **完整模型清單**：每個模型的協定、可用性、Pi／LiteLLM 狀態、官方文件收錄與**能力來源**。

## 重要限制（回報時必須說明）

- `contextWindow` / `maxTokens` 無法用小型請求證明，以 models.dev 為準；若 models.dev、pi.dev、官方文件
  都查無資料才用保守預設值（並留下 `TODO` 註解）。
- `models.json` 只有「本工具先前寫入」的項目（帶 `[自動同步 v…]` 註解）會被重建；人工手寫的項目不會被改寫。
- 套用 LiteLLM 變更會重啟容器（單一交易、一次重啟，失敗自動還原）。
- models.dev 跨 provider 推定的能力（例如 deepseek-flash、glm-5.1、minimax-m2.5）建議人工複核後
  寫入 `metadata-overrides.json`。

## 測試

```bash
cd C:/Users/99018398/.pi/agent/skills/opencode-go-models/scripts
python -m unittest -v test_sync_opencode_go_models_v1_2_0.py
```
