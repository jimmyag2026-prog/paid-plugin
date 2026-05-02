"""Approval state-machine tests (J3 v0.5)."""

from __future__ import annotations

import json

import pytest

from paid import approval


def _make(**over):
    base = dict(
        counterparty_id="telegram_111",
        counterparty_platform="telegram",
        counterparty_user_id="111",
        counterparty_display="Alice",
        junior_session_id="sess-1",
        junior_question="What's the timezone for the Friday demo?",
        draft_answer="Pacific Time, 11am.",
        topic="scheduling",
        stakes="low",
        confidence=0.82,
    )
    base.update(over)
    return approval.create(**base)


def test_create_returns_record_with_id_and_timestamp(paid_tmp):
    req = _make()
    assert req.request_id and len(req.request_id) == 8
    assert req.status == "pending"
    assert req.ts_created > 0
    assert (paid_tmp / "pending_approvals.jsonl").exists()


def test_list_pending_returns_oldest_first(paid_tmp):
    a = _make(junior_question="Q1")
    b = _make(junior_question="Q2")
    c = _make(junior_question="Q3")
    pendings = approval.list_pending()
    ids = [r.request_id for r in pendings]
    assert ids == [a.request_id, b.request_id, c.request_id]


def test_get_returns_latest_state(paid_tmp):
    req = _make()
    assert approval.get(req.request_id).status == "pending"
    approval.set_status(req.request_id, "approved", final_text="Yes — Pacific.")
    fresh = approval.get(req.request_id)
    assert fresh.status == "approved"
    assert fresh.final_text == "Yes — Pacific."
    assert fresh.ts_resolved is not None


def test_set_status_filters_pending_list(paid_tmp):
    a = _make(junior_question="Q1")
    b = _make(junior_question="Q2")
    approval.set_status(a.request_id, "approved", final_text="ok")
    pendings = approval.list_pending()
    assert [r.request_id for r in pendings] == [b.request_id]


def test_set_status_unknown_returns_none(paid_tmp):
    assert approval.set_status("deadbeef", "approved") is None


def test_replay_skips_status_event_without_create(paid_tmp):
    """A corrupt log line referencing a nonexistent request_id must not crash."""
    log = paid_tmp / "pending_approvals.jsonl"
    log.write_text(
        json.dumps({"type": "status", "request_id": "ghost", "status": "approved"}) + "\n",
        encoding="utf-8",
    )
    pendings = approval.list_pending()
    assert pendings == []
    assert approval.get("ghost") is None


def test_event_log_is_append_only_audit_trail(paid_tmp):
    req = _make()
    approval.set_status(req.request_id, "approved", final_text="X")
    approval.set_status(req.request_id, "rejected", final_text="Y")  # idempotent re-set

    lines = (paid_tmp / "pending_approvals.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3  # 1 create + 2 status
    types = [json.loads(l)["type"] for l in lines]
    assert types == ["create", "status", "status"]
    # Latest wins
    assert approval.get(req.request_id).status == "rejected"


def test_short_id_is_unique_across_creates(paid_tmp):
    ids = {_make().request_id for _ in range(20)}
    assert len(ids) == 20
