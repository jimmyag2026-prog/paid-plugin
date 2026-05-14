# PAID v1.5.6 — review-driven patch

**Released:** 2026-05-14
**Triggered by:** Two independent reviews of v1.5.5 (Claude-style structured
findings, see paid-v1.5.5-review pack). Five issues acted on, two reviewer
suggestions verified and rejected as erroneous.

No new user-facing features — all changes harden v1.5.5's owner surface and
fix one UX gap surfaced by review.

## Fixes

### 🔵 review-fix #3 — junior UX on budget exhausted (high impact)

**Before:** When `daily_hard_cap_usd` was exhausted, `call_llm` would raise
`LLMCallError` inside `classifier.classify` → classifier's broad
`except Exception` → fallback `Classification(reasoning="[fallback]...")` →
`decision.decide_action` routes to `state=request` → **owner gets flooded
with J3 approval cards for every junior message during cap-hot window.**

**After:** `__init__.on_pre_llm_call` now reads `cost.cap_status()` BEFORE
calling classifier. When `enabled` AND `daily_hard_exceeded`, returns the
wrap directive that makes hermes' main agent reply with:

```
系统暂时不可用，请稍后再试。
System temporarily unavailable, please try again later.
```

Owner still gets the one-per-day `fatal_alerts.jsonl` row via the inline
enforce in `hermes_io._enforce_cost_cap`. cp gets an honest "try again
later" instead of a confusing "your message went to approval."

Fail-open: if `cap_status()` itself crashes, normal flow runs (the inline
enforce inside `call_llm` is the safety net).

The cap-blocked event is also audited with `extra.blocked_by="cost_cap_exceeded"`
so it surfaces in `/audit` and `/api/summary.json`.

Tests: `tests/test_pre_llm_cost_cap_wrap.py` (5 cases — wrap returned,
audit row written, normal flow under cap, fail-open on ledger error, no-op
when cap disabled).

### 🔵 review-fix #2 — vendor Chart.js (kill CDN dependency)

**Before:** `paid/dashboard.py` `_BASE_HEAD` loaded Chart.js v4 from
`cdn.jsdelivr.net`. Acceptable for `127.0.0.1:7777` (loopback only), but the
design `09_v1.5.5_design.md` §12.3 plans deployment via Caddy reverse-proxy
to `jimmyresearch.com/paid/<pilot>` — at that point a compromised CDN
edge node could inject JS into the dashboard's same-origin context (cp
identities, session state, cost data all exposed). Also broke offline /
firewalled environments (air travel, corp wifi).

**After:** Chart.js 4.4.0 UMD min vendored at `paid/static/chart.umd.min.js`
(~200KB). Flask app configured `static_folder=str(Path(__file__).parent /
"static")` + `static_url_path="/static"`. Script tag changed to
`<script src="/static/chart.umd.min.js"></script>`. Same-origin, no SRI
needed.

(Note: one reviewer suggested adding an `integrity=sha384-...` SRI hash
inline. The hash they provided was **hallucinated** — independent
`shasum -a 384` on the actual CDN file gives a different value. Vendoring
sidesteps the problem entirely; no hash to keep in sync.)

Tests: `test_chartjs_vendored_file_exists` (file present + size sanity
+ first-500 bytes look like Chart.js).

### 🔵 review-fix #1 — flag-file 30-day lazy sweep

**Before:** `_maybe_alert_cost_cap_once` wrote
`cost_cap_alerted_YYYY-MM-DD.flag` per day and never cleaned up. After a
year of hitting cap daily → ~365 empty files in `~/.hermes/paid/`,
cluttering directory listings and degrading some filesystems' listdir
performance with thousands of entries.

**After:** When today's flag is created, opportunistically sweep flags
whose date is more than 30 days old. 30-day retention preserves the
forensic question "when did we last hit cap?" for the recent month while
preventing indefinite accretion.

Sweep is best-effort + double-guarded (internal try/except on each unlink
+ outer try/except at the call site) so a future refactor that breaks the
sweep can't break the alert path.

Tests: 4 new in `tests/test_cost_cap_enforce.py` — unlinks old, leaves
recent + unrelated files, integration through `_enforce_cost_cap`, raise
survives sweep crash.

### 🟢 review-fix #4 — cross-module contract comment

`paid_review/core/state.py` Stage enum now has a comment pointing at the
mirror mapping in `paid/dashboard.py::_derive_review_next_action` so adding
a new stage forces a visible decision about the owner-facing label.

(Verified: the current dashboard fallback already shows the lowercased raw
stage name when an unmapped value arrives, so the "silent degradation" the
reviewer worried about doesn't happen — but the contract comment helps
future-me notice the mirror sites.)

### 🟢 review-fix #5 — `_safe_truncate` type hint widened

`def _safe_truncate(text: str, ...)` → `def _safe_truncate(text: str | None, ...)`.
The implementation already handled `None` (per `test_safe_truncate_empty`);
the hint just lagged.

### 🟢 review-fix #7 — platform_breakdown intentionality comment

Added a comment confirming `cp_count` deliberately includes ignored/blocked
cps so the breakdown shows total platform footprint. `role_counts` already
exposes the breakdown for callers that want active-only.

## Reviewer suggestions verified and rejected

For accountability, two reviewer suggestions were verified false:

| # | Suggestion | Reality | Decision |
|---|---|---|---|
| R1-#2 | "Use SRI hash `sha384-MBofN7SI1cBxCSYrBSzO+EJd0RnRw5gH8+P7JJtXIKUKAG/WgPNbW5B0kCkGvtmR`" | Actual hash via `shasum -a 384` is `sha384-e6nUZLBkQ86NJ6TVVKAeSaK8jWa3NhkYWZFomE39AvDbQWeie9PlQqM3pmYW5d1g` — reviewer hallucinated the value | Rejected. Vendored the file instead (no SRI needed) |
| R1-#4 | "Missing DELIVER stage in dashboard mapping" | `paid_review/core/state.py:12` defines `Stage = Literal["INTAKE", "SUBJECT", "SCAN", "QA", "MERGE", "GATE", "CLOSED"]` — **DELIVER is a module name (`paid_review/core/deliver.py`), not a state**. Reviewer confused them | Rejected. Stage list is complete |

Meta-lesson: **always independently verify any specific value (hash,
enum, line number) from a code reviewer** before applying. Both reviewers
were broadly trustworthy on direction; reviewer 1 hallucinated two
specifics that would have introduced real bugs (SRI hash → Chart.js never
loads, fake DELIVER mapping → dead code).

## Reviewer suggestions deferred to v1.6

- `_compute_platform_breakdown` `log.debug` on unknown cp_id — defer; low
  signal until multi-pilot.

## Test count

```
v1.5.4 baseline:    895 passed, 1 skipped
v1.5.5:             971 passed, 1 skipped   (+76)
v1.5.6 (this rev):  981 passed, 1 skipped   (+10)
```

New tests:
- `tests/test_pre_llm_cost_cap_wrap.py`: 5
- `tests/test_cost_cap_enforce.py`: +4 (sweep cases)
- `tests/test_dashboard.py`: +1 (`test_chartjs_vendored_file_exists`)

## Files changed

```
A  docs/RELEASE_v1.5.6.md
A  paid/static/chart.umd.min.js          (vendored, 200KB)
A  tests/test_pre_llm_cost_cap_wrap.py
M  __init__.py                            (cap-status fast-check in on_pre_llm_call)
M  paid/hermes_io.py                      (_sweep_old_cost_cap_flags + integration)
M  paid/dashboard.py                      (Flask static_folder + script src to /static/ + comments)
M  paid_review/core/state.py              (contract comment on Stage enum)
M  plugin.yaml                            (1.5.5 → 1.5.6)
M  tests/test_cost_cap_enforce.py         (+4 sweep tests)
M  tests/test_dashboard.py                (CDN check → vendored check + vendored file test)
```

## Migration

None. v1.5.6 is fully backward compatible with v1.5.5. The vendored
Chart.js is loaded the same way (just from local `/static/` rather than
remote CDN). The cost-cap fast-check is purely additive. Flag-file sweep
operates only on existing files; no data conversion.

Owners upgrading from v1.5.5 → v1.5.6 see:
- Dashboard loads charts even when offline / behind a firewall
- When daily cost cap is hit, junior gets honest "system unavailable"
  instead of every message turning into an approval request
- Old cap-alert flag files (if any) get cleaned up the next time cap is
  hit
