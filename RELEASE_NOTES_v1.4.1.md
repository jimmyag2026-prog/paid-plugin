# v1.4.1 — explicit blacklist scope (JELabs pilot follow-up)

**Behavior change**: `is_blacklisted` (which routes a cp message to **decline**) now fires **only** when the question explicitly matches the counterparty's `topics_always_escalate` list. The prior heuristic also flagged anything the LLM judged "sensitive (legal/financial/HR/equity)" — that implicit fallback is removed.

## Why

Field-tested during the first Lark pilot (JELabs / SecondEvie, 2026-05-13). The owner asked for "all high-risk topics forwarded to me personally" and naturally read that as **approval card to her DM**. PAID's prior behavior was **decline-to-cp** ("I'm not authorized — please contact owner directly") whenever the LLM heuristic fired on financial / HR words, even with an empty `topics_always_escalate` list. The owner's DM stayed silent and counterparties had to re-ping manually — opposite of what "escalate" intuitively means.

Removing the implicit "or is sensitive" clause makes the owner's `topics_always_escalate` config the only source of truth.

## What flows where after upgrade

| topic match | v1.4.0 behavior | v1.4.1 behavior |
|---|---|---|
| in `topics_allowed` | direct (auto-answer) | direct (auto-answer) |
| in `topics_always_escalate` | decline | decline |
| **sensitive (LLM-judged), not in either list** | decline (implicit) | **request** (approval card) |
| anything else not matched | request | request |

## Upgrade impact

- **Owners who want the old "auto-deflect sensitive topics" net**: add them explicitly to each cp profile's `topics_always_escalate`. The defaults installed by `python3 -m paid setup` already include `["equity", "salary", "hiring", "customer", "finance"]` — verify on existing counterparties.
- **Owners who want the new "everything goes to approval card" model**: clear the cp profile's `topics_always_escalate` (set to `[]`). All non-allow topics will now reach the owner's DM as an interactive card.

Either intent is now explicit. No silent behavior depending on LLM judgment.

## Future work — proper architectural fix (tracked)

This release is a one-line guardrail removal. The proper fix is a per-owner `blacklist_action: "decline" | "request"` setting that controls what to do **when** the blacklist matches — independent of which topics are listed. That work is tracked separately for a future release.

## Tests

- 604 passing (same as v1.4.0).
- No test exercised the removed "or is sensitive" heuristic — it lived only in the prompt comment, no Python branch depended on it. Tests verify `is_blacklisted` controlled by `topics_always_escalate` matching, which is the contract this release strengthens.

## Bundled debug context (informational)

JELabs pilot also surfaced infrastructure issues that are **not** fixed in this release but are documented for the next patch cycle:

- PAID classifier requires `model.api_key` in `~/.hermes/config.yaml` — falls back silently if missing. Bot looked alive but every cp message landed as a fallback `request`. `hermes auth add deepseek …` alone is not enough; the yaml line is mandatory.
- `lark-oapi` is not pre-installed by the hermes installer. First `hermes gateway run` fails with `'NoneType' object has no attribute 'Client'`; restart resolves it.
- Systemd `--user` unit does not load `~/.hermes/.env`. Env vars in there are invisible to the gateway process. Use config.yaml or explicit `EnvironmentFile=` instead.

Each of these has a backlog item with a proposed source fix.

## Commits

- one-line patch to `paid/classifier.py:88` (blacklist scope comment)
- version bump in `plugin.yaml` to 1.4.1
