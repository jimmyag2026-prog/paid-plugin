"""Tests for Module A (audit)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from paid import audit, identity


def _read_lines(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l]


def test_log_action_with_full_payload(paid_tmp: Path):
    cp = identity.ensure_counterparty("telegram", "6914282833", display_name="LM")

    @dataclass
    class Classification:
        topic: str
        stakes: str
        in_scope: bool
        is_blacklisted: bool
        confidence: float
        needs_retrieval: bool
        suggested_queries: list
        draft_answer: str
        reasoning: str

    @dataclass
    class Action:
        state: str
        reason: str

    cls = Classification(
        topic="onboarding",
        stakes="low",
        in_scope=True,
        is_blacklisted=False,
        confidence=0.9,
        needs_retrieval=False,
        suggested_queries=[],
        draft_answer="hi",
        reasoning="clearly junior asking onboarding",
    )
    act = Action(state="direct", reason="confidence>0.75 + low stakes")

    audit.log_action(
        "sess-1",
        cp,
        "How do I clock in?",
        cls,
        act,
        extra={"context_injected": "Persona:..."},
    )
    log_path = paid_tmp / "audit_log.jsonl"
    assert log_path.exists()
    rows = _read_lines(log_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == "sess-1"
    assert row["counterparty"] == "telegram_6914282833"
    assert row["platform"] == "telegram"
    assert row["junior_msg"] == "How do I clock in?"
    assert row["classification"]["topic"] == "onboarding"
    assert row["action"]["state"] == "direct"
    assert row["extra"] == {"context_injected": "Persona:..."}
    assert "T" in row["ts"]  # ISO 8601


def test_log_action_truncates_long_message(paid_tmp: Path):
    long_msg = "x" * 1200
    audit.log_action("s", None, long_msg, None, None)
    rows = _read_lines(paid_tmp / "audit_log.jsonl")
    assert len(rows) == 1
    assert rows[0]["junior_msg"] == "x" * 500
    assert rows[0]["counterparty"] is None
    assert rows[0]["platform"] is None
    assert rows[0]["classification"] is None
    assert rows[0]["action"] is None
    assert rows[0]["extra"] == {}


def test_log_action_appends_multiple(paid_tmp: Path):
    audit.log_action("s1", None, "a", None, None)
    audit.log_action("s2", None, "b", None, None, extra={"k": "v"})
    rows = _read_lines(paid_tmp / "audit_log.jsonl")
    assert len(rows) == 2
    assert rows[0]["session_id"] == "s1"
    assert rows[1]["session_id"] == "s2"
    assert rows[1]["extra"] == {"k": "v"}
