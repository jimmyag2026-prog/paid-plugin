# PAID Review Pack

> Thanks for taking a look. **Time budget: ~45 min** to read this + skim
> the 5 anchored files. You don't need to read the whole codebase. The
> 5 questions at the bottom are what I actually want feedback on.

---

## What PAID is (30 seconds)

A personal AI delegate: people IM you, an AI handles the routine stuff
on your behalf and pings you when something needs your call. Same
principal-agent relationship as a lawyer / accountant / EA, except the
agent is software.

Three outcomes per inbound message — **the J2 three-state pipeline**
that defines the product:

```
inbound from counterparty
  │
  ▼
classifier (topic, stakes, in-scope, blacklist, confidence, draft)
  │
  ├── DIRECT   →  PAID auto-answers (persona + SOP, hard-rule-bounded)
  ├── REQUEST  →  PAID asks owner via J3 approval card; sends placeholder
  │               to counterparty meanwhile; auto-defer after 30 min
  └── DECLINE  →  PAID says "not authorised, please @ owner directly"
```

Plus an optional **review skill** — counterparties can `/review <draft>`
to walk PAID through a 4-pillar (Background / Materials / Framework /
Intent) CSW critique, output is a 6-section brief delivered to the
owner's IM.

Pilot model: owner runs PAID under a dedicated isolated user on a
single shared VPS; first wave of pilots are concierge'd (operator
acts as the pilot's interim CTO).

---

## What's actually built (v1.3.1, May 2026)

| Layer | Module | What it does |
|---|---|---|
| **Classifier** | `paid/classifier.py` | Single LLM call returns `{topic, stakes, in_scope, is_blacklisted, confidence, draft, needs_review}`. Decision rules in `paid/decision.py` map that to direct/request/decline based on per-counterparty `topics_allowed` + `topics_always_escalate`. |
| **Approval (J3)** | `paid/approval.py` + `__init__.py` | On `request`: write pending entry, dispatch interactive card to owner's preferred IM (Lark interactive / TG inline keyboard / Slack Block Kit). Owner taps Approve/Edit/Reject (or slash command). |
| **Sweep / auto-defer** | `bin/sweep_pending.py` | Cron'd; marks pending > 30 min as `timed_out`, DMs counterparty fallback, DMs owner the slip list. |
| **Safety L1-L4** | `paid/safety.py` | L1: prompt-injection regex on inbound (drops to decline, no LLM call). L4a/4b: post-LLM scan for cross-counterparty name leakage + PII (CN ID, phone, email). L4c/d (LLM auditor + source attribution): coded, default-off, observer-only in v0.5. |
| **Hard-rule prompt** | `paid/decision.py` `_DIRECT_HARD_RULES_{ZH,EN}` | Block in direct-state prompt forbidding the LLM from faking approval flow ("I'll forward to owner", "awaiting confirmation") or rewriting SOP content. Added v1.2.5 after dogfood showed the LLM hallucinating an approval workflow that didn't exist. |
| **Owner routing** | `paid/identity.resolve_owner_lark_target` | For Lark owners, prefer routable `ou_` open_id from owner.json over `FEISHU_HOME_CHANNEL` env. Added v1.2.4 after dogfood showed J3 cards routing to a counterparty's chat because of a stale env-var override. |
| **Review skill (M1)** | `paid_review/` | State machine: INTAKE → SUBJECT → SCAN → QA → MERGE → GATE → CLOSED. 4-pillar scan + Responder Sim (two LLM passes), QA findings one-by-one to junior, final gate verdict (READY / READY_WITH_OPEN_ITEMS / FORCED_PARTIAL / FAIL), 6-section brief dispatched to owner. |

**Tests**: 561 passing (pytest, `~/.local/bin/pytest` from repo root).
**Lines**: ~6k core + ~6k review skill + ~5k tests.
**Hermes hooks used**: `pre_llm_call`, `post_llm_call`, `pre_gateway_dispatch`.
**Owner CLI**: `python3 -m paid setup / add-counterparty / status / pending / approve / reject`.
**Owner slash commands** (registered via hermes v0.11 plugin surface):
`/paid-pending /paid-approve /paid-reject /paid-status /card`.

---

## Files to read (in priority order — feel free to stop after #3)

1. **`__init__.py`** — main plugin glue.
   - `on_pre_llm_call` (~line 627) — entry. Owner short-circuit, counterparty resolution, L1 input guard, classifier → decision → context.
   - `_maybe_route_to_review_skill` (~line 498) — `/review` interception.
   - `_dispatch_review_close_to_owner` (~line 626) — splits the close-brief channel so the junior doesn't see internal findings.
   - `_alert_owner` (~line 133) + `_notify_owner_about_request` (~line 270) — owner notification paths.

2. **`paid/decision.py`** — J2 state-machine + the v1.2.5 hard-rule block.
   - `decide_action` — direct/request/decline routing.
   - `_direct_context` + `_DIRECT_HARD_RULES_{ZH,EN}` — what we put in the LLM prompt for direct-state replies.

3. **`paid/safety.py`** — L1 input regex + L4a/4b cross-cp + PII regex.
   - `detect_prompt_injection`, `detect_name_leakage`, `detect_pii`.

4. **`paid_review/api.py`** — review skill state machine + 5 public functions (`intake`, `handle_inbound`, `list_open`, `show`, `force_close`).

5. **`paid_review/prompts/four_pillar.md`** — the CSW methodology that drives the scan.

If you want the higher-level rationale: `https://github.com/jimmyag2026-prog/paid-plugin/blob/main/docs/PILOT_ONBOARDING.md` (the pilot-facing version of how this is meant to be used).

---

## 5 questions I want your conviction on

Don't try to answer all 5. **One sharp opinion on the one you have
most conviction on >> five hedged opinions.** Brutal preferred over
polite.

### Q1 · J2 three-state abstraction (direct / request / decline)

The whole product hinges on this trichotomy. Real-world IM has more
texture than 3 buckets — but more buckets = more owner cognitive
load when reviewing. Is 3 the right cut, or did I over-collapse?
What's the failure mode I'm missing?

### Q2 · Safety layer realism

L1 = input prompt-injection regex; L4a/b = output regex for
cross-counterparty name leakage + PII. Both deterministic. **What's
the biggest attack surface I'm NOT covering?** (E.g. is regex
fundamentally insufficient for L4? Is there a payload class that
walks through both?) Code: `paid/safety.py`.

### Q3 · Review skill scope — too academic?

`/review` triggers a 4-pillar CSW (Completed Staff Work) walk-through.
It's a methodology, not a chatbot loop. Does the average IM user have
the patience to answer 3-5 rounds of pillar-by-pillar questions before
getting the brief? Or is this only useful for owner ↔ direct-report
relationships, and useless for arms-length counterparties?

### Q4 · Multi-tenancy / pilot scaling

Current architecture is **one owner per hermes process**. Pilot
scaling = one Linux user per pilot on a shared VPS, each running
their own hermes. That works for 3 pilots; what breaks at 30? At 300
paying customers? Should v2 be multi-tenant (single hermes serving N
owners with per-owner identity routing), or is per-user-process
fundamentally right and I just need orchestration?

### Q5 · LLM compliance assumption

PAID's request/decline states inject `IGNORE the user question, reply
EXACTLY with: '<placeholder>'` into the prompt and trust the LLM to
comply. Dogfood (5/12) showed ~80% compliance — most replies use the
canonical template, but ~20% the LLM free-styles. The free-styled
replies happened to be benign in our small N, but the failure mode is
real. **How long does a soft-constraint design like this survive in
production?** Should we be using `pre_gateway_dispatch` to rewrite
outbound, or even forking the LLM provider call to enforce structured
output?

---

## What I'm NOT asking about (already decided, not interested in
re-litigating)

- Choice of language (Python) / Choice of framework (hermes plugin)
- Pricing / monetisation (not relevant in May)
- Migration to vector DB / RAG (intentionally deferred — see backlog)
- Multi-language i18n beyond zh/en
- Branding / naming

---

## Reach me

DM Jimmy on whatever channel you got this from. If you'd rather just
type a paragraph of unstructured reaction, that's fine too — frameworks
are scaffolding, not contracts.

Thank you 🙏

— Jimmy
