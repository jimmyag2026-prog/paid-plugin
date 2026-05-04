"""Cursor dataclass + JSON IO for QA finding navigation (spec §14)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Cursor:
    """Tracks which finding is active and what's pending/deferred/done.

    Invariant (§14 Ⓜ16): current_id is NEVER in pending.
    """

    current_id: str | None = None
    pending: list[str] = field(default_factory=list)   # awaiting delivery
    deferred: list[str] = field(default_factory=list)  # beyond top-N, pulled by (more)
    done: list[str] = field(default_factory=list)      # accepted/rejected/modified/unresolvable


def load_cursor(sid_dir: Path) -> Cursor:
    """Read cursor.json from session directory. Returns empty Cursor if missing."""
    path = sid_dir / "cursor.json"
    if not path.exists():
        return Cursor()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return Cursor()
    return Cursor(
        current_id=data.get("current_id"),
        pending=list(data.get("pending", [])),
        deferred=list(data.get("deferred", [])),
        done=list(data.get("done", [])),
    )


def save_cursor(sid_dir: Path, cursor: Cursor) -> None:
    """Persist cursor atomically via tmp-rename."""
    sid_dir.mkdir(parents=True, exist_ok=True)
    path = sid_dir / "cursor.json"
    tmp = path.with_suffix(".tmp")
    payload = {
        "current_id": cursor.current_id,
        "pending": cursor.pending,
        "deferred": cursor.deferred,
        "done": cursor.done,
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    tmp.write_text(text, encoding="utf-8")
    with tmp.open("r", encoding="utf-8") as fh:
        os.fsync(fh.fileno())
    tmp.replace(path)


def advance(cursor: Cursor) -> Cursor:
    """Complete current_id → done; pop pending[0] as new current_id.

    After advance():
    - current_id is the next finding to deliver (or None if queue empty)
    - previous current_id is in done
    - pending never contains current_id (§14 invariant)
    """
    if cursor.current_id and cursor.current_id not in cursor.done:
        cursor.done.append(cursor.current_id)

    if cursor.pending:
        cursor.current_id = cursor.pending.pop(0)
    else:
        cursor.current_id = None

    # Safety: ensure current_id is not accidentally left in pending
    if cursor.current_id and cursor.current_id in cursor.pending:
        cursor.pending.remove(cursor.current_id)

    return cursor


def more(cursor: Cursor, n: int = 3) -> Cursor:
    """Move first *n* deferred items to the tail of pending."""
    to_add = cursor.deferred[:n]
    cursor.deferred = cursor.deferred[n:]
    cursor.pending.extend(to_add)
    return cursor


def is_exhausted(cursor: Cursor) -> bool:
    """True when no active or pending findings remain (ready for done-check)."""
    return cursor.current_id is None and not cursor.pending
