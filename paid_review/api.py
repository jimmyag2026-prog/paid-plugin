"""paid_review.api — 5 public functions for the review skill (spec §5).

Sprint A: happy path INTAKE→SUBJECT→QA→CLOSED works end-to-end with a fake
LLM adapter.  MERGE and GATE handlers raise NotImplementedError (Sprint B/C).

Sprint B: SUBJECT→SCAN now runs the full 4-pillar + Responder Sim two-layer
scan (paid_review.core.scan); QA reply classification uses short-code fast
path + LLM free-text fallback (paid_review.core.qa). Findings rendering
moved to qa.render_finding for i18n + consistent layout.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from paid_review.core.annotation import (
    Annotation,
    all_resolved,
    append_annotation,
    load_annotations,
    open_count,
    update_status,
)
from paid_review.core.cursor import (
    Cursor,
    advance,
    is_exhausted,
    load_cursor,
    more,
    save_cursor,
)
from paid_review.core.state import (
    InvalidStateError,
    SessionState,
    Stage,
    Verdict,
    load_state,
    save_state,
    session_dir,
    transition,
    validate_stage_verdict,
    _now_iso,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public reply / exception types (spec §5)
# ---------------------------------------------------------------------------

@dataclass
class ReviewReply:
    text: str
    stage: Stage
    event_kind: str          # intake_ack / subject_ask / finding / scan_progress /
                             # close_propose / cancelled / …
    closed: bool = False     # True → caller must clear_active_review_session


class IntakeRefused(Exception):
    """intake() raises when cp already has an active_review_session."""


class ReviewSessionConflict(Exception):
    """fcntl.flock contention on cp.profile.json (INV-6 / Ⓜ19)."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _max_rounds_from_env(default: int = 3) -> int:
    raw = os.environ.get("PAID_REVIEW_MAX_ROUNDS", str(default))
    try:
        return min(int(raw), 5)
    except ValueError:
        return default


def _call_llm(prompt: str, system: str = "") -> str:
    """Thin wrapper so tests can monkeypatch paid.hermes_io.call_llm."""
    from paid import hermes_io
    return hermes_io.call_llm(
        system_prompt=system or "You are a review assistant.",
        user_message=prompt,
    )


def _build_subject_options(initial_message: str) -> list[str]:
    """Call LLM to generate up to 4 subject candidate strings."""
    prompt = (
        f"Given this review request:\n\n{initial_message}\n\n"
        "Generate 3-4 concise subject candidates (one per line, no numbering)."
    )
    try:
        raw = _call_llm(prompt)
        # Try JSON array first, fall back to line-split
        try:
            candidates = json.loads(raw)
            if isinstance(candidates, list):
                return [str(c).strip() for c in candidates[:4] if str(c).strip()]
        except json.JSONDecodeError:
            pass
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        return lines[:4]
    except Exception:
        return [initial_message[:80]]


def _build_findings(subject: str, initial_message: str,
                    *, owner_name: str = "the owner",
                    responder_profile: str = "") -> list[Annotation]:
    """Run the Sprint B 2-layer scan (4-pillar + Responder Sim).

    Sprint A used a single mixed LLM call as a placeholder; Sprint B
    delegates to paid_review.core.scan which does two separate LLM
    calls (each layer can fail/succeed independently) and merges + dedups.
    Both layers return [] if the LLM fails — caller must handle the
    no_findings short-circuit (Ⓜ17, already implemented in _handle_subject).
    """
    from paid_review.core import scan
    return scan.run_full_scan(
        subject=subject,
        document=initial_message,
        owner_name=owner_name,
        responder_profile=responder_profile,
    )


def _render_options(opts: list[str]) -> str:
    parts = []
    labels = "abcdefghij"
    for i, opt in enumerate(opts):
        label = labels[i] if i < len(labels) else str(i + 1)
        parts.append(f"({label}) {opt}")
    return "\n".join(parts)


def _finding_text(ann: Annotation, index: int, total: int,
                  lang: str = "zh") -> str:
    """Sprint B: delegated to qa.render_finding for i18n + consistency."""
    from paid_review.core import qa
    return qa.render_finding(ann, index, total, lang=lang)


def _reply_kind_for_answer(text: str, finding: Annotation | None = None) -> str | None:
    """Sprint A short-code matcher; superseded by qa.classify_reply (which
    adds free-text LLM fallback). Kept as a thin wrapper for the few
    legacy call sites that don't have the finding object handy.

    Returns None when text is unrecognized AND no finding is provided
    (caller should treat as modified or fall back to LLM).
    """
    from paid_review.core import qa
    short = qa._short_code(text)
    if short is not None:
        return short
    if finding is not None:
        # Free-text path requires a finding for context
        return qa.classify_reply(text, finding)
    return None


# ---------------------------------------------------------------------------
# Stage handlers (internal)
# ---------------------------------------------------------------------------

def _handle_intake(
    state: SessionState, text: str, sid_dir: Path
) -> ReviewReply:
    """INTAKE: generate subject candidates from normalized doc, move → SUBJECT.

    Reads the seeded normalized.md (written by intake() via ingest backend)
    rather than the trigger text, since the plugin calls handle_inbound with
    text="" right after intake() returns. Falls back to trigger text for
    legacy callers that haven't seeded normalized.md.
    """
    normalized = sid_dir / "normalized.md"
    normalized.parent.mkdir(parents=True, exist_ok=True)
    if normalized.exists():
        seed = normalized.read_text(encoding="utf-8")
    else:
        seed = text or ""
        normalized.write_text(seed, encoding="utf-8")

    # If ingest produced nothing AND trigger has content, fall back to trigger
    if not seed.strip() and text and text.strip():
        seed = text.strip()
        normalized.write_text(seed, encoding="utf-8")

    candidates = _build_subject_options(seed)
    if not candidates:
        candidates = [seed[:80] if seed else "(待补充)"]

    # Store candidates so SUBJECT handler can resolve selection
    meta_extra = sid_dir / "subject_candidates.json"
    meta_extra.write_text(
        json.dumps(candidates, ensure_ascii=False), encoding="utf-8"
    )

    # Transition INTAKE → SUBJECT (verdict stays PENDING — valid for both stages)
    transition(state, "SUBJECT")
    state.last_event_kind = "subject_ask"
    save_state(state)

    opts = _render_options(candidates)
    return ReviewReply(
        text=(
            f"收到！请确认 review 的主题：\n\n{opts}\n\n"
            "(pass) 不用以上选项，我自己说\n(custom) 输入自定义主题"
        ),
        stage="SUBJECT",
        event_kind="subject_ask",
    )


def _handle_subject(
    state: SessionState, text: str, sid_dir: Path
) -> ReviewReply:
    """SUBJECT: resolve selection → run (fake) SCAN → enter QA with first finding."""
    t = text.strip().lower()

    # Load stored candidates
    cand_path = sid_dir / "subject_candidates.json"
    candidates: list[str] = []
    if cand_path.exists():
        try:
            candidates = json.loads(cand_path.read_text(encoding="utf-8"))
        except Exception:
            candidates = []

    label_map = dict(zip("abcdefghij", candidates))

    if t in label_map:
        subject = label_map[t]
    elif t in ("pass", "custom") or t not in ("a", "b", "c", "d", "e"):
        # Free-text subject
        subject = text.strip() if text.strip().lower() not in ("pass", "custom") else ""
        if not subject:
            return ReviewReply(
                text="请输入你的自定义主题：",
                stage="SUBJECT",
                event_kind="subject_ask",
            )
    else:
        return ReviewReply(
            text="不太懂，请回 a/b/c 或输入自定义主题。",
            stage="SUBJECT",
            event_kind="subject_ask",
        )

    state.subject = subject

    # Transition SUBJECT → SCAN (internal; no external input)
    transition(state, "SCAN")
    state.last_event_kind = "scan_in_progress"
    save_state(state)

    # Run scan (LLM call to produce findings)
    initial_message = ""
    norm = sid_dir / "normalized.md"
    if norm.exists():
        initial_message = norm.read_text(encoding="utf-8")

    findings = _build_findings(subject, initial_message)

    # no_findings short-circuit (Ⓜ17): SCAN → CLOSED verdict=READY
    if not findings:
        state.verdict = "READY"
        transition(state, "CLOSED")
        state.forced = True
        state.forced_reason = "no_findings"
        state.closed_at = _now_iso()
        state.last_event_kind = "close_propose"
        save_state(state)
        return ReviewReply(
            text="扫描完成，没有发现需要讨论的 finding。材料已达 decision-ready。",
            stage="CLOSED",
            event_kind="close_propose",
            closed=True,
        )

    # Save findings as annotations
    for ann in findings:
        append_annotation(sid_dir, ann)

    # Build cursor: first finding is current_id, rest in pending
    cursor = Cursor(
        current_id=findings[0].id,
        pending=[f.id for f in findings[1:]],
    )
    save_cursor(sid_dir, cursor)

    # Transition SCAN → QA
    state.rounds += 1
    transition(state, "QA")
    state.last_event_kind = "finding"
    save_state(state)

    first = findings[0]
    total = len(findings)
    return ReviewReply(
        text=_finding_text(first, 1, total),
        stage="QA",
        event_kind="finding",
    )


def _handle_qa(
    state: SessionState, text: str, sid_dir: Path
) -> ReviewReply:
    """QA: answer current finding or send 'done' to close the QA loop."""
    cursor = load_cursor(sid_dir)
    annotations = load_annotations(sid_dir)
    ann_by_id = {a.id: a for a in annotations}
    total = len(annotations)

    t = text.strip().lower()

    # --- done command ---
    if t == "done":
        open_n = open_count(sid_dir)
        if open_n > 0:
            return ReviewReply(
                text=(
                    f"还有 {open_n} 条 finding 没回，继续\n"
                    "(a) 接受 / (b) 保留异议 / (c) 标为无解 / (skip) 跳过"
                ),
                stage="QA",
                event_kind="finding",
            )
        # All resolved and cursor exhausted → proceed
        if not is_exhausted(cursor):
            return ReviewReply(
                text=(
                    f"还有 {len(cursor.pending) + (1 if cursor.current_id else 0)} "
                    "条 finding 未送达，请继续回复。"
                ),
                stage="QA",
                event_kind="finding",
            )

        # INV-2: check rounds limit before proceeding
        if state.rounds >= state.max_rounds:
            return _do_force_close(
                state, sid_dir, reason="rounds_exhausted"
            )

        # Sprint C: real GATE path. Skip MERGE for now (perm=suggest is
        # default but Sprint D/E adds revised.md generation; v0.1 ships
        # with perm=none semantics — junior's accepted findings are the
        # final material). Run final_gate → deliver → CLOSED.
        return _do_gate_and_close(state, sid_dir)

    # --- (more) command ---
    if t == "(more)" or t == "more":
        cursor = more(cursor, 3)
        save_cursor(sid_dir, cursor)
        if cursor.current_id is None and cursor.pending:
            cursor = advance(cursor)
            save_cursor(sid_dir, cursor)
        if cursor.current_id:
            ann = ann_by_id.get(cursor.current_id)
            done_count = len(cursor.done)
            idx = done_count + 1
            return ReviewReply(
                text=_finding_text(ann, idx, total) if ann else "（finding 未找到）",
                stage="QA",
                event_kind="finding",
            )
        return ReviewReply(
            text="没有更多 deferred finding 了。回 done 完成 QA。",
            stage="QA",
            event_kind="finding",
        )

    # --- answer to current finding ---
    if cursor.current_id is None:
        # No active finding; prompt done
        return ReviewReply(
            text="所有 finding 已处理完。回 done 进入下一步。",
            stage="QA",
            event_kind="finding",
        )

    # Sprint B: short-code fast path → free-text LLM fallback (qa.classify_reply)
    from paid_review.core import qa as _qa
    short = _qa._short_code(text)
    if short is not None:
        status = short
    else:
        # Free text → classify via LLM with finding context
        current_ann = ann_by_id.get(cursor.current_id)
        if current_ann is not None:
            status = _qa.classify_reply(text, current_ann)
        else:
            status = "modified"  # defensive fallback when finding missing

    update_status(sid_dir, cursor.current_id, status)

    cursor = advance(cursor)
    save_cursor(sid_dir, cursor)

    # Next finding
    if cursor.current_id:
        ann = ann_by_id.get(cursor.current_id)
        done_count = len(cursor.done)
        idx = done_count + 1
        return ReviewReply(
            text=_finding_text(ann, idx, total) if ann else "（finding 未找到）",
            stage="QA",
            event_kind="finding",
        )

    # Cursor exhausted; suggest done if all resolved
    open_n = open_count(sid_dir)
    if open_n == 0:
        return ReviewReply(
            text="所有 finding 已处理完。回 done 进入下一步，或回 (more) 拉 deferred finding。",
            stage="QA",
            event_kind="finding",
        )
    return ReviewReply(
        text=(
            f"还有 {open_n} 条 finding 未解决。回 done 强制完成（会留在 open items）。"
        ),
        stage="QA",
        event_kind="finding",
    )


def _do_force_close(
    state: SessionState, sid_dir: Path, reason: str
) -> ReviewReply:
    """Internal: set state to CLOSED with FORCED_PARTIAL and save.

    Also writes the audit + summary artifacts so the owner has something
    to read about the partial session (Sprint C). Skips the GATE LLM call
    since the session never reached a clean close.
    """
    from paid_review.core import deliver as _deliver

    state.forced = True
    state.forced_reason = reason
    state.verdict = "FORCED_PARTIAL"
    state.closed_at = _now_iso()
    transition(state, "CLOSED")
    state.last_event_kind = "cancelled"
    save_state(state)
    open_n = open_count(sid_dir)

    # Write artifacts (audit at minimum; summary is best-effort) but DO
    # NOT archive — operator may want to inspect the partial directory.
    try:
        norm = sid_dir / "normalized.md"
        document = norm.read_text(encoding="utf-8") if norm.exists() else ""
        _deliver.write_artifacts(
            sid_dir,
            subject=state.subject or "(force-closed before subject set)",
            junior_name=state.cp_id,
            junior_platform=state.platform,
            rounds=state.rounds, verdict=state.verdict,
            document=document, forced_reason=reason,
        )
    except Exception:
        # Force-close path must never raise; missing artifacts is a soft fail.
        logger.warning("force_close: artifact write failed for sid=%s", state.sid)

    return ReviewReply(
        text=(
            f"Review session 已强制关闭（原因: {reason}）。"
            + (f" 剩余 {open_n} 条 finding 进 open_items。" if open_n else "")
        ),
        stage="CLOSED",
        event_kind="cancelled",
        closed=True,
    )


def _do_gate_and_close(
    state: SessionState, sid_dir: Path
) -> ReviewReply:
    """Internal: GATE form-check → deliver → CLOSED.

    Sprint C entry. Called when QA done and rounds within limit.

    Verdict logic per spec INV-5 + final_gate.md:
      - GATE returns FAIL → state.rounds += 1 + transition back to QA;
        rounds_exhausted check happens in _handle_qa next time
      - GATE returns READY / READY_WITH_OPEN_ITEMS → write artifacts +
        archive + return summary as ReviewReply text
    """
    from paid_review.core import final_gate as _gate
    from paid_review.core import deliver as _deliver
    from paid import storage as _storage

    annotations = load_annotations(sid_dir)
    norm = sid_dir / "normalized.md"
    document = norm.read_text(encoding="utf-8") if norm.exists() else ""
    subject = state.subject or "(no subject)"

    gate_result = _gate.run_final_gate(
        subject=subject, final_document=document,
        annotations=annotations, rounds=state.rounds,
    )

    # Persist the gate JSON for forensic / dashboard use
    try:
        (sid_dir / "final_gate.json").write_text(
            json.dumps(gate_result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass

    verdict = gate_result.get("verdict", "READY_WITH_OPEN_ITEMS")

    if verdict == "FAIL":
        # Intent gate failed — back to QA + rounds++ (INV-2)
        state.rounds += 1
        # FAIL is transient: per INV-5 matrix, FAIL only valid AT GATE→QA edge;
        # we leave verdict=FAIL momentarily then transition flips it via the
        # save path. Use direct assignment + transition (validate will accept
        # FAIL on GATE; then the next QA stage will require verdict=PENDING).
        state.verdict = "FAIL"  # transient marker
        transition(state, "QA")
        # Once in QA, INV-5 requires verdict=PENDING — reset
        state.verdict = "PENDING"
        state.last_event_kind = "gate_fail"
        save_state(state)
        rationale = gate_result.get("rationale", "")
        return ReviewReply(
            text=(
                f"Final gate FAIL — Intent pillar 没通过。本轮 rounds={state.rounds}。\n"
                f"原因: {rationale}\n"
                "请继续 QA 修补，回 done 再次尝试。"
            ),
            stage="QA",
            event_kind="finding",
        )

    # READY or READY_WITH_OPEN_ITEMS — go to CLOSED
    state.verdict = verdict if verdict in ("READY", "READY_WITH_OPEN_ITEMS") else "READY_WITH_OPEN_ITEMS"
    state.closed_at = _now_iso()
    transition(state, "CLOSED")
    state.last_event_kind = "close_propose"
    save_state(state)

    # Write summary + audit + archive
    sessions_root = _storage.PAID_DIR / "review" / "sessions"
    delivered = _deliver.deliver(
        sid_dir, sessions_root,
        subject=subject,
        junior_name=state.cp_id,
        junior_platform=state.platform,
        rounds=state.rounds, verdict=state.verdict,
        document=document,
    )

    return ReviewReply(
        text=delivered["summary"],   # owner gets the 6-section brief
        stage="CLOSED",
        event_kind="close_propose",
        closed=True,
    )


# ---------------------------------------------------------------------------
# Public API — 5 functions (spec §5)
# ---------------------------------------------------------------------------

def intake(
    *,
    cp,                         # paid.identity.Counterparty
    initial_message: str,
    attachments: list,
    classification=None,
) -> str:
    """Create a review session. Returns sid.

    Atomic via fcntl.flock on cp.profile.json (INV-6 / Ⓜ19).
    Raises IntakeRefused if cp already has an active_review_session.
    Raises ReviewSessionConflict on lock contention.
    """
    from paid import identity as _identity, storage as _storage

    profile_path = (
        _storage.PAID_DIR / "counterparties" / cp.cp_id / "profile.json"
    )
    profile_path.parent.mkdir(parents=True, exist_ok=True)

    # Ensure profile file exists so we can open it for locking
    if not profile_path.exists():
        _identity.save_counterparty(cp)

    try:
        import fcntl as _fcntl
    except ImportError:
        _fcntl = None  # type: ignore[assignment]

    # Open in append mode: creates if not exists, never truncates
    with open(str(profile_path), "a") as lock_fh:
        if _fcntl is not None:
            try:
                _fcntl.flock(lock_fh.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            except OSError:
                raise ReviewSessionConflict(
                    f"Concurrent intake on cp {cp.cp_id!r} — retry in a moment."
                )
        try:
            # Re-read fresh cp state while we hold the lock
            fresh_cp = _identity.load_counterparty(cp.platform, cp.user_id)
            if fresh_cp is None:
                fresh_cp = cp

            if fresh_cp.active_review_session:
                raise IntakeRefused(
                    f"cp {cp.cp_id!r} already has active session "
                    f"{fresh_cp.active_review_session!r}"
                )

            # Create session
            sid = uuid.uuid4().hex[:12]
            state = SessionState(
                sid=sid,
                cp_id=fresh_cp.cp_id,
                owner_id=getattr(fresh_cp, "owner_id", ""),
                platform=fresh_cp.platform,
                stage="INTAKE",
                verdict="PENDING",
                max_rounds=_max_rounds_from_env(),
                created_at=_now_iso(),
                updated_at=_now_iso(),
            )
            save_state(state)

            # Mark cp as having this session (write inside lock)
            fresh_cp.active_review_session = sid
            _identity.save_counterparty(fresh_cp)

        finally:
            if _fcntl is not None:
                _fcntl.flock(lock_fh.fileno(), _fcntl.LOCK_UN)

    # Run ingest OUTSIDE the cp.profile.json lock — it writes only into the
    # session dir which is unique per sid so no contention. Best-effort:
    # any failure leaves normalized.md missing and _handle_intake falls back
    # to the trigger text. R5: never raises out of intake().
    try:
        from paid_review import ingest as _ingest_mod
        sid_dir = session_dir(sid)
        sid_dir.mkdir(parents=True, exist_ok=True)
        result = _ingest_mod.ingest(initial_message, attachments or [], sid_dir)
        (sid_dir / "normalized.md").write_text(
            result.normalized_text or (initial_message or ""),
            encoding="utf-8",
        )
        if result.errors:
            logger.warning(
                "[review] sid=%s ingest had %d errors", sid, len(result.errors)
            )
    except Exception as exc:
        logger.warning("[review] sid=%s ingest crashed: %s", sid, exc)

    return sid


def handle_inbound(sid: str, text: str, hook_kwargs: dict) -> ReviewReply:
    """Drive the state machine one step. Stamps state.last_inbound_at = now."""
    try:
        state = load_state(sid)
        if state is None:
            return ReviewReply(
                text=f"Review session {sid!r} not found.",
                stage="CLOSED",
                event_kind="error",
                closed=True,
            )

        state.last_inbound_at = _now_iso()
        sid_dir = session_dir(sid)

        if state.stage == "CLOSED":
            return ReviewReply(
                text="该 review session 已关闭。发 /review <subject> 开新一轮。",
                stage="CLOSED",
                event_kind="close_ack",
                closed=True,
            )

        if state.stage == "INTAKE":
            return _handle_intake(state, text, sid_dir)

        if state.stage == "SUBJECT":
            return _handle_subject(state, text, sid_dir)

        if state.stage == "QA":
            return _handle_qa(state, text, sid_dir)

        if state.stage == "SCAN":
            # SCAN is internal; external input during scan = progress message
            return ReviewReply(
                text="正在扫描中，请稍候...",
                stage="SCAN",
                event_kind="scan_progress",
            )

        if state.stage == "MERGE":
            raise NotImplementedError("MERGE handler in Sprint B")

        if state.stage == "GATE":
            raise NotImplementedError("GATE handler in Sprint C")

        return ReviewReply(
            text=f"未知 stage: {state.stage!r}",
            stage=state.stage,  # type: ignore[arg-type]
            event_kind="error",
        )

    except (IntakeRefused, ReviewSessionConflict, NotImplementedError):
        raise
    except Exception as exc:
        logger.exception("handle_inbound error for sid=%r: %s", sid, exc)
        return ReviewReply(
            text=f"内部错误，请稍后重试。",
            stage="INTAKE",
            event_kind="error",
        )


def list_open(owner_id: str | None = None) -> str:
    """Return a markdown table of all non-CLOSED review sessions."""
    from paid import storage
    sessions_root = storage.PAID_DIR / "review" / "sessions"
    if not sessions_root.exists():
        return "_没有进行中的 review session。_"

    rows: list[str] = []
    for sid_path in sorted(sessions_root.iterdir()):
        if not sid_path.is_dir():
            continue
        state = load_state(sid_path.name)
        if state is None or state.stage == "CLOSED":
            continue
        if owner_id and state.owner_id and state.owner_id != owner_id:
            continue
        subject = state.subject or "(待确认)"
        rows.append(
            f"| {state.sid} | {state.cp_id} | {state.stage} | "
            f"{subject} | {state.rounds}/{state.max_rounds} |"
        )

    if not rows:
        return "_没有进行中的 review session。_"

    header = "| SID | CP | Stage | Subject | Rounds |\n|---|---|---|---|---|"
    return header + "\n" + "\n".join(rows)


def show(sid: str) -> str:
    """Return a markdown summary of one session."""
    state = load_state(sid)
    if state is None:
        return f"_Session {sid!r} 未找到。_"

    sid_dir = session_dir(sid)
    annotations = load_annotations(sid_dir)
    open_n = sum(1 for a in annotations if a.status == "open")
    closed_n = len(annotations) - open_n

    lines = [
        f"## Review Session `{state.sid}`",
        f"- **Stage**: {state.stage}  **Verdict**: {state.verdict}",
        f"- **CP**: {state.cp_id}  **Platform**: {state.platform}",
        f"- **Subject**: {state.subject or '(未确认)'}",
        f"- **Rounds**: {state.rounds}/{state.max_rounds}",
        f"- **Findings**: {len(annotations)} total, {open_n} open, {closed_n} resolved",
        f"- **Created**: {state.created_at}  **Updated**: {state.updated_at}",
    ]
    if state.forced:
        lines.append(f"- **Forced**: {state.forced_reason}")
    return "\n".join(lines)


def force_close(sid: str, *, reason: str = "owner_force") -> str:
    """Force-close from ANY non-CLOSED stage. Returns status message.

    Sets verdict=FORCED_PARTIAL, forced=True (INV-1 / INV-5).
    Special case: reason='no_findings' sets verdict=READY (Ⓜ17 exception).
    """
    state = load_state(sid)
    if state is None:
        return f"Session {sid!r} not found."
    if state.stage == "CLOSED":
        return f"Session {sid!r} is already CLOSED."

    sid_dir = session_dir(sid)

    # Ⓜ17 exception: no_findings → verdict READY (material is decision-ready)
    if reason == "no_findings":
        state.verdict = "READY"
    else:
        state.verdict = "FORCED_PARTIAL"

    state.forced = True
    state.forced_reason = reason
    state.closed_at = _now_iso()

    try:
        transition(state, "CLOSED")
    except InvalidStateError as exc:
        return f"force_close error: {exc}"

    state.last_event_kind = "cancelled"
    save_state(state)
    return f"Session {sid!r} force-closed (reason={reason!r}, verdict={state.verdict!r})."
