# PAID — Personal AI Delegate

[![tests](https://github.com/jimmyag2026-prog/paid-plugin/actions/workflows/tests.yml/badge.svg)](https://github.com/jimmyag2026-prog/paid-plugin/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)
[![Hermes plugin](https://img.shields.io/badge/Hermes-plugin-orange)](https://github.com/NousResearch/hermes-agent)

> Authorise an AI to handle a class of your IM messages on your behalf,
> with an explicit approval gate. Three-state pipeline: auto-answer / ask
> you first / decline back to you. Same category as a lawyer or
> accountant — you stay accountable, the AI works the way you would.

PAID is a [Hermes](https://github.com/NousResearch/hermes-agent) plugin.
It runs locally, all logs stay on your machine, and it speaks through
your existing IM accounts (Telegram / Lark / Feishu / WhatsApp / WeCom /
Slack — whichever Hermes is wired up to).

**Status: v1.4.0 — Lark inline-approve wired.** End-to-end loop is
wired, **586 tests passing**, verified live on Lark approval-card
flow with `hermes-agent` v0.12.0. Ships interactive Lark approval
cards with **fully-routed ✅ / ✏️ / ❌ buttons** (click ✅ with draft
→ direct dispatch; click ✅ without draft → inline "type your reply"
prompt; click ❌ → direct deflection), Telegram InlineKeyboard cards,
Slack Block Kit cards, a local-only Flask web dashboard, daily
snapshot reports, and a `paid status` upgrade with today's metrics +
activity dots.

**v1.3 → v1.4 (2026-05-13)** — Lark approval cards work end-to-end on
click. Pre-v1.4 the click reached PAID but the response string didn't
get delivered back to owner's Lark chat (synthetic-command reply
unreliable). v1.4 rewires `_cmd_card` to push every outcome via
`send_dm`. For the empty-draft case (J3 out-of-scope topics that
PAID can't ground from SOP), ✅ Approve / ✏️ Edit now arm an
"awaiting input" slot — the owner's next plain-text reply in the
same chat becomes the answer forwarded to the junior. `/paid-cancel-input`
clears the slot if the owner changes their mind.

**v1.1 → v1.2 (2026-05-03)** — Multi-platform v0.1: TG + Slack approval
cards. Owner picks `preferred_platform` in `owner.json` (v2 schema with
`home_chat_id` + `enabled` per identity). See
[`docs/USER_TEST_MULTIPLATFORM.md`](docs/USER_TEST_MULTIPLATFORM.md) for
the owner-side walkthrough.

**v1.0 → v1.1 (2026-05-03)** — Code unchanged from v1.0.0 (W2/W3 unblock
batch). v1.1 promotes the design baseline for the next development arc:
review-skill modular plan + hermes v0.11 plugin-hook upgrade evaluation.
See `paid-may` companion repo:

- [`design/02_review_skill_prd.md`](https://github.com/jimmyag2026-prog/paid-may/blob/main/design/02_review_skill_prd.md) — review skill PRD
- [`design/03_review_skill_integration.md`](https://github.com/jimmyag2026-prog/paid-may/blob/main/design/03_review_skill_integration.md) — plugin↔skill integration
- [`design/04_review_skill_architecture.md`](https://github.com/jimmyag2026-prog/paid-may/blob/main/design/04_review_skill_architecture.md) — module/state-machine architecture
- [`design/05_backlog.md`](https://github.com/jimmyag2026-prog/paid-may/blob/main/design/05_backlog.md) — modular development backlog (M1–M9)
- [`design/06_hermes_v011_evaluation.md`](https://github.com/jimmyag2026-prog/paid-may/blob/main/design/06_hermes_v011_evaluation.md) — hermes v0.11 hook adoption plan

Several v1 features intentionally cut for now (see
[Limitations](#known-v09-limitations) below).

---

## What it does

When someone DMs you on a wired IM platform:

1. PAID identifies the sender (owner / known counterparty / unknown).
2. A cheap LLM call classifies the message into a three-state action:
   - **Direct** — in scope, low stakes, high confidence → PAID answers
     directly using your `persona.md` + `sop.md`.
   - **Request** — out of scope, high stakes, or low confidence → PAID
     replies "I'll forward this to you" and DMs **you** an approval
     card. You approve / reject / override via slash command. The
     final answer goes back to the sender.
   - **Decline** — counterparty's blacklist or globally sensitive topic
     → PAID tells the sender to contact you directly.
3. Every step is logged to `~/.hermes/paid/audit_log.jsonl`. Owner
   messages bypass PAID entirely; PAID never speaks for you to yourself.

Five-layer safety design (Layers 1, 4a, 4b shipped; Layer 3 / 4c / 4d
deferred):

| Layer | What | Status |
|---|---|---|
| 1 — INPUT | Prompt-injection regex on incoming message | ✅ shipped |
| 2 — CLASSIFY | LLM-backed three-state classifier with fallback + internal-contradiction validator | ✅ shipped |
| 3 — CONTEXT | Source restriction (only granted files + this counterparty's history) | 🟡 partial — context shaping done, strict enforcement deferred |
| 4a — OUTPUT | Cross-counterparty name leakage detection | ✅ shipped (observer-only in v0.5) |
| 4b — OUTPUT | PII regex (email / SSN / CN ID / mobile / E.164 / large $ / 万亿 / card) | ✅ shipped (observer-only) |
| 4c — OUTPUT | LLM post-check on sensitive topics | ⏳ deferred |
| 4d — OUTPUT | Source-attribution check | ⏳ deferred |
| 5 — AUDIT | Append-only audit log + fatal alerts | ✅ shipped |

---

## Install

Requirements: macOS or Linux, Python 3.9+, Hermes 0.8+ with the gateway
connected to at least one IM platform.

### Option A — clone directly into Hermes plugins dir

```bash
git clone https://github.com/jimmyag2026-prog/paid-plugin ~/.hermes/plugins/paid-v1
cd ~/.hermes/plugins/paid-v1
python3 -m paid setup --owner-id owner_yourname --name "Your Name" \
    --identity telegram:YOUR_TG_USER_ID
hermes plugins enable paid-v1
hermes gateway restart
```

### Option B — install via the bundled script

```bash
git clone https://github.com/jimmyag2026-prog/paid-plugin ~/src/paid-plugin
cd ~/src/paid-plugin
./bin/install.sh             # copies into ~/.hermes/plugins/paid-v1/
python3 -m paid setup --name "Your Name" --identity telegram:YOUR_TG_USER_ID
hermes plugins enable paid-v1
hermes gateway restart
```

For the full step-by-step guide (including how to edit `persona.md` and
`sop.md`, add counterparties, and onboard testers) see
[INSTALL.md](INSTALL.md).

---

## Slash commands (owner-side)

After install, in any IM session where Hermes is listening to your owner
identity:

```
/paid-pending                 # list pending approval requests
/paid-approve <id>            # approve (sends draft as-is)
/paid-approve <id> new text   # approve with override text
/paid-reject  <id>            # decline; sender is told to contact you
/paid-status  <id>            # full state of one request
```

Same actions are available from the shell:

```
python3 -m paid status
python3 -m paid pending
python3 -m paid approve <id> [override text]
python3 -m paid reject  <id>
python3 -m paid add-counterparty <platform> <user_id> --name N --role junior --topic-allow scheduling
```

---

## On-disk layout

PAID never leaves your machine. All state lives under `~/.hermes/paid/`:

```
~/.hermes/paid/
├── owner.json                # which IM identities are *you*
├── persona.md                # how PAID should speak (you edit this)
├── sop.md                    # topic-tagged knowledge it retrieves from
├── settings.json             # update_mode, model_override
├── counterparties/<cp_id>/   # per-sender profile + topics_allowed list
│   └── profile.json
├── pending_approvals.jsonl   # J3 event log — append-only
├── outbound_queue.jsonl      # send_dm fallback queue when gateway is down
├── audit_log.jsonl           # every classify/decide/respond
├── fatal_alerts.jsonl        # crashes + Layer 1/4 hits
└── plugin_runtime.log        # plugin lifecycle log
```

LLM calls go through whatever your Hermes config (`~/.hermes/config.yaml`)
already uses — your provider, your API keys. PAID does not introduce a new
outbound endpoint.

---

## Architecture (one diagram)

```
                 incoming IM message
                          │
              ┌───────────▼────────────┐
              │  Hermes gateway        │
              └───────────┬────────────┘
                          │ pre_llm_call(sender_id, platform, msg, …)
                          │
   ┌──────────────────────▼──────────────────────┐
   │  PAID plugin                                │
   │                                             │
   │  is_owner? ─yes→ pass through (no PAID)    │
   │     │ no                                    │
   │     ▼                                       │
   │  L1 prompt-injection guard ──hit→ decline  │
   │     │ ok                                    │
   │     ▼                                       │
   │  classifier (cheap LLM)                     │
   │     │                                       │
   │     ▼                                       │
   │  decide_action → direct | request | decline │
   │     │                                       │
   │  if request:                                │
   │    approval.create + send_dm to owner       │
   │     │                                       │
   │     ▼                                       │
   │  shape_context → return to Hermes           │
   └──────────────────────┬──────────────────────┘
                          │ {"context": "..."}
              ┌───────────▼────────────┐
              │  Hermes LLM call       │
              └───────────┬────────────┘
                          │ post_llm_call(assistant_response)
                          ▼
                 L4 PII / cross-cp check (observer)
                          ▼
                  audit_log.jsonl
                          ▼
                 reply delivered to sender


              owner reply path (separate IM session):
              /paid-approve <id> [override]
                          │
                          ▼
              approval.set_status → send_dm to junior
              ("{owner} 看了你的问题：\n\n{final}")
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for module decomposition.

---

## Known v0.9 limitations

These are intentionally cut for now. Each will land in a follow-up:

- **Layer 3 strict source restriction** — context is shaped today, but
  there's no enforcement that responses cite only granted material.
- **Layer 4c / 4d output checks** — LLM post-check and source attribution
  are not wired. Layer 4a/b is observer-only (logs but does not redact
  before send).
- **Approval card buttons** — Lark gets a real interactive card with
  ✅ Approve / ❌ Reject buttons (button clicks come back through
  hermes' synthetic-command path and dispatch to the existing handlers).
  Other platforms still get the plain-text card with numbered shortcuts.
- **Approval timeouts** — handled by ``bin/sweep_pending.py``; install it as
  a launchd / systemd timer (5 min cadence). Default timeout 30 min, see
  ``settings.json``.
- **Modify button** — use `/paid-approve <id> <override text>` instead.
- **Auto counterparty discovery** — unknown senders default to the
  `pending` role and PAID DMs you a discovery card on first contact; add
  them with `python3 -m paid add-counterparty …` or block them with
  `python3 -m paid ignore-counterparty <plat> <id> --reason "…"`.
- **Dashboard** — `python3 -m paid status` (CLI) and `python3 -m paid
  dashboard` (Flask web UI on 127.0.0.1:7777) both surface today's
  metrics, recent activity, and counterparty health dots. Read-only;
  install ``flask`` to use the web variant (`pip install --user flask`).
- **Retrieval** — bigram-tokenised substring scorer over `sop.md`. Optional
  ``jieba`` (``pip install jieba``) gives proper Chinese segmentation; the
  fallback bigram path is good enough for SOP-sized corpora. No FTS5, no
  web search.
- **L4 outbound enforcement** — observer-only on the leaked reply, plus a
  best-effort corrective DM ("disregard the previous reply") immediately
  after. True redaction needs an outbound hook hermes 0.12.0 doesn't expose;
  filed upstream.

### Hermes upstream gaps PAID currently works around

These are real upstream issues; PAID ships a workaround so v0.9 is
usable today, but the cleanest fix is in `hermes-agent` itself:

- **`pre_llm_call` hook does not pass `chat_id`.** Lark / Feishu's IM
  API is chat-centric — outbound messages need a `chat_id`, but the hook
  only carries `sender_id`. PAID works around this by inferring
  `receive_id_type` from the ID prefix (`oc_` → chat_id, `ou_` → open_id,
  bare hex → user_id) and bypassing the adapter's hard-coded chat_id
  send path with a direct Lark API call (`paid/hermes_io.py`,
  `_send_lark_direct`). When upstream propagates `chat_id` through the
  hook, PAID will store it on counterparty profiles and use the standard
  adapter path — the direct branch then degrades to a fallback.
- **`post_llm_call` hook does not pass `platform` / `sender_id`.** This
  meant owner replies were over-audited and Layer 4 cross-cp scoping was
  blunt. PAID caches `(session_id → platform, sender_id, cp_id)` at
  pre-hook and resolves at post-hook. In-memory only.
- **Adapter `send()` hard-codes `receive_id_type=chat_id`.** Same root
  cause as the first item; same workaround.
- **Identity bifurcation: `card.action.trigger` events arrive with
  `open_id`, plain text events with `user_id`.** A pairing-approved
  user can fail an auth check on a button click because hermes' approved
  list keys on whichever form was first seen. PAID's
  [`docs/LARK_SETUP.md`](docs/LARK_SETUP.md) walks through adding both
  forms to `feishu-approved.json` AND `owner.json`.

### Live-test learnings (real bugs we found by clicking buttons, not by reading docs)

- **Lark Suite "Interactive Card" capability is a separate toggle from
  the `card.action.trigger` event subscription.** Toggling the event on
  without enabling the capability silently fails — the event won't even
  appear in the Add-Events picker until the capability is on. Symptom:
  error 200340 on every button click. Full walk-through in
  [`docs/LARK_SETUP.md`](docs/LARK_SETUP.md).
- **`SendResult.success` lies for HTTP 200 with API-level failure.**
  Lark's adapter returns a SendResult with `.success=False` when the
  API returned `[230001] invalid receive_id`, but the SDK call returned
  HTTP 200. v0.8 onward inspects `.success` explicitly so a "queued"
  outcome can't masquerade as a delivery.
- **Sweep / cron callers are out-of-process.** v0.9.2 onward, `send_dm`
  falls back to a standalone `lark_oapi.Client` built from
  `~/.hermes/.env` when no in-process gateway adapter is available — so
  `bin/sweep_pending.py` actually delivers, instead of always queuing.

### Quick start tips for new operators

- After enabling the plugin, **DM the bot from your owner account and
  type `/sethome`**. This stores your owner↔bot chat_id into
  `FEISHU_HOME_CHANNEL` (or the equivalent env per platform). PAID prefers
  that chat_id for owner approval-card delivery — slightly more reliable
  than the user_id fallback, and it also suppresses hermes' first-message
  "no home channel set" notice for everyone else.
- Junior dispatch (`/paid-approve` → reply to junior) works without a
  pre-captured chat_id thanks to the direct-API workaround above; no
  per-junior `/sethome` is needed.
- If you're on Lark Suite, complete the 3-step Interactive Card setup
  in [`docs/LARK_SETUP.md`](docs/LARK_SETUP.md) before testing button
  clicks — error 200340 is almost always a missing toggle there.

---

## Testing

```bash
cd /path/to/paid
python3 -m pytest tests/ -q
```

128 tests, ~0.3 s on a 2024 MBP. New contributions should keep it green.

---

## Privacy + safety

- All PAID state lives on the operator's machine. There is no network
  endpoint operated by the project.
- LLM calls use whatever provider your Hermes config already uses.
- Layer 1 (prompt-injection guard) and Layer 4 (PII / cross-cp leakage)
  fire automatically on every message; hits are written to
  `fatal_alerts.jsonl` so leaks are not silent.
- Owner messages are short-circuited before any classification — PAID
  cannot speak as a counterparty for the owner's own session.

---

## Contributing

Issues and small PRs welcome. Please:

- Keep tests green (`pytest tests/`).
- Follow the existing module layout (`storage.py` / `identity.py` /
  `classifier.py` / `decision.py` / `retrieval.py` / `audit.py` /
  `hermes_io.py` / `approval.py` / `safety.py`).
- For non-trivial behaviour changes, open an issue first to discuss.

---

## License

MIT — see [LICENSE](LICENSE).
