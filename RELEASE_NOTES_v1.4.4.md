# v1.4.4 — tech debt + ops (JELabs pilot follow-up #4)

Three independent fixes addressing structural issues the JELabs pilot surfaced.

## What's in

### 1. Standalone `send_dm` now covers Lark `chat_id` receives (`paid/hermes_io.py`)

Pre-v1.4.4, when `send_dm("feishu", "oc_xxx", ...)` was called from outside the gateway process (cron sweep, ad-hoc CLI), the code took this path:

- no live adapter (we're not in the gateway process)
- `receive_id_type` is `chat_id` (oc_…)
- ⇒ falls through to "non-Lark platforms below — adapter must exist"
- ⇒ returns `{"ok": False, "error": "no adapter and platform has no standalone client"}`

Net effect: `bin/sweep_pending.py` cron timer couldn't notify owners about expired approvals. Messages silently queued to `outbound_queue.jsonl` instead of delivering.

v1.4.4 routes `chat_id` Lark sends through `_send_lark_standalone` (which builds a fresh `lark_oapi.Client` from `~/.hermes/.env` creds) when no live adapter is reachable.

### 2. Unified `wrap_exact_reply` helper (`paid/decision.py`)

Pre-v1.4.4 PAID had two parallel "force the LLM to emit this verbatim" formats living in different files:

- Format A in `decision.py` (`IGNORE the user question. Reply EXACTLY with: '…' Nothing else.`)
- Format B in `__init__.py:_wrap_reply_for_hermes` (`IGNORE the user message. Reply EXACTLY with the following text and nothing else, preserving all line breaks: '…'`)

`_unwrap_hermes_context` had to recognise both. The v1.3.7 dogfood found a real bug where adding a new wrapped state branch forgot the unwrap-side equivalent, causing the IGNORE-prefix text to leak verbatim to the cp.

v1.4.4 consolidates to one helper (`paid.decision.wrap_exact_reply`) that produces Format B output. `__init__.py:_wrap_reply_for_hermes` now delegates to it. Apostrophes and backslashes are properly escaped. Unwrap still tolerates both formats so any stored Format A state continues to work.

### 3. Systemd `--user` timer install script + docs (`bin/install_timers.sh` + INSTALL.md §7)

`paid-sweep.timer`, `paid-review-sweep.timer`, `paid-daily-snapshot.timer` were committed without an installer or `INSTALL.md` entry, and the bundled `.service` files hardcoded `/home/paid/...` in `ExecStart` — so they only worked for one user.

v1.4.4:

- Replaces `/home/paid/.hermes/...` with `%h/.hermes/...` in all 3 `.service` files (systemd home expansion = portable across users)
- Adds `bin/install_timers.sh`: copies units to `~/.config/systemd/user/`, daemon-reload, enable+start, warns if linger is off
- Adds `INSTALL.md §7` documenting the unit list, install command, verification steps, and macOS caveat

## Tests

- 643 passing (+7 vs v1.4.3's 636)
- New coverage:
  - 3 tests for chat_id standalone fallback (no-adapter-uses-standalone / no-standalone-queues / user_id-still-works)
  - 4 tests for the unified wrap helper (Format B output / apostrophe escape / backslash escape / `__init__` delegation)
  - Updated 4 existing shape_context tests to assert against unwrapped form (apostrophes now escaped in stored prompt)

## Upgrade path

Backwards-compatible across the board:

- `_send_lark_standalone` was already in v1.4.0; v1.4.4 just exercises it on one more code path
- `wrap_exact_reply` produces Format B which `_unwrap_hermes_context` already supported pre-v1.4.4 (it carried both A and B branches for safety)
- Timer install script is opt-in; existing pilots without timers see no change

## Future work (still in backlog)

- v1.4.5 Architecture: per-cp `blacklist_action: "decline" | "request"` setting (retires the v1.4.1 surgical patch by introducing the right per-owner config knob)
