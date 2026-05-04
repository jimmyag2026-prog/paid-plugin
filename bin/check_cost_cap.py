#!/usr/bin/env python3
"""Daily cost-cap check + alert. Cron-friendly.

Reads the cost ledger, evaluates settings.cost.{daily_soft,daily_hard,
weekly_soft}_cap_usd, and sends an IM alert via _alert_owner when a cap
is exceeded — debounced once per day per cap-tier so cron firing every
hour doesn't spam the owner.

Recommended cron (e.g. systemd --user timer or crontab):
  0 * * * *  /home/paid/.hermes/hermes-agent/venv/bin/python3 \
             /home/paid/src/paid-plugin/bin/check_cost_cap.py

Exit codes:
  0  no cap exceeded (or alert already sent today)
  0  cap exceeded + alert sent
  1  unexpected exception
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from paid import cost, storage  # noqa: E402

_DEBOUNCE_FILE = "_cost_alert_debounce.json"


def _today_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _debounce_path() -> Path:
    return storage.PAID_DIR / _DEBOUNCE_FILE


def _read_debounce() -> dict:
    p = _debounce_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _write_debounce(state: dict) -> None:
    storage.write_json(_debounce_path(), state)


def _already_alerted_today(tier: str) -> bool:
    state = _read_debounce()
    return state.get(tier) == _today_iso()


def _mark_alerted_today(tier: str) -> None:
    state = _read_debounce()
    state[tier] = _today_iso()
    _write_debounce(state)


def _alert_owner_via_plugin(reason: str, detail: str) -> bool:
    """Load the plugin entry via importlib (it's at __init__.py at the repo
    root, which can't be imported by name on its own) and call _alert_owner.

    Returns True on success.
    """
    plugin_init = _HERE / "__init__.py"
    if not plugin_init.exists():
        return False
    spec = importlib.util.spec_from_file_location("paid_plugin_entry_cost", plugin_init)
    if spec is None or spec.loader is None:
        return False
    plug = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(plug)
        plug._alert_owner(reason=reason, detail=detail)
        return True
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paid-dir", default=None,
                        help="override PAID_DIR (default: ~/.hermes/paid)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report status; do not send alerts or write debounce")
    parser.add_argument("--force", action="store_true",
                        help="ignore debounce — send alert even if already sent today")
    args = parser.parse_args()

    if args.paid_dir:
        storage.PAID_DIR = Path(args.paid_dir)
    storage.ensure_dirs()

    status = cost.cap_status()
    print(f"PAID cost-cap check @ {_today_iso()}")
    print(f"  today: ${status['today_usd']:.4f} "
          f"(soft cap ${status['daily_soft_cap']:.2f}, "
          f"hard cap ${status['daily_hard_cap']:.2f})")
    print(f"  week:  ${status['week_usd']:.4f} "
          f"(soft cap ${status['weekly_soft_cap']:.2f})")
    print(f"  enabled: {status['enabled']}")

    if not status["enabled"]:
        print("cost tracking disabled in settings.json — nothing to check")
        return 0

    alerts_to_emit: list[tuple[str, str]] = []

    if status["daily_hard_exceeded"]:
        alerts_to_emit.append((
            "daily_hard",
            f"⚠️ DAILY HARD cap exceeded: ${status['today_usd']:.2f} >= "
            f"${status['daily_hard_cap']:.2f}. Consider pausing PAID until "
            f"00:00 UTC reset, or raising the cap in settings.json.",
        ))
    elif status["daily_soft_exceeded"]:
        alerts_to_emit.append((
            "daily_soft",
            f"⚠️ Daily soft cap reached: ${status['today_usd']:.2f} >= "
            f"${status['daily_soft_cap']:.2f}. PAID continues; hard cap at "
            f"${status['daily_hard_cap']:.2f}.",
        ))
    if status["weekly_soft_exceeded"]:
        alerts_to_emit.append((
            "weekly_soft",
            f"⚠️ Weekly soft cap reached: ${status['week_usd']:.2f} >= "
            f"${status['weekly_soft_cap']:.2f} over 7 days. Review usage "
            f"trends; consider raising cap or tuning workload.",
        ))

    if not alerts_to_emit:
        print("no cap exceeded — quiet exit")
        return 0

    for tier, msg in alerts_to_emit:
        if not args.force and _already_alerted_today(tier):
            print(f"  [{tier}] alert already sent today — skipped (use --force to override)")
            continue
        print(f"  [{tier}] {msg}")
        if args.dry_run:
            print(f"  [{tier}] --dry-run: would send alert")
            continue
        sent = _alert_owner_via_plugin(
            reason=f"cost_cap_{tier}",
            detail=msg,
        )
        if sent:
            _mark_alerted_today(tier)
            print(f"  [{tier}] alert sent + debounce marked")
        else:
            print(f"  [{tier}] _alert_owner failed; will retry next run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
