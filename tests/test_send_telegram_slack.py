"""Tests for hermes_io.send_telegram_card + send_slack_block.

Both functions follow the same pattern as send_lark_card:
  - Pull the live adapter via _get_gateway_adapter
  - Drop down to adapter._bot / adapter._app.client (adapter.send doesn't
    accept reply_markup / blocks)
  - Run the async coroutine via the same loop trampoline
  - Fall back to outbound_queue.jsonl when anything fails

Tests use monkeypatch to inject fake adapters since neither TG nor Slack
gateway is available in CI / paid user (no creds).
"""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

from paid import hermes_io


# python-telegram-bot may or may not be installed in the test env. Provide a
# minimal stub so the formatter import path inside send_telegram_card resolves
# to predictable objects we can assert on.

@pytest.fixture
def fake_telegram_module(monkeypatch):
    if "telegram" in sys.modules:
        # Real lib present (e.g. on hermes venv) — use it as-is.
        yield sys.modules["telegram"]
        return

    fake = types.ModuleType("telegram")

    class InlineKeyboardButton:
        def __init__(self, text=None, callback_data=None, url=None):
            self.text = text
            self.callback_data = callback_data
            self.url = url
        def __repr__(self):
            return f"FakeBtn({self.text!r}, cb={self.callback_data!r})"

    class InlineKeyboardMarkup:
        def __init__(self, rows):
            self.inline_keyboard = rows

    fake.InlineKeyboardButton = InlineKeyboardButton
    fake.InlineKeyboardMarkup = InlineKeyboardMarkup
    monkeypatch.setitem(sys.modules, "telegram", fake)
    yield fake


# --------------------------------------------------------------------------
# Fake adapter scaffolding
# --------------------------------------------------------------------------


class _FakeMessage:
    def __init__(self, message_id):
        self.message_id = message_id


class _FakeBot:
    """Stand-in for python-telegram-bot's Bot; records calls."""
    def __init__(self):
        self.calls = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeMessage(message_id=42)


class _FakeBotFails:
    async def send_message(self, **kwargs):
        raise RuntimeError("simulated TG outage")


class _FakeTGAdapter:
    def __init__(self, bot=None):
        self._bot = bot


class _FakeSlackResp(dict):
    """SlackResponse stand-in — both attr- and dict-accessible."""
    def __init__(self, *, ok=True, ts="1700000000.000100", error=""):
        super().__init__(ok=ok, ts=ts, error=error)
        self.ok = ok
        self.ts = ts
        self.error = error


class _FakeSlackClient:
    def __init__(self, response=None, raises=None):
        self._response = response or _FakeSlackResp()
        self._raises = raises
        self.calls = []

    async def chat_postMessage(self, **kwargs):
        self.calls.append(kwargs)
        if self._raises:
            raise self._raises
        return self._response


class _FakeSlackApp:
    def __init__(self, client=None):
        self.client = client


class _FakeSlackAdapter:
    def __init__(self, app=None):
        self._app = app


# --------------------------------------------------------------------------
# send_telegram_card
# --------------------------------------------------------------------------


def test_send_telegram_card_no_gateway_falls_back_to_queue(paid_tmp):
    """Default test environment: no live gateway → adapter lookup raises;
    must enqueue + return queued result, not crash."""
    result = hermes_io.send_telegram_card(
        chat_id="12345", text="hello", keyboard=None,
    )
    assert result["ok"] is False
    assert "queued" in result
    qp = paid_tmp / "outbound_queue.jsonl"
    assert qp.exists()
    last = json.loads(qp.read_text().strip().splitlines()[-1])
    assert last["platform"] == "telegram"


def test_send_telegram_card_no_gateway_raises_when_no_fallback(paid_tmp):
    with pytest.raises(hermes_io.SendDmError):
        hermes_io.send_telegram_card(
            chat_id="12345", text="hello", fallback_to_queue=False,
        )


def test_send_telegram_card_via_fake_adapter(paid_tmp, monkeypatch, fake_telegram_module):
    bot = _FakeBot()
    monkeypatch.setattr(
        hermes_io, "_get_gateway_adapter",
        lambda platform: _FakeTGAdapter(bot=bot),
    )
    result = hermes_io.send_telegram_card(
        chat_id="12345",
        text="hello world",
        keyboard=[
            [{"text": "✅ Approve", "callback_data": "paid_approve:r1"}],
            [{"text": "❌ Reject", "callback_data": "paid_reject:r1"}],
        ],
    )
    assert result["ok"] is True
    assert result["msg_id"] == "42"
    assert result["platform"] == "telegram"
    # Bot received chat_id, text, parse_mode, reply_markup
    assert len(bot.calls) == 1
    call = bot.calls[0]
    assert call["chat_id"] == "12345"
    assert call["text"] == "hello world"
    assert call["parse_mode"] == "Markdown"
    # reply_markup must be an InlineKeyboardMarkup-like instance
    assert call["reply_markup"] is not None
    # Two rows of buttons preserved
    assert len(call["reply_markup"].inline_keyboard) == 2
    assert call["reply_markup"].inline_keyboard[0][0].callback_data == "paid_approve:r1"


def test_send_telegram_card_keyboard_silently_drops_when_lib_missing(
    paid_tmp, monkeypatch
):
    """If python-telegram-bot is not importable, the keyboard should silently
    be dropped (reply_markup=None) rather than raising — the text still goes
    through. Real production env always has the lib (it's a hermes dep)."""
    bot = _FakeBot()
    monkeypatch.setitem(sys.modules, "telegram", None)  # block import
    monkeypatch.setattr(
        hermes_io, "_get_gateway_adapter",
        lambda platform: _FakeTGAdapter(bot=bot),
    )
    result = hermes_io.send_telegram_card(
        chat_id="12345", text="t",
        keyboard=[[{"text": "x", "callback_data": "y"}]],
    )
    assert result["ok"] is True
    assert bot.calls[0]["reply_markup"] is None


def test_send_telegram_card_no_keyboard_sends_plain(paid_tmp, monkeypatch):
    bot = _FakeBot()
    monkeypatch.setattr(
        hermes_io, "_get_gateway_adapter",
        lambda platform: _FakeTGAdapter(bot=bot),
    )
    result = hermes_io.send_telegram_card(
        chat_id="999", text="just text", keyboard=None,
    )
    assert result["ok"] is True
    assert bot.calls[0]["reply_markup"] is None


def test_send_telegram_card_send_failure_falls_back(paid_tmp, monkeypatch):
    monkeypatch.setattr(
        hermes_io, "_get_gateway_adapter",
        lambda platform: _FakeTGAdapter(bot=_FakeBotFails()),
    )
    result = hermes_io.send_telegram_card(chat_id="999", text="t")
    assert result["ok"] is False
    assert "queued" in result
    assert "TG send_message raised" in result["error"]


def test_send_telegram_card_no_bot_attr_falls_back(paid_tmp, monkeypatch):
    """Adapter object exists but _bot is None (gateway not yet connected)."""
    monkeypatch.setattr(
        hermes_io, "_get_gateway_adapter",
        lambda platform: _FakeTGAdapter(bot=None),
    )
    result = hermes_io.send_telegram_card(chat_id="999", text="t")
    assert result["ok"] is False
    assert "no _bot" in result["error"]


# --------------------------------------------------------------------------
# send_slack_block
# --------------------------------------------------------------------------


def test_send_slack_block_no_gateway_falls_back(paid_tmp):
    result = hermes_io.send_slack_block(
        channel="D12345",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}],
        fallback_text="hi from PAID",
    )
    assert result["ok"] is False
    assert "queued" in result
    qp = paid_tmp / "outbound_queue.jsonl"
    assert qp.exists()
    last = json.loads(qp.read_text().strip().splitlines()[-1])
    assert last["platform"] == "slack"


def test_send_slack_block_via_fake_adapter(paid_tmp, monkeypatch):
    client = _FakeSlackClient()
    monkeypatch.setattr(
        hermes_io, "_get_gateway_adapter",
        lambda platform: _FakeSlackAdapter(app=_FakeSlackApp(client=client)),
    )
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "PAID #1"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "test"}},
    ]
    result = hermes_io.send_slack_block(
        channel="D12345", blocks=blocks, fallback_text="PAID #1",
    )
    assert result["ok"] is True
    assert result["msg_id"] == "1700000000.000100"
    assert result["platform"] == "slack"
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["channel"] == "D12345"
    assert call["blocks"] == blocks
    assert call["text"] == "PAID #1"


def test_send_slack_block_default_fallback_text(paid_tmp, monkeypatch):
    """When fallback_text='' we still must pass a non-empty text to Slack."""
    client = _FakeSlackClient()
    monkeypatch.setattr(
        hermes_io, "_get_gateway_adapter",
        lambda platform: _FakeSlackAdapter(app=_FakeSlackApp(client=client)),
    )
    hermes_io.send_slack_block(
        channel="D1", blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "x"}}],
        fallback_text="",
    )
    assert client.calls[0]["text"] != ""


def test_send_slack_block_not_ok_response_falls_back(paid_tmp, monkeypatch):
    client = _FakeSlackClient(response=_FakeSlackResp(ok=False, ts=None, error="invalid_blocks"))
    monkeypatch.setattr(
        hermes_io, "_get_gateway_adapter",
        lambda platform: _FakeSlackAdapter(app=_FakeSlackApp(client=client)),
    )
    result = hermes_io.send_slack_block(
        channel="D1", blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "x"}}],
    )
    assert result["ok"] is False
    assert "queued" in result
    assert "not-ok" in result["error"]


def test_send_slack_block_chat_postmessage_raises_falls_back(paid_tmp, monkeypatch):
    client = _FakeSlackClient(raises=RuntimeError("simulated outage"))
    monkeypatch.setattr(
        hermes_io, "_get_gateway_adapter",
        lambda platform: _FakeSlackAdapter(app=_FakeSlackApp(client=client)),
    )
    result = hermes_io.send_slack_block(
        channel="D1", blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "x"}}],
    )
    assert result["ok"] is False
    assert "queued" in result
    assert "chat_postMessage raised" in result["error"]


def test_send_slack_block_no_app_falls_back(paid_tmp, monkeypatch):
    """Slack adapter object exists but _app/.client is None."""
    monkeypatch.setattr(
        hermes_io, "_get_gateway_adapter",
        lambda platform: _FakeSlackAdapter(app=None),
    )
    result = hermes_io.send_slack_block(
        channel="D1", blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "x"}}],
    )
    assert result["ok"] is False
    assert "no app.client" in result["error"]


def test_send_slack_block_raises_when_no_fallback(paid_tmp, monkeypatch):
    monkeypatch.setattr(
        hermes_io, "_get_gateway_adapter",
        lambda platform: _FakeSlackAdapter(app=None),
    )
    with pytest.raises(hermes_io.SendDmError):
        hermes_io.send_slack_block(
            channel="D1", blocks=[], fallback_to_queue=False,
        )
