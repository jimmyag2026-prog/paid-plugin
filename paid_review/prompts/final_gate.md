# Final Gate — Form Check

The junior went through {rounds} rounds of Q&A. Now PAID re-scans the
final material to decide if the review can close, and with what verdict.

This is NOT another junior-facing step. This is PAID's own form check:
re-evaluating the 4 pillars against the **current** material (post-
junior-revisions when applicable).

## ⚠️ v0.1 IMPORTANT — no MERGE phase

PAID v0.1 does NOT support the junior revising the document inline
(MERGE phase not shipped — backlog M1.6). The junior could only mark
findings as `accepted` / `rejected` / `unresolvable` during Q&A; they
had no way to actually edit `final_document` between SCAN and this gate.

This means: **the audit's job here is to flag findings to the owner,
NOT to force junior revision before close.** Verdict criteria:

- All findings closed by junior (any of accepted / rejected /
  unresolvable) AND Intent ask is clear in the original document →
  **READY_WITH_OPEN_ITEMS** (owner will see findings in the brief
  and decide what to act on themselves).
- ≥1 finding still in `open` status (no junior response) → **FAIL**
  (junior abandoned the loop; rounds will exhaust → force-close).
- The original document is genuinely missing the Intent pillar — no
  concrete ask at all, just open-ended discussion → **FAIL** (this is
  the one case where revision would actually have been required).

**Do NOT** issue FAIL because "junior didn't revise the doc" — that
expectation was correct for v0 design but wrong for v0.1 reality.
Surface finding details in `rationale` so the owner can read them and
act; close the audit so the brief actually delivers.

## Inputs

Subject: {subject}

### Final material (junior's accepted / unresolved state)
{final_document}

### Findings status from QA
{findings_status}

## What to evaluate

For each pillar, answer pass / fail / regression:

- **Background** — given junior's edits, is `what / why now / current
  state / who cares` now clear enough to enter the decision discussion?
- **Materials** — are decision inputs complete (data with source +
  comparable cases)?
- **Framework** — are the discussion dimensions explicit; is the type
  of answer the owner is asked to give clear?
- **Intent** — **CSW gate**. Single concrete ask? Junior did all their
  own homework? Owner's next action ≤ 1 step?

## Output (strict JSON, no prose)

Return ONLY:

```json
{
  "verdict": "READY" | "READY_WITH_OPEN_ITEMS" | "FAIL",
  "csw_gate_pillar": "Intent",
  "csw_gate_status": "pass" | "fail",
  "pillar_verdict": {
    "Background": "pass" | "fail",
    "Materials":  "pass" | "fail",
    "Framework":  "pass" | "fail",
    "Intent":     "pass" | "fail"
  },
  "regressions": ["short description of any new BLOCKER not in original findings"],
  "rationale": "≤ 60 word justification of overall verdict"
}
```

## Verdict rules

- **READY**: all 4 pillars pass + no unresolved BLOCKER + Intent pass
- **READY_WITH_OPEN_ITEMS**: Intent pass + at least one IMPROVEMENT or
  unresolvable finding remains, but they go to brief §4 open_items + §6
  risks (NOT blocking close)
- **FAIL**: Intent pillar fails OR a new BLOCKER regression spotted that
  wasn't in original findings → review can't close; goes back to QA

`csw_gate_status` MUST equal `pillar_verdict.Intent`. If they
disagree your output is rejected.

Output JSON now:
