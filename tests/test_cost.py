"""Tests for paid.cost — ledger + estimates + cap status (M9.4 v0.1)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from paid import cost


# --------------------------------------------------------------------------
# estimate_cost
# --------------------------------------------------------------------------


def test_estimate_uses_default_rates_for_unknown_model():
    # default rates: input 0.003 / output 0.012 per 1k tokens
    c = cost.estimate_cost(1000, 1000, model="some-model-not-in-table")
    assert c == pytest.approx(0.003 + 0.012)


def test_estimate_known_model_uses_specific_rates():
    c = cost.estimate_cost(1000, 1000, model="deepseek-v4-flash")
    # deepseek-v4-flash: 0.00027 + 0.00110 = 0.00137
    assert c == pytest.approx(0.00027 + 0.00110)


def test_estimate_zero_tokens_returns_zero():
    assert cost.estimate_cost(0, 0) == 0.0


def test_estimate_negative_tokens_clamped_to_zero():
    """Robust: bad inputs (negative tokens from a buggy provider) shouldn't
    produce negative cost."""
    assert cost.estimate_cost(-100, -50) == 0.0


def test_estimate_settings_override_takes_precedence(paid_tmp):
    (paid_tmp / "settings.json").write_text(json.dumps({
        "cost": {"rates": {"my-custom-model": {"input": 0.01, "output": 0.02}}}
    }))
    c = cost.estimate_cost(1000, 1000, model="my-custom-model")
    assert c == pytest.approx(0.01 + 0.02)


# --------------------------------------------------------------------------
# record_call — ledger writes
# --------------------------------------------------------------------------


def test_record_call_writes_ledger_entry(paid_tmp):
    cost.record_call(model="default", prompt_tokens=500, completion_tokens=200)
    ledger = paid_tmp / "cost_ledger.jsonl"
    assert ledger.exists()
    entry = json.loads(ledger.read_text().strip())
    assert entry["model"] == "default"
    assert entry["prompt_tokens"] == 500
    assert entry["completion_tokens"] == 200
    assert entry["cost_usd"] > 0
    assert "ts" in entry and "date" in entry


def test_record_call_appends_multiple_entries(paid_tmp):
    cost.record_call(model="a", prompt_tokens=100, completion_tokens=100)
    cost.record_call(model="b", prompt_tokens=200, completion_tokens=200, purpose="classifier")
    cost.record_call(model="c", prompt_tokens=300, completion_tokens=300)
    ledger = paid_tmp / "cost_ledger.jsonl"
    lines = ledger.read_text().splitlines()
    assert len(lines) == 3
    e2 = json.loads(lines[1])
    assert e2["model"] == "b"
    assert e2["purpose"] == "classifier"


def test_record_call_returns_estimated_cost(paid_tmp):
    c = cost.record_call(model="default", prompt_tokens=1000, completion_tokens=1000)
    assert c == pytest.approx(0.015)


def test_record_call_swallows_exceptions(paid_tmp, monkeypatch):
    """Ledger I/O failures must not break the LLM call that just succeeded."""
    def boom(*a, **kw):
        raise RuntimeError("simulated disk full")
    monkeypatch.setattr(cost.storage, "append_jsonl", boom)
    # Should not raise.
    c = cost.record_call(model="default", prompt_tokens=100, completion_tokens=100)
    assert c == 0.0


# --------------------------------------------------------------------------
# total_for_date / today_total / total_for_last_n_days
# --------------------------------------------------------------------------


def _seed_ledger(paid_tmp, entries: list[dict]) -> None:
    p = paid_tmp / "cost_ledger.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def test_today_total_sums_only_today(paid_tmp):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    _seed_ledger(paid_tmp, [
        {"date": today,     "cost_usd": 0.5, "ts": "x"},
        {"date": yesterday, "cost_usd": 1.0, "ts": "x"},
        {"date": today,     "cost_usd": 0.25, "ts": "x"},
    ])
    assert cost.today_total() == pytest.approx(0.75)


def test_total_for_last_n_days_inclusive_today(paid_tmp):
    today = datetime.now(timezone.utc).date()
    days_ago = lambda n: (today - timedelta(days=n)).strftime("%Y-%m-%d")
    _seed_ledger(paid_tmp, [
        {"date": days_ago(0), "cost_usd": 1.0, "ts": "x"},
        {"date": days_ago(3), "cost_usd": 2.0, "ts": "x"},
        {"date": days_ago(6), "cost_usd": 4.0, "ts": "x"},
        {"date": days_ago(8), "cost_usd": 99.0, "ts": "x"},  # outside 7-day window
    ])
    # last 7 days = days 0..6 inclusive
    assert cost.total_for_last_n_days(7) == pytest.approx(7.0)


def test_total_for_last_n_days_handles_malformed_entries(paid_tmp):
    today = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
    p = paid_tmp / "cost_ledger.jsonl"
    p.write_text(
        json.dumps({"date": today, "cost_usd": 1.5, "ts": "x"}) + "\n"
        + "not-json-at-all\n"
        + json.dumps({"date": "bad-date", "cost_usd": 2.0, "ts": "x"}) + "\n"
        + json.dumps({"date": today, "cost_usd": "not-a-number", "ts": "x"}) + "\n"
        + json.dumps({"date": today, "cost_usd": 0.5, "ts": "x"}) + "\n"
    )
    assert cost.total_for_last_n_days(7) == pytest.approx(2.0)


def test_today_total_zero_when_ledger_missing(paid_tmp):
    assert cost.today_total() == 0.0


# --------------------------------------------------------------------------
# cap_status
# --------------------------------------------------------------------------


def test_cap_status_default_caps(paid_tmp):
    s = cost.cap_status()
    assert s["daily_soft_cap"] == 5.0
    assert s["daily_hard_cap"] == 20.0
    assert s["weekly_soft_cap"] == 25.0
    assert s["enabled"] is True
    assert s["today_usd"] == 0.0
    assert s["daily_soft_exceeded"] is False
    assert s["daily_hard_exceeded"] is False


def test_cap_status_settings_override(paid_tmp):
    (paid_tmp / "settings.json").write_text(json.dumps({
        "cost": {
            "daily_soft_cap_usd": 1.0,
            "daily_hard_cap_usd": 3.0,
            "weekly_soft_cap_usd": 5.0,
        }
    }))
    s = cost.cap_status()
    assert s["daily_soft_cap"] == 1.0
    assert s["daily_hard_cap"] == 3.0
    assert s["weekly_soft_cap"] == 5.0


def test_cap_status_soft_exceeded_when_today_above_soft(paid_tmp):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _seed_ledger(paid_tmp, [
        {"date": today, "cost_usd": 6.0, "ts": "x"},  # above default soft 5
    ])
    s = cost.cap_status()
    assert s["today_usd"] == 6.0
    assert s["daily_soft_exceeded"] is True
    assert s["daily_hard_exceeded"] is False  # 6 < 20


def test_cap_status_hard_exceeded_when_today_above_hard(paid_tmp):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _seed_ledger(paid_tmp, [
        {"date": today, "cost_usd": 25.0, "ts": "x"},
    ])
    s = cost.cap_status()
    assert s["daily_hard_exceeded"] is True
    assert s["daily_soft_exceeded"] is True  # 25 > 5 too


def test_cap_status_disabled_no_alerts(paid_tmp):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _seed_ledger(paid_tmp, [{"date": today, "cost_usd": 100.0, "ts": "x"}])
    (paid_tmp / "settings.json").write_text(json.dumps({
        "cost": {"enabled": False}
    }))
    s = cost.cap_status()
    assert s["enabled"] is False
    # When disabled, exceeded flags must be False even with huge usage.
    assert s["daily_soft_exceeded"] is False
    assert s["daily_hard_exceeded"] is False
