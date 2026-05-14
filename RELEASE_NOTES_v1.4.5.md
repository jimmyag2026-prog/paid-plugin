# v1.4.5 — per-cp `blacklist_action` (architectural fix; retires v1.4.1 surgical patch)

The proper architectural fix that retires v1.4.1's classifier-prompt patch. Owners can now choose per-counterparty what happens when the classifier flags a topic as blacklisted — without trading away the LLM safety-net heuristic.

## Backstory

JELabs pilot 2026-05-13 surfaced a semantic mismatch in PAID's blacklist routing:

- PAID's design: `is_blacklisted=True` → state=`decline` → tell cp "I'm not authorized; please contact owner directly". Owner DM is not notified; cp must re-ping.
- XiaEvie's intent ("forward all high-risk to me personally"): expected approval card in her own DM. Status quo deflected to cp instead.

v1.4.1 hacked around this by removing the classifier's "or is sensitive (legal/financial/HR/equity)" LLM-judged fallback, then asking owners to empty `topics_always_escalate`. That worked tactically but broke the safety net for owners who DON'T configure `topics_always_escalate` explicitly.

## What v1.4.5 does

### 1. Restores the classifier safety net

`paid/classifier.py:88` reverts to the pre-v1.4.1 wording:

```
"is_blacklisted": boolean,  // true if topic matches counterparty.topics_always_escalate or is sensitive (legal/financial/HR/equity)
```

The LLM is once again a backstop guardrail for owners who haven't filled out `topics_always_escalate`.

### 2. Adds per-cp `blacklist_action` setting (`paid/identity.py`)

`Counterparty` dataclass gains a new field:

```python
blacklist_action: str = "decline"   # "decline" | "request"
```

- **`"decline"`** (default, pre-v1.4.5 behavior): tell cp "contact owner directly". Owner DM not notified.
- **`"request"`** (JELabs's preference): escalate to owner via approval card. Owner sees the question in their DM, cp gets the "I'll forward this" placeholder.

### 3. Wires routing in `decide_action` (`paid/decision.py`)

```python
if is_blacklisted:
    action_pref = getattr(counterparty, "blacklist_action", "decline")
    if action_pref == "request":
        return Action(state="request", reason="blacklisted topic, escalating to owner per cp.blacklist_action=request")
    return Action(state="decline", reason="counterparty blacklisted topic")
```

Defensive: unknown values (`"bogus"`, `None`) fall back to `"decline"`. Case-insensitive.

### 4. CLI flag (`paid/cli.py`)

```bash
python3 -m paid add-counterparty feishu <user_id> \
    --name "JE Labs" \
    --role junior \
    --topic-allow scheduling --topic-allow logistics \
    --blacklist-action request    # ← new in v1.4.5
```

### 5. Migration (`bin/migrate_cp_profiles.py`)

The migration default for `blacklist_action` is `"decline"`. Existing v1.4.4 profiles missing this field get the safe default backfilled.

Re-run on already-v1.4.5 profiles is a no-op.

## Upgrade path for the JELabs pilot

Two ways to apply v1.4.5 to the live `paid-jelabs` instance:

**Quick way** — patch the cp profiles directly:

```python
# In each counterparties/feishu_<id>/profile.json:
"blacklist_action": "request"
```

**Clean way** — run the migration script, then set per-cp:

```bash
~/.hermes/plugins/paid-v1/bin/migrate_cp_profiles.py
~/.hermes/hermes-agent/venv/bin/python -m paid add-counterparty feishu 94d6c5be \
    --blacklist-action request \
    ... (other flags)
```

After this, the v1.4.1 surgical patch (the classifier prompt line removal) can be REVERTED on `paid-jelabs` because the cp profile config does the equivalent — and now the safety net is back for everyone else.

## Tests

- 653 passing (+10 vs v1.4.4's 643)
- New coverage:
  - 5 tests for `decide_action` routing (decline default / request escalation / missing field / invalid value / case-insensitive)
  - 4 tests for `Counterparty` schema (default value / load missing field / load with request / round-trip)
  - 1 test for migration (v1.4.4 profile backfills `blacklist_action: "decline"`)

## Backwards compatibility

- Existing v1.4.4 cp profiles without the field load with default `"decline"` (= pre-v1.4.5 behavior)
- No breaking changes to wire format / hooks / commands
- The classifier prompt revert restores the broader safety net — owners who explicitly want the previous-v1.4.1 "only explicit topics_always_escalate" behavior can achieve it by setting `topics_always_escalate=[]` AND `blacklist_action=request` per cp; the LLM may still flag a topic, but it routes to approval card so the owner sees it
