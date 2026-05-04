"""Module Co — LLM cost tracking + cap status.

v0.1 scope: observe-only. Records every call_llm into a ledger; provides
``cap_status()`` for callers (CLI / cron / dashboard). Does NOT short-circuit
call_llm itself, on purpose — observability first, then enforcement once
we have real cost data per pilot.

Hermes' chat-completion responses include ``usage.{prompt_tokens,
completion_tokens}`` — hermes_io.call_llm extracts these and calls
``record_call`` after a successful response. Estimation uses per-1k-token
rates (default approximations, owner can override per-model in
``settings.cost.rates``).

A separate cron-friendly entry (``bin/check_cost_cap.py``) reads the ledger
once a day and sends an IM alert when daily totals cross thresholds —
keeps the alert path out of the hot J2 flow.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import storage

_LEDGER_FILE = "cost_ledger.jsonl"


# Conservative per-1k-token defaults in USD. Real rates vary by provider;
# owners override via settings.cost.rates if they want precise cost tracking.
# These defaults err on the high side so 'today_usd' over-estimates rather
# than under-estimates (better to over-alert than miss a runaway).
_DEFAULT_RATES: dict[str, dict[str, float]] = {
    "default":              {"input": 0.003,    "output": 0.012},
    "deepseek-v4-flash":    {"input": 0.00027,  "output": 0.00110},
    "deepseek-v4":          {"input": 0.0014,   "output": 0.0028},
    "claude-haiku":         {"input": 0.0008,   "output": 0.004},
    "claude-sonnet":        {"input": 0.003,    "output": 0.015},
    "gpt-4o-mini":          {"input": 0.00015,  "output": 0.0006},
    "gpt-4o":               {"input": 0.0025,   "output": 0.010},
}


def _ledger_path() -> Path:
    return storage.PAID_DIR / _LEDGER_FILE


def _today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolve_rates(model: str) -> dict[str, float]:
    """Resolve rate row for *model*, with settings.cost.rates override."""
    try:
        from . import settings as _settings  # lazy
        cfg = _settings.load().get("cost", {}) if hasattr(_settings, "load") else {}
        overrides = cfg.get("rates", {}) or {}
        if model in overrides:
            row = overrides[model]
            if isinstance(row, dict) and "input" in row and "output" in row:
                return {"input": float(row["input"]), "output": float(row["output"])}
    except Exception:
        pass
    return _DEFAULT_RATES.get(model, _DEFAULT_RATES["default"])


def estimate_cost(
    prompt_tokens: int, completion_tokens: int, *, model: str = "default"
) -> float:
    """Per-call USD estimate. Cached rates; no I/O on this path."""
    rates = _resolve_rates(model)
    return (
        (max(0, int(prompt_tokens)) / 1000.0) * rates["input"]
        + (max(0, int(completion_tokens)) / 1000.0) * rates["output"]
    )


def record_call(
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    purpose: str = "",
) -> float:
    """Append one ledger entry; returns the per-call USD estimate.

    ``purpose`` is a free-form tag (e.g. ``"classifier"``, ``"l4c"``,
    ``"summary"``) — handy for per-feature cost breakdown later.

    Best-effort: any I/O failure is swallowed so the cost path never
    breaks the LLM call that just completed.
    """
    try:
        cost = estimate_cost(prompt_tokens, completion_tokens, model=model)
        storage.append_jsonl(_ledger_path(), {
            "ts": _now_iso(),
            "date": _today_iso(),
            "model": model,
            "prompt_tokens": int(prompt_tokens),
            "completion_tokens": int(completion_tokens),
            "cost_usd": cost,
            "purpose": purpose or "",
        })
        return cost
    except Exception:
        return 0.0


def _read_ledger() -> list[dict[str, Any]]:
    p = _ledger_path()
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for raw in p.read_text().splitlines():
        if not raw.strip():
            continue
        try:
            import json as _json
            out.append(_json.loads(raw))
        except Exception:
            continue
    return out


def total_for_date(date_iso: str) -> float:
    total = 0.0
    for entry in _read_ledger():
        if entry.get("date") == date_iso:
            try:
                total += float(entry.get("cost_usd", 0.0))
            except (TypeError, ValueError):
                continue
    return total


def today_total() -> float:
    return total_for_date(_today_iso())


def total_for_last_n_days(n: int) -> float:
    """Rolling N-day window ending today (inclusive)."""
    from datetime import timedelta
    today = datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=max(0, int(n) - 1))
    total = 0.0
    for entry in _read_ledger():
        date_str = entry.get("date", "")
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except Exception:
            continue
        if d >= cutoff:
            try:
                total += float(entry.get("cost_usd", 0.0))
            except (TypeError, ValueError):
                continue
    return total


def _resolve_caps() -> tuple[float, float, float, bool]:
    """Returns (daily_soft, daily_hard, weekly_soft, enabled)."""
    try:
        from . import settings as _settings
        cfg = _settings.load().get("cost", {}) if hasattr(_settings, "load") else {}
        return (
            float(cfg.get("daily_soft_cap_usd", 5.0)),
            float(cfg.get("daily_hard_cap_usd", 20.0)),
            float(cfg.get("weekly_soft_cap_usd", 25.0)),
            bool(cfg.get("enabled", True)),
        )
    except Exception:
        return 5.0, 20.0, 25.0, True


def cap_status() -> dict[str, Any]:
    """Snapshot for CLI / cron / dashboard.

    Returns:
        {
            today_usd: float,
            week_usd:  float,
            daily_soft_cap: float,
            daily_hard_cap: float,
            weekly_soft_cap: float,
            daily_soft_exceeded: bool,
            daily_hard_exceeded: bool,
            weekly_soft_exceeded: bool,
            enabled: bool,
        }
    """
    daily_soft, daily_hard, weekly_soft, enabled = _resolve_caps()
    today = today_total()
    week = total_for_last_n_days(7)
    return {
        "today_usd": today,
        "week_usd": week,
        "daily_soft_cap": daily_soft,
        "daily_hard_cap": daily_hard,
        "weekly_soft_cap": weekly_soft,
        "daily_soft_exceeded": enabled and today >= daily_soft,
        "daily_hard_exceeded": enabled and today >= daily_hard,
        "weekly_soft_exceeded": enabled and week >= weekly_soft,
        "enabled": enabled,
    }
