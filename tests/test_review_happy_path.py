"""Happy-path integration test: INTAKE → SUBJECT → QA → CLOSED (Sprint A stub).

Fake LLM returns canned 4-pillar findings.  No real API calls.
Drives intake → confirm subject → answer 3 findings → done → CLOSED.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# Canned LLM responses
# ---------------------------------------------------------------------------

_SUBJECT_CANDIDATES = ["Q3 Marketing Budget $240k", "Channel Mix 60/40", "Tier-2 City Pilot"]

_FINDINGS = [
    {"id": "f1", "pillar": "data",   "text": "Sales data source not cited; revenue assumption +12% YoY unsourced."},
    {"id": "f2", "pillar": "logic",  "text": "ROI model assumes 3x ROAS with no prior-campaign benchmark."},
    {"id": "f3", "pillar": "intent", "text": "Decision ask is split (budget + pilot): one meeting, two asks — consider separating."},
]


def _make_fake_llm(call_log: list[str]):
    """Return a fake call_llm that records calls and returns canned data."""
    call_count = [0]

    def fake_llm(**kw):
        prompt = kw.get("user_message", "")
        call_log.append(prompt[:120])
        call_count[0] += 1
        # First call: subject candidates
        if call_count[0] == 1:
            return json.dumps(_SUBJECT_CANDIDATES)
        # Second call: findings
        return json.dumps(_FINDINGS)

    return fake_llm


# ---------------------------------------------------------------------------
# Happy-path test
# ---------------------------------------------------------------------------

def test_happy_path_end_to_end(paid_tmp, monkeypatch):
    """Full happy path: intake → subject selection → 3 findings → done → CLOSED."""
    from paid import storage, hermes_io, identity
    storage.PAID_DIR = paid_tmp

    call_log: list[str] = []
    monkeypatch.setattr(hermes_io, "call_llm", _make_fake_llm(call_log))

    # --- Setup counterparty ---
    cp = identity.ensure_counterparty("telegram", "101", "Junior X")

    # --- Step 1: intake() ---
    from paid_review.api import intake, handle_inbound, show, list_open
    sid = intake(
        cp=cp,
        initial_message="Q3 营销预算草稿，请 review",
        attachments=[],
    )
    assert sid and len(sid) == 12

    # cp profile updated
    cp_reloaded = identity.load_counterparty("telegram", "101")
    assert cp_reloaded.active_review_session == sid

    # --- Step 2: INTAKE handler → SUBJECT ---
    reply = handle_inbound(sid, "Q3 营销预算草稿，请 review", {})
    assert reply.stage == "SUBJECT"
    assert reply.event_kind == "subject_ask"
    assert "(a)" in reply.text
    assert reply.closed is False

    # --- Step 3: SUBJECT → SCAN → QA (first finding) ---
    reply = handle_inbound(sid, "a", {})  # pick candidate (a)
    assert reply.stage == "QA", f"Expected QA, got {reply.stage}: {reply.text}"
    assert reply.event_kind == "finding"
    assert "f1" in reply.text.lower() or "data" in reply.text.lower() or "sales" in reply.text.lower()
    assert reply.closed is False

    # --- Step 4: Answer finding 1 (accept) ---
    reply = handle_inbound(sid, "a", {})  # accept
    assert reply.stage == "QA"
    assert reply.closed is False
    # Should now be on finding 2
    assert "logic" in reply.text.lower() or "roi" in reply.text.lower() or "f2" in reply.text.lower()

    # --- Step 5: Answer finding 2 (reject/dissent) ---
    reply = handle_inbound(sid, "b", {})  # dissent
    assert reply.stage == "QA"
    assert reply.closed is False

    # --- Step 6: Answer finding 3 (accept) ---
    reply = handle_inbound(sid, "a", {})  # accept
    assert reply.stage == "QA"
    assert reply.closed is False
    # Cursor exhausted; should prompt 'done'
    assert "done" in reply.text.lower() or "完成" in reply.text or "处理" in reply.text

    # --- Step 7: done ---
    reply = handle_inbound(sid, "done", {})
    assert reply.stage == "CLOSED"
    assert reply.closed is True

    # --- Verify final state ---
    from paid_review.core.state import load_state
    state = load_state(sid)
    assert state.stage == "CLOSED"
    assert state.verdict in ("READY", "FORCED_PARTIAL")  # Sprint A stub uses READY
    assert state.closed_at is not None

    # --- list_open should not show this session ---
    listing = list_open()
    assert sid not in listing or "没有" in listing

    # --- show() should work ---
    summary = show(sid)
    assert sid in summary
    assert "CLOSED" in summary

    # --- LLM was called at least once ---
    assert len(call_log) >= 1


def test_happy_path_intake_refused_on_double_intake(paid_tmp, monkeypatch):
    """Second intake for same cp raises IntakeRefused."""
    from paid import storage, hermes_io, identity
    storage.PAID_DIR = paid_tmp

    call_log: list[str] = []
    monkeypatch.setattr(hermes_io, "call_llm", _make_fake_llm(call_log))

    cp = identity.ensure_counterparty("telegram", "202", "Junior Y")

    from paid_review.api import intake, IntakeRefused
    sid1 = intake(cp=cp, initial_message="draft 1", attachments=[])
    assert sid1

    cp2 = identity.load_counterparty("telegram", "202")
    with pytest.raises(IntakeRefused):
        intake(cp=cp2, initial_message="draft 2", attachments=[])


def test_happy_path_no_findings_short_circuit(paid_tmp, monkeypatch):
    """When LLM returns 0 findings, session closes immediately with verdict=READY (Ⓜ17)."""
    from paid import storage, hermes_io, identity
    storage.PAID_DIR = paid_tmp

    def fake_llm_no_findings(**kw):
        prompt = kw.get("user_message", "")
        if "Generate 3-4" in prompt or "subject" in prompt.lower():
            return json.dumps(["Subject A", "Subject B"])
        # SCAN finds nothing
        return json.dumps([])

    monkeypatch.setattr(hermes_io, "call_llm", fake_llm_no_findings)

    cp = identity.ensure_counterparty("telegram", "303", "Junior Z")

    from paid_review.api import intake, handle_inbound
    sid = intake(cp=cp, initial_message="perfect memo", attachments=[])

    # Drive through INTAKE
    reply = handle_inbound(sid, "perfect memo", {})
    assert reply.stage == "SUBJECT"

    # Confirm subject → SCAN → finds nothing → CLOSED
    reply = handle_inbound(sid, "a", {})
    assert reply.stage == "CLOSED"
    assert reply.closed is True
    assert "no_findings" in reply.text.lower() or "没有" in reply.text or "decision" in reply.text.lower()

    from paid_review.core.state import load_state
    state = load_state(sid)
    assert state.stage == "CLOSED"
    assert state.verdict == "READY"
    assert state.forced_reason == "no_findings"


def test_happy_path_show_and_list(paid_tmp, monkeypatch):
    """list_open() shows in-progress sessions; show() returns markdown."""
    from paid import storage, hermes_io, identity
    storage.PAID_DIR = paid_tmp

    monkeypatch.setattr(hermes_io, "call_llm", _make_fake_llm([]))

    cp = identity.ensure_counterparty("telegram", "404", "Junior W")

    from paid_review.api import intake, handle_inbound, list_open, show
    sid = intake(cp=cp, initial_message="doc to review", attachments=[])
    handle_inbound(sid, "doc to review", {})  # INTAKE → SUBJECT

    listing = list_open()
    assert sid in listing
    assert "SUBJECT" in listing

    summary = show(sid)
    assert sid in summary
    assert "SUBJECT" in summary
