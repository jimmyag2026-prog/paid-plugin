#!/usr/bin/env python3
"""PAID doctor — CLI entry. Prints check results to stdout.

Usage:
  python -m bin.paid_doctor
  python bin/paid_doctor.py

Exit code 0 = all checks pass; 1 = at least one fails.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make ``import paid`` work whether installed or run from a checkout.
_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from paid.doctor import run_checks, format_plain_text, overall_ok


def main() -> int:
    rows = run_checks()
    print(format_plain_text(rows))
    return 0 if overall_ok(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
