"""Unit tests for the v1.5.5 A2 cost-ceiling inline enforce path.

The check lives at the top of paid.hermes_io.call_llm; ``cap_status()`` is
mocked so we never make a real HTTP call. We assert:
  - daily_hard_exceeded -> LLMCallError raised BEFORE HTTP
  - alert flag dedupes within same UTC day (one fatal_alerts.jsonl row only)
  - settings.cost.enabled=False -> no enforcement
  - cap_status() itself raising -> fail-open (call_llm proceeds)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from paid import hermes_io
from paid import storage


def _fake_cap_status(today_usd=0.0, daily_hard_cap=20.0, enabled=True):
    return {
        "today_usd": today_usd,
        "week_usd": today_usd,
        "daily_soft_cap": 5.0,
        "daily_hard_cap": daily_hard_cap,
        "weekly_soft_cap": 25.0,
        "daily_soft_exceeded": False,
        "daily_hard_exceeded": today_usd >= daily_hard_cap,
        "weekly_soft_exceeded": False,
        "enabled": enabled,
    }


# ---------------------------------------------------------------------------
# Raise path
# ---------------------------------------------------------------------------

def test_enforce_raises_when_hard_cap_exceeded(paid_tmp, monkeypatch):
    from paid import cost
    monkeypatch.setattr(cost, "cap_status",
                        lambda: _fake_cap_status(today_usd=25.0, daily_hard_cap=20.0))
    # Also stub out _load_hermes_config so call_llm doesn't crash on config
    # before reaching the cap check — though the cap check is FIRST so this
    # is belt-and-suspenders.
    monkeypatch.setattr(hermes_io, "_load_hermes_config", lambda: {})
    with pytest.raises(hermes_io.LLMCallError) as ei:
        hermes_io.call_llm("hi")
    assert "budget exhausted" in str(ei.value)
    assert "$25.00" in str(ei.value)
    assert "$20.00" in str(ei.value)


def test_enforce_no_raise_when_under_cap(paid_tmp, monkeypatch):
    from paid import cost
    monkeypatch.setattr(cost, "cap_status",
                        lambda: _fake_cap_status(today_usd=1.0))
    # _enforce_cost_cap should be a no-op; call into _enforce directly to verify
    # without triggering the real HTTP call.
    # (Calling call_llm would proceed past _enforce_cost_cap and try HTTP.)
    hermes_io._enforce_cost_cap()  # must not raise


def test_enforce_no_raise_when_disabled(paid_tmp, monkeypatch):
    from paid import cost
    monkeypatch.setattr(cost, "cap_status",
                        lambda: _fake_cap_status(today_usd=999.0, enabled=False))
    hermes_io._enforce_cost_cap()  # disabled = no-op even when exceeded


def test_enforce_fail_open_when_cap_status_errors(paid_tmp, monkeypatch):
    from paid import cost
    def _boom():
        raise RuntimeError("ledger read failed")
    monkeypatch.setattr(cost, "cap_status", _boom)
    # Must not raise — fail-open is the chosen policy
    hermes_io._enforce_cost_cap()


# ---------------------------------------------------------------------------
# Alert dedup
# ---------------------------------------------------------------------------

def test_alert_dedup_one_per_day(paid_tmp, monkeypatch):
    from paid import cost
    monkeypatch.setattr(cost, "cap_status",
                        lambda: _fake_cap_status(today_usd=25.0))

    # First exceed -> raises + writes 1 fatal_alerts row + writes flag file
    with pytest.raises(hermes_io.LLMCallError):
        hermes_io._enforce_cost_cap()
    fa_path = paid_tmp / "fatal_alerts.jsonl"
    assert fa_path.exists()
    rows1 = [json.loads(l) for l in fa_path.read_text().splitlines() if l.strip()]
    assert len(rows1) == 1
    assert rows1[0]["reason"] == "cost_cap_exceeded"
    detail = json.loads(rows1[0]["detail"])
    assert detail["today_usd"] == 25.0
    assert detail["daily_hard_cap"] == 20.0

    # Flag file present
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert (paid_tmp / f"cost_cap_alerted_{today}.flag").exists()

    # Second exceed -> raises again BUT does NOT add another fatal_alerts row
    with pytest.raises(hermes_io.LLMCallError):
        hermes_io._enforce_cost_cap()
    rows2 = [json.loads(l) for l in fa_path.read_text().splitlines() if l.strip()]
    assert len(rows2) == 1  # still one


def test_alert_path_failure_does_not_prevent_raise(paid_tmp, monkeypatch):
    """If we can't write the flag or the fatal row, raise must still fire."""
    from paid import cost
    monkeypatch.setattr(cost, "cap_status",
                        lambda: _fake_cap_status(today_usd=25.0))

    def _broken_touch(self, *a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "touch", _broken_touch)
    # Should still raise the cap error
    with pytest.raises(hermes_io.LLMCallError):
        hermes_io._enforce_cost_cap()


# ---------------------------------------------------------------------------
# call_llm integration: cap check fires BEFORE HTTP/config resolution
# ---------------------------------------------------------------------------

def test_call_llm_short_circuits_before_http(paid_tmp, monkeypatch):
    """Ensure no HTTP request fires when the cap is exceeded."""
    from paid import cost
    monkeypatch.setattr(cost, "cap_status",
                        lambda: _fake_cap_status(today_usd=25.0))

    called = {"post": False, "config": False}

    def _spy_config():
        called["config"] = True
        return {}

    def _spy_post(*a, **kw):
        called["post"] = True
        raise AssertionError("HTTP should not be reached past cap")

    monkeypatch.setattr(hermes_io, "_load_hermes_config", _spy_config)
    monkeypatch.setattr(hermes_io, "_post_with_retry", _spy_post)

    with pytest.raises(hermes_io.LLMCallError):
        hermes_io.call_llm("hi")

    assert called["config"] is False  # we never reached config resolution
    assert called["post"] is False
