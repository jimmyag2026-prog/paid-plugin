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
    out = tpl.render(s=s_empty, cps=[], pending=[], recent=[], reviews_active=[])
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
    out2 = tpl.render(s=s_with, cps=[], pending=[], recent=recent, reviews_active=[])
    assert "67.5%" in out2
    assert "$3.50" in out2
    assert "cp_a" in out2
    assert "hello world" in out2
