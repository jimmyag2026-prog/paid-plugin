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


# --- v1.2.5: direct-state hard rules against approval-mimicking phrasing ---
# Regression for 2026-05-12 dogfood: in direct state the LLM was emitting
# "已记录待确认 / 等待 Jimmy 确认" (faking an approval flow that doesn't
# exist) and "rewriting" SOP content (calendly.com/jimmy → jimmyyin). The
# hard-rule block in _direct_context tells the LLM both behaviours are
# forbidden — these tests assert the rules are present in the prompt.

_FAKE_ESCALATION_PHRASES_ZH = ["等待", "确认", "已记录", "转给"]
_FAKE_ESCALATION_PHRASES_EN = ["forward", "follow up", "awaiting", "logged"]


def test_direct_state_chinese_includes_hard_rules():
    out = shape_context(
        Action(state="direct", reason="ok"),
        FakeClassification(draft_answer="周一到周五 10:00-18:30"),
        persona="P", counterparty=FakeCounterparty(),
        sop_excerpt="logistics: 办公室 10:00-18:30",
        owner_name="Jimmy", lang="zh",
    )
    # Hard rule block header present
    assert "硬规则" in out
    # All three rules referenced (approval-mimicking, SOP-rewriting, no-fake-action)
    assert "不要假装走审批流程" in out or "审批流程" in out
    assert "SOP" in out
    # Owner name substituted for placeholder
    assert "Jimmy" in out
    # Specific forbidden phrasings listed so the LLM knows what NOT to emit
    for phrase in _FAKE_ESCALATION_PHRASES_ZH:
        assert phrase in out, f"hard-rule list missing forbidden phrase: {phrase}"


def test_direct_state_english_includes_hard_rules():
    out = shape_context(
        Action(state="direct", reason="ok"),
        FakeClassification(draft_answer="Office: Mon-Fri 10:00-18:30"),
        persona="P", counterparty=FakeCounterparty(),
        sop_excerpt="logistics: office 10-18:30",
        owner_name="Jimmy", lang="en",
    )
    assert "Hard rules" in out
    assert "approval flow" in out.lower() or "approval" in out.lower()
    assert "verbatim" in out or "SOP says X" in out
    for phrase in _FAKE_ESCALATION_PHRASES_EN:
        assert phrase.lower() in out.lower(), f"missing forbidden phrase: {phrase}"


def test_direct_state_hard_rules_use_actual_owner_name():
    """OWNER placeholder must be replaced — pilots have different names and
    a literal 'OWNER' in the prompt is confusing for the LLM."""
    out = shape_context(
        Action(state="direct", reason="ok"),
        FakeClassification(draft_answer="x"),
        persona="P", counterparty=FakeCounterparty(),
        sop_excerpt="", owner_name="Alice", lang="zh",
    )
    assert "Alice" in out
    # Literal OWNER token must not leak through (substitution failed if it does)
    assert "OWNER" not in out

    out_en = shape_context(
        Action(state="direct", reason="ok"),
        FakeClassification(draft_answer="x"),
        persona="P", counterparty=FakeCounterparty(),
        sop_excerpt="", owner_name="Alice", lang="en",
    )
    assert "Alice" in out_en
    assert "OWNER" not in out_en


def test_direct_state_request_decline_unaffected():
    """Hard-rules block is direct-only — request/decline still use the
    exact-reply override (no persona / SOP / rules leakage)."""
    req = shape_context(
        Action(state="request", reason="default"),
        FakeClassification(topic="logistics"), persona="P",
        counterparty=FakeCounterparty(), sop_excerpt="",
        owner_name="Jimmy", lang="zh",
    )
    assert "硬规则" not in req

    dec = shape_context(
        Action(state="decline", reason="blacklist"),
        FakeClassification(topic="equity"), persona="P",
        counterparty=FakeCounterparty(), sop_excerpt="",
        owner_name="Jimmy", lang="zh",
    )
    assert "硬规则" not in dec


def test_shape_context_request_english_warm_copy():
    a = Action(state="request", reason="default")
    out = shape_context(
        a, FakeClassification(topic="logistics"), persona="P",
        counterparty=FakeCounterparty(), sop_excerpt="",
        owner_name="Jimmy", lang="en",
    )
    assert "IGNORE" in out
    # v1.4.4: Format B wrap escapes apostrophes for the LLM prompt; the
    # delivered text (post-LLM-unwrap) is plain. Tests assert on the
    # logical content, allowing for escape characters in the wrapped form.
    out_logical = out.replace("\\'", "'")
    assert "Jimmy's AI assistant" in out_logical
    assert "30 min" in out_logical
    assert "Jimmy's PAID" in out_logical


def test_shape_context_request_chinese_warm_copy():
    a = Action(state="request", reason="default")
    out = shape_context(
        a, FakeClassification(topic=""), persona="P",
        counterparty=FakeCounterparty(), sop_excerpt="",
        owner_name="Jimmy", lang="zh",
    )
    assert "IGNORE" in out
    out_logical = out.replace("\\'", "'")
    assert "Jimmy 的 AI 助理" in out_logical
    assert "30 分钟" in out_logical
    assert "Jimmy's PAID" in out_logical


def test_shape_context_decline_english():
    a = Action(state="decline", reason="blacklist")
    out = shape_context(
        a, FakeClassification(topic="equity"), persona="P",
        counterparty=FakeCounterparty(), sop_excerpt="",
        owner_name="Jimmy", lang="en",
    )
    assert "IGNORE" in out
    out_logical = out.replace("\\'", "'")
    assert "Jimmy's AI assistant" in out_logical
    assert "@ Jimmy" in out_logical
    assert "Jimmy's PAID" in out_logical


def test_shape_context_decline_chinese():
    a = Action(state="decline", reason="blacklist")
    out = shape_context(
        a, FakeClassification(topic="equity"), persona="P",
        counterparty=FakeCounterparty(), sop_excerpt="",
        owner_name="Jimmy", lang="zh",
    )
    assert "IGNORE" in out
    out_logical = out.replace("\\'", "'")
    assert "@ Jimmy" in out_logical
    assert "Jimmy's PAID" in out_logical


def test_shape_context_uses_owner_name_param():
    a = Action(state="request", reason="default")
    out = shape_context(
        a, FakeClassification(), persona="P", counterparty=FakeCounterparty(),
        sop_excerpt="", owner_name="Alice", lang="en",
    )
    assert "Alice" in out
    assert "Jimmy" not in out


# ---------------------------------------------------------------------------
# v1.4.3: length-cap rule in _DIRECT_HARD_RULES (backlog v1.4.9)
# ---------------------------------------------------------------------------


def test_direct_hard_rules_zh_contains_length_cap():
    from paid import decision
    rules = decision._DIRECT_HARD_RULES_ZH
    # The new rule mentions sentence count + markdown ban
    assert "1-3" in rules or "1 句" in rules
    assert "markdown" in rules.lower()


def test_direct_hard_rules_en_contains_length_cap():
    from paid import decision
    rules = decision._DIRECT_HARD_RULES_EN
    assert "1-3 sentences" in rules
    assert "markdown" in rules.lower()


def test_direct_context_includes_length_rule(monkeypatch):
    from paid import decision
    ctx = decision._direct_context(
        persona="brief", sop_excerpt="x", draft="d",
        owner_name="X", lang="zh",
    )
    # The compiled context must carry the length rule downstream
    assert "1-3 句" in ctx or "1-3" in ctx


# ---------------------------------------------------------------------------
# v1.4.4: wrap_exact_reply canonical helper (backlog v1.4.2)
# ---------------------------------------------------------------------------


def test_wrap_exact_reply_produces_format_b():
    """v1.4.4 canonical wrap uses Format B wording (line-break-preserving)."""
    from paid.decision import wrap_exact_reply
    out = wrap_exact_reply("Hello world.")
    assert "preserving all line breaks" in out
    assert "Hello world." in out


def test_wrap_exact_reply_escapes_apostrophes():
    """Apostrophes inside the exact text must be escaped so they don't
    terminate the single-quoted instruction prematurely."""
    from paid.decision import wrap_exact_reply
    out = wrap_exact_reply("It's a test.")
    assert "It\\'s a test." in out  # escaped form in the prompt
    assert "It's a test." not in out  # raw form would break the wrap


def test_wrap_exact_reply_escapes_backslashes():
    from paid.decision import wrap_exact_reply
    out = wrap_exact_reply(r"path\to\file")
    # Backslashes are doubled so the LLM sees one backslash each
    assert r"path\\to\\file" in out


def test_init_wrap_reply_for_hermes_uses_unified_helper():
    """v1.4.4: __init__._wrap_reply_for_hermes delegates to wrap_exact_reply,
    no longer duplicating the IGNORE-prefix string."""
    from pathlib import Path as _Path
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "paid_init_for_test",
        _Path(__file__).resolve().parent.parent / "__init__.py",
    )
    _init = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(_init)

    out = _init._wrap_reply_for_hermes("Hi there.")
    assert isinstance(out, dict)
    assert "context" in out
    assert "preserving all line breaks" in out["context"]
    assert "Hi there." in out["context"]


# ---------------------------------------------------------------------------
# v1.4.5: per-cp blacklist_action routing (backlog v1.4.7)
# ---------------------------------------------------------------------------


@dataclass
class FakeCounterpartyWithBlacklist:
    cp_id: str = "telegram_xyz"
    platform: str = "telegram"
    user_id: str = "xyz"
    display_name: str = "Test"
    role: str = "junior"
    topics_allowed: list[str] = None  # type: ignore[assignment]
    topics_always_escalate: list[str] = None  # type: ignore[assignment]
    web_search_allowed: bool = True
    notes: str = ""
    blacklist_action: str = "decline"


def test_blacklist_action_decline_routes_to_decline():
    """Default 'decline' preserves pre-v1.4.5 behavior."""
    cls = FakeClassification(is_blacklisted=True, in_scope=False, stakes="high", confidence=1.0)
    cp = FakeCounterpartyWithBlacklist(blacklist_action="decline")
    a = decide_action(cls, cp)
    assert a.state == "decline"
    assert "blacklisted" in a.reason.lower()


def test_blacklist_action_request_routes_to_approval_card():
    """'request' setting sends blacklisted topics to owner's approval queue."""
    cls = FakeClassification(is_blacklisted=True, in_scope=False, stakes="high", confidence=1.0)
    cp = FakeCounterpartyWithBlacklist(blacklist_action="request")
    a = decide_action(cls, cp)
    assert a.state == "request"
    assert "escalat" in a.reason.lower()  # "escalating to owner"
    assert "blacklist_action=request" in a.reason


def test_blacklist_action_missing_defaults_to_decline():
    """Counterparty without the field (e.g. pre-v1.4.5 profile) defaults
    to 'decline' for backwards compatibility."""
    # FakeCounterparty (no blacklist_action attribute) — the original fixture
    cls = FakeClassification(is_blacklisted=True, in_scope=False, stakes="high", confidence=1.0)
    cp = FakeCounterparty()  # no blacklist_action attribute
    a = decide_action(cls, cp)
    assert a.state == "decline"


def test_blacklist_action_invalid_value_falls_back_to_decline():
    """Defensive: a malformed value (string typo, None) shouldn't crash —
    fall back to 'decline'."""
    cls = FakeClassification(is_blacklisted=True, in_scope=False, stakes="high", confidence=1.0)
    cp = FakeCounterpartyWithBlacklist(blacklist_action="bogus")
    a = decide_action(cls, cp)
    assert a.state == "decline"


def test_blacklist_action_case_insensitive():
    """'REQUEST' / 'Request' should work like 'request'."""
    cls = FakeClassification(is_blacklisted=True, in_scope=False, stakes="high", confidence=1.0)
    cp = FakeCounterpartyWithBlacklist(blacklist_action="REQUEST")
    a = decide_action(cls, cp)
    assert a.state == "request"
