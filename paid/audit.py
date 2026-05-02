"""Module A — append-only audit log."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import storage


def _audit_log_path() -> Path:
    return storage.PAID_DIR / "audit_log.jsonl"


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
    """Append a single audit entry to ~/.hermes/paid/audit_log.jsonl."""
    cp_dict = _to_dict(counterparty)
    cp_id = None
    platform = None
    if isinstance(cp_dict, dict):
        cp_id = cp_dict.get("cp_id")
        platform = cp_dict.get("platform")

    msg = junior_msg or ""
    if len(msg) > 500:
        msg = msg[:500]

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id or "",
        "counterparty": cp_id,
        "platform": platform,
        "junior_msg": msg,
        "classification": _to_dict(classification),
        "action": _to_dict(action),
        "extra": extra or {},
    }
    storage.append_jsonl(_audit_log_path(), entry)
