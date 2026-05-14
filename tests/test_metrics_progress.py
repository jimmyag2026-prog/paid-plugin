"""Tests for paid.metrics_progress (v1.5.5 A6)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from paid import metrics_progress as mp
from paid import storage


def _today_iso(hour: int = 12) -> str:
    today = datetime.now(timezone.utc).date()
    return datetime(today.year, today.month, today.day, hour, tzinfo=timezone.utc).isoformat()


def _write_settings(paid_tmp, settings_dict):
    (paid_tmp / "settings.json").write_text(json.dumps(settings_dict), encoding="utf-8")


def _write_owner(paid_tmp):
    (paid_tmp / "owner.json").write_text(json.dumps({
        "schema_version": 2,
        "owner_id": "owner_test",
        "name": "Test",
        "identities": [{"platform": "feishu", "user_id": "ou_test"}],
    }), encoding="utf-8")


def _write_junior_cp(paid_tmp, platform="feishu", user_id="ou_junior",
                     role="junior"):
    """Write a counterparty profile using identity's <platform>_<user_id> id convention.

    Returns the cp_id so callers can reference it in audit rows.
    """
    cp_id = f"{platform}_{user_id}"
    cp_dir = paid_tmp / "counterparties" / cp_id
    cp_dir.mkdir(parents=True, exist_ok=True)
    (cp_dir / "profile.json").write_text(json.dumps({
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


# ---------------------------------------------------------------------------
# Shape + count
# ---------------------------------------------------------------------------

def test_collect_returns_six_rows(paid_tmp):
    rows = mp.collect()
    assert len(rows) == 6
    ids = [r["id"] for r in rows]
    assert ids == [1, 2, 3, 4, 5, 6]
    for r in rows:
        assert {"id", "title", "status", "detail", "source"} <= set(r.keys())
        assert r["status"] in ("done", "pending")
        assert r["source"] in ("derived", "manual")


def test_n_done_counts_only_done():
    rows = [
        {"id": 1, "status": "done"},
        {"id": 2, "status": "pending"},
        {"id": 3, "status": "done"},
    ]
    assert mp.n_done(rows) == 2


# ---------------------------------------------------------------------------
# Indicator #1 — pilot cycle (derived)
# ---------------------------------------------------------------------------

def test_indicator_1_no_junior_cp(paid_tmp):
    r = mp._indicator_1_pilot_cycle()
    assert r["id"] == 1
    assert r["status"] == "pending"
    assert "no junior cp" in r["detail"]


def test_indicator_1_junior_no_direct_yet(paid_tmp):
    _write_junior_cp(paid_tmp)
    r = mp._indicator_1_pilot_cycle()
    assert r["status"] == "pending"
    assert "1 junior cp(s) registered" in r["detail"]


def test_indicator_1_junior_with_direct(paid_tmp):
    cp_id = _write_junior_cp(paid_tmp, user_id="ou_junior_x")
    audit = paid_tmp / "audit_log.jsonl"
    audit.write_text(json.dumps({
        "ts": _today_iso(11),
        "counterparty": cp_id,
        "action": {"state": "direct"},
    }) + "\n", encoding="utf-8")
    r = mp._indicator_1_pilot_cycle()
    assert r["status"] == "done"
    assert "1 junior cp received" in r["detail"]


# ---------------------------------------------------------------------------
# Indicator #2 — weekly reports (derived)
# ---------------------------------------------------------------------------

def test_indicator_2_no_dir(paid_tmp):
    r = mp._indicator_2_weekly_reports()
    assert r["status"] == "pending"
    assert "weekly_reports" in r["detail"]


def test_indicator_2_one_report_pending(paid_tmp):
    d = paid_tmp / "weekly_reports"
    d.mkdir()
    (d / "w-2026-05-01.md").write_text("# week 1", encoding="utf-8")
    r = mp._indicator_2_weekly_reports()
    assert r["status"] == "pending"
    assert "1 weekly report" in r["detail"]


def test_indicator_2_two_reports_done(paid_tmp):
    d = paid_tmp / "weekly_reports"
    d.mkdir()
    (d / "w-2026-05-01.md").write_text("# w1", encoding="utf-8")
    (d / "w-2026-05-08.md").write_text("# w2", encoding="utf-8")
    r = mp._indicator_2_weekly_reports()
    assert r["status"] == "done"


# ---------------------------------------------------------------------------
# Manual indicators (3, 4, 6) — flags
# ---------------------------------------------------------------------------

def test_manual_flags_default_pending(paid_tmp):
    rows = mp.collect()
    for idx in (3, 4, 6):
        r = next(r for r in rows if r["id"] == idx)
        assert r["status"] == "pending"
        assert r["source"] == "manual"


def test_manual_flag_done_when_set(paid_tmp):
    _write_settings(paid_tmp, {"metrics_progress": {
        "cross_org_demo_done": True,
        "twitter_long_post_done": True,
        "readme_for_strangers_done": True,
    }})
    rows = mp.collect()
    for idx in (3, 4, 6):
        r = next(r for r in rows if r["id"] == idx)
        assert r["status"] == "done"


# ---------------------------------------------------------------------------
# Indicator #5 — deep chats count
# ---------------------------------------------------------------------------

def test_indicator_5_zero_default(paid_tmp):
    rows = mp.collect()
    r5 = next(r for r in rows if r["id"] == 5)
    assert r5["status"] == "pending"
    assert "0/5" in r5["detail"]


def test_indicator_5_below_target(paid_tmp):
    _write_settings(paid_tmp, {"metrics_progress": {"deep_chats_count": 3}})
    rows = mp.collect()
    r5 = next(r for r in rows if r["id"] == 5)
    assert r5["status"] == "pending"
    assert "3/5" in r5["detail"]


def test_indicator_5_at_or_over_target(paid_tmp):
    _write_settings(paid_tmp, {"metrics_progress": {"deep_chats_count": 5}})
    rows = mp.collect()
    r5 = next(r for r in rows if r["id"] == 5)
    assert r5["status"] == "done"

    _write_settings(paid_tmp, {"metrics_progress": {"deep_chats_count": 10}})
    rows = mp.collect()
    r5 = next(r for r in rows if r["id"] == 5)
    assert r5["status"] == "done"
    assert "10/5" in r5["detail"]


def test_indicator_5_bad_value_clamped_to_zero(paid_tmp):
    _write_settings(paid_tmp, {"metrics_progress": {"deep_chats_count": "five"}})
    rows = mp.collect()
    r5 = next(r for r in rows if r["id"] == 5)
    assert r5["status"] == "pending"
    assert "0/5" in r5["detail"]


# ---------------------------------------------------------------------------
# Settings shape resilience
# ---------------------------------------------------------------------------

def test_collect_when_settings_metrics_progress_not_dict(paid_tmp):
    _write_settings(paid_tmp, {"metrics_progress": "garbage"})
    rows = mp.collect()
    assert len(rows) == 6  # no crash
    for idx in (3, 4, 6):
        r = next(r for r in rows if r["id"] == idx)
        assert r["status"] == "pending"


def test_metrics_progress_template_renders(paid_tmp):
    """Smoke: /metrics-progress template renders."""
    try:
        from jinja2 import Template
    except ImportError:
        pytest.skip("jinja2 not installed")
    from paid import dashboard
    tpl = Template(dashboard._METRICS_PROGRESS_TEMPLATE)
    rows = mp.collect()
    out = tpl.render(rows=rows, n_done=mp.n_done(rows), n_total=len(rows))
    assert "Master-design §6" in out
    # All six titles render
    for r in rows:
        assert r["title"] in out
