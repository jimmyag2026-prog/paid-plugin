"""Tests for paid.classifier — Module C.

hermes_io.call_llm is mocked everywhere; no LLM/network calls.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from paid import classifier  # noqa: E402
from paid.classifier import Classification  # noqa: E402


# A minimal Counterparty stand-in (the real one lives in paid.identity).
@dataclass
class FakeCP:
    display_name: str = "alice"
    role: str = "junior"
    topics_allowed: list[str] = field(default_factory=lambda: ["logistics", "schedule"])
    topics_always_escalate: list[str] = field(
        default_factory=lambda: ["equity", "vesting", "salary"]
    )


def test_classification_dataclass_defaults_are_conservative():
    c = Classification()
    assert c.in_scope is False
    assert c.is_blacklisted is False
    assert c.confidence == 0.0
    assert c.draft_answer == ""
    assert c.suggested_queries == []


def test_classify_parses_well_formed_json():
    """Happy path: well-formed JSON from LLM → fields populated."""
    canned = {
        "topic": "logistics",
        "stakes": "low",
        "in_scope": True,
        "is_blacklisted": False,
        "confidence": 0.88,
        "needs_retrieval": False,
        "suggested_queries": ["office hours", "lunch break"],
        "draft_answer": "We start at 10am.",
        "reasoning": "matches topics_allowed",
    }
    with mock.patch.object(
        classifier.hermes_io, "call_llm", return_value=json.dumps(canned)
    ) as call_mock:
        result = classifier.classify(
            user_message="When does the office open?",
            counterparty=FakeCP(),
            owner_name="Jimmy",
            sop_excerpt="Office opens at 10am Mon–Fri.",
        )

    assert isinstance(result, Classification)
    assert result.topic == "logistics"
    assert result.stakes == "low"
    assert result.in_scope is True
    assert result.is_blacklisted is False
    assert result.confidence == pytest.approx(0.88)
    assert result.needs_retrieval is False
    assert result.suggested_queries == ["office hours", "lunch break"]
    assert result.draft_answer == "We start at 10am."
    assert result.reasoning == "matches topics_allowed"

    # Verify the prompt embedded the right context
    args, kwargs = call_mock.call_args
    sent_prompt = kwargs.get("prompt") or args[0]
    assert "Jimmy" in sent_prompt
    assert "alice" in sent_prompt
    assert "logistics" in sent_prompt  # topics_allowed
    assert "equity" in sent_prompt  # topics_always_escalate
    assert "When does the office open?" in sent_prompt
    assert "Office opens at 10am" in sent_prompt
    assert kwargs.get("json_mode") is True


def test_classify_strips_markdown_code_fences():
    """Some LLMs wrap JSON in ```json ... ``` — strip and parse."""
    fenced = (
        "```json\n"
        '{"topic":"vesting","stakes":"high","in_scope":false,'
        '"is_blacklisted":true,"confidence":0.95,"needs_retrieval":false,'
        '"suggested_queries":[],"draft_answer":"","reasoning":"sensitive"}\n'
        "```"
    )
    with mock.patch.object(classifier.hermes_io, "call_llm", return_value=fenced):
        result = classifier.classify(
            user_message="What's my vesting?",
            counterparty=FakeCP(),
            owner_name="Jimmy",
            sop_excerpt="",
        )
    assert result.topic == "vesting"
    assert result.is_blacklisted is True
    assert result.stakes == "high"
    assert result.confidence == pytest.approx(0.95)


def test_classify_malformed_json_returns_fallback():
    """Garbage from the LLM → conservative fallback Classification."""
    with mock.patch.object(
        classifier.hermes_io, "call_llm", return_value="not even json {{{"
    ):
        result = classifier.classify(
            user_message="anything",
            counterparty=FakeCP(),
            owner_name="Jimmy",
            sop_excerpt="",
        )
    assert isinstance(result, Classification)
    assert result.in_scope is False
    assert result.confidence == 0.0
    assert result.draft_answer == ""
    assert "malformed" in result.reasoning.lower()


def test_classify_llm_call_exception_returns_fallback():
    """If hermes_io raises, classifier still returns a safe fallback (never raises)."""
    with mock.patch.object(
        classifier.hermes_io,
        "call_llm",
        side_effect=RuntimeError("boom"),
    ):
        result = classifier.classify(
            user_message="hi",
            counterparty=FakeCP(),
            owner_name="Jimmy",
            sop_excerpt="",
        )
    assert isinstance(result, Classification)
    assert result.in_scope is False
    assert result.confidence == 0.0
    assert result.draft_answer == ""
    assert "boom" in result.reasoning


def test_classify_clamps_confidence_and_normalizes_stakes():
    """Out-of-range or unknown values get coerced into safe defaults."""
    weird = {
        "topic": "x",
        "stakes": "EXTREME",  # invalid → "medium"
        "in_scope": "yes",  # truthy non-bool → True
        "is_blacklisted": 0,  # falsy non-bool → False
        "confidence": 1.7,  # >1 → clamp to 1.0
        "needs_retrieval": True,
        "suggested_queries": ["a", None, "b"],  # None filtered
        "draft_answer": 42,  # non-str coerced
        "reasoning": "ok",
    }
    with mock.patch.object(
        classifier.hermes_io, "call_llm", return_value=json.dumps(weird)
    ):
        result = classifier.classify(
            user_message="q",
            counterparty=FakeCP(),
            owner_name="Jimmy",
            sop_excerpt="",
        )
    assert result.stakes == "medium"
    assert result.in_scope is True
    assert result.is_blacklisted is False
    assert result.confidence == 1.0
    assert result.suggested_queries == ["a", "b"]
    assert result.draft_answer == "42"


def test_classify_empty_response_returns_fallback():
    with mock.patch.object(classifier.hermes_io, "call_llm", return_value="   "):
        result = classifier.classify(
            user_message="q",
            counterparty=FakeCP(),
            owner_name="Jimmy",
            sop_excerpt="",
        )
    assert result.in_scope is False
    assert result.confidence == 0.0
    assert "empty" in result.reasoning.lower()


# ---- v1.3.2 H2: fallback rate counter for silent-degradation visibility ----


def test_fallback_rate_starts_empty():
    classifier.reset_classifier_history()
    fb, total, ratio = classifier.fallback_rate_recent()
    assert fb == 0 and total == 0 and ratio == 0.0


def test_fallback_rate_tracks_real_vs_fallback(monkeypatch):
    classifier.reset_classifier_history()
    # Mock the LLM to alternate: good JSON, then exception, good, exception
    good = json.dumps({
        "topic": "logistics", "stakes": "low", "in_scope": True,
        "is_blacklisted": False, "confidence": 0.9,
        "needs_retrieval": False, "suggested_queries": [],
        "draft_answer": "x", "reasoning": "ok",
    })
    calls = {"n": 0}

    def fake_llm(**kwargs):
        calls["n"] += 1
        if calls["n"] % 2 == 0:
            raise RuntimeError("LLM down")
        return good

    monkeypatch.setattr(classifier.hermes_io, "call_llm", fake_llm)

    cp = FakeCP(topics_allowed=["logistics"])
    for _ in range(4):
        classifier.classify("hi", cp, "Jimmy", "")

    fb, total, ratio = classifier.fallback_rate_recent()
    assert total == 4
    assert fb == 2  # 2 of 4 raised exception
    assert ratio == 0.5

    classifier.reset_classifier_history()


def test_fallback_rate_window_caps_at_100(monkeypatch):
    classifier.reset_classifier_history()
    monkeypatch.setattr(
        classifier.hermes_io, "call_llm",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("always fail")),
    )
    cp = FakeCP(topics_allowed=["logistics"])
    for _ in range(120):
        classifier.classify("msg", cp, "J", "")

    fb, total, ratio = classifier.fallback_rate_recent()
    assert total == 100  # capped
    assert fb == 100
    assert ratio == 1.0

    classifier.reset_classifier_history()


# ---------------------------------------------------------------------------
# v1.6.14 — internal people-management carve-out (jelabs pilot day-1)
# ---------------------------------------------------------------------------


def test_v1614_carveout_guidance_is_in_prompt():
    """Contract guard: the classifier prompt MUST carry the internal
    people-management carve-out. jelabs day-1 — cp asked "我想问问周三
    是否可以wfh？", classifier tagged logistics→direct→answered, but the
    owner only delegated external logistics, not authority over their
    own team's WFH. The fix is prompt-level; pin it so it can't silently
    regress (same philosophy as the v1.6.10 call_llm contract test)."""
    canned = {
        "topic": "logistics", "stakes": "low", "in_scope": False,
        "is_blacklisted": True, "confidence": 0.9, "needs_retrieval": False,
        "suggested_queries": [], "draft_answer": "", "reasoning": "internal",
    }
    with mock.patch.object(
        classifier.hermes_io, "call_llm", return_value=json.dumps(canned)
    ) as call_mock:
        classifier.classify(
            user_message="我想问问周三是否可以wfh？",
            counterparty=FakeCP(),
            owner_name="XiaEvie",
            sop_excerpt="(no WFH policy in SOP)",
        )
    args, kwargs = call_mock.call_args
    sent_prompt = kwargs.get("prompt") or args[0]
    low = sent_prompt.lower()
    assert "internal people-management carve-out" in low
    # The specific employee-management cases must be enumerated.
    assert "wfh" in low
    assert "leave" in low and "compensation" in low
    # And it must instruct in_scope=false + is_blacklisted=true for these.
    assert "in_scope=false and is_blacklisted=true" in low


def test_v1614_carveout_lets_llm_escalate_internal_wfh():
    """With the carve-out, the LLM is expected to return
    in_scope=false/is_blacklisted=true for an internal WFH ask. We mock
    that compliant response and assert the Classification carries it
    through (so the decision module will escalate, not auto-answer)."""
    canned = {
        "topic": "logistics", "stakes": "high", "in_scope": False,
        "is_blacklisted": True, "confidence": 0.92, "needs_retrieval": False,
        "suggested_queries": [], "draft_answer": "",
        "reasoning": "internal people-management — owner decision",
    }
    with mock.patch.object(
        classifier.hermes_io, "call_llm", return_value=json.dumps(canned)
    ):
        result = classifier.classify(
            user_message="周三能不能在家办公？",
            counterparty=FakeCP(),
            owner_name="XiaEvie",
            sop_excerpt="(no WFH policy)",
        )
    assert result.in_scope is False
    assert result.is_blacklisted is True
