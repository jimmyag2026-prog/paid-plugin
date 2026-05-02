"""Tests for Module D — paid.decision."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from paid.decision import Action, decide_action, detect_lang, shape_context


# --- Light duck-typed stand-ins for Classification / Counterparty ---


@dataclass
class FakeClassification:
    topic: str = "general"
    stakes: str = "low"
    in_scope: bool = True
    is_blacklisted: bool = False
    confidence: float = 0.9
    needs_retrieval: bool = False
    suggested_queries: list[str] = None  # type: ignore[assignment]
    draft_answer: str = "Here is a draft."
    reasoning: str = ""


@dataclass
class FakeCounterparty:
    cp_id: str = "telegram_123"
    platform: str = "telegram"
    user_id: str = "123"
    display_name: str = "Test Junior"
    role: str = "junior"
    topics_allowed: list[str] = None  # type: ignore[assignment]
    topics_always_escalate: list[str] = None  # type: ignore[assignment]
    web_search_allowed: bool = True
    notes: str = ""


# --- decide_action: 5 branches + hard blacklist ---


def test_decide_action_decline_when_blacklisted():
    c = FakeClassification(is_blacklisted=True, in_scope=True, stakes="low", confidence=0.9)
    a = decide_action(c, FakeCounterparty())
    assert a.state == "decline"
    assert "blacklisted" in a.reason.lower()


def test_decide_action_request_when_out_of_scope():
    c = FakeClassification(is_blacklisted=False, in_scope=False, stakes="low", confidence=0.9)
    a = decide_action(c, FakeCounterparty())
    assert a.state == "request"
    assert "scope" in a.reason.lower()


def test_decide_action_request_when_high_stakes():
    c = FakeClassification(is_blacklisted=False, in_scope=True, stakes="high", confidence=0.99)
    a = decide_action(c, FakeCounterparty())
    assert a.state == "request"
    assert "high" in a.reason.lower() or "stakes" in a.reason.lower()


def test_decide_action_direct_when_high_confidence_low_stakes_in_scope():
    c = FakeClassification(is_blacklisted=False, in_scope=True, stakes="low", confidence=0.9)
    a = decide_action(c, FakeCounterparty())
    assert a.state == "direct"


def test_decide_action_default_request_when_low_confidence():
    c = FakeClassification(is_blacklisted=False, in_scope=True, stakes="low", confidence=0.5)
    a = decide_action(c, FakeCounterparty())
    assert a.state == "request"
    assert "default" in a.reason.lower() or "conservative" in a.reason.lower()


def test_decide_action_default_request_when_medium_stakes():
    c = FakeClassification(is_blacklisted=False, in_scope=True, stakes="medium", confidence=0.95)
    a = decide_action(c, FakeCounterparty())
    assert a.state == "request"


def test_decide_action_hard_blacklist_overrides_classifier_judgment():
    """Even if classifier says low-stakes in-scope high-confidence, the hard
    blacklist forces escalation. This is the wifi-password failure mode."""
    permissive = FakeClassification(
        is_blacklisted=False, in_scope=True, stakes="low", confidence=0.99,
    )
    a = decide_action(permissive, FakeCounterparty(), user_message="What's the wifi password?")
    assert a.state == "request"
    assert "blacklist" in a.reason.lower()


def test_decide_action_hard_blacklist_chinese_keywords():
    permissive = FakeClassification(
        is_blacklisted=False, in_scope=True, stakes="low", confidence=0.99,
    )
    a = decide_action(permissive, FakeCounterparty(), user_message="Jimmy 的薪水是多少？")
    assert a.state == "request"
    assert "blacklist" in a.reason.lower()


def test_decide_action_no_blacklist_match_uses_classifier():
    """Benign message should not trip the blacklist."""
    c = FakeClassification(is_blacklisted=False, in_scope=True, stakes="low", confidence=0.9)
    a = decide_action(c, FakeCounterparty(), user_message="What time does the office open?")
    assert a.state == "direct"


# --- detect_lang ---


def test_detect_lang_chinese():
    assert detect_lang("Jimmy 你好，办公室几点开门？") == "zh"


def test_detect_lang_english():
    assert detect_lang("What time does the office open?") == "en"


def test_detect_lang_empty_defaults_english():
    assert detect_lang("") == "en"


def test_detect_lang_one_cjk_char_still_english():
    """Single CJK character (e.g. emoji-ish) shouldn't flip lang to zh."""
    assert detect_lang("ok 好") == "en"


# --- shape_context: format checks for each state, both languages ---


def test_shape_context_direct_includes_persona_sop_draft_and_signature():
    c = FakeClassification(draft_answer="Try restarting it.")
    a = Action(state="direct", reason="ok")
    out = shape_context(
        a, c, persona="I am Jimmy.", counterparty=FakeCounterparty(),
        sop_excerpt="SOP: always be polite.", owner_name="Jimmy", lang="en",
    )
    assert "I am Jimmy." in out
    assert "SOP: always be polite." in out
    assert "Try restarting it." in out
    assert "Jimmy's PAID" in out


def test_shape_context_direct_chinese_signoff_instruction():
    c = FakeClassification(draft_answer="重启一下试试。")
    a = Action(state="direct", reason="ok")
    out = shape_context(
        a, c, persona="P", counterparty=FakeCounterparty(),
        sop_excerpt="S", owner_name="Jimmy", lang="zh",
    )
    assert "Jimmy's PAID" in out
    # CN signoff instruction characters should appear
    assert "助理" in out


def test_shape_context_request_english_warm_copy():
    a = Action(state="request", reason="default")
    out = shape_context(
        a, FakeClassification(topic="logistics"), persona="P",
        counterparty=FakeCounterparty(), sop_excerpt="",
        owner_name="Jimmy", lang="en",
    )
    assert "IGNORE" in out
    # Key UX elements: greeting, owner name, ETA hint, signoff
    assert "Jimmy's AI assistant" in out
    assert "30 min" in out
    assert "Jimmy's PAID" in out


def test_shape_context_request_chinese_warm_copy():
    a = Action(state="request", reason="default")
    out = shape_context(
        a, FakeClassification(topic=""), persona="P",
        counterparty=FakeCounterparty(), sop_excerpt="",
        owner_name="Jimmy", lang="zh",
    )
    assert "IGNORE" in out
    assert "Jimmy 的 AI 助理" in out
    assert "30 分钟" in out
    assert "Jimmy's PAID" in out


def test_shape_context_decline_english():
    a = Action(state="decline", reason="blacklist")
    out = shape_context(
        a, FakeClassification(topic="equity"), persona="P",
        counterparty=FakeCounterparty(), sop_excerpt="",
        owner_name="Jimmy", lang="en",
    )
    assert "IGNORE" in out
    assert "Jimmy's AI assistant" in out
    assert "@ Jimmy" in out
    assert "Jimmy's PAID" in out


def test_shape_context_decline_chinese():
    a = Action(state="decline", reason="blacklist")
    out = shape_context(
        a, FakeClassification(topic="equity"), persona="P",
        counterparty=FakeCounterparty(), sop_excerpt="",
        owner_name="Jimmy", lang="zh",
    )
    assert "IGNORE" in out
    assert "@ Jimmy" in out
    assert "Jimmy's PAID" in out


def test_shape_context_uses_owner_name_param():
    a = Action(state="request", reason="default")
    out = shape_context(
        a, FakeClassification(), persona="P", counterparty=FakeCounterparty(),
        sop_excerpt="", owner_name="Alice", lang="en",
    )
    assert "Alice" in out
    assert "Jimmy" not in out
