"""Slack Block-Kit button callback routing (v1.7.0 / M3.5.C-slack).

Covers:
  - (action_id, value) parser whitelisting + opt_<key> form
  - owner-gated authz: non-owner click → ephemeral "Not authorized" +
    no state change
  - approve click → _do_approve fires, card updated, blocks replaced
  - reject click → _do_reject fires
  - reply click → arms awaiting_input, no _do_approve / _do_reject
  - opt_<key> click → currently no-op (deferred to v1.7.2), but ack still
    happens and handler returns cleanly (does not crash)
  - malformed action_id → silently acked, no dispatch
  - chat_update failure → falls back to chat_postMessage
  - registration idempotent (only first call registers)
  - registration when slack adapter missing → no-op, flag stays False
    (retries next hook)
  - registration when AsyncApp.action raises → flag set, fatal_alert fired

Smoke (real-Slack-workspace) lives in RELEASE_v1.7.0_PLAN.md §4.2 — these
unit tests mock slack_bolt's AsyncApp + Slack body payloads so the suite
runs fully offline.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_plugin_module():
    """Re-import the plugin top-level module fresh so module-level state
    (``_callback_registered``) doesn't bleed across tests."""
    spec = importlib.util.spec_from_file_location(
        "paid_v1_slack_test_module", _ROOT / "__init__.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mock_owner(monkeypatch, plugin, owner_slack_user_id: str = "U_OWNER"):
    """Wire identity.is_owner so ("slack", owner_slack_user_id) returns True."""
    def is_owner(platform, sender_id):
        return platform == "slack" and sender_id == owner_slack_user_id
    monkeypatch.setattr(plugin.identity, "is_owner", is_owner)


def _make_body(
    *,
    action_id: str,
    value: str,
    user_id: str,
    channel_id: str = "D_OWNER_DM",
    message_ts: str = "1715812345.000100",
) -> dict:
    """Build a minimal Slack block_actions body payload (the subset of fields
    PAID's handler consumes)."""
    return {
        "actions": [{"action_id": action_id, "value": value}],
        "user": {"id": user_id},
        "container": {
            "type": "message",
            "channel_id": channel_id,
            "message_ts": message_ts,
        },
    }


def _make_client():
    """Build an AsyncMock client mimicking slack_bolt's web client."""
    client = MagicMock()
    client.chat_update = AsyncMock()
    client.chat_postMessage = AsyncMock()
    client.chat_postEphemeral = AsyncMock()
    return client


# ---------------------------------------------------------------------------
# (action_id, value) parser
# ---------------------------------------------------------------------------


def test_parse_paid_slack_action_valid():
    plugin = _fresh_plugin_module()
    assert plugin._parse_paid_slack_action("paid_approve", "abc12345") == ("approve", "abc12345")
    assert plugin._parse_paid_slack_action("paid_reject", "xyz99999") == ("reject", "xyz99999")
    assert plugin._parse_paid_slack_action("paid_reply", "abc12345") == ("reply", "abc12345")


def test_parse_paid_slack_action_opt_form():
    plugin = _fresh_plugin_module()
    # Options-block buttons use paid_opt_<key>, value is also the key.
    assert plugin._parse_paid_slack_action("paid_opt_a", "a") == ("opt", "a")
    assert plugin._parse_paid_slack_action("paid_opt_pass", "pass") == ("opt", "pass")


def test_parse_paid_slack_action_rejects_unknown_verb():
    plugin = _fresh_plugin_module()
    # Not whitelisted → None so a future prefix collision can't fire approval.
    assert plugin._parse_paid_slack_action("paid_drop", "abc12345") is None
    assert plugin._parse_paid_slack_action("paid_kill", "abc12345") is None


def test_parse_paid_slack_action_rejects_malformed():
    plugin = _fresh_plugin_module()
    assert plugin._parse_paid_slack_action("", "abc") is None
    assert plugin._parse_paid_slack_action("hermes_approve_once", "abc") is None
    assert plugin._parse_paid_slack_action("paid_approve", "") is None  # empty rid
    assert plugin._parse_paid_slack_action("paid_opt_", "x") is None    # empty key
    assert plugin._parse_paid_slack_action("paid_", "abc") is None       # no verb


# ---------------------------------------------------------------------------
# Authz
# ---------------------------------------------------------------------------


def test_callback_non_owner_ephemeral_no_dispatch(monkeypatch):
    plugin = _fresh_plugin_module()
    _mock_owner(monkeypatch, plugin, owner_slack_user_id="U_OWNER")

    do_approve = MagicMock(return_value="should not be called")
    monkeypatch.setattr(plugin, "_do_approve", do_approve)
    monkeypatch.setattr(plugin, "_do_reject", MagicMock())

    body = _make_body(action_id="paid_approve", value="abc12345", user_id="U_ATTACKER")
    client = _make_client()
    ack = AsyncMock()

    asyncio.run(plugin._on_paid_slack_action(ack, body, client))

    ack.assert_awaited_once()
    client.chat_postEphemeral.assert_awaited_once()
    eph_kwargs = client.chat_postEphemeral.await_args.kwargs
    assert eph_kwargs["user"] == "U_ATTACKER"
    assert "Not authorized" in eph_kwargs["text"]

    do_approve.assert_not_called()
    client.chat_update.assert_not_called()


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def test_callback_acks_first(monkeypatch):
    """Slack requires ack within 3s. Our handler must await ack() before
    any owner check or dispatch."""
    plugin = _fresh_plugin_module()
    _mock_owner(monkeypatch, plugin, owner_slack_user_id="U_OWNER")

    call_order: list[str] = []

    async def ack_impl():
        call_order.append("ack")

    def is_owner(platform, sender_id):
        call_order.append("owner_check")
        return platform == "slack" and sender_id == "U_OWNER"
    monkeypatch.setattr(plugin.identity, "is_owner", is_owner)

    monkeypatch.setattr(plugin, "_do_approve", MagicMock(return_value="ok"))
    monkeypatch.setattr(plugin.approval, "get", lambda rid: SimpleNamespace(
        draft_answer="Yes", junior_question="Q",
    ))

    body = _make_body(action_id="paid_approve", value="abc12345", user_id="U_OWNER")
    client = _make_client()
    ack = AsyncMock(side_effect=ack_impl)

    asyncio.run(plugin._on_paid_slack_action(ack, body, client))

    assert call_order[0] == "ack", f"ack must happen first; got {call_order}"


def test_callback_approve_dispatches_and_updates_card(monkeypatch):
    plugin = _fresh_plugin_module()
    _mock_owner(monkeypatch, plugin, owner_slack_user_id="U_OWNER")

    do_approve = MagicMock(return_value="PAID: #abc12345 approved → delivered to slack:U_JUN")
    monkeypatch.setattr(plugin, "_do_approve", do_approve)
    monkeypatch.setattr(plugin.approval, "get", lambda rid: SimpleNamespace(
        draft_answer="Yes — Pacific Time, 11am.",
        junior_question="Q",
    ))

    body = _make_body(action_id="paid_approve", value="abc12345", user_id="U_OWNER")
    client = _make_client()
    ack = AsyncMock()

    asyncio.run(plugin._on_paid_slack_action(ack, body, client))

    do_approve.assert_called_once_with("abc12345", override_text="")
    ack.assert_awaited_once()
    client.chat_update.assert_awaited_once()

    update_kwargs = client.chat_update.await_args.kwargs
    assert update_kwargs["channel"] == "D_OWNER_DM"
    assert update_kwargs["ts"] == "1715812345.000100"
    assert "approved" in update_kwargs["text"]
    assert "abc12345" in update_kwargs["text"]
    # blocks should be the simplified resolution block; no buttons remain
    blocks = update_kwargs["blocks"]
    assert isinstance(blocks, list) and len(blocks) >= 1
    block_types = {b.get("type") for b in blocks}
    assert "actions" not in block_types


def test_callback_approve_empty_draft_sends_default_agreement(monkeypatch):
    """Match TG behaviour: empty draft → _do_approve called with
    language-matched default agreement override."""
    plugin = _fresh_plugin_module()
    _mock_owner(monkeypatch, plugin, owner_slack_user_id="U_OWNER")

    do_approve = MagicMock(return_value="PAID: #x approved")
    monkeypatch.setattr(plugin, "_do_approve", do_approve)
    monkeypatch.setattr(plugin.approval, "get", lambda rid: SimpleNamespace(
        draft_answer="",
        junior_question="我下周四能在家办工吗？",
    ))

    body = _make_body(action_id="paid_approve", value="abc12345", user_id="U_OWNER")
    client = _make_client()
    ack = AsyncMock()

    asyncio.run(plugin._on_paid_slack_action(ack, body, client))

    do_approve.assert_called_once()
    call_kwargs = do_approve.call_args.kwargs
    assert call_kwargs.get("override_text") in ("可以的。", "Approved.")


def test_callback_reject_dispatches(monkeypatch):
    plugin = _fresh_plugin_module()
    _mock_owner(monkeypatch, plugin, owner_slack_user_id="U_OWNER")

    do_reject = MagicMock(return_value="PAID: #abc12345 rejected → delivered to slack:U_JUN")
    monkeypatch.setattr(plugin, "_do_reject", do_reject)
    do_approve_spy = MagicMock()
    monkeypatch.setattr(plugin, "_do_approve", do_approve_spy)

    body = _make_body(action_id="paid_reject", value="abc12345", user_id="U_OWNER")
    client = _make_client()
    ack = AsyncMock()

    asyncio.run(plugin._on_paid_slack_action(ack, body, client))

    do_reject.assert_called_once_with("abc12345")
    do_approve_spy.assert_not_called()
    client.chat_update.assert_awaited_once()


def test_callback_reply_arms_awaiting_input_no_state_change(monkeypatch):
    """✏️ Reply arms an awaiting_input slot scoped to the Slack channel
    where the card was clicked. _do_approve / _do_reject must NOT fire."""
    plugin = _fresh_plugin_module()
    _mock_owner(monkeypatch, plugin, owner_slack_user_id="U_OWNER")

    do_approve = MagicMock()
    do_reject = MagicMock()
    monkeypatch.setattr(plugin, "_do_approve", do_approve)
    monkeypatch.setattr(plugin, "_do_reject", do_reject)
    monkeypatch.setattr(plugin.identity, "load_owner", lambda: SimpleNamespace(
        identities=[{"platform": "slack", "user_id": "U_OWNER",
                     "home_chat_id": "D_OWNER_DM", "enabled": True}],
    ))
    monkeypatch.setattr(plugin.approval, "get", lambda rid: SimpleNamespace(
        counterparty_display="Evie", counterparty_user_id="U_EVIE",
        draft_answer="",
    ))

    body = _make_body(
        action_id="paid_reply", value="abc12345",
        user_id="U_OWNER", channel_id="D_OWNER_DM",
    )
    client = _make_client()
    ack = AsyncMock()

    asyncio.run(plugin._on_paid_slack_action(ack, body, client))

    do_approve.assert_not_called()
    do_reject.assert_not_called()
    assert plugin._AWAITING_INPUT  # armed
    update_text = client.chat_update.await_args.kwargs["text"]
    assert "等你输入" in update_text
    assert "abc12345" in update_text


def test_callback_opt_click_is_noop_in_v1_7_0(monkeypatch):
    """v1.7.2 will route opt → review skill; v1.7.0 acks + no-ops cleanly."""
    plugin = _fresh_plugin_module()
    _mock_owner(monkeypatch, plugin, owner_slack_user_id="U_OWNER")

    do_approve = MagicMock()
    do_reject = MagicMock()
    monkeypatch.setattr(plugin, "_do_approve", do_approve)
    monkeypatch.setattr(plugin, "_do_reject", do_reject)

    body = _make_body(action_id="paid_opt_a", value="a", user_id="U_OWNER")
    client = _make_client()
    ack = AsyncMock()

    asyncio.run(plugin._on_paid_slack_action(ack, body, client))

    ack.assert_awaited_once()
    do_approve.assert_not_called()
    do_reject.assert_not_called()
    # No card mutation either; junior fallback path still works via reply.
    client.chat_update.assert_not_called()
    client.chat_postMessage.assert_not_called()


def test_callback_malformed_action_acked_silently(monkeypatch):
    plugin = _fresh_plugin_module()
    _mock_owner(monkeypatch, plugin, owner_slack_user_id="U_OWNER")

    do_approve = MagicMock()
    monkeypatch.setattr(plugin, "_do_approve", do_approve)

    body = _make_body(action_id="paid_approve", value="", user_id="U_OWNER")  # empty rid
    client = _make_client()
    ack = AsyncMock()

    asyncio.run(plugin._on_paid_slack_action(ack, body, client))

    ack.assert_awaited_once()
    do_approve.assert_not_called()
    client.chat_update.assert_not_called()


def test_callback_chat_update_failure_falls_back_to_post_message(monkeypatch):
    plugin = _fresh_plugin_module()
    _mock_owner(monkeypatch, plugin, owner_slack_user_id="U_OWNER")

    monkeypatch.setattr(plugin, "_do_approve",
                        MagicMock(return_value="PAID: #abc12345 approved"))
    monkeypatch.setattr(plugin.approval, "get", lambda rid: SimpleNamespace(
        draft_answer="Yes", junior_question="Q",
    ))

    body = _make_body(action_id="paid_approve", value="abc12345", user_id="U_OWNER")
    client = _make_client()
    client.chat_update.side_effect = RuntimeError("message_not_found")
    ack = AsyncMock()

    asyncio.run(plugin._on_paid_slack_action(ack, body, client))

    # Fell through to chat_postMessage so the operator still sees the
    # outcome even when the original card can't be edited.
    client.chat_postMessage.assert_awaited_once()
    pm_kwargs = client.chat_postMessage.await_args.kwargs
    assert pm_kwargs["channel"] == "D_OWNER_DM"
    assert "approved" in pm_kwargs["text"]


def test_callback_dispatch_exception_still_updates_card(monkeypatch):
    """If _do_approve raises, the card must still update so the operator
    sees the error rather than the original pending card forever."""
    plugin = _fresh_plugin_module()
    _mock_owner(monkeypatch, plugin, owner_slack_user_id="U_OWNER")

    monkeypatch.setattr(plugin, "_do_approve",
                        MagicMock(side_effect=RuntimeError("dispatch boom")))
    monkeypatch.setattr(plugin.approval, "get", lambda rid: SimpleNamespace(
        draft_answer="Yes", junior_question="Q",
    ))

    body = _make_body(action_id="paid_approve", value="abc12345", user_id="U_OWNER")
    client = _make_client()
    ack = AsyncMock()

    asyncio.run(plugin._on_paid_slack_action(ack, body, client))

    client.chat_update.assert_awaited_once()
    update_text = client.chat_update.await_args.kwargs["text"]
    assert "internal error" in update_text
    assert "abc12345" in update_text


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_idempotent_after_first_success(monkeypatch):
    plugin = _fresh_plugin_module()

    fake_adapter = MagicMock()
    fake_app = MagicMock()
    fake_adapter._app = fake_app

    monkeypatch.setattr(
        plugin.hermes_io, "_get_gateway_adapter",
        lambda platform: fake_adapter,
    )

    # First call → calls app.action(constraint) and chains the returned
    # decorator with our handler.
    plugin._ensure_slack_callback_registered()
    assert fake_app.action.call_count == 1
    assert plugin._callback_registered["slack"] is True

    # Verify the constraint was a dict with action_id regex
    constraint_arg = fake_app.action.call_args.args[0]
    assert isinstance(constraint_arg, dict)
    assert "action_id" in constraint_arg
    # The decorator from app.action(...) was called with our handler.
    fake_app.action.return_value.assert_called_once()

    # Subsequent calls → no-op (flag check).
    plugin._ensure_slack_callback_registered()
    plugin._ensure_slack_callback_registered()
    assert fake_app.action.call_count == 1


def test_register_when_adapter_missing_keeps_retrying(monkeypatch):
    plugin = _fresh_plugin_module()

    def _no_adapter(platform):
        raise plugin.hermes_io.SendDmError("no live GatewayRunner")
    monkeypatch.setattr(plugin.hermes_io, "_get_gateway_adapter", _no_adapter)

    plugin._ensure_slack_callback_registered()
    # Flag stays False so next hook retries (adapter may come online late).
    assert plugin._callback_registered["slack"] is False


def test_register_when_app_not_built_yet_keeps_retrying(monkeypatch):
    plugin = _fresh_plugin_module()
    fake_adapter = MagicMock()
    fake_adapter._app = None
    monkeypatch.setattr(
        plugin.hermes_io, "_get_gateway_adapter",
        lambda platform: fake_adapter,
    )

    plugin._ensure_slack_callback_registered()
    assert plugin._callback_registered["slack"] is False


def test_register_when_action_raises_sets_flag_and_alerts(monkeypatch):
    plugin = _fresh_plugin_module()

    fake_adapter = MagicMock()
    fake_app = MagicMock()
    fake_app.action.side_effect = RuntimeError("Bolt app not initialized")
    fake_adapter._app = fake_app
    monkeypatch.setattr(
        plugin.hermes_io, "_get_gateway_adapter",
        lambda platform: fake_adapter,
    )

    alert_spy = MagicMock()
    monkeypatch.setattr(plugin, "_alert_owner", alert_spy)

    plugin._ensure_slack_callback_registered()

    assert plugin._callback_registered["slack"] is True  # don't keep retrying
    alert_spy.assert_called_once()
    alert_kwargs = alert_spy.call_args.kwargs
    assert alert_kwargs.get("reason") == "paid_slack_callback_register"


def test_register_slack_does_not_affect_telegram_flag(monkeypatch):
    """Cross-platform isolation: registering Slack must not flip TG's flag
    (and vice versa)."""
    plugin = _fresh_plugin_module()

    fake_adapter = MagicMock()
    fake_app = MagicMock()
    fake_adapter._app = fake_app
    monkeypatch.setattr(
        plugin.hermes_io, "_get_gateway_adapter",
        lambda platform: fake_adapter,
    )

    plugin._ensure_slack_callback_registered()
    assert plugin._callback_registered["slack"] is True
    assert plugin._callback_registered["telegram"] is False
