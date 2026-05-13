"""R4: cross-cp / cross-sid data isolation tests (spec §8 R4).

Verifies that when building LLM prompts or reading session data for sid_A,
content from sid_B (different cp) does NOT appear in the context.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from paid_review.core.annotation import Annotation, append_annotation
from paid_review.core.state import SessionState, save_state, session_dir


def _write_session(paid_tmp, sid: str, cp_id: str, content: str) -> Path:
    """Create a minimal session with normalized.md containing *content*."""
    from paid import storage
    storage.PAID_DIR = paid_tmp

    state = SessionState(
        sid=sid,
        cp_id=cp_id,
        platform="telegram",
        stage="QA",
        verdict="PENDING",
        rounds=1,
        subject=f"Subject for {cp_id}",
    )
    save_state(state)

    d = session_dir(sid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "normalized.md").write_text(content, encoding="utf-8")

    ann = Annotation(
        id=f"{sid}_f1",
        pillar="data",
        text=f"Private finding for {cp_id}",
        status="open",
    )
    append_annotation(d, ann)
    return d


def test_r4_normalized_md_isolation(paid_tmp):
    """normalized.md from sid_B must not appear when reading sid_A's data."""
    from paid import storage
    storage.PAID_DIR = paid_tmp

    sid_a = "aaaabbbbcccc"
    sid_b = "bbbbccccdddd"

    _write_session(paid_tmp, sid_a, "tg_alice", "SECRET_ALICE: Q3 budget $500k")
    _write_session(paid_tmp, sid_b, "tg_bob",   "SECRET_BOB: layoff plan confidential")

    d_a = session_dir(sid_a)
    content_a = (d_a / "normalized.md").read_text(encoding="utf-8")

    assert "SECRET_ALICE" in content_a
    assert "SECRET_BOB" not in content_a, "B's content leaked into A's normalized.md"


def test_r4_annotations_isolation(paid_tmp):
    """Annotations loaded for sid_A must not contain sid_B's findings."""
    from paid import storage
    from paid_review.core.annotation import load_annotations
    storage.PAID_DIR = paid_tmp

    sid_a = "aaaabbbbcccc"
    sid_b = "bbbbccccdddd"

    _write_session(paid_tmp, sid_a, "tg_alice", "Alice doc")
    _write_session(paid_tmp, sid_b, "tg_bob",   "Bob doc")

    from paid_review.core.state import session_dir
    anns_a = load_annotations(session_dir(sid_a))
    anns_b = load_annotations(session_dir(sid_b))

    ids_a = {a.id for a in anns_a}
    ids_b = {a.id for a in anns_b}

    assert ids_a.isdisjoint(ids_b), f"Annotation IDs overlap: {ids_a & ids_b}"
    for ann in anns_a:
        assert "BOB" not in ann.text.upper(), "B finding leaked into A annotations"
    for ann in anns_b:
        assert "ALICE" not in ann.text.upper(), "A finding leaked into B annotations"


def test_r4_prompt_does_not_contain_other_cp_data(paid_tmp, monkeypatch):
    """When handle_inbound builds an LLM prompt for sid_A, sid_B content absent."""
    from paid import storage, hermes_io
    storage.PAID_DIR = paid_tmp

    sid_a = "aaaabbbbcccc"
    sid_b = "bbbbccccdddd"

    _write_session(paid_tmp, sid_a, "tg_alice", "ALICE_ONLY_CONTENT")
    _write_session(paid_tmp, sid_b, "tg_bob",   "BOB_PRIVATE_CONTENT")

    captured_prompts: list[str] = []

    def fake_llm(**kw):
        prompt = kw.get("user_message", "") + kw.get("system_prompt", "")
        captured_prompts.append(prompt)
        # Return empty findings list so SCAN short-circuits or returns minimal data
        return json.dumps([])

    monkeypatch.setattr(hermes_io, "call_llm", fake_llm)

    # Drive sid_A through INTAKE → SUBJECT (which calls LLM)
    from paid_review.api import handle_inbound

    # INTAKE stage: provide initial message for A
    reply_intake = handle_inbound(sid_a, "ALICE_ONLY_CONTENT", {})

    all_prompt_text = " ".join(captured_prompts)

    assert "BOB_PRIVATE_CONTENT" not in all_prompt_text, (
        "B's private content appeared in A's LLM prompt"
    )


def test_r4_state_isolation(paid_tmp):
    """load_state for sid_A returns A's data; sid_B's meta is not accessible."""
    from paid import storage
    from paid_review.core.state import load_state
    storage.PAID_DIR = paid_tmp

    sid_a = "aaaabbbbcccc"
    sid_b = "bbbbccccdddd"

    _write_session(paid_tmp, sid_a, "tg_alice", "")
    _write_session(paid_tmp, sid_b, "tg_bob",   "")

    state_a = load_state(sid_a)
    state_b = load_state(sid_b)

    assert state_a is not None
    assert state_b is not None
    assert state_a.cp_id == "tg_alice"
    assert state_b.cp_id == "tg_bob"
    assert state_a.sid != state_b.sid
    # A cannot see B's subject through load_state
    assert state_a.subject != state_b.subject or (
        state_a.subject == state_b.subject is None
    )
