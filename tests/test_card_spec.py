"""Tests for paid.card_spec — platform-agnostic approval card spec."""

from __future__ import annotations

import pytest

from paid import approval, card_spec


# --------------------------------------------------------------------------
# confidence_badge / stakes_badge — visual hint boundaries
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "conf,expected",
    [
        (0.0, "🔴"),
        (0.49, "🔴"),
        (0.5, "🟡"),
        (0.74, "🟡"),
        (0.75, "🟢"),
        (1.0, "🟢"),
    ],
)
def test_confidence_badge_thresholds(conf, expected):
    assert card_spec._confidence_badge(conf) == expected


@pytest.mark.parametrize(
    "stakes,expected_substring",
    [
        ("high", "HIGH"),
        ("HIGH", "HIGH"),
        ("medium", "medium"),
        ("low", "low"),
        ("", "?"),
        ("unknown", "unknown"),  # passes through if not low/medium/high
    ],
)
def test_stakes_badge(stakes, expected_substring):
    assert expected_substring in card_spec._stakes_badge(stakes)


# --------------------------------------------------------------------------
# ApprovalCardSpec dataclass
# --------------------------------------------------------------------------


def test_spec_dataclass_defaults():
    spec = card_spec.ApprovalCardSpec(
        request_id="abc123",
        junior_name="Evie",
        junior_platform="feishu",
        junior_msg="hello",
        topic="logistics",
        confidence=0.6,
        stakes="medium",
        draft="",
        has_draft=False,
    )
    assert spec.timeout_min == 30
    assert spec.instructions == ""
    assert spec.sources == []


def test_spec_confidence_label_and_pill():
    spec = card_spec.ApprovalCardSpec(
        request_id="x", junior_name="J", junior_platform="telegram",
        junior_msg="m", topic="t", confidence=0.65, stakes="medium",
        draft="d", has_draft=True,
    )
    assert spec.confidence_pill() == "🟡"
    assert spec.confidence_label() == "🟡 0.65"
    assert "medium" in spec.stakes_pill()


def test_spec_header_title_includes_id():
    spec = card_spec.ApprovalCardSpec(
        request_id="abc123", junior_name="J", junior_platform="telegram",
        junior_msg="", topic="", confidence=0.5, stakes="low",
        draft="", has_draft=False,
    )
    assert "abc123" in spec.header_title()
    assert spec.header_title().startswith("📨")


# --------------------------------------------------------------------------
# from_pending_approval — field mapping + truncation + defaults
# --------------------------------------------------------------------------


def _make_req(**overrides):
    base = dict(
        request_id="r1",
        ts_created=1.0,
        counterparty_id="telegram_99",
        counterparty_platform="telegram",
        counterparty_user_id="99",
        counterparty_display="Junior J",
        junior_session_id="sess",
        junior_question="please review my Q3 plan draft, three sections",
        draft_answer="Sure thing — let me forward this to Jimmy.",
        topic="onboarding",
        stakes="medium",
        confidence=0.65,
    )
    base.update(overrides)
    return approval.PendingApproval(**base)


def test_from_pending_approval_field_mapping():
    req = _make_req()
    spec = card_spec.ApprovalCardSpec.from_pending_approval(req)
    assert spec.request_id == "r1"
    assert spec.junior_name == "Junior J"        # display preferred
    assert spec.junior_platform == "telegram"
    assert spec.topic == "onboarding"
    assert spec.confidence == 0.65
    assert spec.stakes == "medium"
    assert spec.has_draft is True
    assert spec.draft.startswith("Sure thing")
    assert spec.junior_msg.startswith("please review")


def test_from_pending_approval_no_display_falls_back_to_user_id():
    req = _make_req(counterparty_display="")
    spec = card_spec.ApprovalCardSpec.from_pending_approval(req)
    assert spec.junior_name == "99"


def test_from_pending_approval_empty_draft_marks_no_draft():
    req = _make_req(draft_answer="")
    spec = card_spec.ApprovalCardSpec.from_pending_approval(req)
    assert spec.has_draft is False
    assert spec.draft == ""


def test_from_pending_approval_whitespace_only_draft_marks_no_draft():
    """Sensitive topics come back with whitespace-only drafts on purpose."""
    req = _make_req(draft_answer="   \n  ")
    spec = card_spec.ApprovalCardSpec.from_pending_approval(req)
    assert spec.has_draft is False


def test_from_pending_approval_truncates_long_msg():
    long_msg = "x" * 1000
    req = _make_req(junior_question=long_msg)
    spec = card_spec.ApprovalCardSpec.from_pending_approval(req)
    assert len(spec.junior_msg) == card_spec._MAX_JUNIOR_MSG_CHARS
    assert spec.junior_msg == "x" * card_spec._MAX_JUNIOR_MSG_CHARS


def test_from_pending_approval_truncates_long_draft():
    long_draft = "y" * 1000
    req = _make_req(draft_answer=long_draft)
    spec = card_spec.ApprovalCardSpec.from_pending_approval(req)
    assert len(spec.draft) == card_spec._MAX_DRAFT_CHARS


def test_from_pending_approval_default_instructions_bilingual():
    req = _make_req()
    spec = card_spec.ApprovalCardSpec.from_pending_approval(req)
    assert "/paid-approve r1" in spec.instructions
    assert "/paid-reject r1" in spec.instructions
    assert "操作" in spec.instructions  # ZH
    assert "Action" in spec.instructions  # EN


def test_from_pending_approval_explicit_instructions_override():
    req = _make_req()
    spec = card_spec.ApprovalCardSpec.from_pending_approval(
        req, instructions="custom CTA"
    )
    assert spec.instructions == "custom CTA"


def test_from_pending_approval_explicit_timeout():
    req = _make_req()
    spec = card_spec.ApprovalCardSpec.from_pending_approval(req, timeout_min=15)
    assert spec.timeout_min == 15


def test_from_pending_approval_handles_missing_attrs_gracefully():
    """Duck-typed: callers can pass minimal stand-ins (used by formatters in
    tests / snapshots without the full PendingApproval baggage)."""

    class Stub:
        request_id = "s1"
        # Intentionally missing counterparty_display, draft_answer, etc.

    spec = card_spec.ApprovalCardSpec.from_pending_approval(Stub())
    assert spec.request_id == "s1"
    assert spec.junior_name == "(unknown)"
    assert spec.has_draft is False
    assert spec.confidence == 0.0
    assert spec.topic == "—"
