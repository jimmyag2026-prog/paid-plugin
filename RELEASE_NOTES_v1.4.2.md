# v1.4.2 — install hardening (JELabs pilot follow-up #2)

Three install/runtime safeguards extracted from the JELabs pilot deployment (2026-05-13). All three were silent failures — the bot looked alive while half-broken — which is the worst class of bug for a pilot owner because nothing in their IM client signals "go ask Jimmy."

## What's in

### 1. `api_key` resolution chain (yaml → env → clear error)

`paid/hermes_io.py::_resolve_model_section` now resolves `api_key` in order:

1. `~/.hermes/config.yaml` `model.api_key:` (explicit, unchanged)
2. provider-specific env var (`DEEPSEEK_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `KIMI_API_KEY`, ...)
3. raise `HermesConfigError` whose message names the exact env var the operator needs to set + warns that `hermes auth add` is **not** consulted

Also auto-loads `~/.hermes/.env` on demand (idempotent, doesn't override pre-set vars) so systemd `--user` gateway units that don't `EnvironmentFile=` it still see operator-set keys.

**JELabs root cause this fixes**: operator ran `hermes auth add deepseek --api-key sk-...`, added `DEEPSEEK_API_KEY=sk-...` to `.env`, but didn't put `api_key:` in `config.yaml`. Pre-v1.4.2 every cp message hit `[CLASSIFY-FALLBACK]` and fell through to `request`. Owner saw approval cards for "what's your timezone?" with no clue why.

### 2. Classifier dry-run health check at plugin register

`__init__.py::register()` now calls `_classifier_health_check()` after hooks are wired. It pings the LLM once with a 1-token prompt.

- **Success** → `[health-check] classifier dry-run OK (reply preview: ...)` in `plugin_runtime.log`
- **Config error** → `_alert_owner(reason="classifier_config_invalid", ...)` → owner DM + `fatal_alerts.jsonl` entry, with the actionable error pointing at config.yaml or env var
- **LLM unreachable** → `_alert_owner(reason="classifier_llm_unreachable", ...)` → covers bad key, network/firewall, provider down
- Health check failures **don't crash plugin registration** — plugin still loads, hooks still wired, owner just knows the LLM path is broken

### 3. `bin/install.sh` pre-installs `lark-oapi` when feishu is enabled

If `~/.hermes/config.yaml` has `platforms.feishu.enabled: true` and the hermes venv pip is reachable, install runs `pip install lark-oapi==1.5.5` upfront.

**JELabs root cause this fixes**: hermes v0.13 installer treats `lark-oapi` as an optional dep. First `hermes gateway run` after enabling feishu would crash with `'NoneType' object has no attribute 'Client'` because `try: import lark_oapi as lark` failed at module import (dep not installed yet); hermes async-pip-installed it during startup, but the gateway process had already cached `lark = None`. Restart fixed it — but the first crash scared the operator and made the install feel broken.

## Upgrade path

This release is backwards-compatible:
- Existing v1.4.1 deployments with `model.api_key:` set in `config.yaml` keep working unchanged
- Env-var fallback only kicks in when yaml's `api_key` is missing — no overrides
- `bin/install.sh` lark-oapi step only runs when feishu is enabled; other-platform pilots unaffected

## Tests

- 611 passing (+7 new tests covering env-var fallback, dotenv loading, error message hints)
- Pre-v1.4.2 baseline: 604 passing

## Commits

- `paid/hermes_io.py`: api_key fallback chain + dotenv loader + env-var helpers
- `__init__.py::register()`: dry-run health check via `_classifier_health_check()`
- `bin/install.sh`: feishu detection + lark-oapi pre-install
- `tests/test_hermes_io.py`: 7 new tests for v1.4.2 paths
- `plugin.yaml`: 1.4.1 → 1.4.2

## Future work (still in backlog)

- v1.4.3 UX: markdown strip, response length cap, L4 observer whitelist
- v1.4.4 Tech debt + ops: wrap format unification, timer install docs, standalone send_dm fix
- v1.4.5 Architecture: per-cp `blacklist_action` setting (retires the v1.4.1 1-line patch in favor of explicit owner config)
