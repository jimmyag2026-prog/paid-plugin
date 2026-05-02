"""Module Ap — approval lifecycle (J3, v0.5 simplified).

Append-only event log at ``~/.hermes/paid/pending_approvals.jsonl``.
Each line is one event:

  - ``{"type":"create",  "request_id":..., "ts":..., ...payload...}``
  - ``{"type":"status",  "request_id":..., "ts":..., "status":"approved"|"rejected", "final_text":...}``

State of a request = latest status event for its ``request_id``; if no status
event has been written, it is ``pending``.

v0.5 deliberately drops:
  - ``modify`` button (only approve / reject)
  - 5-min reminder + 30-min timeout (no background worker today)
  - card-msg-id reverse lookup (no card surface yet — owner uses slash commands)

See ``design/01_review_decisions.md §2.1`` for the W2 expansion.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Literal

from . import storage

Status = Literal["pending", "approved", "rejected", "timed_out"]


def _pending_log() -> Path:
    """Path to the append-only event log.

    Resolved on each call so tests can monkeypatch ``storage.PAID_DIR``.
    """
    return storage.PAID_DIR / "pending_approvals.jsonl"


# Back-compat: legacy module attr that callers may import directly. We keep
# it as a lazy property-ish via a custom getattr below so it always reflects
# the current ``storage.PAID_DIR``.
def __getattr__(name: str):  # pragma: no cover — trivial proxy
    if name == "PENDING_LOG":
        return _pending_log()
    raise AttributeError(name)


@dataclass
class PendingApproval:
    request_id: str
    ts_created: float
    counterparty_id: str
    counterparty_platform: str
    counterparty_user_id: str
    counterparty_display: str
    junior_session_id: str
    junior_question: str
    draft_answer: str
    topic: str
    stakes: str
    confidence: float
    status: Status = "pending"
    final_text: str = ""
    ts_resolved: float | None = None


def _short_id() -> str:
    return uuid.uuid4().hex[:8]


def _now() -> float:
    return time.time()


def create(
    *,
    counterparty_id: str,
    counterparty_platform: str,
    counterparty_user_id: str,
    counterparty_display: str,
    junior_session_id: str,
    junior_question: str,
    draft_answer: str,
    topic: str,
    stakes: str,
    confidence: float,
) -> PendingApproval:
    """Create a new pending approval and write its create event to the log."""
    req = PendingApproval(
        request_id=_short_id(),
        ts_created=_now(),
        counterparty_id=counterparty_id,
        counterparty_platform=counterparty_platform,
        counterparty_user_id=counterparty_user_id,
        counterparty_display=counterparty_display,
        junior_session_id=junior_session_id,
        junior_question=junior_question,
        draft_answer=draft_answer,
        topic=topic,
        stakes=stakes,
        confidence=confidence,
    )
    storage.append_jsonl(
        _pending_log(),
        {
            "type": "create",
            "request_id": req.request_id,
            "ts": req.ts_created,
            "counterparty_id": req.counterparty_id,
            "counterparty_platform": req.counterparty_platform,
            "counterparty_user_id": req.counterparty_user_id,
            "counterparty_display": req.counterparty_display,
            "junior_session_id": req.junior_session_id,
            "junior_question": req.junior_question,
            "draft_answer": req.draft_answer,
            "topic": req.topic,
            "stakes": req.stakes,
            "confidence": req.confidence,
        },
    )
    return req


def _replay(events: Iterable[dict]) -> dict[str, PendingApproval]:
    """Reduce an event stream into the latest state per request_id."""
    state: dict[str, PendingApproval] = {}
    for ev in events:
        if not isinstance(ev, dict):
            continue
        rid = ev.get("request_id")
        if not isinstance(rid, str):
            continue
        if ev.get("type") == "create":
            state[rid] = PendingApproval(
                request_id=rid,
                ts_created=float(ev.get("ts") or 0.0),
                counterparty_id=ev.get("counterparty_id", ""),
                counterparty_platform=ev.get("counterparty_platform", ""),
                counterparty_user_id=ev.get("counterparty_user_id", ""),
                counterparty_display=ev.get("counterparty_display", ""),
                junior_session_id=ev.get("junior_session_id", ""),
                junior_question=ev.get("junior_question", ""),
                draft_answer=ev.get("draft_answer", ""),
                topic=ev.get("topic", ""),
                stakes=ev.get("stakes", ""),
                confidence=float(ev.get("confidence") or 0.0),
            )
        elif ev.get("type") == "status":
            cur = state.get(rid)
            if cur is None:
                # Status event without a create — skip (corrupt log).
                continue
            cur.status = ev.get("status", cur.status)  # type: ignore[assignment]
            cur.final_text = ev.get("final_text", "") or ""
            cur.ts_resolved = float(ev.get("ts") or 0.0)
    return state


def _read_all() -> list[dict]:
    """Read the full event log (returns [] if missing)."""
    log = _pending_log()
    if not log.exists():
        return []
    out: list[dict] = []
    import json as _json  # local import keeps storage's mockability
    with log.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(_json.loads(line))
            except _json.JSONDecodeError:
                continue
    return out


def list_pending() -> list[PendingApproval]:
    """All requests currently in ``pending`` state, oldest first."""
    state = _replay(_read_all())
    pendings = [r for r in state.values() if r.status == "pending"]
    pendings.sort(key=lambda r: r.ts_created)
    return pendings


def get(request_id: str) -> PendingApproval | None:
    """Latest state of one request_id, or None if unknown."""
    state = _replay(_read_all())
    return state.get(request_id)


def set_status(
    request_id: str,
    new_status: Status,
    final_text: str = "",
) -> PendingApproval | None:
    """Append a status event; returns the updated record or None if unknown.

    Idempotency: re-applying the same status is allowed and produces a fresh
    log entry (the audit trail is preserved). Callers should check ``.status``
    on the return to decide whether to dispatch.
    """
    cur = get(request_id)
    if cur is None:
        return None
    storage.append_jsonl(
        _pending_log(),
        {
            "type": "status",
            "request_id": request_id,
            "ts": _now(),
            "status": new_status,
            "final_text": final_text,
        },
    )
    cur.status = new_status
    cur.final_text = final_text
    cur.ts_resolved = _now()
    return cur


def list_overdue(timeout_seconds: float) -> list[PendingApproval]:
    """Return pending approvals older than *timeout_seconds*, oldest first.

    Used by the timeout sweeper (``bin/sweep_pending.py``) to find requests
    that need to be auto-marked ``timed_out`` so the junior gets an
    explanatory reply and the owner gets reminded.
    """
    if timeout_seconds <= 0:
        return []
    cutoff = _now() - timeout_seconds
    return [r for r in list_pending() if r.ts_created < cutoff]
