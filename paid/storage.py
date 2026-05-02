"""Module S — paths + IO."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

PAID_DIR: Path = Path.home() / ".hermes" / "paid"

# Bump when on-disk JSON shape changes incompatibly. Reads tolerate missing.
SCHEMA_VERSION: int = 1


def ensure_dirs() -> None:
    """Ensure PAID_DIR and core subdirs exist."""
    PAID_DIR.mkdir(parents=True, exist_ok=True)
    (PAID_DIR / "counterparties").mkdir(parents=True, exist_ok=True)
    (PAID_DIR / "persons").mkdir(parents=True, exist_ok=True)


def read_json(path: Path) -> dict | None:
    """Read JSON file. Return None if missing or unreadable."""
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        return None
    except (json.JSONDecodeError, OSError):
        return None


def write_json(path: Path, data: dict) -> None:
    """Write dict as pretty JSON. Creates parent dirs.

    Stamps schema_version on writes when the dict doesn't already carry one,
    and fsyncs to survive crash mid-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if "schema_version" not in data:
        data = {"schema_version": SCHEMA_VERSION, **data}
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())


def read_text(path: Path) -> str | None:
    """Read text file. Return None if missing."""
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def append_jsonl(path: Path, entry: dict) -> None:
    """Append one JSON line. fsync at end so crash leaves no half-line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())
