"""SessionState dataclass + stage transition enforcement (INV-1..6 spec §3.1, §4)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

Stage = Literal["INTAKE", "SUBJECT", "SCAN", "QA", "MERGE", "GATE", "CLOSED"]
Verdict = Literal["READY", "READY_WITH_OPEN_ITEMS", "FORCED_PARTIAL", "FAIL", "PENDING"]

# Legal stage→stage edges per spec §3 state diagram.
# CLOSED is reachable from every non-CLOSED stage (force_close is universal — INV-1).
LEGAL_TRANSITIONS: dict[str, set[str]] = {
    "INTAKE":   {"INTAKE", "SUBJECT", "CLOSED"},
    "SUBJECT":  {"SCAN", "CLOSED"},
    "SCAN":     {"QA", "CLOSED", "SUBJECT"},
    "QA":       {"QA", "MERGE", "GATE", "CLOSED"},
    "MERGE":    {"GATE", "QA", "CLOSED"},
    "GATE":     {"CLOSED", "QA"},
    "CLOSED":   set(),
}

# INV-5 matrix: valid (stage, verdict) pairs per spec §3.1.
# Keyed by stage; value = set of verdicts that are legal for that stage.
VALID_VERDICTS: dict[str, set[str]] = {
    "INTAKE":   {"PENDING"},
    "SUBJECT":  {"PENDING"},
    "SCAN":     {"PENDING"},
    "QA":       {"PENDING"},
    "MERGE":    {"PENDING"},
    "GATE":     {"PENDING", "READY", "READY_WITH_OPEN_ITEMS", "FAIL"},
    "CLOSED":   {"READY", "READY_WITH_OPEN_ITEMS", "FORCED_PARTIAL"},
}


class InvalidStateError(Exception):
    """Raised on illegal stage→stage or stage×verdict combination (INV-5)."""


@dataclass
class SessionState:
    """Per-session state machine record. Stored at sessions/<sid>/meta.json."""

    sid: str
    schema_version: int = 1
    created_at: str = ""
    updated_at: str = ""
    last_inbound_at: str = ""      # TTL anchor; stamped by handle_inbound
    cp_id: str = ""
    owner_id: str = ""
    platform: str = ""             # tg | lark | slack
    stage: Stage = "INTAKE"
    subject: str | None = None
    rounds: int = 0
    max_rounds: int = 3
    verdict: Verdict = "PENDING"
    doc_edit_permission: Literal["none", "suggest", "direct"] = "suggest"
    forced: bool = False
    forced_reason: str = ""        # rounds_exhausted / TTL_expired / owner_force / …
    closed_at: str | None = None
    last_event_kind: str = ""
    llm_cost_usd: float = 0.0
    trace_id: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_stage_verdict(stage: Stage, verdict: Verdict) -> None:
    """Raise InvalidStateError if (stage, verdict) is illegal per INV-5 matrix."""
    valid = VALID_VERDICTS.get(stage, set())
    if verdict not in valid:
        raise InvalidStateError(
            f"Illegal verdict {verdict!r} for stage {stage!r} "
            f"(valid: {sorted(valid)})"
        )


def transition(state: SessionState, new_stage: Stage) -> SessionState:
    """Move *state* to *new_stage*, enforcing:
      1. CLOSED is terminal — no further transitions allowed
      2. stage→new_stage edge must exist in LEGAL_TRANSITIONS
      3. (new_stage, state.verdict) must be valid per INV-5 matrix

    Caller must set state.verdict to a value valid for *new_stage* BEFORE calling
    transition().  Stamps state.updated_at on success.
    """
    if state.stage == "CLOSED":
        raise InvalidStateError(
            f"CLOSED is terminal; cannot transition to {new_stage!r}"
        )

    legal = LEGAL_TRANSITIONS.get(state.stage, set())
    if new_stage not in legal:
        raise InvalidStateError(
            f"Illegal stage transition: {state.stage!r} → {new_stage!r}"
        )

    validate_stage_verdict(new_stage, state.verdict)

    state.stage = new_stage
    state.updated_at = _now_iso()
    return state


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def session_dir(sid: str) -> Path:
    from paid import storage
    return storage.PAID_DIR / "review" / "sessions" / sid


def load_state(sid: str) -> SessionState | None:
    """Load SessionState from sessions/<sid>/meta.json. Returns None if missing."""
    meta = session_dir(sid) / "meta.json"
    if not meta.exists():
        return None
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return SessionState(
        sid=data.get("sid", sid),
        schema_version=int(data.get("schema_version", 1)),
        created_at=data.get("created_at", ""),
        updated_at=data.get("updated_at", ""),
        last_inbound_at=data.get("last_inbound_at", ""),
        cp_id=data.get("cp_id", ""),
        owner_id=data.get("owner_id", ""),
        platform=data.get("platform", ""),
        stage=data.get("stage", "INTAKE"),
        subject=data.get("subject"),
        rounds=int(data.get("rounds", 0)),
        max_rounds=int(data.get("max_rounds", 3)),
        verdict=data.get("verdict", "PENDING"),
        doc_edit_permission=data.get("doc_edit_permission", "suggest"),
        forced=bool(data.get("forced", False)),
        forced_reason=data.get("forced_reason", ""),
        closed_at=data.get("closed_at"),
        last_event_kind=data.get("last_event_kind", ""),
        llm_cost_usd=float(data.get("llm_cost_usd", 0.0)),
        trace_id=data.get("trace_id"),
    )


def save_state(state: SessionState) -> None:
    """Atomically persist SessionState via tmp-rename pattern."""
    d = session_dir(state.sid)
    d.mkdir(parents=True, exist_ok=True)
    meta = d / "meta.json"
    tmp = meta.with_suffix(".tmp")
    payload = asdict(state)
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    tmp.write_text(text, encoding="utf-8")
    # fsync before rename for crash-safety (R8)
    with tmp.open("r", encoding="utf-8") as fh:
        os.fsync(fh.fileno())
    tmp.replace(meta)
