"""Tests for paid.observer — Usage Observation Learner (v1.6.3)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from paid import observer as ob
from paid import profile as p
from paid import storage


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "PAID_DIR", tmp_path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ago_iso(days: float = 0, hours: float = 0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days, hours=hours)
    return dt.isoformat(timespec="seconds")


def _write_audit(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _write_pending(path: Path, events: list[dict]) -> None:
    """Write pending.jsonl events. v1.6.8: approval_rate and
    decision_window_p75 read from pending events, not audit rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def _now_unix() -> float:
    return datetime.now(timezone.utc).timestamp()


_AUDIT_ROW_COUNTER = [0]


def _audit_row(topic: str = "logistics", state: str = "direct",
               draft: str = "", reply: str = "", ts: str | None = None) -> dict:
    """Build an audit row matching the real v1.6.x schema.

    Each row gets a unique entry_id so audit.read_all_entries dedup
    (added in v1.6.6) doesn't collapse fixtures with identical content
    down to a single row.
    """
    _AUDIT_ROW_COUNTER[0] += 1
    return {
        "entry_id": f"e{_AUDIT_ROW_COUNTER[0]:016d}",
        "ts": ts or _now_iso(),
        "session_id": f"s{_AUDIT_ROW_COUNTER[0]}",
        "counterparty": "cp_test",
        "platform": "feishu",
        "junior_msg": "hi",
        "classification": {
            "topic": topic,
            "stakes": "low",
            "in_scope": True,
            "is_blacklisted": False,
            "confidence": 0.9,
            "draft_answer": draft,
            "reasoning": "",
        },
        "action": {"state": state, "reason": ""},
        "extra": (
            {"assistant_response_preview": reply} if reply else {}
        ),
    }


# ---------------------------------------------------------------------------
# scan_audit_log — empty / missing
# ---------------------------------------------------------------------------


def test_scan_empty_audit():
    stats = ob.scan_audit_log(audit_path=storage.PAID_DIR / "audit_log.jsonl")
    assert stats["entries_scanned"] == 0
    assert stats["approval_rate"] is None
    assert stats["top_escalated_topics"] == []


def test_scan_missing_file():
    stats = ob.scan_audit_log(audit_path=storage.PAID_DIR / "no_such.jsonl")
    assert stats["entries_scanned"] == 0


# ---------------------------------------------------------------------------
# scan_audit_log — approval rate
# ---------------------------------------------------------------------------


def test_approval_rate_all_approved(tmp_path):
    """v1.6.8: approval_rate reads pending.jsonl status events."""
    # Write 2 approvals to per-cp pending.jsonl
    cp_dir = tmp_path / "counterparties" / "cp_test"
    pending_path = cp_dir / "pending.jsonl"
    _write_pending(pending_path, [
        {"type": "create", "request_id": "r1", "ts": _now_unix() - 100,
         "counterparty_id": "cp_test"},
        {"type": "status", "request_id": "r1", "ts": _now_unix() - 50,
         "status": "approved"},
        {"type": "create", "request_id": "r2", "ts": _now_unix() - 100,
         "counterparty_id": "cp_test"},
        {"type": "status", "request_id": "r2", "ts": _now_unix() - 50,
         "status": "approved"},
    ])
    stats = ob.scan_audit_log()
    assert stats["approval_rate"] == 1.0


def test_approval_rate_mixed(tmp_path):
    cp_dir = tmp_path / "counterparties" / "cp_test"
    _write_pending(cp_dir / "pending.jsonl", [
        {"type": "status", "request_id": "r1", "ts": _now_unix(), "status": "approved"},
        {"type": "status", "request_id": "r2", "ts": _now_unix(), "status": "rejected"},
        {"type": "status", "request_id": "r3", "ts": _now_unix(), "status": "rejected"},
        {"type": "status", "request_id": "r4", "ts": _now_unix(), "status": "approved"},
    ])
    stats = ob.scan_audit_log()
    assert stats["approval_rate"] == 0.5


def test_approval_rate_no_decisions(tmp_path):
    """Only create events, no status events → None."""
    cp_dir = tmp_path / "counterparties" / "cp_test"
    _write_pending(cp_dir / "pending.jsonl", [
        {"type": "create", "request_id": "r1", "ts": _now_unix()},
    ])
    stats = ob.scan_audit_log()
    assert stats["approval_rate"] is None


def test_approval_rate_timed_out_counts_against(tmp_path):
    """timed_out is in the denominator (owner-ghosted approvals)."""
    cp_dir = tmp_path / "counterparties" / "cp_test"
    _write_pending(cp_dir / "pending.jsonl", [
        {"type": "status", "request_id": "r1", "ts": _now_unix(), "status": "approved"},
        {"type": "status", "request_id": "r2", "ts": _now_unix(), "status": "timed_out"},
    ])
    stats = ob.scan_audit_log()
    assert stats["approval_rate"] == 0.5


# ---------------------------------------------------------------------------
# Top topics (v1.6.8: reads classification.topic + filters action.state="request")
# ---------------------------------------------------------------------------


def test_top_topics(tmp_path):
    cp_dir = tmp_path / "counterparties" / "cp_test"
    audit_path = cp_dir / "audit.jsonl"
    _write_audit(audit_path, [
        _audit_row(topic="customer", state="request"),
        _audit_row(topic="hiring", state="request"),
        _audit_row(topic="customer", state="request"),
        _audit_row(topic="finance", state="request"),
        _audit_row(topic="customer", state="request"),
        # Direct-state rows should NOT count (they're handled, not escalated)
        _audit_row(topic="logistics", state="direct"),
        _audit_row(topic="logistics", state="direct"),
    ])
    stats = ob.scan_audit_log()
    assert stats["top_escalated_topics"][0] == "customer"
    assert "hiring" in stats["top_escalated_topics"]
    assert "logistics" not in stats["top_escalated_topics"], \
        "direct-state topics must be excluded from escalated list"


def test_top_topics_empty(tmp_path):
    cp_dir = tmp_path / "counterparties" / "cp_test"
    _write_audit(cp_dir / "audit.jsonl", [
        _audit_row(topic="logistics", state="direct"),  # no escalations
    ])
    stats = ob.scan_audit_log()
    assert stats["top_escalated_topics"] == []


# ---------------------------------------------------------------------------
# Average reply length (v1.6.8: reads extra.assistant_response_preview)
# ---------------------------------------------------------------------------


def test_avg_reply_len(tmp_path):
    cp_dir = tmp_path / "counterparties" / "cp_test"
    _write_audit(cp_dir / "audit.jsonl", [
        _audit_row(reply="Hello"),     # 5
        _audit_row(reply="Hi there!"),  # 9
    ])
    stats = ob.scan_audit_log()
    assert stats["avg_reply_length_chars"] == 7.0


def test_avg_reply_len_falls_back_to_draft(tmp_path):
    """If post_llm row hasn't landed, classification.draft_answer is used."""
    cp_dir = tmp_path / "counterparties" / "cp_test"
    _write_audit(cp_dir / "audit.jsonl", [
        _audit_row(draft="draft only", reply=""),  # 10
    ])
    stats = ob.scan_audit_log()
    assert stats["avg_reply_length_chars"] == 10.0


def test_avg_reply_len_no_answers(tmp_path):
    cp_dir = tmp_path / "counterparties" / "cp_test"
    _write_audit(cp_dir / "audit.jsonl", [
        _audit_row(draft="", reply=""),
    ])
    stats = ob.scan_audit_log()
    assert stats["avg_reply_length_chars"] is None


# ---------------------------------------------------------------------------
# Decision window P75 (v1.6.8: reads pending.jsonl create→status diff)
# ---------------------------------------------------------------------------


def test_decision_window_p75(tmp_path):
    """Four approvals with 1h/2h/3h/4h windows → P75 linear = 3.25h."""
    cp_dir = tmp_path / "counterparties" / "cp_test"
    now = _now_unix()
    events = []
    for i, hours in enumerate([1, 2, 3, 4]):
        rid = f"r{i}"
        create_ts = now - hours * 3600 - 10
        status_ts = now - 10
        events.append({"type": "create", "request_id": rid, "ts": create_ts})
        events.append({"type": "status", "request_id": rid, "ts": status_ts,
                       "status": "approved"})
    _write_pending(cp_dir / "pending.jsonl", events)
    stats = ob.scan_audit_log()
    # Linear interpolation on [1,2,3,4] @ q=0.75 → pos=2.25 → 3 + 0.25 = 3.25
    assert stats["preferred_decision_window_hrs"] == 3.25


def test_decision_window_p75_excludes_timed_out(tmp_path):
    """timed_out approvals shouldn't pollute decision-speed stats."""
    cp_dir = tmp_path / "counterparties" / "cp_test"
    now = _now_unix()
    events = [
        {"type": "create", "request_id": "r1", "ts": now - 3600},  # 1h ago
        {"type": "status", "request_id": "r1", "ts": now - 10, "status": "approved"},
        {"type": "create", "request_id": "r2", "ts": now - 100 * 3600},  # 100h ago
        {"type": "status", "request_id": "r2", "ts": now - 10, "status": "timed_out"},
    ]
    _write_pending(cp_dir / "pending.jsonl", events)
    stats = ob.scan_audit_log()
    # Only the approved one counts; 100h timed-out is excluded
    assert stats["preferred_decision_window_hrs"] is not None
    assert stats["preferred_decision_window_hrs"] < 2.0


# ---------------------------------------------------------------------------
# Lookback filtering
# ---------------------------------------------------------------------------


def test_lookback_filters_old_entries(tmp_path):
    audit_path = tmp_path / "audit_log.jsonl"
    entries = [
        {"timestamp": _ago_iso(days=45), "decision": "approved"},  # too old
        {"timestamp": _now_iso(), "decision": "rejected"},          # recent
    ]
    _write_audit(audit_path, entries)
    stats = ob.scan_audit_log(audit_path=audit_path, lookback_days=30)
    # Only the recent entry
    assert stats["entries_scanned"] == 1


# ---------------------------------------------------------------------------
# update_profile_observed
# ---------------------------------------------------------------------------


def test_update_profile_observed():
    prof = p.new_profile("jimmy", name="Jimmy")
    p.save_profile(prof)

    stats = {
        "approval_rate": 0.75,
        "top_escalated_topics": ["customer", "hiring"],
        "avg_reply_length_chars": 95.0,
        "preferred_decision_window_hrs": 2.5,
        "entries_scanned": 10,
    }
    changed = ob.update_profile_observed(stats)
    assert changed is True

    updated = p.load_profile()
    assert updated.observed.approval_rate == 0.75
    assert updated.observed.top_escalated_topics == ["customer", "hiring"]
    assert updated.observed.avg_reply_length_chars == 95.0
    assert updated.observed.preferred_decision_window_hrs == 2.5
    assert updated.observed.last_updated_at is not None


def test_update_profile_observed_no_profile():
    stats = {"approval_rate": 0.5, "entries_scanned": 5}
    changed = ob.update_profile_observed(stats)
    assert changed is False


# ---------------------------------------------------------------------------
# build_weekly_digest
# ---------------------------------------------------------------------------


def test_build_weekly_digest_no_data():
    stats = ob._empty_stats()
    digest = ob.build_weekly_digest(stats)
    assert "没有" in digest or "数据" in digest


def test_build_weekly_digest_with_data():
    stats = {
        "approval_rate": 0.8,
        "top_escalated_topics": ["customer", "hiring"],
        "avg_reply_length_chars": 95.0,
        "preferred_decision_window_hrs": 2.0,
        "entries_scanned": 42,
    }
    digest = ob.build_weekly_digest(stats)
    assert "42" in digest
    assert "80%" in digest
    assert "customer" in digest
    assert "2.0" in digest
