"""Tests for paid.dashboard — data helpers (no Flask needed)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from paid import dashboard, identity, storage  # noqa: E402


def _today_iso(hour: int = 12) -> str:
    today = datetime.now(timezone.utc).date()
    return datetime(today.year, today.month, today.day, hour, tzinfo=timezone.utc).isoformat()


def _yesterday_iso() -> str:
    y = datetime.now(timezone.utc) - timedelta(days=2)
    return y.isoformat()


def test_collect_summary_empty(paid_tmp):
    s = dashboard.collect_summary()
    assert s["total_decisions_today"] == 0
    assert s["direct_rate_today"] == 0
    assert s["pending_count"] == 0


def test_collect_summary_counts_today_only(paid_tmp):
    audit = storage.PAID_DIR / "audit_log.jsonl"
    rows = [
        {"ts": _today_iso(9), "action": {"state": "direct"}, "extra": {}},
        {"ts": _today_iso(10), "action": {"state": "direct"}, "extra": {}},
        {"ts": _today_iso(11), "action": {"state": "request"}, "extra": {}},
        {"ts": _today_iso(12), "action": {"state": "decline"}, "extra": {}},
        {"ts": _yesterday_iso(), "action": {"state": "direct"}, "extra": {}},
    ]
    audit.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    s = dashboard.collect_summary()
    assert s["total_decisions_today"] == 4
    assert s["direct_today"] == 2
    assert s["request_today"] == 1
    assert s["decline_today"] == 1
    assert s["direct_rate_today"] == 50.0


def test_collect_summary_counts_l1_l4_fallback(paid_tmp):
    rows = [
        {"ts": _today_iso(9), "action": {"state": "decline"},
         "extra": {"blocked_by": "layer_1_prompt_injection"}},
        {"ts": _today_iso(10), "action": {"state": "direct"},
         "extra": {"l4_ok": False, "l4_pii": ["email"]}},
        {"ts": _today_iso(11), "action": {"state": "request"},
         "extra": {"fallback": True}},
    ]
    (storage.PAID_DIR / "audit_log.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    s = dashboard.collect_summary()
    assert s["l1_hits_today"] == 1
    assert s["l4_hits_today"] == 1
    assert s["classifier_fallback_today"] == 1


def test_counterparty_health_dot_buckets():
    now = datetime.now(timezone.utc)
    assert dashboard._counterparty_health(now - timedelta(hours=1)) == "🟢"
    assert dashboard._counterparty_health(now - timedelta(days=3)) == "🟡"
    assert dashboard._counterparty_health(now - timedelta(days=20)) == "⚪"
    assert dashboard._counterparty_health(None) == "⚪"


def test_collect_counterparties_orders_by_last_seen(paid_tmp):
    identity.ensure_counterparty("feishu", "user_a", display_name="A")
    identity.ensure_counterparty("feishu", "user_b", display_name="B")

    audit = storage.PAID_DIR / "audit_log.jsonl"
    rows = [
        {"ts": _today_iso(9), "counterparty": "feishu_user_a"},
        {"ts": _today_iso(10), "counterparty": "feishu_user_b"},
        {"ts": _today_iso(11), "counterparty": "feishu_user_b"},
    ]
    audit.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    cps = dashboard.collect_counterparties()
    assert [c["cp_id"] for c in cps[:2]] == ["feishu_user_b", "feishu_user_a"]
    b = cps[0]
    assert b["msg_count"] == 2
    assert b["health"] == "🟢"


def test_collect_audit_for_date_filters_to_specified_day(paid_tmp):
    rows = [
        {"ts": _today_iso(9), "action": {"state": "direct"}},
        {"ts": _yesterday_iso(), "action": {"state": "request"}},
    ]
    (storage.PAID_DIR / "audit_log.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    used, picked = dashboard.collect_audit_for_date(None)
    assert used == datetime.now(timezone.utc).date().isoformat()
    assert len(picked) == 1
    assert picked[0]["action"]["state"] == "direct"


# ---------------------------------------------------------------------------
# v1.5.5 A3 — metric bars + q-preview tests
# ---------------------------------------------------------------------------

def test_collect_summary_includes_cost_fields(paid_tmp):
    """A3: summary now exposes cost_today_usd / soft/hard caps / enabled."""
    s = dashboard.collect_summary()
    # Fields must always be present (numeric / bool), even on empty install
    assert "cost_today_usd" in s
    assert "cost_soft_cap_usd" in s
    assert "cost_hard_cap_usd" in s
    assert "cost_cap_enabled" in s
    assert isinstance(s["cost_today_usd"], float)
    assert isinstance(s["cost_soft_cap_usd"], float)
    assert isinstance(s["cost_hard_cap_usd"], float)
    assert isinstance(s["cost_cap_enabled"], bool)
    # direct_rate_target master design §6
    assert s["direct_rate_target"] == 50


def test_collect_summary_cost_reads_real_ledger(paid_tmp):
    """When cost_ledger has entries, today_usd should reflect them."""
    today_iso = datetime.now(timezone.utc).date().isoformat()
    ledger = paid_tmp / "cost_ledger.jsonl"
    ledger.write_text(json.dumps({
        "ts": _today_iso(10),
        "date": today_iso,
        "cost_usd": 1.25,
        "model": "deepseek-v4-flash",
    }) + "\n", encoding="utf-8")
    s = dashboard.collect_summary()
    assert s["cost_today_usd"] >= 1.25 - 1e-6


def test_safe_truncate_short_passthrough():
    assert dashboard._safe_truncate("hello", 80) == "hello"


def test_safe_truncate_collapses_whitespace():
    assert dashboard._safe_truncate("a\n\nb   c", 80) == "a b c"


def test_safe_truncate_appends_ellipsis_when_over():
    long_text = "x" * 200
    out = dashboard._safe_truncate(long_text, 50)
    assert len(out) == 50
    assert out.endswith("…")


def test_safe_truncate_cjk_safe():
    # 50 CJK chars are 50 code points; truncate at 30 yields 29 + …
    s = "你好世界" * 20  # 80 chars
    out = dashboard._safe_truncate(s, 30)
    assert len(out) == 30
    assert out.endswith("…")
    # No mojibake: all chars are valid CJK
    assert all(ord(c) >= 0x4E00 or c == "…" for c in out)


def test_safe_truncate_empty():
    assert dashboard._safe_truncate("", 10) == ""
    assert dashboard._safe_truncate(None, 10) == ""  # defensive


def test_collect_recent_activity_empty(paid_tmp):
    assert dashboard.collect_recent_activity() == []


def test_collect_recent_activity_orders_newest_first(paid_tmp):
    audit = paid_tmp / "audit_log.jsonl"
    rows = [
        {"ts": _today_iso(8), "counterparty": "cp_a",
         "action": {"state": "direct"},
         "classification": {"topic": "policy"},
         "junior_msg": "  early question  "},
        {"ts": _today_iso(11), "counterparty": "cp_b",
         "action": {"state": "request"},
         "classification": {"topic": "scope"},
         "junior_msg": "later question " * 10},  # > 80 chars
    ]
    audit.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    out = dashboard.collect_recent_activity(limit=10)
    assert len(out) == 2
    # Newest first
    assert out[0]["cp"] == "cp_b"
    assert out[1]["cp"] == "cp_a"
    # q_preview truncated + collapsed
    assert "  " not in out[1]["q_preview"]  # whitespace collapsed
    assert len(out[0]["q_preview"]) <= 80
    # State + topic present
    assert out[0]["state"] == "request"
    assert out[1]["topic"] == "policy"


def test_collect_recent_activity_respects_limit(paid_tmp):
    audit = paid_tmp / "audit_log.jsonl"
    rows = [
        {"ts": _today_iso(8 + i), "counterparty": f"cp_{i}",
         "action": {"state": "direct"}, "junior_msg": "q"}
        for i in range(15)
    ]
    audit.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    out = dashboard.collect_recent_activity(limit=5)
    assert len(out) == 5


# ---------------------------------------------------------------------------
# v1.5.5 A4 — review session collector tests
# ---------------------------------------------------------------------------

def _make_meta(sid: str, **kw):
    """Helper: write meta.json for a review session at <sessions>/<sid>/."""
    sessions_root = storage.PAID_DIR / "review" / "sessions"
    sid_dir = sessions_root / sid
    sid_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "sid": sid,
        "schema_version": 2,
        "cp_id": kw.get("cp_id", "cp_test"),
        "platform": kw.get("platform", "feishu"),
        "stage": kw.get("stage", "QA"),
        "verdict": kw.get("verdict", "PENDING"),
        "rounds": kw.get("rounds", 1),
        "max_rounds": kw.get("max_rounds", 3),
        "created_at": kw.get("created_at", _today_iso(9)),
        "updated_at": kw.get("updated_at", _today_iso(10)),
        "last_event_kind": kw.get("last_event_kind", ""),
    }
    if kw.get("closed_at"):
        payload["closed_at"] = kw["closed_at"]
    (sid_dir / "meta.json").write_text(json.dumps(payload), encoding="utf-8")
    return sid_dir


def _make_archived_meta(sid: str, month: str = "2026-05", **kw):
    sessions_root = storage.PAID_DIR / "review" / "sessions"
    sid_dir = sessions_root / "_closed" / month / sid
    sid_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "sid": sid,
        "schema_version": 2,
        "cp_id": kw.get("cp_id", "cp_archived"),
        "platform": "feishu",
        "stage": "CLOSED",
        "verdict": kw.get("verdict", "READY"),
        "rounds": 3,
        "max_rounds": 3,
        "created_at": kw.get("created_at", _today_iso(5)),
        "updated_at": kw.get("updated_at", _today_iso(8)),
        "closed_at": kw.get("closed_at", _today_iso(8)),
    }
    (sid_dir / "meta.json").write_text(json.dumps(payload), encoding="utf-8")


def test_collect_review_sessions_empty(paid_tmp):
    assert dashboard.collect_review_sessions() == []
    assert dashboard.collect_review_sessions(include_closed=True) == []


def test_collect_review_sessions_active_only_by_default(paid_tmp):
    _make_meta("sid_active_1", stage="QA")
    _make_meta("sid_active_2", stage="GATE")
    _make_archived_meta("sid_closed_1")
    rows = dashboard.collect_review_sessions()
    sids = {r["sid"] for r in rows}
    assert sids == {"sid_active_1", "sid_active_2"}
    # is_closed flag false for active
    assert all(not r["is_closed"] for r in rows)


def test_collect_review_sessions_include_closed(paid_tmp):
    _make_meta("sid_active", stage="QA")
    _make_archived_meta("sid_closed")
    rows = dashboard.collect_review_sessions(include_closed=True)
    sids = {r["sid"] for r in rows}
    assert sids == {"sid_active", "sid_closed"}
    closed = next(r for r in rows if r["sid"] == "sid_closed")
    assert closed["is_archived"] is True
    assert closed["is_closed"] is True


def test_collect_review_sessions_skips_corrupt_meta(paid_tmp):
    _make_meta("sid_good", stage="SCAN")
    # Write a session dir with bad json
    bad_dir = storage.PAID_DIR / "review" / "sessions" / "sid_bad"
    bad_dir.mkdir(parents=True)
    (bad_dir / "meta.json").write_text("{not valid json", encoding="utf-8")
    # And one with no meta.json at all
    (storage.PAID_DIR / "review" / "sessions" / "sid_empty").mkdir()

    rows = dashboard.collect_review_sessions()
    sids = {r["sid"] for r in rows}
    assert sids == {"sid_good"}


def test_collect_review_sessions_sorted_newest_first(paid_tmp):
    _make_meta("sid_old", updated_at=_today_iso(8))
    _make_meta("sid_new", updated_at=_today_iso(11))
    _make_meta("sid_mid", updated_at=_today_iso(10))
    rows = dashboard.collect_review_sessions()
    assert [r["sid"] for r in rows] == ["sid_new", "sid_mid", "sid_old"]


def test_collect_review_sessions_limit(paid_tmp):
    for i in range(10):
        _make_meta(f"sid_{i}", updated_at=_today_iso(8 + i % 12))
    rows = dashboard.collect_review_sessions(limit=3)
    assert len(rows) == 3


def test_derive_review_next_action_covers_all_stages():
    # Just spot-check a few + smoke unknown
    assert "subject" in dashboard._derive_review_next_action("INTAKE", "")
    assert "Q&A" in dashboard._derive_review_next_action("SCAN", "") or \
           "junior" in dashboard._derive_review_next_action("SCAN", "")
    assert "junior" in dashboard._derive_review_next_action("QA", "")
    assert "gate" in dashboard._derive_review_next_action("GATE", "")
    assert dashboard._derive_review_next_action("CLOSED", "") == "closed"
    # Unknown stage shouldn't crash
    out = dashboard._derive_review_next_action("WEIRD", "")
    assert isinstance(out, str)


def test_collect_review_sessions_ignores_underscore_closed_at_top_level(paid_tmp):
    """The _closed dir shouldn't be enumerated as if it were a session."""
    # Create _closed dir without an active session under it
    closed_dir = storage.PAID_DIR / "review" / "sessions" / "_closed"
    closed_dir.mkdir(parents=True)
    # No real session inside
    rows = dashboard.collect_review_sessions(include_closed=False)
    assert rows == []


# ---------------------------------------------------------------------------
# v1.5.5 A5 — trend collector tests
# ---------------------------------------------------------------------------

def test_collect_trend_default_7_days_empty(paid_tmp):
    t = dashboard.collect_trend()
    assert t["days"] == 7
    assert len(t["labels"]) == 7
    assert len(t["direct"]) == 7
    assert len(t["request"]) == 7
    assert len(t["decline"]) == 7
    assert len(t["cost_usd"]) == 7
    assert all(v == 0 for v in t["direct"])
    assert all(v == 0.0 for v in t["cost_usd"])
    assert t["totals"]["direct"] == 0
    assert t["totals"]["cost_usd"] == 0.0


def test_collect_trend_labels_are_chronological(paid_tmp):
    t = dashboard.collect_trend(days=7)
    today_iso = datetime.now(timezone.utc).date().isoformat()
    # Newest label is today (UTC)
    assert t["labels"][-1] == today_iso
    # Strictly ascending
    assert t["labels"] == sorted(t["labels"])


def test_collect_trend_buckets_audit_by_utc_day(paid_tmp):
    audit = paid_tmp / "audit_log.jsonl"
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    rows = [
        # 2 direct today + 1 request today
        {"ts": datetime(today.year, today.month, today.day, 9, tzinfo=timezone.utc).isoformat(),
         "action": {"state": "direct"}},
        {"ts": datetime(today.year, today.month, today.day, 10, tzinfo=timezone.utc).isoformat(),
         "action": {"state": "direct"}},
        {"ts": datetime(today.year, today.month, today.day, 11, tzinfo=timezone.utc).isoformat(),
         "action": {"state": "request"}},
        # 1 decline yesterday
        {"ts": datetime(yesterday.year, yesterday.month, yesterday.day, 15, tzinfo=timezone.utc).isoformat(),
         "action": {"state": "decline"}},
    ]
    audit.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    t = dashboard.collect_trend(days=7)
    # Today is last index
    assert t["direct"][-1] == 2
    assert t["request"][-1] == 1
    assert t["decline"][-1] == 0
    # Yesterday is second-to-last
    assert t["decline"][-2] == 1
    assert t["direct"][-2] == 0
    # Totals match
    assert t["totals"]["direct"] == 2
    assert t["totals"]["request"] == 1
    assert t["totals"]["decline"] == 1


def test_collect_trend_buckets_cost_by_date_field(paid_tmp):
    ledger = paid_tmp / "cost_ledger.jsonl"
    today_iso = datetime.now(timezone.utc).date().isoformat()
    yesterday_iso = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    rows = [
        {"ts": "ignored", "date": today_iso, "cost_usd": 1.50},
        {"ts": "ignored", "date": today_iso, "cost_usd": 0.25},
        {"ts": "ignored", "date": yesterday_iso, "cost_usd": 2.0},
    ]
    ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    t = dashboard.collect_trend(days=7)
    assert abs(t["cost_usd"][-1] - 1.75) < 1e-6
    assert abs(t["cost_usd"][-2] - 2.0) < 1e-6
    assert abs(t["totals"]["cost_usd"] - 3.75) < 1e-6


def test_collect_trend_handles_bad_rows(paid_tmp):
    audit = paid_tmp / "audit_log.jsonl"
    today = datetime.now(timezone.utc).date()
    rows = [
        {"ts": "not-a-date", "action": {"state": "direct"}},      # bad ts → skip
        {"ts": "ignored", "action": "not-a-dict"},                  # bad action → skip
        {"ts": datetime(today.year, today.month, today.day, 9, tzinfo=timezone.utc).isoformat(),
         "action": {"state": "direct"}},                            # good
    ]
    audit.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    ledger = paid_tmp / "cost_ledger.jsonl"
    bad_ledger = [
        {"date": "not-a-date", "cost_usd": 5.0},
        {"date": today.isoformat(), "cost_usd": "not-a-float"},
        {"date": today.isoformat(), "cost_usd": 0.50},
    ]
    ledger.write_text("\n".join(json.dumps(r) for r in bad_ledger) + "\n", encoding="utf-8")

    t = dashboard.collect_trend(days=7)
    assert t["direct"][-1] == 1
    assert abs(t["cost_usd"][-1] - 0.50) < 1e-6


def test_collect_trend_out_of_window_ignored(paid_tmp):
    audit = paid_tmp / "audit_log.jsonl"
    ten_days_ago = datetime.now(timezone.utc) - timedelta(days=10)
    rows = [
        {"ts": ten_days_ago.isoformat(), "action": {"state": "direct"}},
    ]
    audit.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    t = dashboard.collect_trend(days=7)
    assert t["totals"]["direct"] == 0
    t30 = dashboard.collect_trend(days=30)
    assert t30["totals"]["direct"] == 1


def test_collect_trend_30_days(paid_tmp):
    t = dashboard.collect_trend(days=30)
    assert t["days"] == 30
    assert len(t["labels"]) == 30


def test_collect_trend_min_1_day(paid_tmp):
    t = dashboard.collect_trend(days=0)
    assert t["days"] == 1
    assert len(t["labels"]) == 1


def test_trends_template_renders(paid_tmp):
    try:
        from jinja2 import Template
    except ImportError:
        import pytest
        pytest.skip("jinja2 not installed in this test env")
    tpl = Template(dashboard._TRENDS_TEMPLATE)
    out = tpl.render(t7=dashboard.collect_trend(7), t30=dashboard.collect_trend(30))
    assert "7-day decisions" in out
    assert "30-day decisions" in out
    assert "/static/chart.umd.min.js" in out  # v1.5.6: vendored, no CDN
    assert "trend7Decisions" in out
    assert "trend30Cost" in out


def test_chartjs_vendored_file_exists():
    """v1.5.6 review fix #2: Chart.js must be vendored at paid/static/. Catches
    a regression where someone deletes the file or moves the static dir."""
    from paid import dashboard as _d
    static_file = Path(_d.__file__).resolve().parent / "static" / "chart.umd.min.js"
    assert static_file.exists(), f"missing vendored Chart.js at {static_file}"
    # Sanity check it actually contains Chart.js (catches truncation / replaced-by-html)
    head = static_file.read_text(encoding="utf-8", errors="replace")[:500]
    assert "Chart" in head or "chartjs" in head.lower()
    # Size sanity: Chart.js 4.4.0 UMD min is ~200KB; flag clearly broken files
    assert static_file.stat().st_size > 50_000


# ---------------------------------------------------------------------------
# v1.5.5 A7 — platform_breakdown schema tests
# ---------------------------------------------------------------------------

def _write_cp_for_platform(paid_tmp, platform, user_id, role="junior"):
    """Helper: write a counterparty profile that identity.list_all_counterparties picks up."""
    cp_id = f"{platform}_{user_id}"
    d = storage.PAID_DIR / "counterparties" / cp_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "profile.json").write_text(json.dumps({
        "schema_version": 2,
        "cp_id": cp_id,
        "platform": platform,
        "user_id": user_id,
        "display_name": cp_id,
        "role": role,
        "topics_allowed": [],
        "topics_always_escalate": [],
        "web_search_allowed": True,
        "blacklist_action": "decline",
        "ignore_reason": "",
        "ignore_set_at": "",
        "discovery_notified_at": "",
        "active_review_session": "",
        "review_history": [],
        "msg_count": 0,
        "first_seen": _today_iso(8),
        "last_seen": _today_iso(10),
    }), encoding="utf-8")
    return cp_id


def test_platform_breakdown_empty_when_no_cps(paid_tmp):
    s = dashboard.collect_summary()
    assert s["platform_breakdown"] == {}


def test_platform_breakdown_counts_cps_per_platform(paid_tmp):
    _write_cp_for_platform(paid_tmp, "feishu", "ou_a")
    _write_cp_for_platform(paid_tmp, "feishu", "ou_b", role="external")
    _write_cp_for_platform(paid_tmp, "telegram", "tg_c")

    s = dashboard.collect_summary()
    pb = s["platform_breakdown"]
    assert set(pb.keys()) == {"feishu", "telegram"}
    assert pb["feishu"]["cp_count"] == 2
    assert pb["telegram"]["cp_count"] == 1
    # role_counts breakdown
    assert pb["feishu"]["role_counts"] == {"junior": 1, "external": 1}
    assert pb["telegram"]["role_counts"] == {"junior": 1}


def test_platform_breakdown_counts_today_decisions(paid_tmp):
    cp_lark = _write_cp_for_platform(paid_tmp, "feishu", "ou_a")
    cp_tg = _write_cp_for_platform(paid_tmp, "telegram", "tg_b")

    audit = paid_tmp / "audit_log.jsonl"
    rows = [
        # Explicit platform field
        {"ts": _today_iso(9), "platform": "feishu",
         "counterparty": cp_lark, "action": {"state": "direct"}},
        # Older row without platform — falls back to cp lookup
        {"ts": _today_iso(10), "counterparty": cp_lark,
         "action": {"state": "request"}},
        {"ts": _today_iso(11), "counterparty": cp_tg,
         "action": {"state": "direct"}},
    ]
    audit.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    s = dashboard.collect_summary()
    pb = s["platform_breakdown"]
    assert pb["feishu"]["decisions_today"] == 2
    assert pb["telegram"]["decisions_today"] == 1


def test_platform_breakdown_robust_to_unknown_platform(paid_tmp):
    """Audit row pointing at a cp that doesn't exist anymore shouldn't crash
    or create a phantom platform key."""
    audit = paid_tmp / "audit_log.jsonl"
    audit.write_text(json.dumps({
        "ts": _today_iso(10),
        "counterparty": "feishu_ou_ghost",  # no profile
        "action": {"state": "direct"},
    }) + "\n", encoding="utf-8")
    s = dashboard.collect_summary()
    # No cps registered, no platform → empty breakdown
    assert s["platform_breakdown"] == {}


def test_home_template_renders_with_progress_bars(paid_tmp):
    """A3 smoke: home template must render with the new metric bars + recent
    activity section, without raising on either zero-data or full-data shape.

    We bypass Flask (not installed in test env) and render the template
    directly via Jinja2 (a paid dependency: Jinja comes via Flask BUT also
    via standalone usage). On systems without Jinja, skip — that just means
    Flask runtime isn't available either."""
    try:
        from jinja2 import Template
    except ImportError:
        import pytest
        pytest.skip("jinja2 not installed in this test env")

    tpl = Template(dashboard._HOME_TEMPLATE)

    s_empty = dashboard.collect_summary()
    out = tpl.render(
        s=s_empty, cps=[], pending=[], recent=[], reviews_active=[],
        trend7=dashboard.collect_trend(7), mp_done=0, mp_total=6,
    )
    assert "Direct-answer rate today" in out
    # Either cost bar OR disabled note must render (depends on settings)
    assert "LLM cost today" in out
    assert "barwrap" in out
    assert "Recent activity" in out

    # With data
    s_with = dict(s_empty)
    s_with["direct_rate_today"] = 67.5
    s_with["cost_today_usd"] = 3.5
    s_with["cost_soft_cap_usd"] = 5.0
    s_with["cost_hard_cap_usd"] = 20.0
    s_with["cost_cap_enabled"] = True
    recent = [
        {"ts": _today_iso(10), "cp": "cp_a", "state": "direct",
         "topic": "policy", "q_preview": "hello world"},
    ]
    out2 = tpl.render(
        s=s_with, cps=[], pending=[], recent=recent, reviews_active=[],
        trend7=dashboard.collect_trend(7), mp_done=3, mp_total=6,
    )
    assert "67.5%" in out2
    assert "$3.50" in out2
    assert "cp_a" in out2
    assert "hello world" in out2
