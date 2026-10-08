# pi-opencode-go-models

[English](README.en.md) | **繁體中文**

一個 **Pi 技能（skill）＋ 命令列工具**，用來把 **OpenCode Go 訂閱目前實際可用**的模型清單與能力，同步到：

- **Pi**：`~/.pi/agent/models.json`（自動補上 pi.dev 官方目錄尚未收錄的模型）
- **LiteLLM**：`config.yaml`（新增／更新／移除 `opencode-go/*` 路由的能力旗標與成本）

> 本專案負責「**資料正確**」：模型能不能呼叫、context / max output、成本、以及各項能力旗標。
> 「Pi 顯示清單要隱藏哪些已下架 ID」屬於另一個元件：[pi-ext-opencode-go-catalog](https://github.com/terrydada-art/pi-ext-opencode-go-catalog)。

---

## 特色

| 能力 | 說明 |
| --- | --- |
| 以實測為準 | 對每個模型送出最小請求，確認「真的可呼叫」才寫入設定；catalog 列出不代表可用 |
| 能力實測 | 8 項能力 × 3 種協定（chat / responses / anthropic）實際驗證，結果優先於任何文件 |
| 多來源交叉比對 | `models.dev`、OpenCode Go 官方文件、pi.dev 遠端目錄、跨 provider 眾數、人工覆寫 |
| 交易式寫入 LiteLLM | 備份 → 容器內 YAML 驗證 → 原子取代 → 單次重啟 → `/v1/models` 驗證，失敗自動還原 |
| 殘留項目證據導向處理 | 上游已消失者會**額外實測**，確認無法呼叫才自動移除（`--keep-stale` 可關閉） |
| 一致性驗證 | 同步後自動執行 `pi --list-models`（唯讀）確認 Pi 顯示清單符合預期 |
| 零第三方依賴 | 純 Python 3.12 標準函式庫；不需要 `requests` / `pyyaml` 等套件 |
| 可回溯 | 每次執行都產生 Markdown + JSON 報告，並保留設定檔備份 |

---

## 運作原理

### 1. 資料來源優先序

```text
人工覆寫（metadata-overrides.json）
  → 能力實測（本次執行）
  → models.dev（provider = opencode-go）
  → models.dev（provider = opencode，同廠商）
  → 跨 provider 眾數
  → OpenCode Go 官方文件
  → pi.dev 遠端目錄
  → 單次實測推論
  → 保守預設值
```

### 2. 能力實測（8 項 × 3 協定）

`system_messages`、`function_calling`、`tool_choice`、`native_structured_output`、
`native_streaming`、`vision`、`prompt_caching`、`reasoning`

- 協定：`openai-completions`（chat）、`openai-responses`、`anthropic-messages`。
- **可信度政策**：strong 類（system messages、function calling、tool_choice、strict JSON、streaming）
  成功與失敗都採用；confirm 類（reasoning、vision、prompt caching）**只採用陽性結果**，
  避免快取暖機或偵測方式造成偽陰性降級。
- 遇 401 / 403 / 429 / 502 / 503 / 504 會中止該模型其餘測試，未測到的能力不會被當成 `false`。
- 結果快取 30 天（`--capability-ttl-days`、`--refresh-capabilities` 可調）。

### 3. 寫入策略

- **Pi `models.json`**：只補 pi.dev 遠端目錄**未收錄**的模型；本工具寫入的項目帶有
  `// [自動同步 v…]` 註解，重跑時會**先移除再重寫**（不產生重複定義）；人工手寫的項目不會被動到。
- **LiteLLM**：一次備份 → 容器內 PyYAML 驗證 → 原子取代 → 一次重啟 → `/v1/models` 驗證，
  任何步驟失敗都自動還原；每個 route 的固定 `extra_headers.x-opencode-session` 會保留。
- **殘留項目**：上游 catalog 已不存在的模型（例如改名後的 `space-bunny-free`）會額外實測，
  **確認無法呼叫才自動移除**（`--prune` / `--prune-pi` 為無條件移除、`--keep-stale` 完全不自動移除）。

---

## 系統需求

- **Python 3.12+**（僅標準函式庫；已在 Windows 與 Git Bash 環境驗證）
- **Pi**（若要同步 `models.json`；Pi 安裝於任何位置皆可，透過 `--pi-agent-dir` 指定）
- **LiteLLM**（若要同步 `config.yaml`）：設定檔路徑 + 可選的 Docker 容器重啟能力
- **OpenCode Go API Key**：讀取順序為環境變數 `OPENCODE_GO_API_KEY`
  → `~/.pi/agent/auth.json` 的 `opencode-go.key`。**本工具不會輸出或寫入任何金鑰。**

### 可選依賴

LiteLLM 的 route 區塊改寫會以 `importlib` 動態載入一個「既有管理工具」模組（`--manage-tool`）。
預設值是作者環境中的 `manage_litellm_opencode_models_v1_0_0.py`。若你沒有這個模組：

- 使用 `--skip-litellm` 只同步 Pi；或
- 用 `--manage-tool` 指向你自己的 LiteLLM route 管理模組（需提供 `find_model_blocks` /
  `add_route` / `remove_route` / `validate_candidate` 等函式）。

---

## 安裝

### A. 作為 Pi 技能（推薦）

```bash
# 複製到 Pi 的技能目錄
mkdir -p ~/.pi/agent/skills/opencode-go-models
cp -r SKILL.md scripts ~/.pi/agent/skills/opencode-go-models/
```

之後在 Pi 內使用 `/skill:opencode-go-models`，或直接執行腳本。

### B. 獨立使用

```bash
git clone https://github.com/terrydada-art/pi-opencode-go-models.git
cd pi-opencode-go-models/scripts
python sync_opencode_go_models_v1_2_0.py --dry-run
```

---

## 快速開始

```bash
cd scripts

# 1) 預覽（不寫入任何設定檔，會印出 LiteLLM unified diff）
python sync_opencode_go_models_v1_2_0.py --dry-run

# 2) 正式套用（預設就是套用；會先自動備份）
python sync_opencode_go_models_v1_2_0.py

# 常用變化
python sync_opencode_go_models_v1_2_0.py --skip-litellm        # 只同步 Pi
python sync_opencode_go_models_v1_2_0.py --no-probe            # 省額度，不做可用性實測
python sync_opencode_go_models_v1_2_0.py --capabilities all    # 對所有模型補齊能力實測
python sync_opencode_go_models_v1_2_0.py --offline-metadata    # 只用 Pi/LiteLLM 現況，不抓遠端 metadata
```

> 預設值（`M:\AI\ai-router-2026\...`、容器名 `litellm`）是作者的環境；請用
> `--config` / `--backup-dir` / `--container` / `--manage-tool` 或對應環境變數改成你的路徑。

---

## 參數

| 參數 | 預設 | 說明 |
| --- | --- | --- |
| `--dry-run` | 關 | 只預覽與驗證，不寫入 `models.json` 與 `config.yaml` |
| `--no-probe` | 關 | 不對每個模型送出測試請求（省額度，但無法確認是否可呼叫） |
| `--probe-vision` / `--no-probe-vision` | 開 | 對「Pi 尚未收錄」的候選模型額外驗證圖片輸入能力 |
| `--capabilities auto\|all\|off\|<id,id>` | `auto` | 能力實測範圍：`auto` = 新增與缺 models.dev 資料者 |
| `--capability-ttl-days N` | `30` | 能力實測快取有效天數 |
| `--refresh-capabilities` | 關 | 忽略能力實測快取，強制重測 |
| `--keep-stale` | 關 | 不自動移除「上游已消失且實測確認無法呼叫」的殘留項目 |
| `--prune` | 關 | 無條件移除 LiteLLM 上游 catalog 已消失的路由 |
| `--prune-pi` | 關 | 無條件移除 `models.json` 內上游 catalog 已消失的自訂模型 |
| `--include-remote` | 關 | 連 pi.dev 遠端目錄已收錄的模型也寫入 `models.json` |
| `--check-pi-catalog` / `--no-check-pi-catalog` | 開 / 關 | 同步後以 `pi --list-models` 驗證 Pi 顯示清單（唯讀） |
| `--pi-command` | `pi` | 驗證用的 Pi 指令（環境變數 `OCG_PI_COMMAND`） |
| `--check-pi-timeout SEC` | `120` | 驗證逾時（環境變數 `OCG_CHECK_PI_TIMEOUT`） |
| `--skip-litellm` / `--skip-pi` | 關 | 只同步其中一邊 |
| `--no-refresh-litellm` | 關 | 不依 models.dev 更新既有 LiteLLM 路由的能力與成本 |
| `--pi-mode auto\|report` | `auto` | `report` 只報告 Pi 差異，不寫入 |
| `--offline-metadata` | 關 | 不抓 models.dev、官方文件與 pi.dev 目錄 |
| `--models-dev-provider ID` | `opencode-go` | models.dev provider id |
| `--models-dev-ttl 時數` | `6` | models.dev 快取有效時數 |
| `--refresh-models-dev` | 關 | 忽略 models.dev 快取重新下載 |
| `--pi-agent-dir PATH` | `~/.pi/agent` | Pi 設定目錄 |
| `--config PATH` | `M:\AI\ai-router-2026\litellm\config.yaml` | LiteLLM 設定檔（環境變數 `LITELLM_CONFIG`） |
| `--backup-dir PATH` | `M:\AI\ai-router-2026\backups` | LiteLLM 備份目錄 |
| `--manage-tool PATH` | 作者的 `manage_litellm_opencode_models_v1_0_0.py` | LiteLLM route 管理模組 |
| `--container NAME` | `litellm` | LiteLLM 容器名稱（環境變數 `LITELLM_CONTAINER`） |
| `--report-dir PATH` | 技能 `reports/` | 報告與狀態檔目錄 |
| `--overrides PATH` | `scripts/metadata-overrides.json` | 人工覆寫檔 |
| `--timeout SEC` | `120` | 單一 HTTP 請求逾時（環境變數 `OCG_SYNC_TIMEOUT`） |
| `--workers N` | `3`（上限 8） | 模型探測並行數（環境變數 `OCG_SYNC_WORKERS`） |
| `--quarantine-after N` | `3` | 連續失敗幾次後列入需人工確認 |
| `--health-timeout SEC` | `180` | 等待 LiteLLM 容器 healthy 的秒數 |
| `--no-restart` | 關 | LiteLLM 套用後不重啟（不建議） |
| `--fail-on-unavailable` | 關 | 有模型不可呼叫時以 exit code 2 結束 |

**環境變數**：`OPENCODE_GO_API_KEY`、`LITELLM_CONFIG`、`LITELLM_CONTAINER`、`OCG_SYNC_TIMEOUT`、
`OCG_SYNC_WORKERS`、`OCG_SYNC_PROBE_VISION`、`MODELS_DEV_PROVIDER`、`MODELS_DEV_TTL_HOURS`、
`OCG_CAPABILITIES`、`OCG_CAPABILITY_TTL_DAYS`、`OCG_PI_COMMAND`、`OCG_CHECK_PI_TIMEOUT`

---

## 報告與狀態檔

執行後會寫入 `reports/`：

| 檔案 | 用途 |
| --- | --- |
| `models-sync-latest.md` / `.json` | 本次同步報告（含「Pi 顯示清單驗證」段落） |
| `models-sync-<日期>.md` | 當日快照 |
| `models-dev-cache.json` | models.dev 能力快取（預設 6 小時） |
| `protocol-cache.json` | 各模型的 API 協定判定快取 |
| `capabilities-cache.json` | 能力實測快取（預設 30 天） |
| `capabilities/<model>-latest.{md,json}` | 逐模型能力實測報告 |
| `failures.json` | 連續失敗計數（用於 quarantine 判定） |

### Exit codes

| 值 | 意義 |
| --- | --- |
| `0` | 成功 |
| `1` | catalog 或 API Key 取得失敗（**未變更任何設定**） |
| `2` | 有模型不可呼叫（僅在 `--fail-on-unavailable` 時） |

---

## 與 Pi extension 的分工

| 元件 | 職責 |
| --- | --- |
| 本工具（技能） | 資料正確：能力／成本／context → Pi `models.json` + LiteLLM |
| [pi-ext-opencode-go-catalog](https://github.com/terrydada-art/pi-ext-opencode-go-catalog) | Pi 顯示什麼：接管 provider 註冊，隱藏上游已下架的 ID（例如 `space-bunny-free`） |

兩者版本號**各自獨立**（技能 v1.2.0 ≠ extension v1.1.0，不是先後關係）。
本工具的 `--check-pi-catalog` 會驗證兩邊是否一致。

---

## 重要限制

- catalog 列出模型**不代表**供應商端點當下可服務；本工具以最小生成請求判定可用性，
  失敗次數會累計，連續失敗者列入「需人工確認」。
- `contextWindow` / `maxTokens` 無法用小型請求證明；官方未公布時會寫保守值
  （131072 / 32768）並在報告標註，可用 `scripts/metadata-overrides.json` 人工覆寫。
- 官方文件解析依賴 `https://opencode.ai/docs/go/` 的 HTML 結構；改版時會自動降級使用
  models.dev 與 pi.dev 資料（不會中斷）。
- 思考參數（`reasoning_effort` / `thinking`）在各模型支援度極不一致，本工具採保守策略：
  只在有明確證據時才產生對應，否則不送思考參數。

---

## 版本歷程

| 版本 | 重點 |
| --- | --- |
| **v1.2.0**（現行） | 合併能力實測（8 項 × 3 協定）、殘留項目證據導向自動移除、`--check-pi-catalog` Pi 顯示清單驗證 |
| v1.1.0 | 新增 models.dev 能力來源；既有 LiteLLM route 的能力／成本更新（單一交易） |
| v1.0.0 | 初版：catalog 比對、Pi `models.json` 與 LiteLLM route 新增 |

舊版腳本（`sync_opencode_go_models_v1_0_0.py`、`v1_1_0.py`）保留在同目錄供回溯，主線一律使用 v1.2.0。

---

## 測試

```bash
cd scripts
python -m unittest -v test_sync_opencode_go_models_v1_2_0.py   # 89 tests（主線）
python -m unittest -v test_sync_opencode_go_models_v1_1_0.py   # 46 tests
python -m unittest -v test_sync_opencode_go_models_v1_0_0.py   # 28 tests
```

測試只涵蓋純函式（不連網、不改設定檔）。

---

## 授權

MIT，詳見 [LICENSE](LICENSE)。
