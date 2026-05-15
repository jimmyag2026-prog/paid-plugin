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
    audit_path = tmp_path / "audit_log.jsonl"
    entries = [
        {"timestamp": _now_iso(), "decision": "approved", "topics": ["customer"]},
        {"timestamp": _now_iso(), "decision": "approved", "topics": ["hiring"]},
    ]
    _write_audit(audit_path, entries)
    stats = ob.scan_audit_log(audit_path=audit_path)
    assert stats["approval_rate"] == 1.0
    assert stats["entries_scanned"] == 2


def test_approval_rate_mixed(tmp_path):
    audit_path = tmp_path / "audit_log.jsonl"
    entries = [
        {"timestamp": _now_iso(), "decision": "approved"},
        {"timestamp": _now_iso(), "decision": "rejected"},
        {"timestamp": _now_iso(), "decision": "rejected"},
        {"timestamp": _now_iso(), "decision": "approved"},
    ]
    _write_audit(audit_path, entries)
    stats = ob.scan_audit_log(audit_path=audit_path)
    assert stats["approval_rate"] == 0.5


def test_approval_rate_no_decisions(tmp_path):
    audit_path = tmp_path / "audit_log.jsonl"
    entries = [{"timestamp": _now_iso(), "draft_answer": "Hello cp"}]
    _write_audit(audit_path, entries)
    stats = ob.scan_audit_log(audit_path=audit_path)
    assert stats["approval_rate"] is None


# ---------------------------------------------------------------------------
# Top topics
# ---------------------------------------------------------------------------


def test_top_topics(tmp_path):
    audit_path = tmp_path / "audit_log.jsonl"
    entries = [
        {"timestamp": _now_iso(), "topics": ["customer", "hiring"]},
        {"timestamp": _now_iso(), "topics": ["customer"]},
        {"timestamp": _now_iso(), "topics": ["finance"]},
        {"timestamp": _now_iso(), "topics": ["customer"]},
    ]
    _write_audit(audit_path, entries)
    stats = ob.scan_audit_log(audit_path=audit_path)
    assert stats["top_escalated_topics"][0] == "customer"
    assert "hiring" in stats["top_escalated_topics"]


def test_top_topics_empty(tmp_path):
    audit_path = tmp_path / "audit_log.jsonl"
    entries = [{"timestamp": _now_iso(), "draft_answer": "no topics here"}]
    _write_audit(audit_path, entries)
    stats = ob.scan_audit_log(audit_path=audit_path)
    assert stats["top_escalated_topics"] == []


# ---------------------------------------------------------------------------
# Average reply length
# ---------------------------------------------------------------------------


def test_avg_reply_len(tmp_path):
    audit_path = tmp_path / "audit_log.jsonl"
    entries = [
        {"timestamp": _now_iso(), "draft_answer": "Hello"},     # 5
        {"timestamp": _now_iso(), "draft_answer": "Hi there!"},  # 9
    ]
    _write_audit(audit_path, entries)
    stats = ob.scan_audit_log(audit_path=audit_path)
    assert stats["avg_reply_length_chars"] == 7.0


def test_avg_reply_len_no_answers(tmp_path):
    audit_path = tmp_path / "audit_log.jsonl"
    entries = [{"timestamp": _now_iso(), "decision": "approved"}]
    _write_audit(audit_path, entries)
    stats = ob.scan_audit_log(audit_path=audit_path)
    assert stats["avg_reply_length_chars"] is None


# ---------------------------------------------------------------------------
# Decision window P75
# ---------------------------------------------------------------------------


def test_decision_window_p75(tmp_path):
    audit_path = tmp_path / "audit_log.jsonl"
    # 1hr, 2hr, 3hr, 4hr → P75 index = floor(4*0.75) - 1 = 2 → 3hrs
    entries = [
        {"timestamp": _ago_iso(hours=5), "received_at": _ago_iso(hours=5), "decided_at": _ago_iso(hours=4)},  # 1h
        {"timestamp": _ago_iso(hours=5), "received_at": _ago_iso(hours=5), "decided_at": _ago_iso(hours=3)},  # 2h
        {"timestamp": _ago_iso(hours=5), "received_at": _ago_iso(hours=5), "decided_at": _ago_iso(hours=2)},  # 3h
        {"timestamp": _ago_iso(hours=5), "received_at": _ago_iso(hours=5), "decided_at": _ago_iso(hours=1)},  # 4h
    ]
    _write_audit(audit_path, entries)
    stats = ob.scan_audit_log(audit_path=audit_path)
    assert stats["preferred_decision_window_hrs"] is not None
    assert 1.0 <= stats["preferred_decision_window_hrs"] <= 4.0


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
