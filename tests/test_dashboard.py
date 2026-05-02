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
