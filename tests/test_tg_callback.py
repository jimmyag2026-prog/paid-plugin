"""Telegram inline-keyboard button callback routing (v1.4.0 / M3.5.C).

Covers:
  - callback_data parser whitelisting
  - owner-gated authz: non-owner click → "Not authorized" + no state change
  - approve click → _do_approve fires, card edited, keyboard removed
  - reject click → _do_reject fires, card edited
  - edit click → operator told to use slash; no state mutation
  - malformed callback_data → silently acked, no dispatch
  - registration idempotent (only first call adds handler)
  - registration when telegram adapter missing → no-op, flag stays False
    (retries next hook)
  - registration when PTB CallbackQueryHandler import fails → flag set,
    fatal_alert fired (won't keep retrying)

Smoke (real-TG dogfood) lives in the PR body checklist — these unit tests
mock the PTB Application + Query objects so the test suite runs fully
offline.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
    # The plugin top-level is __init__.py — import by its package-style path
    # via importlib; since pyproject sets `paid-v1` package at the repo root,
    # we drop a sys.path entry and import __init__.py as a uniquely named
    # module here.
    spec = importlib.util.spec_from_file_location(
        "paid_v1_test_module", _ROOT / "__init__.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _install_fake_telegram_ext(monkeypatch):
    """Make ``from telegram.ext import CallbackQueryHandler`` succeed in
    tests where python-telegram-bot isn't installed.

    Returns the fake CallbackQueryHandler class so tests can assert it
    was instantiated.
    """
    class _FakeCallbackQueryHandler:
        def __init__(self, callback, pattern=None):
            self.callback = callback
            self.pattern = pattern

    fake_pkg = types.ModuleType("telegram")
    fake_ext = types.ModuleType("telegram.ext")
    fake_ext.CallbackQueryHandler = _FakeCallbackQueryHandler
    monkeypatch.setitem(sys.modules, "telegram", fake_pkg)
    monkeypatch.setitem(sys.modules, "telegram.ext", fake_ext)
    return _FakeCallbackQueryHandler


def _mock_owner(monkeypatch, plugin, owner_tg_user_id: str = "777"):
    """Wire identity.is_owner so ("telegram", owner_tg_user_id) returns True."""
    def is_owner(platform, sender_id):
        return platform == "telegram" and sender_id == owner_tg_user_id
    monkeypatch.setattr(plugin.identity, "is_owner", is_owner)


def _make_query(
    *,
    data: str,
    from_user_id: str,
    message_text: str = "Original card",
    chat_id: int = 12345,
):
    """Build a MagicMock that mimics telegram.Update.callback_query."""
    query = MagicMock()
    query.data = data
    query.from_user.id = from_user_id
    query.message.text = message_text
    query.message.chat_id = chat_id
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    return query


def _make_update_context(query):
    update = MagicMock()
    update.callback_query = query
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    return update, context


# ---------------------------------------------------------------------------
# callback_data parser
# ---------------------------------------------------------------------------


def test_parse_paid_callback_data_valid():
    plugin = _fresh_plugin_module()
    assert plugin._parse_paid_callback_data("paid_approve:abc12345") == ("approve", "abc12345")
    assert plugin._parse_paid_callback_data("paid_reject:xyz99999") == ("reject", "xyz99999")
    assert plugin._parse_paid_callback_data("paid_reply:abc12345") == ("reply", "abc12345")
    # Legacy "edit" alias for cards rendered before the v1.4 rename.
    assert plugin._parse_paid_callback_data("paid_edit:abc12345") == ("edit", "abc12345")


def test_parse_paid_callback_data_rejects_unknown_action():
    plugin = _fresh_plugin_module()
    # paid_DROP is not whitelisted — must return None so future prefix
    # collisions can't accidentally route through.
    assert plugin._parse_paid_callback_data("paid_drop:abc12345") is None
    assert plugin._parse_paid_callback_data("paid_kill:abc12345") is None


def test_parse_paid_callback_data_rejects_malformed():
    plugin = _fresh_plugin_module()
    assert plugin._parse_paid_callback_data("") is None
    assert plugin._parse_paid_callback_data(None) is None
    assert plugin._parse_paid_callback_data("ea:once:42") is None  # hermes prefix
    assert plugin._parse_paid_callback_data("paid_approve") is None  # no rid
    assert plugin._parse_paid_callback_data("paid_:abc") is None    # no action
    assert plugin._parse_paid_callback_data("paid_approve:") is None  # empty rid


# ---------------------------------------------------------------------------
# Authz
# ---------------------------------------------------------------------------


def test_callback_non_owner_rejected_no_dispatch(monkeypatch):
    plugin = _fresh_plugin_module()
    _mock_owner(monkeypatch, plugin, owner_tg_user_id="777")

    do_approve = MagicMock(return_value="should not be called")
    monkeypatch.setattr(plugin, "_do_approve", do_approve)
    monkeypatch.setattr(plugin, "_do_reject", MagicMock())

    query = _make_query(data="paid_approve:abc12345", from_user_id="999")  # not owner
    update, context = _make_update_context(query)

    asyncio.run(plugin._on_paid_telegram_callback(update, context))

    # Owner check failed → answers with rejection text, never calls _do_approve.
    query.answer.assert_awaited_once_with(text="⛔ Not authorized.")
    do_approve.assert_not_called()
    query.edit_message_text.assert_not_called()


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def test_callback_approve_dispatches_and_edits_card(monkeypatch):
    plugin = _fresh_plugin_module()
    _mock_owner(monkeypatch, plugin, owner_tg_user_id="777")

    do_approve = MagicMock(return_value="PAID: #abc12345 approved → delivered to telegram:111")
    monkeypatch.setattr(plugin, "_do_approve", do_approve)
    # Stub approval.get so the pre-call inspection (used to decide default
    # agreement vs draft) doesn't hit a real jsonl file.
    from types import SimpleNamespace
    monkeypatch.setattr(plugin.approval, "get", lambda rid: SimpleNamespace(
        draft_answer="Yes — Pacific Time, 11am.",  # non-empty → no override
        junior_question="Q",
    ))

    query = _make_query(
        data="paid_approve:abc12345",
        from_user_id="777",
        message_text="*PAID approval needed* [#abc12345]\n…",
    )
    update, context = _make_update_context(query)

    asyncio.run(plugin._on_paid_telegram_callback(update, context))

    # New v1.4 signature: _do_approve(rid, override_text=<empty when draft>)
    do_approve.assert_called_once_with("abc12345", override_text="")
    # Ack happened (no-text answer after authz pass — text-arg only on rejection)
    query.answer.assert_awaited_once_with()
    # Card edited: original retained + new outcome appended + buttons gone.
    query.edit_message_text.assert_awaited_once()
    kwargs = query.edit_message_text.await_args.kwargs
    assert "approved" in kwargs["text"]
    assert "abc12345" in kwargs["text"]
    assert "Original" in kwargs["text"] or "approval needed" in kwargs["text"]
    assert kwargs["reply_markup"] is None  # keyboard removed


def test_callback_approve_empty_draft_sends_default_agreement(monkeypatch):
    """v1.4 (rebased on Lark inline-approve): when the card's
    draft_answer is empty, ✅ Approve still dispatches immediately —
    with a language-matched default ("可以的" zh / "Approved" en) —
    rather than the v1.3 silent failure or the now-removed
    awaiting-input prompt."""
    plugin = _fresh_plugin_module()
    _mock_owner(monkeypatch, plugin, owner_tg_user_id="777")

    do_approve = MagicMock(return_value="PAID: #x approved")
    monkeypatch.setattr(plugin, "_do_approve", do_approve)
    from types import SimpleNamespace
    monkeypatch.setattr(plugin.approval, "get", lambda rid: SimpleNamespace(
        draft_answer="",
        junior_question="我下周四能在家办工吗？",
    ))

    query = _make_query(data="paid_approve:abc12345", from_user_id="777")
    update, context = _make_update_context(query)
    asyncio.run(plugin._on_paid_telegram_callback(update, context))

    # _do_approve called with the override text (default agreement).
    do_approve.assert_called_once()
    call_kwargs = do_approve.call_args.kwargs
    assert call_kwargs.get("override_text") in ("可以的。", "Approved.")


def test_callback_reject_dispatches(monkeypatch):
    plugin = _fresh_plugin_module()
    _mock_owner(monkeypatch, plugin, owner_tg_user_id="777")

    do_reject = MagicMock(return_value="PAID: #abc12345 rejected → delivered to telegram:111")
    monkeypatch.setattr(plugin, "_do_reject", do_reject)
    do_approve_spy = MagicMock()
    monkeypatch.setattr(plugin, "_do_approve", do_approve_spy)

    query = _make_query(data="paid_reject:abc12345", from_user_id="777")
    update, context = _make_update_context(query)

    asyncio.run(plugin._on_paid_telegram_callback(update, context))

    do_reject.assert_called_once_with("abc12345")
    do_approve_spy.assert_not_called()


def test_callback_reply_arms_awaiting_input_no_state_change(monkeypatch):
    """v1.4 (rebased on Lark inline-approve): ✏️ Reply arms an
    awaiting_input slot keyed by the owner's TG identity, scoped to the
    chat where the card was clicked. The owner's next plain-text reply
    in that chat is consumed by on_pre_gateway_dispatch and dispatched
    via _do_approve(override_text=...). The button click itself does
    NOT mutate approval state."""
    plugin = _fresh_plugin_module()
    _mock_owner(monkeypatch, plugin, owner_tg_user_id="777")

    do_approve = MagicMock()
    do_reject = MagicMock()
    monkeypatch.setattr(plugin, "_do_approve", do_approve)
    monkeypatch.setattr(plugin, "_do_reject", do_reject)
    # Stub identity.load_owner so _record_awaiting_input can resolve
    # owner identities to keys.
    from types import SimpleNamespace
    monkeypatch.setattr(plugin.identity, "load_owner", lambda: SimpleNamespace(
        identities=[{"platform": "telegram", "user_id": "777",
                     "home_chat_id": "777", "enabled": True}],
    ))
    # Stub approval.get so the junior_label resolves cleanly.
    monkeypatch.setattr(plugin.approval, "get", lambda rid: SimpleNamespace(
        counterparty_display="Evie", counterparty_user_id="ou_evie",
        draft_answer="",
    ))

    query = _make_query(data="paid_reply:abc12345", from_user_id="777")
    update, context = _make_update_context(query)

    asyncio.run(plugin._on_paid_telegram_callback(update, context))

    do_approve.assert_not_called()
    do_reject.assert_not_called()
    assert plugin._AWAITING_INPUT  # armed
    body = query.edit_message_text.await_args.kwargs["text"]
    assert "等你输入" in body
    assert "abc12345" in body


def test_callback_legacy_edit_action_still_routes_to_reply(monkeypatch):
    """Cards rendered before the v1.4 button rename carry
    paid_action=edit; dispatcher must still arm awaiting_input."""
    plugin = _fresh_plugin_module()
    _mock_owner(monkeypatch, plugin, owner_tg_user_id="777")

    monkeypatch.setattr(plugin, "_do_approve", MagicMock())
    from types import SimpleNamespace
    monkeypatch.setattr(plugin.identity, "load_owner", lambda: SimpleNamespace(
        identities=[{"platform": "telegram", "user_id": "777",
                     "home_chat_id": "777", "enabled": True}],
    ))
    monkeypatch.setattr(plugin.approval, "get", lambda rid: SimpleNamespace(
        counterparty_display="Evie", counterparty_user_id="ou_evie",
        draft_answer="",
    ))

    query = _make_query(data="paid_edit:abc12345", from_user_id="777")
    update, context = _make_update_context(query)
    asyncio.run(plugin._on_paid_telegram_callback(update, context))

    assert plugin._AWAITING_INPUT


def test_callback_malformed_data_acked_silently(monkeypatch):
    plugin = _fresh_plugin_module()
    _mock_owner(monkeypatch, plugin, owner_tg_user_id="777")

    do_approve = MagicMock()
    monkeypatch.setattr(plugin, "_do_approve", do_approve)

    query = _make_query(data="not_a_paid_action", from_user_id="777")
    update, context = _make_update_context(query)

    asyncio.run(plugin._on_paid_telegram_callback(update, context))

    # The pattern filter would catch non-paid_* prefixes before us in real
    # PTB; here we test that our handler defensively shrugs at malformed
    # paid_-prefixed data too (e.g., paid_approve with no rid).
    do_approve.assert_not_called()
    query.edit_message_text.assert_not_called()


def test_callback_edit_failure_falls_back_to_new_message(monkeypatch):
    plugin = _fresh_plugin_module()
    _mock_owner(monkeypatch, plugin, owner_tg_user_id="777")

    monkeypatch.setattr(plugin, "_do_approve", MagicMock(return_value="PAID: #abc12345 approved"))

    query = _make_query(data="paid_approve:abc12345", from_user_id="777")
    query.edit_message_text.side_effect = RuntimeError("message too old to edit")
    update, context = _make_update_context(query)

    asyncio.run(plugin._on_paid_telegram_callback(update, context))

    # Fell through to context.bot.send_message — operator still sees the
    # outcome even when the original card can't be edited.
    context.bot.send_message.assert_awaited_once()
    sm_kwargs = context.bot.send_message.await_args.kwargs
    assert sm_kwargs["chat_id"] == 12345
    assert "approved" in sm_kwargs["text"]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_idempotent_after_first_success(monkeypatch):
    plugin = _fresh_plugin_module()
    _install_fake_telegram_ext(monkeypatch)

    fake_adapter = MagicMock()
    fake_app = MagicMock()
    fake_adapter._app = fake_app

    monkeypatch.setattr(
        plugin.hermes_io, "_get_gateway_adapter",
        lambda platform: fake_adapter,
    )

    # First call → adds handler.
    plugin._ensure_telegram_callback_registered()
    assert fake_app.add_handler.call_count == 1
    assert plugin._callback_registered["telegram"] is True

    # Verify it was added to group=-1 so we run before hermes' catch-all.
    call_kwargs = fake_app.add_handler.call_args.kwargs
    assert call_kwargs.get("group") == -1

    # Subsequent calls → no-op (flag check).
    plugin._ensure_telegram_callback_registered()
    plugin._ensure_telegram_callback_registered()
    assert fake_app.add_handler.call_count == 1


def test_register_when_adapter_missing_keeps_retrying(monkeypatch):
    plugin = _fresh_plugin_module()

    def _no_adapter(platform):
        raise plugin.hermes_io.SendDmError("no live GatewayRunner")
    monkeypatch.setattr(plugin.hermes_io, "_get_gateway_adapter", _no_adapter)

    plugin._ensure_telegram_callback_registered()
    # Flag stays False so next hook retries (adapter may come online late).
    assert plugin._callback_registered["telegram"] is False


def test_register_when_app_not_built_yet_keeps_retrying(monkeypatch):
    plugin = _fresh_plugin_module()
    fake_adapter = MagicMock()
    fake_adapter._app = None  # connect() hasn't built it yet
    monkeypatch.setattr(
        plugin.hermes_io, "_get_gateway_adapter",
        lambda platform: fake_adapter,
    )

    plugin._ensure_telegram_callback_registered()
    assert plugin._callback_registered["telegram"] is False


def test_register_when_ptb_import_fails_sets_flag_and_alerts(monkeypatch):
    plugin = _fresh_plugin_module()

    fake_adapter = MagicMock()
    fake_app = MagicMock()
    fake_adapter._app = fake_app
    monkeypatch.setattr(
        plugin.hermes_io, "_get_gateway_adapter",
        lambda platform: fake_adapter,
    )

    # Force the inline import to fail by removing telegram.ext from sys.modules
    # and inserting a dummy that raises.
    fake_ext = types.ModuleType("telegram.ext")
    def _raise():
        raise ImportError("simulated missing CallbackQueryHandler")
    fake_ext.__getattr__ = lambda name: _raise()
    monkeypatch.setitem(sys.modules, "telegram.ext", fake_ext)

    alert_spy = MagicMock()
    monkeypatch.setattr(plugin, "_alert_owner", alert_spy)

    plugin._ensure_telegram_callback_registered()

    # Flag set so we don't keep retrying a broken import every inbound.
    assert plugin._callback_registered["telegram"] is True
    fake_app.add_handler.assert_not_called()
    alert_spy.assert_called_once()
    alert_kwargs = alert_spy.call_args.kwargs
    assert alert_kwargs.get("reason") == "paid_tg_callback_register"


def test_register_when_add_handler_raises_sets_flag_and_alerts(monkeypatch):
    plugin = _fresh_plugin_module()

    fake_adapter = MagicMock()
    fake_app = MagicMock()
    fake_app.add_handler.side_effect = RuntimeError("Application not initialized")
    fake_adapter._app = fake_app
    monkeypatch.setattr(
        plugin.hermes_io, "_get_gateway_adapter",
        lambda platform: fake_adapter,
    )

    alert_spy = MagicMock()
    monkeypatch.setattr(plugin, "_alert_owner", alert_spy)

    plugin._ensure_telegram_callback_registered()

    assert plugin._callback_registered["telegram"] is True  # don't retry
    alert_spy.assert_called_once()
