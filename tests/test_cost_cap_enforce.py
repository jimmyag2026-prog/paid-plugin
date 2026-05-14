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


# ---------------------------------------------------------------------------
# v1.5.6 review fix #1: flag-file lazy sweep (>30 day old flags get unlinked
# when today's flag is created)
# ---------------------------------------------------------------------------

def test_sweep_old_flags_unlinks_files_older_than_retention(paid_tmp):
    """Manual call to _sweep_old_cost_cap_flags: today + 5d ago + 60d ago →
    after sweep, only today + 5d ago remain (default retention 30d)."""
    from datetime import datetime, timezone, timedelta as _td
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    recent_iso = (datetime.now(timezone.utc).date() - _td(days=5)).isoformat()
    old_iso = (datetime.now(timezone.utc).date() - _td(days=60)).isoformat()

    for iso in (today_iso, recent_iso, old_iso):
        (paid_tmp / f"cost_cap_alerted_{iso}.flag").touch()

    hermes_io._sweep_old_cost_cap_flags()

    assert (paid_tmp / f"cost_cap_alerted_{today_iso}.flag").exists()
    assert (paid_tmp / f"cost_cap_alerted_{recent_iso}.flag").exists()
    assert not (paid_tmp / f"cost_cap_alerted_{old_iso}.flag").exists()


def test_sweep_ignores_unrelated_files(paid_tmp):
    """Sweep must NOT touch files that aren't cost_cap_alerted_*.flag."""
    (paid_tmp / "some_other.flag").write_text("keep me")
    (paid_tmp / "settings.json").write_text("{}")
    (paid_tmp / "cost_cap_alerted_garbage.flag").touch()  # bad date format
    from datetime import datetime, timezone, timedelta as _td
    old_iso = (datetime.now(timezone.utc).date() - _td(days=99)).isoformat()
    (paid_tmp / f"cost_cap_alerted_{old_iso}.flag").touch()

    hermes_io._sweep_old_cost_cap_flags()

    assert (paid_tmp / "some_other.flag").exists()
    assert (paid_tmp / "settings.json").exists()
    # Bad date format → left alone (not deleted)
    assert (paid_tmp / "cost_cap_alerted_garbage.flag").exists()
    # Old well-formed flag → deleted
    assert not (paid_tmp / f"cost_cap_alerted_{old_iso}.flag").exists()


def test_sweep_runs_when_today_flag_created(paid_tmp, monkeypatch):
    """Integration: _maybe_alert_cost_cap_once on first-of-day write should
    trigger the sweep automatically."""
    from datetime import datetime, timezone, timedelta as _td
    old_iso = (datetime.now(timezone.utc).date() - _td(days=90)).isoformat()
    old_flag = paid_tmp / f"cost_cap_alerted_{old_iso}.flag"
    old_flag.touch()
    assert old_flag.exists()

    from paid import cost
    monkeypatch.setattr(cost, "cap_status",
                        lambda: _fake_cap_status(today_usd=25.0))
    with pytest.raises(hermes_io.LLMCallError):
        hermes_io._enforce_cost_cap()

    # Today's flag exists; old one is gone
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert (paid_tmp / f"cost_cap_alerted_{today_iso}.flag").exists()
    assert not old_flag.exists()


def test_sweep_failure_does_not_prevent_raise(paid_tmp, monkeypatch):
    """If the sweep itself errors, the cap raise must still fire."""
    from paid import cost
    monkeypatch.setattr(cost, "cap_status",
                        lambda: _fake_cap_status(today_usd=25.0))

    def _boom(*a, **kw):
        raise RuntimeError("filesystem hiccup")
    monkeypatch.setattr(hermes_io, "_sweep_old_cost_cap_flags", _boom)

    # _maybe_alert_cost_cap_once must NOT itself raise even though sweep does.
    # The cap-enforce raise is upstream of alert, so it still fires:
    with pytest.raises(hermes_io.LLMCallError):
        hermes_io._enforce_cost_cap()


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
