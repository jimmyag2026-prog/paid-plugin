"""Tests for v1.6.9 blockers from VPS manual testing.

V1: conv_capture._TRIGGER_PATTERNS missed the natural Chinese phrasing
    "以后客户问 pricing 直接拒绝，我不想看". v1.6.9 broadens the regex set.

V2: conv_capture._CC_SYSTEM prompt listed allowed fields but omitted
    topics.always_decline. Even if the regex had fired, the LLM had no
    way to know it could propose a decline entry; user's "直接拒绝"
    intent had no home.

V3: send_dm for feishu/lark + chat_id rid_type fell through to the
    generic async-adapter branch with ``fut.result(timeout=30)``. On
    live VPS every /paid-setup reply stalled ~30s waiting for the
    coroutine to resolve. v1.6.9 routes chat_id sends through the same
    synchronous direct-client path that open_id sends already use.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from paid import conv_capture, hermes_io


# ---------------------------------------------------------------------------
# V1 — trigger pattern coverage
# ---------------------------------------------------------------------------


def test_v1_trigger_catches_zh_decline_intent():
    """The exact live phrasing that failed pre-v1.6.9."""
    assert conv_capture.should_scan("以后客户问 pricing 直接拒绝，我不想看")


def test_v1_trigger_catches_zh_decline_variants():
    """Other natural variants of the same intent should also trigger."""
    cases = [
        "以后这种合同问题一律不要回复",
        "以后投资人问估值直接拒绝",
        "以后客户问报价都拒了",
        "以后这类话题统统不理",
        "以后客户问 hiring 我不想看",
        "以后这种事情懒得看",
    ]
    for c in cases:
        assert conv_capture.should_scan(c), f"missed: {c!r}"


def test_v1_trigger_catches_en_decline_intent():
    cases = [
        "From now on please reject pricing questions outright",
        "always decline budget adjustment asks going forward",
        "Just refuse hiring queries from now on",
    ]
    for c in cases:
        assert conv_capture.should_scan(c), f"missed: {c!r}"


def test_v1_trigger_does_not_fire_on_casual_chat():
    """False-positive sanity check — chitchat should not trigger."""
    cases = [
        "好的",
        "thanks",
        "ok 收到了",
        "hello",
        "what's for lunch",
    ]
    for c in cases:
        assert not conv_capture.should_scan(c), f"false positive: {c!r}"


# ---------------------------------------------------------------------------
# V2 — prompt allow-list includes topics.always_decline
# ---------------------------------------------------------------------------


def test_v2_cc_system_prompt_lists_always_decline():
    """The conv_capture system prompt must advertise topics.always_decline
    so the LLM can map "直接拒绝" intent to the right profile field."""
    assert "topics.always_decline" in conv_capture._CC_SYSTEM


def test_v2_cc_system_prompt_distinguishes_escalate_vs_decline():
    """The prompt should give the LLM a concrete distinction so it
    doesn't blur escalate (bother me each time) with decline (reject
    outright, do NOT bother me again)."""
    text = conv_capture._CC_SYSTEM
    assert "escalate" in text.lower()
    assert "decline" in text.lower()
    # Must explain the difference; one of the disambiguators should appear
    assert any(
        marker in text
        for marker in (
            "owner decide", "let them decide", "bother me",
            "Distinguishing", "vs decline",
        )
    )


def test_v2_cc_system_does_not_introduce_credentials():
    """Regression guard — prompt still excludes sensitive material."""
    text = conv_capture._CC_SYSTEM.lower()
    assert "credential" in text or "token" in text or "api key" in text


# ---------------------------------------------------------------------------
# V3 — feishu chat_id send_dm uses the synchronous direct path
# ---------------------------------------------------------------------------


def test_v3_feishu_chat_id_uses_send_lark_direct(monkeypatch):
    """feishu/lark + chat_id (oc_… or oc_xxx) must NOT route through the
    async adapter branch that uses fut.result(timeout=30). Routing must
    match the open_id branch and go through _send_lark_direct."""
    fake_adapter = MagicMock()

    calls = {"direct": 0, "async_adapter": 0}

    def _fake_direct(adapter, receive_id, message):
        calls["direct"] += 1
        return {"ok": True, "msg_id": "om_x", "platform": "feishu"}

    def _fake_run_coroutine_threadsafe(coro, loop):
        calls["async_adapter"] += 1
        raise AssertionError(
            "fast path regression: feishu chat_id send fell through to the "
            "async adapter branch instead of _send_lark_direct"
        )

    monkeypatch.setattr(hermes_io, "_get_gateway_adapter", lambda p: fake_adapter)
    monkeypatch.setattr(hermes_io, "_send_lark_direct", _fake_direct)
    monkeypatch.setattr(
        hermes_io, "_detect_lark_receive_id_type",
        lambda uid: "chat_id",
    )

    import asyncio
    monkeypatch.setattr(
        asyncio, "run_coroutine_threadsafe", _fake_run_coroutine_threadsafe,
    )

    result = hermes_io.send_dm("feishu", "oc_test_chat", "hello")
    assert result["ok"] is True
    assert calls["direct"] == 1
    assert calls["async_adapter"] == 0


def test_v3_feishu_chat_id_falls_back_to_standalone_on_direct_failure(monkeypatch):
    """If the direct adapter-client path raises, _send_lark_standalone should
    be tried before queueing."""
    fake_adapter = MagicMock()

    calls = {"direct": 0, "standalone": 0}

    def _raising_direct(adapter, receive_id, message):
        calls["direct"] += 1
        raise hermes_io.SendDmError("adapter._client gone")

    def _fake_standalone(receive_id, message):
        calls["standalone"] += 1
        return {"ok": True, "msg_id": "om_standalone", "platform": "feishu"}

    monkeypatch.setattr(hermes_io, "_get_gateway_adapter", lambda p: fake_adapter)
    monkeypatch.setattr(hermes_io, "_send_lark_direct", _raising_direct)
    monkeypatch.setattr(hermes_io, "_send_lark_standalone", _fake_standalone)
    monkeypatch.setattr(
        hermes_io, "_detect_lark_receive_id_type",
        lambda uid: "chat_id",
    )

    result = hermes_io.send_dm("feishu", "oc_test_chat", "hello")
    assert result["ok"] is True
    assert calls["direct"] == 1
    assert calls["standalone"] == 1


def test_v3_feishu_open_id_still_uses_direct(monkeypatch):
    """The pre-v1.6.9 open_id path is unchanged — make sure we didn't
    accidentally regress it while wiring the chat_id branch."""
    fake_adapter = MagicMock()
    calls = {"direct": 0}

    def _fake_direct(adapter, receive_id, message):
        calls["direct"] += 1
        return {"ok": True, "msg_id": "om_x", "platform": "feishu"}

    monkeypatch.setattr(hermes_io, "_get_gateway_adapter", lambda p: fake_adapter)
    monkeypatch.setattr(hermes_io, "_send_lark_direct", _fake_direct)
    monkeypatch.setattr(
        hermes_io, "_detect_lark_receive_id_type",
        lambda uid: "open_id",
    )

    result = hermes_io.send_dm("feishu", "ou_abc123", "hello")
    assert result["ok"] is True
    assert calls["direct"] == 1
