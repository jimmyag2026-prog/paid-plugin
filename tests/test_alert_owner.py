"""Tests for plugin __init__.py._alert_owner — IM channel + debounce.

Loads the plugin entry as a module for direct function testing. The function
is deliberately self-contained (no async, no hooks) so this works without a
live hermes gateway.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

# Make the plugin entry importable as a module named "paid_plugin_entry".
# Loading via importlib instead of `import __init__` avoids shadowing any
# tests/__init__.py and keeps a stable name across test runs.
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

import importlib.util as _ilu
_spec = _ilu.spec_from_file_location(
    "paid_plugin_entry", _PLUGIN_ROOT / "__init__.py"
)
_plug = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_plug)


@pytest.fixture(autouse=True)
def _clear_debounce():
    """Each test starts with a fresh debounce table."""
    _plug._ALERT_LAST_SENT.clear()
    yield
    _plug._ALERT_LAST_SENT.clear()


def _seed_owner(paid_tmp: Path, *, identities: list[dict] | None = None) -> None:
    if identities is None:
        identities = [{"platform": "telegram", "user_id": "11111"}]
    (paid_tmp / "owner.json").write_text(
        json.dumps(
            {"owner_id": "owner_jimmy", "identities": identities, "name": "Jimmy"}
        )
    )


def _patch_send_dm_capture(monkeypatch):
    sent: list[dict] = []

    def _fake(platform, user_id, message, **kw):
        sent.append({"platform": platform, "user_id": user_id, "message": message})
        return {"ok": True, "msg_id": "stub", "platform": platform}

    monkeypatch.setattr(_plug.hermes_io, "send_dm", _fake)
    return sent


def test_alert_owner_writes_fatal_jsonl_with_no_owner(paid_tmp, monkeypatch):
    """Even without owner.json, fatal_alerts.jsonl must still capture."""
    _patch_send_dm_capture(monkeypatch)
    _plug._alert_owner("test_reason", "trace details")
    fatal = paid_tmp / "fatal_alerts.jsonl"
    assert fatal.exists()
    rec = json.loads(fatal.read_text().strip().splitlines()[-1])
    assert rec["reason"] == "test_reason"
    assert "trace details" in rec["detail"]


def test_alert_owner_sends_im_to_owner(paid_tmp, monkeypatch):
    _seed_owner(paid_tmp)
    sent = _patch_send_dm_capture(monkeypatch)
    _plug._alert_owner("classifier_crash", "stacktrace line 1\nstacktrace line 2")
    assert len(sent) == 1
    msg = sent[0]
    assert msg["platform"] == "telegram"
    assert msg["user_id"] == "11111"
    assert "classifier_crash" in msg["message"]
    # First line of detail is included; "fatal_alerts.jsonl" pointer present.
    assert "stacktrace line 1" in msg["message"]
    assert "fatal_alerts.jsonl" in msg["message"]


def test_alert_owner_lark_uses_home_channel_override(
    paid_tmp, monkeypatch
):
    _seed_owner(paid_tmp, identities=[{"platform": "feishu", "user_id": "ou_x"}])
    monkeypatch.setenv("FEISHU_HOME_CHANNEL", "oc_homechannel123")
    sent = _patch_send_dm_capture(monkeypatch)
    _plug._alert_owner("lark_test", "detail")
    assert len(sent) == 1
    # Should target the home channel chat_id, NOT the bare open_id.
    assert sent[0]["user_id"] == "oc_homechannel123"


def test_alert_owner_debounces_same_reason(paid_tmp, monkeypatch):
    _seed_owner(paid_tmp)
    sent = _patch_send_dm_capture(monkeypatch)
    for _ in range(5):
        _plug._alert_owner("same_reason", "x")
    # Only first IM should land — rest debounced.
    assert len(sent) == 1
    # But fatal_alerts.jsonl gets every entry.
    fatal_lines = (paid_tmp / "fatal_alerts.jsonl").read_text().strip().splitlines()
    assert len(fatal_lines) == 5


def test_alert_owner_does_not_debounce_different_reasons(paid_tmp, monkeypatch):
    _seed_owner(paid_tmp)
    sent = _patch_send_dm_capture(monkeypatch)
    _plug._alert_owner("reason_a", "x")
    _plug._alert_owner("reason_b", "x")
    _plug._alert_owner("reason_c", "x")
    assert len(sent) == 3


def test_alert_owner_swallows_send_dm_exception(paid_tmp, monkeypatch):
    _seed_owner(paid_tmp)

    def _explode(*a, **kw):
        raise RuntimeError("gateway exploded")

    monkeypatch.setattr(_plug.hermes_io, "send_dm", _explode)
    # MUST NOT raise — alert path is best-effort.
    _plug._alert_owner("explosive", "detail")
    # fatal log still captured.
    assert (paid_tmp / "fatal_alerts.jsonl").exists()


def test_alert_recently_sent_window():
    """Mechanism check: time-based debounce window."""
    import time as _t
    _plug._ALERT_LAST_SENT.clear()
    assert _plug._alert_recently_sent("im", "r1") is False
    _plug._mark_alert_sent("im", "r1")
    assert _plug._alert_recently_sent("im", "r1") is True
    # Manually rewind the timestamp past the window to simulate expiry.
    _plug._ALERT_LAST_SENT[("im", "r1")] = _t.time() - (
        _plug._ALERT_IM_DEBOUNCE_SECONDS + 1
    )
    assert _plug._alert_recently_sent("im", "r1") is False
