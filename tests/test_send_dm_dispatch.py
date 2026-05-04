"""Tests for send_dm's platform-specific options_block dispatch (v1.2.0).

When a caller passes options_block:
  - telegram → send_telegram_card with inline keyboard
  - slack    → send_slack_block with action buttons
  - other    → plain-text path (existing v1.0.0 behaviour)

Card-path failures fall through to plain text so the recipient still sees
the message + bullets (queued via outbound_queue.jsonl when needed).
"""

from __future__ import annotations

import json

import pytest

from paid import hermes_io


_OPTIONS = [
    {"key": "a", "label": "accept"},
    {"key": "pass", "label": "skip"},
    {"key": "custom", "label": "free text"},
]


# --------------------------------------------------------------------------
# _options_block_to_* helpers (unit)
# --------------------------------------------------------------------------


def test_options_to_telegram_keyboard_one_row_per_option():
    rows = hermes_io._options_block_to_telegram_keyboard(_OPTIONS)
    assert len(rows) == 3
    # Each row is single-button, full-width (mobile readability).
    for row in rows:
        assert len(row) == 1


def test_options_to_telegram_keyboard_callback_data_prefix():
    rows = hermes_io._options_block_to_telegram_keyboard(_OPTIONS)
    cbs = [row[0]["callback_data"] for row in rows]
    assert cbs == ["paid_opt:a", "paid_opt:pass", "paid_opt:custom"]


def test_options_to_telegram_keyboard_skips_malformed():
    rows = hermes_io._options_block_to_telegram_keyboard(
        [{"key": "a", "label": "ok"}, "not-a-dict", {"key": ""}, None]
    )
    assert len(rows) == 1
    assert rows[0][0]["callback_data"] == "paid_opt:a"


def test_options_to_slack_blocks_shape():
    blocks = hermes_io._options_block_to_slack_blocks("hello", _OPTIONS)
    assert len(blocks) == 2
    assert blocks[0]["type"] == "section"
    assert blocks[0]["text"]["text"] == "hello"
    assert blocks[1]["type"] == "actions"
    elements = blocks[1]["elements"]
    assert len(elements) == 3
    action_ids = [e["action_id"] for e in elements]
    assert action_ids == ["paid_opt_a", "paid_opt_pass", "paid_opt_custom"]


def test_options_to_slack_blocks_no_options_omits_actions():
    blocks = hermes_io._options_block_to_slack_blocks("just text", [])
    # Only the section block; no actions block when no buttons.
    assert len(blocks) == 1
    assert blocks[0]["type"] == "section"


# --------------------------------------------------------------------------
# send_dm dispatch — telegram
# --------------------------------------------------------------------------


def test_send_dm_telegram_with_options_calls_card_path(paid_tmp, monkeypatch):
    """When options_block is present + platform=telegram, send_dm should
    call send_telegram_card and NOT fall through to the plain-text adapter
    path."""
    called = {}

    def fake_card(chat_id, text, *, keyboard=None, parse_mode="Markdown",
                  fallback_to_queue=True):
        called["card"] = {"chat_id": chat_id, "text": text,
                          "keyboard": keyboard, "parse_mode": parse_mode}
        return {"ok": True, "msg_id": "t1", "platform": "telegram"}

    monkeypatch.setattr(hermes_io, "send_telegram_card", fake_card)
    # No adapter — make sure send_dm doesn't reach the adapter path.
    monkeypatch.setattr(
        hermes_io, "_get_gateway_adapter",
        lambda p: (_ for _ in ()).throw(hermes_io.SendDmError("no gw in test")),
    )

    result = hermes_io.send_dm(
        "telegram", "12345", "finding 1: tighten ask",
        options_block=_OPTIONS,
    )
    assert result["ok"] is True
    assert result["platform"] == "telegram"
    assert "card" in called
    # message stays clean — options block went into keyboard, not appended
    assert called["card"]["text"] == "finding 1: tighten ask"
    assert len(called["card"]["keyboard"]) == 3


def test_send_dm_telegram_card_failure_falls_through_to_plain(paid_tmp, monkeypatch):
    """If send_telegram_card raises SendDmError (no live gateway), send_dm
    falls through to the plain-text path which queues with bullets."""
    def fake_card_raise(*a, **kw):
        raise hermes_io.SendDmError("no live gateway")

    monkeypatch.setattr(hermes_io, "send_telegram_card", fake_card_raise)
    # Plain path also fails (no adapter) → queue fallback.
    result = hermes_io.send_dm(
        "telegram", "12345", "msg body", options_block=_OPTIONS,
    )
    assert result["ok"] is False
    assert "queued" in result
    last = json.loads(
        (paid_tmp / "outbound_queue.jsonl").read_text().splitlines()[-1]
    )
    # Plain-text path must include both the body and the rendered bullets.
    assert "msg body" in last["message"]
    assert "(a) accept" in last["message"]


def test_send_dm_telegram_no_options_skips_card_path(paid_tmp, monkeypatch):
    """No options_block → no card dispatch (plain adapter path only)."""
    def fake_card(*a, **kw):
        raise AssertionError("send_telegram_card should NOT be called when no options_block")

    monkeypatch.setattr(hermes_io, "send_telegram_card", fake_card)
    result = hermes_io.send_dm("telegram", "12345", "plain only")
    # Falls through to adapter path → queued (no live gateway).
    assert result["ok"] is False
    assert "queued" in result


# --------------------------------------------------------------------------
# send_dm dispatch — slack
# --------------------------------------------------------------------------


def test_send_dm_slack_with_options_calls_block_path(paid_tmp, monkeypatch):
    called = {}

    def fake_block(channel, blocks, *, fallback_text="", fallback_to_queue=True):
        called["block"] = {"channel": channel, "blocks": blocks,
                           "fallback_text": fallback_text}
        return {"ok": True, "msg_id": "s1", "platform": "slack"}

    monkeypatch.setattr(hermes_io, "send_slack_block", fake_block)
    monkeypatch.setattr(
        hermes_io, "_get_gateway_adapter",
        lambda p: (_ for _ in ()).throw(hermes_io.SendDmError("no gw")),
    )

    result = hermes_io.send_dm(
        "slack", "D12345", "finding 1: tighten ask",
        options_block=_OPTIONS,
    )
    assert result["ok"] is True
    assert result["platform"] == "slack"
    assert "block" in called
    # Section block has the message; actions block has 3 buttons.
    blocks = called["block"]["blocks"]
    assert any(b["type"] == "section" for b in blocks)
    assert any(b["type"] == "actions" and len(b["elements"]) == 3 for b in blocks)
    # fallback_text matches body for accessibility / mobile push.
    assert called["block"]["fallback_text"] == "finding 1: tighten ask"


def test_send_dm_slack_block_failure_falls_through(paid_tmp, monkeypatch):
    def fake_block_raise(*a, **kw):
        raise hermes_io.SendDmError("slack down")

    monkeypatch.setattr(hermes_io, "send_slack_block", fake_block_raise)
    result = hermes_io.send_dm(
        "slack", "D12345", "body", options_block=_OPTIONS,
    )
    assert result["ok"] is False
    assert "queued" in result


# --------------------------------------------------------------------------
# send_dm dispatch — other platforms (lark / wecom / etc.) — plain only
# --------------------------------------------------------------------------


def test_send_dm_lark_with_options_uses_plain_path(paid_tmp, monkeypatch):
    """Lark dispatch is unchanged: PAID does NOT route options_block through
    send_lark_card here. Approval cards use a different code path that calls
    send_lark_card directly with a hand-built interactive card; for ad-hoc
    options_block messages we fall back to plain text (which still shows
    the bullets)."""
    def fake_card(*a, **kw):
        raise AssertionError("send_telegram_card should not be called for lark")

    def fake_block(*a, **kw):
        raise AssertionError("send_slack_block should not be called for lark")

    monkeypatch.setattr(hermes_io, "send_telegram_card", fake_card)
    monkeypatch.setattr(hermes_io, "send_slack_block", fake_block)

    result = hermes_io.send_dm(
        "lark", "ou_xxx", "body for lark", options_block=_OPTIONS,
    )
    # Will queue (no live gateway in test); message must contain bullets.
    assert result["ok"] is False
    last = json.loads(
        (paid_tmp / "outbound_queue.jsonl").read_text().splitlines()[-1]
    )
    assert "(a) accept" in last["message"]


def test_send_dm_wecom_with_options_uses_plain_path(paid_tmp):
    """Other-platform sanity: wecom always goes plain — no card senders for it."""
    result = hermes_io.send_dm(
        "wecom", "uid", "wechat hello", options_block=_OPTIONS,
    )
    assert result["ok"] is False
    last = json.loads(
        (paid_tmp / "outbound_queue.jsonl").read_text().splitlines()[-1]
    )
    assert "wechat hello" in last["message"]
    assert "(a) accept" in last["message"]
