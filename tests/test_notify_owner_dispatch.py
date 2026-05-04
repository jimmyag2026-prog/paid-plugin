"""Integration test for _notify_owner_about_request platform dispatch.

Loads the plugin entry via importlib (matches test_alert_owner.py pattern)
so we can monkey-patch hermes_io send functions and assert which path was
taken for each owner.preferred_platform value.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from paid import approval

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

_spec = importlib.util.spec_from_file_location(
    "paid_plugin_entry_dispatch", _PLUGIN_ROOT / "__init__.py"
)
_plug = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_plug)


def _seed_owner(paid_tmp, *, identities, preferred_platform=""):
    (paid_tmp / "owner.json").write_text(json.dumps({
        "schema_version": 2,
        "owner_id": "owner_jimmy",
        "name": "Jimmy",
        "preferred_platform": preferred_platform,
        "identities": identities,
    }))


def _make_req(**kw):
    base = dict(
        request_id="r-disp",
        ts_created=1700000000.0,
        counterparty_id="cp1",
        counterparty_platform="telegram",
        counterparty_user_id="999",
        counterparty_display="Test Junior",
        junior_session_id="s1",
        junior_question="should we ship today?",
        draft_answer="I'll check with Jimmy and reply within an hour.",
        topic="release-readiness",
        stakes="medium",
        confidence=0.7,
    )
    base.update(kw)
    return approval.PendingApproval(**base)


def _patch_senders(monkeypatch):
    calls: dict[str, list] = {"lark": [], "telegram": [], "slack": [], "dm": []}

    monkeypatch.setattr(
        _plug.hermes_io, "send_lark_card",
        lambda *a, **k: (calls["lark"].append({"args": a, "kwargs": k})
                         or {"ok": True, "msg_id": "lark-1", "platform": "feishu"}),
    )
    monkeypatch.setattr(
        _plug.hermes_io, "send_telegram_card",
        lambda *a, **k: (calls["telegram"].append({"args": a, "kwargs": k})
                         or {"ok": True, "msg_id": "tg-1", "platform": "telegram"}),
    )
    monkeypatch.setattr(
        _plug.hermes_io, "send_slack_block",
        lambda *a, **k: (calls["slack"].append({"args": a, "kwargs": k})
                         or {"ok": True, "msg_id": "slack-1", "platform": "slack"}),
    )
    monkeypatch.setattr(
        _plug.hermes_io, "send_dm",
        lambda *a, **k: (calls["dm"].append({"args": a, "kwargs": k})
                         or {"ok": True, "msg_id": "dm-1", "platform": a[0] if a else ""}),
    )
    return calls


def test_dispatch_to_lark_when_preferred(paid_tmp, monkeypatch):
    _seed_owner(paid_tmp,
                preferred_platform="feishu",
                identities=[
                    {"platform": "feishu", "user_id": "ou_x", "home_chat_id": "oc_y"},
                    {"platform": "telegram", "user_id": "1"},
                ])
    calls = _patch_senders(monkeypatch)
    _plug._notify_owner_about_request(_make_req())
    assert len(calls["lark"]) == 1
    assert calls["telegram"] == calls["slack"] == calls["dm"] == []


def test_dispatch_to_telegram_when_preferred(paid_tmp, monkeypatch):
    _seed_owner(paid_tmp,
                preferred_platform="telegram",
                identities=[
                    {"platform": "telegram", "user_id": "12345"},
                    {"platform": "feishu", "user_id": "ou_x"},
                ])
    calls = _patch_senders(monkeypatch)
    _plug._notify_owner_about_request(_make_req())
    assert len(calls["telegram"]) == 1
    assert calls["lark"] == calls["slack"] == calls["dm"] == []
    # send_telegram_card called with correct shape: chat_id, text, kwargs
    args, kw = calls["telegram"][0]["args"], calls["telegram"][0]["kwargs"]
    assert args[0] == "12345"  # home_chat_id (= user_id by default)
    assert "PAID approval" in args[1]
    assert "keyboard" in kw and kw["keyboard"] is not None
    assert kw.get("parse_mode") == "Markdown"


def test_dispatch_to_slack_when_preferred(paid_tmp, monkeypatch):
    _seed_owner(paid_tmp,
                preferred_platform="slack",
                identities=[
                    {"platform": "slack", "user_id": "U01", "home_chat_id": "D01"},
                ])
    calls = _patch_senders(monkeypatch)
    _plug._notify_owner_about_request(_make_req())
    assert len(calls["slack"]) == 1
    args, kw = calls["slack"][0]["args"], calls["slack"][0]["kwargs"]
    assert args[0] == "D01"  # home_chat_id (DM channel id)
    assert isinstance(args[1], list)  # blocks
    assert kw.get("fallback_text", "").startswith("PAID approval")


def test_dispatch_falls_through_to_plain_when_lark_card_fails(paid_tmp, monkeypatch):
    _seed_owner(paid_tmp,
                preferred_platform="feishu",
                identities=[{"platform": "feishu", "user_id": "ou_x"}])

    calls = {"lark": [], "dm": []}
    # Lark card returns ok=False (queued).
    monkeypatch.setattr(
        _plug.hermes_io, "send_lark_card",
        lambda *a, **k: (calls["lark"].append(1)
                         or {"ok": False, "queued": "/tmp/q.jsonl"}),
    )
    monkeypatch.setattr(
        _plug.hermes_io, "send_dm",
        lambda *a, **k: (calls["dm"].append({"args": a, "kwargs": k})
                         or {"ok": True, "msg_id": "fallback-1"}),
    )
    monkeypatch.setattr(_plug.hermes_io, "send_telegram_card",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("nope")))
    monkeypatch.setattr(_plug.hermes_io, "send_slack_block",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("nope")))

    _plug._notify_owner_about_request(_make_req())
    assert len(calls["lark"]) == 1
    assert len(calls["dm"]) == 1
    # send_dm called with plain-text body (formatted plain card).
    body = calls["dm"][0]["args"][2]
    assert "📨 PAID approval" in body
    assert "1️⃣ APPROVE" in body  # numeric shortcuts present


def test_dispatch_falls_through_when_telegram_card_raises(paid_tmp, monkeypatch):
    _seed_owner(paid_tmp,
                preferred_platform="telegram",
                identities=[{"platform": "telegram", "user_id": "12345"}])

    monkeypatch.setattr(
        _plug.hermes_io, "send_telegram_card",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("simulated TG outage")),
    )
    dm_calls = []
    monkeypatch.setattr(
        _plug.hermes_io, "send_dm",
        lambda *a, **k: (dm_calls.append({"args": a, "kwargs": k})
                         or {"ok": True, "msg_id": "x"}),
    )
    _plug._notify_owner_about_request(_make_req())
    assert len(dm_calls) == 1
    assert dm_calls[0]["args"][0] == "telegram"


def test_dispatch_unknown_platform_uses_plain(paid_tmp, monkeypatch):
    """e.g. owner is on a platform PAID has no card formatter for —
    should silently use the plain-text path, no crash."""
    _seed_owner(paid_tmp,
                preferred_platform="discord",
                identities=[{"platform": "discord", "user_id": "U99"}])
    monkeypatch.setattr(_plug.hermes_io, "send_lark_card",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("nope")))
    monkeypatch.setattr(_plug.hermes_io, "send_telegram_card",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("nope")))
    monkeypatch.setattr(_plug.hermes_io, "send_slack_block",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("nope")))
    dm_calls = []
    monkeypatch.setattr(
        _plug.hermes_io, "send_dm",
        lambda *a, **k: (dm_calls.append({"args": a}) or {"ok": True}),
    )
    _plug._notify_owner_about_request(_make_req())
    assert len(dm_calls) == 1
    assert dm_calls[0]["args"][0] == "discord"


def test_dispatch_v1_owner_json_uses_first_enabled(paid_tmp, monkeypatch):
    """v1 owner.json (no schema_version, no preferred_platform) → falls
    back to first identity (legacy behaviour preserved)."""
    (paid_tmp / "owner.json").write_text(json.dumps({
        "owner_id": "owner_x",
        "identities": [
            {"platform": "telegram", "user_id": "1"},
            {"platform": "feishu", "user_id": "ou_y"},
        ],
    }))
    calls = _patch_senders(monkeypatch)
    _plug._notify_owner_about_request(_make_req())
    # First identity is telegram → tg card path used.
    assert len(calls["telegram"]) == 1
    assert calls["lark"] == []


def test_dispatch_no_owner_skips_silently(paid_tmp, monkeypatch):
    # No owner.json at all.
    calls = _patch_senders(monkeypatch)
    _plug._notify_owner_about_request(_make_req())
    # All channels untouched — no DM attempt, no crash.
    assert calls["lark"] == calls["telegram"] == calls["slack"] == calls["dm"] == []
