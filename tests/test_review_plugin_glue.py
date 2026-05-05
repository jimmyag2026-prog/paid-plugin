"""Tests for Sprint D plugin glue — _maybe_route_to_review_skill (R1 critical).

These tests guard against the highest-risk regression in M1: pre_llm_call
re-route accidentally hijacking non-review inbound and breaking PAID's
J2 main path.

Loads the plugin entry via importlib (matches test_alert_owner.py pattern).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

_spec = importlib.util.spec_from_file_location(
    "paid_plugin_entry_review_router", _PLUGIN_ROOT / "__init__.py"
)
_plug = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_plug)

# Pre-import paid_review.api so monkeypatch.setattr works on the real
# module attribute. The router does `from paid_review import api as
# _review_api` which caches the attribute on the package object — so
# replacing sys.modules['paid_review.api'] doesn't help; we must patch
# attributes on the actual api module.
from paid_review import api as _review_api_module  # noqa: E402


def _make_cp(active_session: str = "", cp_id: str = "feishu_test"):
    """Lightweight cp stand-in. _maybe_route_to_review_skill only reads
    cp.active_review_session and cp.cp_id."""
    return SimpleNamespace(
        active_review_session=active_session,
        cp_id=cp_id,
        platform="feishu",
        user_id="test",
    )


# ==========================================================================
# Path 4: not a review-skill message → None (J2 fall-through)
# ==========================================================================


def test_normal_inbound_returns_none(paid_tmp):
    """Most critical regression test: random inbound from cp with no
    active session must return None so the J2 classifier path runs."""
    cp = _make_cp()
    out = _plug._maybe_route_to_review_skill(
        cp, "Jimmy 周五能开会吗？", {},
    )
    assert out is None


def test_empty_message_returns_none(paid_tmp):
    cp = _make_cp()
    assert _plug._maybe_route_to_review_skill(cp, "", {}) is None
    assert _plug._maybe_route_to_review_skill(cp, "   ", {}) is None


def test_message_starting_with_slash_but_not_review_returns_none(paid_tmp):
    """e.g. /paid-pending or some other slash command — not for us."""
    cp = _make_cp()
    assert _plug._maybe_route_to_review_skill(cp, "/help", {}) is None
    assert _plug._maybe_route_to_review_skill(cp, "/paid-pending", {}) is None


# ==========================================================================
# Path 1: /review cancel
# ==========================================================================


def test_cancel_with_no_active_session_friendly_reject(paid_tmp):
    cp = _make_cp(active_session="")
    out = _plug._maybe_route_to_review_skill(cp, "/review cancel", {})
    assert out is not None
    ctx = out["context"]
    assert "没有进行中的 review" in ctx
    # Must NOT touch identity (no active to clear)
    assert "active_review_session" not in dir(cp) or cp.active_review_session == ""


def test_cancel_with_active_session_force_closes(paid_tmp, monkeypatch):
    cp = _make_cp(active_session="sid_abc")
    calls = {"force_close": [], "clear": []}

    def fake_force_close(sid, reason):
        calls["force_close"].append({"sid": sid, "reason": reason})
        return f"已关闭 {sid}"

    def fake_clear(cp, archive):
        calls["clear"].append({"cp_id": cp.cp_id, "archive": archive})

    monkeypatch.setattr(_review_api_module, "force_close", fake_force_close)
    monkeypatch.setattr(_plug.identity, "clear_active_review_session", fake_clear)

    out = _plug._maybe_route_to_review_skill(cp, "/review cancel", {})
    assert out is not None
    assert "已关闭" in out["context"]
    assert calls["force_close"] == [{"sid": "sid_abc", "reason": "junior_cancel"}]
    assert len(calls["clear"]) == 1
    assert calls["clear"][0]["archive"]["closed_reason"] == "junior_cancel"


def test_cancel_short_form_r_cancel(paid_tmp, monkeypatch):
    cp = _make_cp(active_session="sid_x")
    monkeypatch.setattr(_review_api_module, "force_close",
                        lambda s, reason: "ok")
    monkeypatch.setattr(_plug.identity, "clear_active_review_session",
                        lambda cp, archive: None)
    out = _plug._maybe_route_to_review_skill(cp, "/r cancel", {})
    assert out is not None


# ==========================================================================
# Path 2: /review <subject> intake
# ==========================================================================


def test_review_with_active_session_refused(paid_tmp):
    """Junior already has session → refuse, don't try a second intake."""
    cp = _make_cp(active_session="sid_existing")
    out = _plug._maybe_route_to_review_skill(
        cp, "/review another topic", {},
    )
    assert out is not None
    assert "已有进行中的 review" in out["context"]
    assert "sid_existing" in out["context"]


def test_bare_review_command_asks_for_subject(paid_tmp, monkeypatch):
    """User types just `/review` with no subject → friendly prompt."""
    cp = _make_cp()
    out = _plug._maybe_route_to_review_skill(cp, "/review", {})
    assert out is not None
    assert "subject" in out["context"]


def test_bare_r_command_asks_for_subject(paid_tmp):
    cp = _make_cp()
    out = _plug._maybe_route_to_review_skill(cp, "/r", {})
    assert out is not None
    assert "subject" in out["context"]


def test_review_with_subject_calls_intake(paid_tmp, monkeypatch):
    cp = _make_cp(active_session="")
    calls = {"intake": [], "set": [], "handle": []}

    def fake_intake(*, cp, initial_message, attachments, classification=None):
        calls["intake"].append({"initial": initial_message,
                                "attachments": attachments})
        return "sid_new"

    def fake_handle(sid, text, hook_kwargs):
        calls["handle"].append({"sid": sid, "text": text})
        return SimpleNamespace(
            text="请确认 review 的主题: a/b/c/...",
            stage="SUBJECT", event_kind="subject_ask", closed=False,
        )

    monkeypatch.setattr(_review_api_module, "intake", fake_intake)
    monkeypatch.setattr(_review_api_module, "handle_inbound", fake_handle)
    monkeypatch.setattr(_plug.identity, "set_active_review_session",
                        lambda cp, sid: calls["set"].append({"sid": sid}))

    out = _plug._maybe_route_to_review_skill(
        cp, "/review Q3 OKR 草稿", {"attachments": [{"a": 1}]},
    )

    assert out is not None
    assert calls["intake"][0]["initial"] == "Q3 OKR 草稿"
    assert calls["intake"][0]["attachments"] == [{"a": 1}]
    assert calls["set"] == [{"sid": "sid_new"}]
    assert "请确认" in out["context"]


def test_review_intake_failure_friendly_message(paid_tmp, monkeypatch):
    cp = _make_cp()
    def boom_intake(**kw):
        raise RuntimeError("disk full")
    def nope_handle(*a, **kw):
        raise AssertionError("should not be called")

    monkeypatch.setattr(_review_api_module, "intake", boom_intake)
    monkeypatch.setattr(_review_api_module, "handle_inbound", nope_handle)
    monkeypatch.setattr(_plug.identity, "set_active_review_session",
                        lambda cp, sid: None)
    out = _plug._maybe_route_to_review_skill(cp, "/review topic", {})
    assert out is not None
    assert "失败" in out["context"]
    assert "disk full" in out["context"]


# ==========================================================================
# Path 3: active session, regular message → handle_inbound
# ==========================================================================


def test_active_session_routes_to_handle_inbound(paid_tmp, monkeypatch):
    cp = _make_cp(active_session="sid_active")
    calls = {"handle": []}

    def fake_handle(sid, text, hook_kwargs):
        calls["handle"].append({"sid": sid, "text": text})
        return SimpleNamespace(
            text="next finding: ...",
            stage="QA", event_kind="finding", closed=False,
        )
    monkeypatch.setattr(_review_api_module, "handle_inbound", fake_handle)

    out = _plug._maybe_route_to_review_skill(cp, "a", {"attachments": []})
    assert out is not None
    assert calls["handle"] == [{"sid": "sid_active", "text": "a"}]
    assert "next finding" in out["context"]


def test_active_session_closed_reply_clears_active(paid_tmp, monkeypatch):
    cp = _make_cp(active_session="sid_close")
    calls = {"clear": []}

    def fake_handle(sid, text, hook_kwargs):
        return SimpleNamespace(
            text="# 会前简报\n## 1. 议题摘要\nx",
            stage="CLOSED", event_kind="close_propose", closed=True,
        )
    monkeypatch.setattr(_review_api_module, "handle_inbound", fake_handle)
    monkeypatch.setattr(_plug.identity, "clear_active_review_session",
                        lambda cp, archive: calls["clear"].append(archive))

    out = _plug._maybe_route_to_review_skill(cp, "done", {})
    assert out is not None
    assert "会前简报" in out["context"]
    assert len(calls["clear"]) == 1
    assert calls["clear"][0]["sid"] == "sid_close"


def test_active_session_scan_progress_does_not_clear(paid_tmp, monkeypatch):
    """SCAN progress reply: stage=SCAN, closed=False — must NOT clear
    active_review_session (Ⓜ8 spec contract). Session continues."""
    cp = _make_cp(active_session="sid_scanning")
    calls = {"clear": []}

    def fake_handle(sid, text, hook_kwargs):
        return SimpleNamespace(
            text="扫描中，给我一会儿...",
            stage="SCAN", event_kind="scan_progress", closed=False,
        )
    monkeypatch.setattr(_review_api_module, "handle_inbound", fake_handle)
    monkeypatch.setattr(_plug.identity, "clear_active_review_session",
                        lambda cp, archive: calls["clear"].append(archive))

    out = _plug._maybe_route_to_review_skill(cp, "ok", {})
    assert out is not None
    assert "扫描中" in out["context"]
    # Critical: progress must NOT clear active
    assert calls["clear"] == []


def test_active_session_handle_inbound_exception_friendly(paid_tmp, monkeypatch):
    cp = _make_cp(active_session="sid_x")
    def boom(sid, text, hook_kwargs):
        raise RuntimeError("LLM timeout")
    monkeypatch.setattr(_review_api_module, "handle_inbound", boom)
    out = _plug._maybe_route_to_review_skill(cp, "a", {})
    assert out is not None
    assert "暂时出错" in out["context"]
    assert "/review cancel" in out["context"]


# ==========================================================================
# Context wrapper
# ==========================================================================


def test_wrap_reply_escapes_single_quotes():
    """The wrapper uses single-quoted EXACTLY-with text. Apostrophes in
    the reply text must be escaped or hermes splits the string."""
    out = _plug._wrap_reply_for_hermes("don't break this")
    assert "don\\'t break this" in out["context"]


def test_wrap_reply_escapes_backslashes():
    out = _plug._wrap_reply_for_hermes("path: a\\b")
    # Backslashes doubled before single-quote escape
    assert "a\\\\b" in out["context"]


def test_wrap_reply_returns_context_dict():
    out = _plug._wrap_reply_for_hermes("hello")
    assert "context" in out
    assert "IGNORE" in out["context"]
    assert "EXACTLY" in out["context"]
    assert "hello" in out["context"]
