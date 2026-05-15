"""Module A — append-only audit log (v1.6.4: per-cp physical isolation).

v1.6.4 write path:  counterparties/<cp_id>/audit.jsonl  (per-cp)
v1.6.4 read path:   all per-cp files  +  legacy audit_log.jsonl (grace period)

Migration script (bin/migrate_to_per_cp_audit.py) moves legacy entries
to per-cp files and renames the legacy file so it's no longer written.
Until migration, reads always cover both locations.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import storage

logger = logging.getLogger(__name__)

# Legacy path — kept for backward-compat reads; NOT written after v1.6.4.
_LEGACY_AUDIT_LOG = "audit_log.jsonl"


def _audit_log_path() -> Path:
    """Legacy single-file path (kept for grace-period reads)."""
    return storage.PAID_DIR / _LEGACY_AUDIT_LOG


def _cp_audit_path(cp_id: str) -> Path:
    """Per-cp audit file path (v1.6.4 write target)."""
    safe = _safe_cp_id(cp_id)
    return storage.PAID_DIR / "counterparties" / safe / "audit.jsonl"


def _safe_cp_id(cp_id: str) -> str:
    """Strip path-unsafe chars from cp_id to use as directory name."""
    import re
    return re.sub(r"[^A-Za-z0-9_\-@.]", "_", str(cp_id)) or "unknown"


def _to_dict(obj: Any) -> Any:
    """Best-effort convert dataclass/obj to a JSON-serializable dict."""
    if obj is None:
        return None
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, dict):
        return obj
    return obj


def log_action(
    session_id: str,
    counterparty: Any,  # Counterparty | None
    junior_msg: str,
    classification: Any,  # Classification | None
    action: Any,  # Action | None
    extra: dict | None = None,
) -> None:
    """Append a single audit entry.

    v1.6.4: writes to counterparties/<cp_id>/audit.jsonl (per-cp).
    Falls back to legacy audit_log.jsonl only when cp_id is unknown.
    """
    cp_id = None
    platform = None
    if counterparty is not None:
        if is_dataclass(counterparty):
            cp_dict = asdict(counterparty)
            cp_id = cp_dict.get("cp_id")
            platform = cp_dict.get("platform")
        elif isinstance(counterparty, dict):
            cp_id = counterparty.get("cp_id")
            platform = counterparty.get("platform")
        elif hasattr(counterparty, "cp_id"):
            cp_id = getattr(counterparty, "cp_id", None)
            platform = getattr(counterparty, "platform", None)

    msg = junior_msg or ""
    if len(msg) > 500:
        msg = msg[:500]

    entry = {
        # v1.6.6: entry_id is the canonical dedup key. Previously dedup
        # relied on (ts, session_id) which collided when session_id was
        # empty (system events) and two entries shared the same isoformat
        # timestamp at sub-microsecond granularity.
        "entry_id": secrets.token_hex(8),
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id or "",
        "counterparty": cp_id,
        "platform": platform,
        "junior_msg": msg,
        "classification": _to_dict(classification),
        "action": _to_dict(action),
        "extra": extra or {},
    }

    if cp_id:
        storage.append_jsonl(_cp_audit_path(cp_id), entry)
    else:
        # No cp_id — fall back to legacy (e.g. system events)
        storage.append_jsonl(_audit_log_path(), entry)


# ---------------------------------------------------------------------------
# Read helpers (v1.6.4: merge per-cp + legacy)
# ---------------------------------------------------------------------------


def _read_jsonl_safe(path: Path) -> list[dict]:
    """Read all JSON-lines from path. Return [] on any error."""
    if not path.exists():
        return []
    rows: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        rows.append(obj)
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return rows


def read_all_entries(
    lookback_days: int | None = None,
    cp_id: str | None = None,
) -> list[dict]:
    """Return all audit entries, merging per-cp files + legacy.

    Args:
        lookback_days: if set, exclude entries older than this many days.
        cp_id: if set, return only entries for this counterparty.

    Entries are sorted by 'ts' ascending. Duplicate rows (same ts+session)
    are deduplicated (can occur if both legacy and per-cp have same entry).
    """
    rows: list[dict] = []

    # 1. Legacy single file (grace period)
    for row in _read_jsonl_safe(_audit_log_path()):
        if cp_id is None or row.get("counterparty") == cp_id:
            rows.append(row)

    # 2. Per-cp dirs
    cp_dir = storage.PAID_DIR / "counterparties"
    if cp_dir.exists():
        if cp_id:
            # Only the target cp
            target = _cp_audit_path(cp_id)
            rows.extend(_read_jsonl_safe(target))
        else:
            for audit_file in cp_dir.glob("*/audit.jsonl"):
                rows.extend(_read_jsonl_safe(audit_file))

    if lookback_days is not None:
        cutoff = _cutoff_ts(lookback_days)
        rows = [r for r in rows if _row_ts(r) is None or _row_ts(r) >= cutoff]

    # v1.6.6: dedup by entry_id when available, falling back to a wider
    # composite key for pre-v1.6.6 rows that lack entry_id.
    seen: set = set()
    deduped: list[dict] = []
    for r in rows:
        eid = r.get("entry_id")
        if eid:
            key: Any = ("eid", eid)
        else:
            key = (
                "legacy",
                r.get("ts", ""),
                r.get("session_id", ""),
                r.get("counterparty"),
                (r.get("junior_msg") or "")[:50],
            )
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    deduped.sort(key=lambda r: r.get("ts") or "")
    return deduped


def _cutoff_ts(days: int) -> float:
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    return (now - timedelta(days=days)).timestamp()


def _row_ts(row: dict) -> float | None:
    ts_str = row.get("ts") or row.get("timestamp") or row.get("created_at")
    if ts_str is None:
        return None
    try:
        return datetime.fromisoformat(str(ts_str)).timestamp()
    except (ValueError, TypeError):
        return None


def list_known_cp_ids() -> list[str]:
    """Return list of cp_ids that have a per-cp audit directory."""
    cp_dir = storage.PAID_DIR / "counterparties"
    if not cp_dir.exists():
        return []
    return [d.name for d in cp_dir.iterdir() if d.is_dir() and (d / "audit.jsonl").exists()]
