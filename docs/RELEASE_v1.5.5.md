# PAID v1.5.5 — owner-facing tooling + cost ceiling enforcement

**Released:** 2026-05-14

v1.5.5 strengthens the owner side without touching the junior-facing J2/J3/J4
hot paths. Two themes:

1. **Self-service diagnostics** — owner can answer "is PAID OK right now?" in
   one command (CLI) or one IM (slash → Lark card).
2. **Owner-facing observability** — dashboard gains progress bars, review
   session list, multi-day trend charts, master-design §6 progress card.

Junior visible behavior: **unchanged**. All existing pytest (895 baseline) +
manual_smoke (48 cases) still green; v1.5.5 adds **+76 new tests** for the
new surface.

---

## A1 · `/paid-doctor` health check (7 checks)

New module `paid/doctor.py` + CLI shell `bin/paid_doctor.py` + slash command
`/paid-doctor`.

| # | Check | Looks at |
|---|---|---|
| 1 | hermes_config | `~/.hermes/config.yaml` model.{default, base_url, api_key} (yaml or provider env var) |
| 2 | owner_json | `~/.hermes/paid/owner.json` schema_version=2 + identities |
| 3 | hermes_version | hermes_cli importable + invoke_hook present (v0.11+ proxy) |
| 4 | systemd_timers | paid-sweep / paid-review-sweep / paid-daily-snapshot all active (Linux only) |
| 5 | data_files | audit_log / cost_ledger / pending_approvals writable |
| 6 | settings_schema | confidence_threshold ∈ [0,1] / approval_timeout > 0 / soft ≤ hard cap |
| 7 | recent_errors | no fatal events in past 1h |

Slash output:
- **Lark/Feishu**: pushes an interactive card to owner's preferred identity
  (green header all-pass, red on fail). Slash reply itself is a one-liner ack.
- **TG / Slack / CLI**: plain text report with `[✓]` / `[✗]` per row, fix hint
  on each failure.

CLI:
```
python -m bin.paid_doctor
# exit 0 = all pass, 1 = any fail
```

## A2 · Cost ceiling inline enforcement (M9.4)

`paid/hermes_io.call_llm` now calls `_enforce_cost_cap` BEFORE the HTTP POST:

- If `settings.cost.enabled` AND `cap_status().daily_hard_exceeded` → raises
  `LLMCallError` with a clear "budget exhausted" message.
- One `fatal_alerts.jsonl` row written per UTC day (deduped via flag file
  `cost_cap_alerted_<YYYY-MM-DD>.flag`). Existing
  `bin/check_cost_cap.py` cron + tail watchers fire the owner IM alert.
- Fail-open: if `cap_status()` itself errors (e.g. ledger read failure),
  call_llm proceeds — cost-tracking bugs MUST NOT block PAID's J2 path.

This closes the gap noted in design/05_backlog.md M2.9 2026-05-14 重审: PAID's
`call_llm` goes direct to `/v1/chat/completions`, bypassing hermes
agent-tool-loop entirely, so pre_tool_call hook never fires on PAID's LLM
spend. Inline enforce is the only point that covers 100% of PAID-attributable
cost.

## A3 · Dashboard top-of-home metric bars + q-preview

`paid/dashboard.py` home page now opens with two visual progress bars:

- **Direct-answer rate today** vs 50% target (green ≥50% / yellow ≥30% / red).
  Marker shows the 50% goal.
- **LLM cost today** vs soft/hard caps. Soft-cap marker on bar; fills to
  hard-cap. Disabled-state notice when `settings.cost.enabled=false`.

Plus a new **"Recent activity (newest 10)"** table on home with 80-char
question preview per row. CJK-safe truncation via `_safe_truncate` that also
collapses whitespace.

## A4 · Active review session list

New collector `collect_review_sessions(include_closed)` scans
`~/.hermes/paid/review/sessions/<sid>/meta.json` (active) and
`_closed/<month>/<sid>/meta.json` (archived). Robust against missing/
corrupt meta.

New `/reviews` route shows two tables: Active + recent Closed (max 50).
Home page gets a summary "Active review sessions (N) [view all →]" section.
CP-detail pages gain a "Review sessions for this cp" history table.

Per-stage next-action mapping shown to owner:
| Stage | Next action |
|---|---|
| INTAKE | awaiting subject from junior |
| SUBJECT | scanning material |
| SCAN | preparing Q&A for junior |
| QA | awaiting junior reply |
| MERGE | merging findings |
| GATE | **awaiting your gate decision** |
| CLOSED | closed |

## A5 · Trend + cost charts (Chart.js v4)

New collector `collect_trend(days)` aggregates audit_log + cost_ledger by
UTC day. Robust against bad rows (skip silently).

New `/trends` view shows:
- 7-day decisions line chart (direct / request / decline)
- 7-day LLM cost bar chart (USD)
- 30-day decisions line chart
- 30-day LLM cost bar chart

Home page shows a compact 7-day decisions line chart.

**Chart.js v4 via jsdelivr CDN** (MIT licensed, ~60KB minified). If the CDN
is unreachable, an inline JS check shows: "Chart library failed to load — see
/api/trends.json for raw data."

New API: `/api/trends.json` returns both 7-day and 30-day series.

## A6 · Master-design §6 progress card (6 indicators)

New module `paid/metrics_progress.py` + view `/metrics-progress`:

| # | Indicator | Type |
|---|---|---|
| 1 | ≥1 pilot 走完任务全周期 | derived (junior cp + ≥1 direct response — approx, M7.1 will refine) |
| 2 | 周报自动生成 ≥2 周 | derived (count files in `weekly_reports/`) |
| 3 | 跨组织 demo | manual flag |
| 4 | Twitter 长文 + demo 视频 | manual flag |
| 5 | 5 个相关方深度私聊 | manual count 0-5 |
| 6 | README 给陌生人看 | manual flag |

Home page shows a compact "Master-design §6 progress: X/6 indicators done [details →]" badge.

Manual flags live under `settings.metrics_progress.*`:

```json
{
  "metrics_progress": {
    "cross_org_demo_done": true,
    "twitter_long_post_done": false,
    "deep_chats_count": 3,
    "readme_for_strangers_done": false
  }
}
```

No-cache — edit settings.json, refresh page, see updates.

## A7 · Platform breakdown schema (UI deferred)

`collect_summary()` now returns `platform_breakdown` mapping each platform to
`{cp_count, decisions_today, pending_count, role_counts}`. Exposed at
`/api/summary.json` for external tools / future multi-pilot UI.

No new UI in v1.5.5 — owner has 1 pilot today (JELabs). UI will land when 2nd
pilot onboards.

## Skipped in v1.5.5 (explicit)

- M2.1 progressive disclosure (collapsible card sources/reasoning) — defer until pilot ≥3 reports density friction
- M2.2 `modified` terminal state — defer, audit can derive
- M2.7 namespacing flat→nested — risk too high vs JELabs already on flat keys
- M2.9 整模块 pre_tool_call — closed (architecturally wrong target, see backlog)
- M5.1 chat_id 透传 (hermes upstream PR) — closed (prefix推断 100% reliable)
- M3.5.A/B fork hermes for TG/Slack callbacks — v1.4.0 M3.5.C lazy-hook already covers

## Track B (next: hermes upstream PRs)

Per owner decision 2026-05-14, B-track runs after A-track ship:
- **B1 M5.2** — hermes `run_agent.py:13790` post_llm_call invoke add
  `sender_id=getattr(self, "_user_id", None) or ""` (1 line)
  + PAID deletes `_SESSION_META_CACHE` (~30 lines)
- **B2 M5.3** — hermes `gateway/platforms/feishu.py:1694` send() signature add
  `receive_id_type="chat_id"` optional kwarg (5 lines)
  + PAID deletes `_send_lark_direct` / `_send_lark_card_direct` (~300 lines)

Both PRs are nothing but optional kwargs (backward compatible). Track B will
ship as v1.6 after both upstreams merge.

## Migration

None required. v1.5.5 is fully backward compatible with v1.5.4 state files.
Pilots can upgrade in place — no settings.json migration, no cp profile
migration, no data conversion.

## Test count

- v1.0.0 baseline: 179 pytest
- v1.5.4 baseline: 895 pytest, 1 skipped
- **v1.5.5: 971 pytest, 1 skipped (+76 new tests)**:
  - A1 doctor: 21 tests
  - A2 cost ceiling: 7 tests
  - A3 metric bars + q-preview: 11 tests
  - A4 review sessions: 8 tests
  - A5 trends + chart template: 9 tests
  - A6 metrics progress: 16 tests
  - A7 platform breakdown: 4 tests

## Files changed

```
A  bin/paid_doctor.py
A  paid/doctor.py
A  paid/metrics_progress.py
A  tests/test_doctor.py
A  tests/test_cost_cap_enforce.py
A  tests/test_metrics_progress.py
M  __init__.py            (+_cmd_paid_doctor, register_command)
M  paid/card_formatters.py (+format_doctor_card_lark)
M  paid/dashboard.py      (5 new collectors + 5 new routes + 3 new templates)
M  paid/hermes_io.py      (call_llm cost-cap enforce)
M  plugin.yaml            (1.5.4 → 1.5.5)
M  tests/test_dashboard.py (+34 tests A3-A7)
M  tests/test_minimum_hermes_version.py (+/paid-doctor in expected set)
```

## Acceptance checklist

The §11 manual validation list from `design/09_v1.5.5_design.md`:

- [ ] `python -m bin.paid_doctor` exit 0 (in hermes venv), 7 ✓
- [ ] `/paid-doctor` IM yields ≤30-line summary / card
- [ ] daily_hard_cap_usd=$0.01 → next junior IM triggers raise + owner alert
- [ ] `/` shows 2 metric bars + Recent activity + Active review sessions + 7-day trend
- [ ] Start a /review session not closed → home shows count, /reviews shows detail
- [ ] /trends 7-day + 30-day double-view loads charts
- [ ] /metrics-progress shows 6 cards (at least 1 derived row has real signal)
- [ ] settings.json metrics_progress.cross_org_demo_done=true → refresh shows ✓
- [ ] J2 three-state regression still works (cp direct/request/decline unchanged)
- [ ] sweep / snapshot timers fire one cycle without error
