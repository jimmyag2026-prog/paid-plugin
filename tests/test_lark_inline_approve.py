"""Lark inline-approve flow tests (v1.4.0).

Covers `_cmd_card`'s dispatch behavior:
  - approve, draft non-empty → _do_approve(draft) → owner DM'd confirmation
  - approve, draft empty → _do_approve(default agreement "可以的"/"Approved")
    → owner DM'd confirmation. NO awaiting_input armed (revised after
    user feedback 2026-05-13 — ✅ always means "yes, agree directly")
  - reply (renamed from "edit") → arms awaiting_input → only path that
    asks for owner input. Legacy alias "edit" still accepted by dispatcher.
  - reply capture: owner's next plain text → _do_approve with override
  - reject → always direct → _do_reject → owner DM'd
  - unknown request_id → owner DM'd with error
  - already-resolved request → owner DM'd with status, no second dispatch
  - chat_id scoping: owner text in different chat does NOT consume slot
  - TTL: text after 30 min does NOT consume slot
  - /paid-cancel-input: clears armed slot
  - default agreement text matches junior question language (zh vs en)
"""

from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _fresh_plugin():
    """Reload plugin module so _AWAITING_INPUT doesn't bleed across tests."""
    spec = importlib.util.spec_from_file_location(
        "paid_v1_lark_inline_test", _ROOT / "__init__.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_pending(plugin, paid_tmp, **over):
    """Create a real PendingApproval via the approval module so .status,
    .draft_answer etc. round-trip through the jsonl log."""
    base = dict(
        counterparty_id="feishu_evie",
        counterparty_platform="feishu",
        counterparty_user_id="ou_evie",
        counterparty_display="Evie",
        junior_session_id="sess-1",
        junior_question="周五能早下班吗？",
        draft_answer="可以的，提前 1 小时跟我说一声就行。",  # case 1 default
        topic="logistics",
        stakes="low",
        confidence=0.85,
    )
    base.update(over)
    return plugin.approval.create(**base)


def _mock_owner_identity(monkeypatch, plugin, *, platform="feishu", uid="owner_lark"):
    """Make identity.load_owner() return a stub with one enabled identity +
    preferred_identity() returning it."""
    from types import SimpleNamespace

    pref = SimpleNamespace(platform=platform, home_chat_id=uid)
    owner = SimpleNamespace(
        identities=[
            {"platform": platform, "user_id": uid, "home_chat_id": uid, "enabled": True},
        ],
        preferred_identity=lambda: pref,
        display_name="Jimmy",
    )
    monkeypatch.setattr(plugin.identity, "load_owner", lambda: owner)
    monkeypatch.setattr(plugin.identity, "display_name", lambda o: "Jimmy")
    # _resolve_owner_lark_target → identity returns uid as-is in our stub
    monkeypatch.setattr(plugin.identity, "resolve_owner_lark_target", lambda u: u)
    monkeypatch.setattr(plugin.identity, "is_owner", lambda p, s: p == platform and s == uid)
    return owner


def _capture_owner_dms(monkeypatch, plugin):
    """Stub send_dm to record what we DM'd the owner; return the list."""
    sent: list[tuple[str, str, str]] = []
    def fake_send(platform, user_id, message, **kw):
        sent.append((platform, user_id, message))
        return {"ok": True, "msg_id": "stub", "platform": platform}
    monkeypatch.setattr(plugin.hermes_io, "send_dm", fake_send)
    return sent


def _payload(rid: str, action: str) -> str:
    """Build the raw_args string hermes' feishu adapter would synthesize."""
    import json
    return f'button {json.dumps({"paid_action": action, "request_id": rid})}'


# ---------------------------------------------------------------------------
# Case 1: approve with draft
# ---------------------------------------------------------------------------


def test_approve_with_draft_dispatches_directly(paid_tmp, monkeypatch):
    plugin = _fresh_plugin()
    _mock_owner_identity(monkeypatch, plugin)
    sent = _capture_owner_dms(monkeypatch, plugin)

    req = _make_pending(plugin, paid_tmp)  # has draft

    rv = plugin._cmd_card(_payload(req.request_id, "approve"))

    assert rv == ""  # return is no longer the path
    # Junior received draft + owner got confirmation.
    junior_send = [s for s in sent if s[1] == "ou_evie"]
    owner_send = [s for s in sent if s[1] == "owner_lark"]
    assert len(junior_send) == 1
    assert "可以的" in junior_send[0][2]  # draft body forwarded
    assert len(owner_send) == 1
    assert "approved" in owner_send[0][2]

    # Approval status updated to "approved" in the log.
    fresh = plugin.approval.get(req.request_id)
    assert fresh.status == "approved"

    # No awaiting_input armed.
    assert plugin._AWAITING_INPUT == {}


# ---------------------------------------------------------------------------
# Case 2: approve without draft → arm awaiting_input
# ---------------------------------------------------------------------------


def test_approve_without_draft_sends_default_agreement_zh(paid_tmp, monkeypatch):
    """Revised v1.4.0 (2026-05-13): ✅ Approve with empty draft DOES NOT
    prompt for owner input. It dispatches a language-matched default
    agreement to the junior — Chinese question gets "可以的"."""
    plugin = _fresh_plugin()
    _mock_owner_identity(monkeypatch, plugin)
    sent = _capture_owner_dms(monkeypatch, plugin)

    req = _make_pending(
        plugin, paid_tmp,
        draft_answer="",
        junior_question="我下周四能在家办工吗？",
    )

    rv = plugin._cmd_card(_payload(req.request_id, "approve"))

    assert rv == ""
    # Junior received default agreement immediately.
    junior_send = [s for s in sent if s[1] == "ou_evie"]
    owner_send = [s for s in sent if s[1] == "owner_lark"]
    assert len(junior_send) == 1
    assert "可以的" in junior_send[0][2]
    # Owner got confirmation; NO "type your reply" prompt.
    assert len(owner_send) == 1
    assert "approved" in owner_send[0][2]
    assert "等你输入" not in owner_send[0][2]

    # No awaiting_input slot armed — owner doesn't need to type anything.
    assert plugin._AWAITING_INPUT == {}

    fresh = plugin.approval.get(req.request_id)
    assert fresh.status == "approved"


def test_approve_without_draft_sends_default_agreement_en(paid_tmp, monkeypatch):
    """English-language junior question → "Approved." default."""
    plugin = _fresh_plugin()
    _mock_owner_identity(monkeypatch, plugin)
    sent = _capture_owner_dms(monkeypatch, plugin)

    req = _make_pending(
        plugin, paid_tmp,
        draft_answer="",
        junior_question="Can I work from home next Thursday?",
    )

    plugin._cmd_card(_payload(req.request_id, "approve"))

    junior_send = [s for s in sent if s[1] == "ou_evie"]
    assert len(junior_send) == 1
    assert "Approved" in junior_send[0][2]


def test_reply_action_arms_awaiting_input(paid_tmp, monkeypatch):
    """✏️ Reply is the only button that prompts owner for input."""
    plugin = _fresh_plugin()
    _mock_owner_identity(monkeypatch, plugin)
    sent = _capture_owner_dms(monkeypatch, plugin)

    req = _make_pending(plugin, paid_tmp)  # has draft; reply ignores it anyway

    rv = plugin._cmd_card(_payload(req.request_id, "reply"))

    assert rv == ""
    owner_send = [s for s in sent if s[1] == "owner_lark"]
    junior_send = [s for s in sent if s[1] == "ou_evie"]
    assert len(owner_send) == 1
    assert "等你输入" in owner_send[0][2]
    assert len(junior_send) == 0  # owner hasn't typed yet
    assert plugin._AWAITING_INPUT
    fresh = plugin.approval.get(req.request_id)
    assert fresh.status == "pending"


def test_legacy_edit_action_still_routes_to_reply(paid_tmp, monkeypatch):
    """Cards rendered before v1.4.0-r2 carry paid_action=edit. The
    dispatcher must still accept it as an alias for reply so an
    in-flight card doesn't become a dead button."""
    plugin = _fresh_plugin()
    _mock_owner_identity(monkeypatch, plugin)
    sent = _capture_owner_dms(monkeypatch, plugin)

    req = _make_pending(plugin, paid_tmp)
    plugin._cmd_card(_payload(req.request_id, "edit"))

    assert plugin._AWAITING_INPUT
    owner_send = [s for s in sent if s[1] == "owner_lark"]
    assert any("等你输入" in s[2] for s in owner_send)


# ---------------------------------------------------------------------------
# Case 2 capture: owner's next text becomes the answer
# ---------------------------------------------------------------------------


def _make_event(text: str, platform: str = "feishu", user_id: str = "owner_lark",
                chat_id: str = "chat_default"):
    from types import SimpleNamespace
    plat_enum = SimpleNamespace(value=platform)
    src = SimpleNamespace(
        platform=plat_enum, user_id=user_id, chat_id=chat_id,
    )
    return SimpleNamespace(source=src, text=text)


def test_owner_text_consumed_after_approve_case_2(paid_tmp, monkeypatch):
    plugin = _fresh_plugin()
    _mock_owner_identity(monkeypatch, plugin)
    sent = _capture_owner_dms(monkeypatch, plugin)

    req = _make_pending(plugin, paid_tmp, draft_answer="")
    # Use ✏️ Reply to arm awaiting_input — ✅ Approve now sends default
    # agreement immediately without prompting.
    plugin._cmd_card(_payload(req.request_id, "reply"))

    # Owner now types their answer.
    sent.clear()
    event = _make_event("好的可以，提前来问我一下", chat_id="chat_default")
    rv = plugin.on_pre_gateway_dispatch(event=event)

    # Captured → returns skip to suppress normal dispatch.
    assert rv == {"action": "skip", "reason": "paid_owner_input_consumed"}
    junior_send = [s for s in sent if s[1] == "ou_evie"]
    owner_send = [s for s in sent if s[1] == "owner_lark"]
    assert len(junior_send) == 1
    assert "好的可以" in junior_send[0][2]
    assert len(owner_send) == 1
    assert "approved with your reply" in owner_send[0][2]

    fresh = plugin.approval.get(req.request_id)
    assert fresh.status == "approved"
    # Slot is now cleared.
    assert plugin._AWAITING_INPUT == {}


def test_owner_slash_text_does_not_consume_awaiting_input(paid_tmp, monkeypatch):
    plugin = _fresh_plugin()
    _mock_owner_identity(monkeypatch, plugin)
    sent = _capture_owner_dms(monkeypatch, plugin)

    req = _make_pending(plugin, paid_tmp, draft_answer="")
    plugin._cmd_card(_payload(req.request_id, "reply"))

    sent.clear()
    # Owner types a slash command — must NOT be eaten as answer text.
    event = _make_event("/paid-pending")
    rv = plugin.on_pre_gateway_dispatch(event=event)

    assert rv is None  # let slash dispatcher handle it normally
    assert plugin._AWAITING_INPUT  # still armed
    fresh = plugin.approval.get(req.request_id)
    assert fresh.status == "pending"


def test_owner_text_in_different_chat_does_not_consume(paid_tmp, monkeypatch):
    plugin = _fresh_plugin()
    _mock_owner_identity(monkeypatch, plugin)
    sent = _capture_owner_dms(monkeypatch, plugin)

    req = _make_pending(plugin, paid_tmp, draft_answer="")
    # Card-click event's chat_id isn't reachable from _cmd_card's signature,
    # so the expected_chat_id is None — i.e., we DON'T do chat scoping in
    # the v1.4.0 default path. But once we have expected_chat_id, the
    # capture should reject mismatches. Verify by manually setting it.
    plugin._cmd_card(_payload(req.request_id, "reply"))
    # Manually inject expected_chat_id for this test.
    for k in list(plugin._AWAITING_INPUT.keys()):
        plugin._AWAITING_INPUT[k]["expected_chat_id"] = "card_chat"

    sent.clear()
    event = _make_event("好的", chat_id="some_other_chat")
    rv = plugin.on_pre_gateway_dispatch(event=event)

    # Different chat → don't consume; let it pass through normally.
    assert rv is None
    assert plugin._AWAITING_INPUT  # still armed
    fresh = plugin.approval.get(req.request_id)
    assert fresh.status == "pending"


def test_owner_text_after_ttl_does_not_consume(paid_tmp, monkeypatch):
    plugin = _fresh_plugin()
    _mock_owner_identity(monkeypatch, plugin)
    _capture_owner_dms(monkeypatch, plugin)

    req = _make_pending(plugin, paid_tmp, draft_answer="")
    plugin._cmd_card(_payload(req.request_id, "reply"))

    # Force the slot to look 31 min old.
    for k in plugin._AWAITING_INPUT:
        plugin._AWAITING_INPUT[k]["since_ts"] = time.time() - (31 * 60)

    event = _make_event("好的")
    rv = plugin.on_pre_gateway_dispatch(event=event)

    # Expired → slot cleared, pass-through.
    assert rv is None
    assert plugin._AWAITING_INPUT == {}
    fresh = plugin.approval.get(req.request_id)
    assert fresh.status == "pending"


# ---------------------------------------------------------------------------
# Reject path
# ---------------------------------------------------------------------------


def test_reject_dispatches_directly_no_awaiting_input(paid_tmp, monkeypatch):
    plugin = _fresh_plugin()
    _mock_owner_identity(monkeypatch, plugin)
    sent = _capture_owner_dms(monkeypatch, plugin)

    # Reject works even on no-draft requests — no draft needed.
    req = _make_pending(plugin, paid_tmp, draft_answer="")

    rv = plugin._cmd_card(_payload(req.request_id, "reject"))

    assert rv == ""
    junior_send = [s for s in sent if s[1] == "ou_evie"]
    owner_send = [s for s in sent if s[1] == "owner_lark"]
    assert len(junior_send) == 1
    assert "Jimmy" in junior_send[0][2]
    assert "直接回复" in junior_send[0][2]
    assert len(owner_send) == 1
    assert "rejected" in owner_send[0][2]

    fresh = plugin.approval.get(req.request_id)
    assert fresh.status == "rejected"
    assert plugin._AWAITING_INPUT == {}  # no slot armed


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_click_on_unknown_request_dms_owner_error(paid_tmp, monkeypatch):
    plugin = _fresh_plugin()
    _mock_owner_identity(monkeypatch, plugin)
    sent = _capture_owner_dms(monkeypatch, plugin)

    rv = plugin._cmd_card(_payload("doesnotexist", "approve"))

    assert rv == ""
    # Owner notified, no junior dispatch.
    owner_send = [s for s in sent if s[1] == "owner_lark"]
    junior_send = [s for s in sent if s[1] == "ou_evie"]
    assert len(junior_send) == 0
    assert len(owner_send) == 1
    assert "unknown request id" in owner_send[0][2]


def test_click_on_already_resolved_request_dms_owner_status(paid_tmp, monkeypatch):
    plugin = _fresh_plugin()
    _mock_owner_identity(monkeypatch, plugin)
    sent = _capture_owner_dms(monkeypatch, plugin)

    req = _make_pending(plugin, paid_tmp)
    plugin.approval.set_status(req.request_id, "rejected", final_text="…")

    rv = plugin._cmd_card(_payload(req.request_id, "approve"))

    assert rv == ""
    owner_send = [s for s in sent if s[1] == "owner_lark"]
    junior_send = [s for s in sent if s[1] == "ou_evie"]
    assert len(junior_send) == 0
    assert len(owner_send) == 1
    assert "already rejected" in owner_send[0][2]

    # No double-dispatch: junior didn't receive a second message.
    fresh = plugin.approval.get(req.request_id)
    assert fresh.status == "rejected"


def test_unknown_paid_action_logs_and_noops(paid_tmp, monkeypatch):
    plugin = _fresh_plugin()
    _mock_owner_identity(monkeypatch, plugin)
    sent = _capture_owner_dms(monkeypatch, plugin)

    req = _make_pending(plugin, paid_tmp)
    rv = plugin._cmd_card(_payload(req.request_id, "frobnicate"))

    assert rv == ""
    # Nothing sent — no junior, no owner DM. Just the log.
    assert sent == []
    fresh = plugin.approval.get(req.request_id)
    assert fresh.status == "pending"


def test_cancel_input_clears_armed_slot(paid_tmp, monkeypatch):
    plugin = _fresh_plugin()
    _mock_owner_identity(monkeypatch, plugin)
    _capture_owner_dms(monkeypatch, plugin)

    req = _make_pending(plugin, paid_tmp, draft_answer="")
    plugin._cmd_card(_payload(req.request_id, "reply"))
    assert plugin._AWAITING_INPUT  # armed

    rv = plugin._cmd_paid_cancel_input("")
    assert "cancelled pending input" in rv
    assert req.request_id in rv
    assert plugin._AWAITING_INPUT == {}


def test_cancel_input_when_none_armed_returns_no_pending(paid_tmp, monkeypatch):
    plugin = _fresh_plugin()
    _mock_owner_identity(monkeypatch, plugin)

    rv = plugin._cmd_paid_cancel_input("")
    assert "no pending input" in rv


# ---------------------------------------------------------------------------
# Owner pass-through preserved
# ---------------------------------------------------------------------------


def test_owner_text_without_awaiting_input_passes_through(paid_tmp, monkeypatch):
    """Pre-existing behavior: owner's messages flow normally to hermes when
    no inline-input slot is armed. The capture should NOT intercept
    arbitrary owner chat."""
    plugin = _fresh_plugin()
    _mock_owner_identity(monkeypatch, plugin)
    _capture_owner_dms(monkeypatch, plugin)

    event = _make_event("hi claude, write me a poem about cats")
    rv = plugin.on_pre_gateway_dispatch(event=event)
    assert rv is None  # owner pass-through
