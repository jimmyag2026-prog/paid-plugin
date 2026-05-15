"""v1.6.15b — ingest-failure UX gate.

When a link couldn't be fetched (anti-scrape wall / no backend / Lark
download failed) the review must NOT silently run SCAN/QA on degraded
input. The first reviewer-facing turn must surface the failure and ask
the user to continue or `/review cancel` (jelabs pilot day-1: cp's
Twitter + Lark-file links produced "no opinion provided" garbage).
"""

from __future__ import annotations

import pytest

from paid_review.core.state import load_state, save_state


def _intake_with_ingest_error(paid_tmp, monkeypatch, err="anti-scrape wall"):
    from paid import storage, identity

    storage.PAID_DIR = paid_tmp
    cp = identity.ensure_counterparty("telegram", "900", "Junior")
    from paid_review.api import intake

    sid = intake(
        cp=cp,
        initial_message="看看这个 https://x.com/foo/status/1 适合转推吗",
        attachments=[],
    )
    st = load_state(sid)
    st.ingest_errors = [f"[web_scrape:https://x.com/foo/status/1] {err}"]
    save_state(st)
    return sid


def test_gate_fires_on_first_turn_when_ingest_failed(paid_tmp, monkeypatch):
    from paid_review.api import handle_inbound

    sid = _intake_with_ingest_error(paid_tmp, monkeypatch)
    reply = handle_inbound(sid, "", {})

    assert reply.event_kind == "ingest_failed"
    assert reply.stage == "INTAKE"
    assert reply.closed is False
    # Must show the failure + the two choices.
    assert "anti-scrape wall" in reply.text
    assert "/review cancel" in reply.text
    assert ("继续" in reply.text) or ("continue" in reply.text.lower())
    # Did NOT silently jump to subject/scan.
    st = load_state(sid)
    assert st.last_event_kind == "ingest_failed"
    assert st.stage == "INTAKE"


def test_gate_continue_proceeds_to_subject(paid_tmp, monkeypatch):
    from paid_review.api import handle_inbound

    sid = _intake_with_ingest_error(paid_tmp, monkeypatch)
    handle_inbound(sid, "", {})  # gate shown
    reply = handle_inbound(sid, "继续", {})  # user opts to continue

    # Gate consumed → normal subject flow resumes.
    assert reply.event_kind == "subject_ask"
    assert reply.stage == "SUBJECT"
    assert reply.closed is False
    st = load_state(sid)
    assert st.last_event_kind == "subject_ask"


def test_gate_continue_english_token(paid_tmp, monkeypatch):
    from paid_review.api import handle_inbound

    sid = _intake_with_ingest_error(paid_tmp, monkeypatch)
    handle_inbound(sid, "", {})
    reply = handle_inbound(sid, "continue", {})
    assert reply.event_kind == "subject_ask"


def test_gate_cancel_closes_session(paid_tmp, monkeypatch):
    from paid_review.api import handle_inbound

    sid = _intake_with_ingest_error(paid_tmp, monkeypatch)
    handle_inbound(sid, "", {})
    reply = handle_inbound(sid, "取消", {})

    assert reply.event_kind == "cancelled"
    assert reply.stage == "CLOSED"
    assert reply.closed is True
    st = load_state(sid)
    assert st.stage == "CLOSED"
    assert st.verdict == "FORCED_PARTIAL"
    assert st.forced is True
    assert st.forced_reason == "junior_cancelled_on_ingest_error"


def test_gate_unclear_reply_reprompts(paid_tmp, monkeypatch):
    from paid_review.api import handle_inbound

    sid = _intake_with_ingest_error(paid_tmp, monkeypatch)
    handle_inbound(sid, "", {})
    reply = handle_inbound(sid, "啊这是啥意思", {})

    # Still gated, not advanced.
    assert reply.event_kind == "ingest_failed"
    assert reply.stage == "INTAKE"
    st = load_state(sid)
    assert st.last_event_kind == "ingest_failed"


def test_no_gate_when_ingest_clean(paid_tmp, monkeypatch):
    """Sanity: a clean ingest must NOT trigger the gate (no behaviour
    change for the normal happy path)."""
    from paid import storage, identity
    from paid_review.api import intake, handle_inbound

    storage.PAID_DIR = paid_tmp
    cp = identity.ensure_counterparty("telegram", "901", "Junior")
    sid = intake(cp=cp, initial_message="Q3 预算草稿请 review", attachments=[])
    reply = handle_inbound(sid, "", {})
    assert reply.event_kind == "subject_ask"
    assert reply.stage == "SUBJECT"
