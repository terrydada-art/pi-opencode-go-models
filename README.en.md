# pi-opencode-go-models

**繁體中文** | [English](README.en.md)

A **Pi skill + command-line tool** that syncs the models your **OpenCode Go** subscription
actually serves — and their capabilities — into:

- **Pi**: `~/.pi/agent/models.json` (adds models that the official pi.dev catalog does not list yet)
- **LiteLLM**: `config.yaml` (adds / updates / removes `opencode-go/*` routes, capability flags and cost)

> This project owns **data accuracy**: can a model be called at all, its context / max output,
> its cost, and its capability flags.
> Which models Pi *shows* (including hiding retired IDs) is handled by a separate component:
> [pi-ext-opencode-go-catalog](https://github.com/terrydada-art/pi-ext-opencode-go-catalog).

---

## Highlights

| Feature | Description |
| --- | --- |
| Verified, not assumed | Sends a minimal request per model; only models that really answer are written |
| Capability probing | 8 capabilities × 3 protocols (chat / responses / anthropic), measured, not guessed |
| Multi-source cross-check | `models.dev`, OpenCode Go docs, pi.dev remote catalog, cross-provider majority, manual overrides |
| Transactional LiteLLM writes | backup → in-container YAML validation → atomic replace → single restart → `/v1/models` verification, auto rollback |
| Evidence-based cleanup | Models gone upstream are probed again; removal only after a failed call (`--keep-stale` opts out) |
| Consistency check | After syncing, runs read-only `pi --list-models` to verify the visible catalog |
| No third-party deps | Pure Python 3.12 standard library (no `requests`, no `pyyaml`) |
| Traceable | Markdown + JSON report for every run, plus backups of every file it touches |

---

## How it works

### 1. Metadata priority

```text
manual overrides (metadata-overrides.json)
  → capability probe (this run)
  → models.dev (provider = opencode-go)
  → models.dev (provider = opencode, same vendor)
  → cross-provider majority
  → OpenCode Go official docs
  → pi.dev remote catalog
  → inference from a single probe
  → conservative default
```

### 2. Capability probing (8 × 3)

`system_messages`, `function_calling`, `tool_choice`, `native_structured_output`,
`native_streaming`, `vision`, `prompt_caching`, `reasoning` — over `openai-completions`,
`openai-responses` and `anthropic-messages`.

- **Trust policy**: "strong" capabilities (system messages, function calling, tool choice,
  strict JSON, streaming) are trusted both ways; "confirm" capabilities (reasoning, vision,
  prompt caching) only adopt **positive** results, so a cold cache can never downgrade a known-good flag.
- 401 / 403 / 429 / 502 / 503 / 504 abort the remaining tests for that model; untested
  capabilities are never stored as `false`.
- Results are cached for 30 days (`--capability-ttl-days`, `--refresh-capabilities`).

### 3. Write policy

- **Pi `models.json`**: only models missing from the pi.dev catalog. Entries written by this tool carry a
  `// [自動同步 v…]` marker and are removed-then-rewritten on each run (no duplicate definitions);
  hand-written entries are left untouched.
- **LiteLLM**: single transaction — backup, in-container PyYAML validation, atomic replace, one restart,
  `/v1/models` verification, automatic rollback on any failure. Existing
  `extra_headers.x-opencode-session` values are preserved.
- **Retired models**: models that disappeared upstream (e.g. the renamed `space-bunny-free`) are probed
  again and only removed after a confirmed failure (`--prune` / `--prune-pi` force it, `--keep-stale` disables it).

---

## Requirements

- **Python 3.12+** (standard library only; verified on Windows and Git Bash)
- **Pi** (when syncing `models.json`; location is configurable via `--pi-agent-dir`)
- **LiteLLM** (when syncing `config.yaml`): config path plus, optionally, Docker to restart the container
- **OpenCode Go API key**: from `OPENCODE_GO_API_KEY`, otherwise `~/.pi/agent/auth.json` →
  `opencode-go.key`. **The tool never prints or writes a key.**

### Optional dependency

Rewriting LiteLLM route blocks dynamically loads an existing "manage tool" module (`--manage-tool`).
The default points at the author's `manage_litellm_opencode_models_v1_0_0.py`. If you do not have it:

- run with `--skip-litellm` to sync Pi only, or
- point `--manage-tool` at your own module exposing `find_model_blocks` / `add_route` /
  `remove_route` / `validate_candidate`.

---

## Installation

### A. As a Pi skill (recommended)

```bash
mkdir -p ~/.pi/agent/skills/opencode-go-models
cp -r SKILL.md scripts ~/.pi/agent/skills/opencode-go-models/
```

Then use `/skill:opencode-go-models` inside Pi, or run the script directly.

### B. Standalone

```bash
git clone https://github.com/terrydada-art/pi-opencode-go-models.git
cd pi-opencode-go-models/scripts
python sync_opencode_go_models_v1_2_0.py --dry-run
```

---

## Quick start

```bash
cd scripts

# 1) Preview only (writes nothing, prints a LiteLLM unified diff)
python sync_opencode_go_models_v1_2_0.py --dry-run

# 2) Apply (this is the default mode; a backup is taken automatically)
python sync_opencode_go_models_v1_2_0.py

# Common variants
python sync_opencode_go_models_v1_2_0.py --skip-litellm        # Pi only
python sync_opencode_go_models_v1_2_0.py --no-probe            # save quota, skip availability probes
python sync_opencode_go_models_v1_2_0.py --capabilities all    # probe every model
python sync_opencode_go_models_v1_2_0.py --offline-metadata    # never fetch remote metadata
```

> Defaults (`M:\AI\ai-router-2026\...`, container `litellm`) come from the author's machine.
> Override them with `--config` / `--backup-dir` / `--container` / `--manage-tool` or the matching
> environment variables.

---

## Options

| Option | Default | Description |
| --- | --- | --- |
| `--dry-run` | off | Preview and validate only; never writes `models.json` / `config.yaml` |
| `--no-probe` | off | Skip per-model test requests (saves quota, cannot confirm availability) |
| `--probe-vision` / `--no-probe-vision` | on | Extra image-input probe for candidates missing in Pi |
| `--capabilities auto\|all\|off\|<ids>` | `auto` | Probe scope: `auto` = new models + those without models.dev data |
| `--capability-ttl-days N` | `30` | Capability cache lifetime |
| `--refresh-capabilities` | off | Ignore the capability cache and re-probe |
| `--keep-stale` | off | Never auto-remove retired models that failed a probe |
| `--prune` | off | Unconditionally remove LiteLLM routes absent upstream |
| `--prune-pi` | off | Unconditionally remove `models.json` entries absent upstream |
| `--include-remote` | off | Also write models already present in the pi.dev catalog |
| `--check-pi-catalog` / `--no-check-pi-catalog` | on / off | Verify Pi's visible catalog with read-only `pi --list-models` |
| `--pi-command` | `pi` | Pi command used for verification (`OCG_PI_COMMAND`) |
| `--check-pi-timeout SEC` | `120` | Verification timeout (`OCG_CHECK_PI_TIMEOUT`) |
| `--skip-litellm` / `--skip-pi` | off | Sync only one side |
| `--no-refresh-litellm` | off | Do not refresh capability/cost of existing LiteLLM routes |
| `--pi-mode auto\|report` | `auto` | `report` only reports Pi differences |
| `--offline-metadata` | off | Do not fetch models.dev, docs or pi.dev |
| `--models-dev-provider ID` | `opencode-go` | models.dev provider id |
| `--models-dev-ttl hours` | `6` | models.dev cache lifetime |
| `--refresh-models-dev` | off | Ignore the models.dev cache |
| `--pi-agent-dir PATH` | `~/.pi/agent` | Pi config directory |
| `--config PATH` | `M:\AI\ai-router-2026\litellm\config.yaml` | LiteLLM config (`LITELLM_CONFIG`) |
| `--backup-dir PATH` | `M:\AI\ai-router-2026\backups` | LiteLLM backup directory |
| `--manage-tool PATH` | author's route manager | LiteLLM route management module |
| `--container NAME` | `litellm` | LiteLLM container name (`LITELLM_CONTAINER`) |
| `--report-dir PATH` | skill `reports/` | Report and state directory |
| `--overrides PATH` | `scripts/metadata-overrides.json` | Manual override file |
| `--timeout SEC` | `120` | Per-request HTTP timeout (`OCG_SYNC_TIMEOUT`) |
| `--workers N` | `3` (max 8) | Probe concurrency (`OCG_SYNC_WORKERS`) |
| `--quarantine-after N` | `3` | Consecutive failures before "needs attention" |
| `--health-timeout SEC` | `180` | Seconds to wait for the LiteLLM container to become healthy |
| `--no-restart` | off | Do not restart LiteLLM after applying (not recommended) |
| `--fail-on-unavailable` | off | Exit with code 2 if any model is unavailable |

**Environment variables**: `OPENCODE_GO_API_KEY`, `LITELLM_CONFIG`, `LITELLM_CONTAINER`,
`OCG_SYNC_TIMEOUT`, `OCG_SYNC_WORKERS`, `OCG_SYNC_PROBE_VISION`, `MODELS_DEV_PROVIDER`,
`MODELS_DEV_TTL_HOURS`, `OCG_CAPABILITIES`, `OCG_CAPABILITY_TTL_DAYS`, `OCG_PI_COMMAND`,
`OCG_CHECK_PI_TIMEOUT`

---

## Reports and state files

| File | Purpose |
| --- | --- |
| `models-sync-latest.md` / `.json` | Latest sync report (includes the "Pi visible catalog" section) |
| `models-sync-<date>.md` | Daily snapshot |
| `models-dev-cache.json` | models.dev capability cache (6 h default) |
| `protocol-cache.json` | Detected API protocol per model |
| `capabilities-cache.json` | Capability probe cache (30 d default) |
| `capabilities/<model>-latest.{md,json}` | Per-model probe report |
| `failures.json` | Consecutive failure counters (quarantine) |

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success |
| `1` | Catalog or API key failure (**nothing was changed**) |
| `2` | A model was unavailable (only with `--fail-on-unavailable`) |

---

## Relationship to the Pi extension

| Component | Responsibility |
| --- | --- |
| This tool (skill) | Data accuracy: capabilities / cost / context → Pi `models.json` + LiteLLM |
| [pi-ext-opencode-go-catalog](https://github.com/terrydada-art/pi-ext-opencode-go-catalog) | What Pi displays: takes over provider registration to hide retired IDs such as `space-bunny-free` |

Their version numbers are **independent** (skill v1.2.0 ≠ extension v1.1.0 — not a sequence).
`--check-pi-catalog` verifies that both sides agree.

---

## Known limitations

- A catalog entry does not guarantee a working endpoint. Availability is decided by a minimal
  generation request; repeated failures accumulate and eventually require manual review.
- `contextWindow` / `maxTokens` cannot be proven by a small request. When the vendor publishes
  nothing, conservative values (131072 / 32768) are written and flagged in the report; override them
  in `scripts/metadata-overrides.json`.
- Official-docs parsing depends on the HTML of `https://opencode.ai/docs/go/`; when that changes the
  tool degrades gracefully to models.dev and pi.dev data.
- Thinking parameters (`reasoning_effort` / `thinking`) are wildly inconsistent across models, so the
  tool is conservative: it emits them only with clear evidence.

---

## Version history

| Version | Highlights |
| --- | --- |
| **v1.2.0** (current) | Capability probing (8 × 3 protocols), evidence-based auto-removal of retired models, `--check-pi-catalog` |
| v1.1.0 | models.dev as capability source; capability/cost refresh of existing LiteLLM routes (single transaction) |
| v1.0.0 | Initial release: catalog diff, Pi `models.json` and LiteLLM route additions |

Older scripts (`sync_opencode_go_models_v1_0_0.py`, `v1_1_0.py`) are kept for reference;
v1.2.0 is the mainline.

---

## Tests

```bash
cd scripts
python -m unittest -v test_sync_opencode_go_models_v1_2_0.py   # 89 tests (mainline)
python -m unittest -v test_sync_opencode_go_models_v1_1_0.py   # 46 tests
python -m unittest -v test_sync_opencode_go_models_v1_0_0.py   # 28 tests
```

Tests cover pure functions only — no network access, no config writes.

---

## License

MIT — see [LICENSE](LICENSE).
