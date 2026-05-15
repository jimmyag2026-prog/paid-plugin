#!/usr/bin/env python3
"""Cron entry point for the Usage Observation Learner (v1.6.3).

Usage:
  python -m bin.run_observer            # daily pass only
  python -m bin.run_observer --weekly   # daily pass + send digest DM

Suggested crontab entries (paid user on VPS):
  # Daily stats update at 02:00 UTC
  0 2 * * * /home/paid/.hermes/plugins/paid-v1/bin/run_observer.py >> /home/paid/.hermes/paid/observer.log 2>&1
  # Weekly digest every Monday 09:00 UTC
  0 9 * * 1 /home/paid/.hermes/plugins/paid-v1/bin/run_observer.py --weekly >> /home/paid/.hermes/paid/observer.log 2>&1
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main() -> int:
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [observer] %(levelname)s %(message)s",
    )

    weekly = "--weekly" in sys.argv
    from paid import observer
    return observer.run_daily(send_digest=weekly)


if __name__ == "__main__":
    sys.exit(main())
