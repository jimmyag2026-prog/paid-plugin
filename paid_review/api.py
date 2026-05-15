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
from paid_review.i18n import detect_lang, t as _i18n_t
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
    """Thin wrapper so tests can monkeypatch paid.hermes_io.call_llm.

    v1.3.5 fix: pre-fix used system_prompt= and user_message= kwargs
    which don't exist on hermes_io.call_llm — every call raised
    TypeError, swallowed by wrapping try/except → silent return.
    Same root-cause bug existed in scan.py / qa.py / final_gate.py /
    build_summary.py — all fixed in this commit. Tests never caught
    it because they monkeypatch _call_llm directly.
    """
    from paid import hermes_io
    return hermes_io.call_llm(
        prompt=prompt,
        system=system or "You are a review assistant.",
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
                    responder_profile: str = ""
                    ) -> tuple[list[Annotation], list[str]]:
    """Run the Sprint B 2-layer scan (4-pillar + Responder Sim).

    Returns ``(annotations, failed_layers)``. v1.3.2 — previously
    returned just the list, which couldn't distinguish "LLM ran fine,
    0 findings" from "both LLMs failed, 0 findings". The latter case
    used to silently close the review as READY (Ⓜ17 short-circuit),
    which means a transient LLM outage produced a clean bill of
    health. Caller (_handle_subject) now checks failed_layers and
    escalates instead of auto-closing when the scan never produced a
    real signal.
    """
    from paid_review.core import scan
    return scan.run_full_scan_with_errors(
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

    # v1.5.3 — detect cp language from seed (or fall back to trigger text)
    # for downstream i18n. Stored on state so SCAN / QA / cancel replies
    # in this session keep using the same language as the cp typed in.
    if not state.lang:
        state.lang = detect_lang(seed or text or "")

    # v1.6.15b: ingest-failure UX gate. If a link couldn't be fetched
    # (anti-scrape wall / no backend / Lark download failed), don't
    # silently run SCAN/QA on degraded input and emit "no opinion
    # provided" noise (jelabs pilot day-1). Surface the failure and let
    # the user decide: continue (they'll paste text / proceed with what
    # we have) or /review cancel. Fires exactly once — tracked via
    # last_event_kind == "ingest_failed".
    if state.ingest_errors and state.last_event_kind != "ingest_failed":
        state.last_event_kind = "ingest_failed"
        save_state(state)
        return ReviewReply(
            text=_i18n_t(
                "ingest_failed_gate", state.lang or "zh",
                errors="\n".join(f"- {e}" for e in state.ingest_errors),
            ),
            stage="INTAKE",
            event_kind="ingest_failed",
        )
    if state.last_event_kind == "ingest_failed":
        decision = (text or "").strip().lower()
        if decision in _INGEST_ABORT_TOKENS:
            state.verdict = "FORCED_PARTIAL"
            state.forced = True
            state.forced_reason = "junior_cancelled_on_ingest_error"
            state.closed_at = _now_iso()
            transition(state, "CLOSED")
            state.last_event_kind = "cancelled"
            save_state(state)
            return ReviewReply(
                text=_i18n_t("ingest_failed_cancelled", state.lang or "zh"),
                stage="CLOSED",
                event_kind="cancelled",
                closed=True,
            )
        if decision in _INGEST_CONTINUE_TOKENS:
            # Consume the gate; fall through to normal subject flow.
            state.last_event_kind = ""
            save_state(state)
        else:
            return ReviewReply(
                text=_i18n_t(
                    "ingest_failed_clarify", state.lang or "zh",
                    errors="\n".join(
                        f"- {e}" for e in state.ingest_errors
                    ),
                ),
                stage="INTAKE",
                event_kind="ingest_failed",
            )

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

    # v1.3.2 H7: fast-start subject confirmation.
    # Pre-fix UX was 3-5 round-trips: PAID lists a/b/c → user picks → if
    # pass/custom → asks "输入自定义主题" → user types. Pilot bounced off.
    # New UX: auto-select the top candidate as the assumed subject and ask
    # one yes/no — if "yes"/empty/affirm, proceed; else treat user's text
    # as the corrected subject. Other candidates surfaced as hints.
    # v1.5.3: tone + i18n via paid_review.i18n.t. zh/en/ko supported.
    top = candidates[0]
    alt_hint = ""
    if len(candidates) > 1:
        alt_hint = _i18n_t(
            "subject_ask_alt_hint", state.lang or "zh",
            alts=" / ".join(candidates[1:5]),
        )
    return ReviewReply(
        text=_i18n_t(
            "subject_ask", state.lang or "zh",
            top=top, alt_hint=alt_hint,
        ),
        stage="SUBJECT",
        event_kind="subject_ask",
    )


_SUBJECT_AFFIRM_TOKENS = {"yes", "y", "ok", "okay", "对", "对的", "是", "是的", "正确",
                          "确认", "好的", "可以", "go", "start"}

# v1.6.15b ingest-failure gate decisions. /review cancel is intercepted
# upstream in __init__.py before it reaches here, but a bare cancel /
# 取消 / 算了 typed at the gate must also abort.
_INGEST_CONTINUE_TOKENS = {
    "continue", "cont", "c", "yes", "y", "ok", "okay", "go", "proceed",
    "start", "继续", "继续吧", "接着", "对", "是", "好的", "可以", "没关系",
    "无所谓", "直接来", "照旧",
}
_INGEST_ABORT_TOKENS = {
    "cancel", "/review cancel", "/r cancel", "no", "n", "stop", "abort",
    "取消", "算了", "不用了", "退出", "结束",
}


def _handle_subject(
    state: SessionState, text: str, sid_dir: Path
) -> ReviewReply:
    """SUBJECT: resolve top-candidate confirmation OR explicit subject → run
    SCAN → enter QA. v1.3.2 H7 simplifies grammar: yes-token confirms top
    candidate; legacy a/b/c letter pick still works for back-compat;
    anything else is treated as a corrected subject."""
    t_raw = text.strip()
    t = t_raw.lower()

    # Load stored candidates
    cand_path = sid_dir / "subject_candidates.json"
    candidates: list[str] = []
    if cand_path.exists():
        try:
            candidates = json.loads(cand_path.read_text(encoding="utf-8"))
        except Exception:
            candidates = []

    top = candidates[0] if candidates else ""

    lang = state.lang or "zh"
    # 1) Affirm token → use the top candidate we proposed in subject_ask
    if t in _SUBJECT_AFFIRM_TOKENS:
        if not top:
            return ReviewReply(
                text=_i18n_t("subject_no_candidates", lang),
                stage="SUBJECT",
                event_kind="subject_ask",
            )
        subject = top
    # 2) Legacy a/b/c picker still works
    elif t in dict(zip("abcdefghij", candidates)):
        subject = dict(zip("abcdefghij", candidates))[t]
    # 3) pass / custom is now redundant but tolerated (legacy)
    elif t in ("pass", "custom"):
        return ReviewReply(
            text=_i18n_t("subject_pass_legacy", lang),
            stage="SUBJECT",
            event_kind="subject_ask",
        )
    # 4) Anything else (non-empty) is treated as a corrected subject
    elif t_raw:
        subject = t_raw
    else:
        # Empty inbound (rare; user hit enter) — re-prompt with top
        return ReviewReply(
            text=_i18n_t("subject_ask_reprompt", lang, top=top or "(待补充)"),
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

    findings, failed_layers = _build_findings(subject, initial_message)

    # v1.3.2 B4: distinguish "scan ran fine, 0 findings" from "scan
    # LLMs failed → 0 findings". Pre-fix both paths short-circuited to
    # verdict=READY which gave a transient outage a clean bill of
    # health. Now both-layers-failed escalates to owner via force_close
    # with a distinctive forced_reason so the brief makes clear the
    # scan never actually ran.
    if not findings and len(failed_layers) >= 2:
        # FORCED_PARTIAL is the canonical "closed without clean outcome"
        # verdict per INV-5; FAIL is only legal at the GATE→QA edge.
        # (v1.3.5: pre-fix was state.verdict='FAIL' which crashed
        # transition() with InvalidStateError. Caught in dogfood.)
        state.verdict = "FORCED_PARTIAL"
        transition(state, "CLOSED")
        state.forced = True
        state.forced_reason = "scan_unavailable"
        state.closed_at = _now_iso()
        state.last_event_kind = "close_propose"
        save_state(state)
        return ReviewReply(
            text=(
                "⚠️ Review 扫描层都没拿到 LLM 响应（可能是限流或临时故障）。"
                "已上报给 owner，他会跟你直接对接。session 已关闭。"
            ),
            stage="CLOSED",
            event_kind="close_propose",
            closed=True,
        )

    # no_findings short-circuit (Ⓜ17): SCAN → CLOSED verdict=READY
    # Only when at MOST one layer failed AND that one was the
    # non-empty layer (i.e. we have evidence the scan actually ran).
    if not findings:
        state.verdict = "READY"
        transition(state, "CLOSED")
        state.forced = True
        state.forced_reason = "no_findings"
        state.closed_at = _now_iso()
        state.last_event_kind = "close_propose"
        save_state(state)
        partial_note = ""
        if failed_layers:
            partial_note = (
                f"\n\n_注：{failed_layers[0]} 层 LLM 调用失败，已用单层结果。_"
            )
        return ReviewReply(
            text="扫描完成，没有发现需要讨论的 finding。材料已达 decision-ready。" + partial_note,
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
    lang = state.lang or "zh"

    # --- done command ---
    if t == "done":
        open_n = open_count(sid_dir)
        if open_n > 0:
            return ReviewReply(
                text=_i18n_t("qa_continue_pending", lang, n=open_n),
                stage="QA",
                event_kind="finding",
            )
        # All resolved and cursor exhausted → proceed
        if not is_exhausted(cursor):
            n_left = len(cursor.pending) + (1 if cursor.current_id else 0)
            return ReviewReply(
                text=_i18n_t("qa_continue_pending", lang, n=n_left),
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
            text=_i18n_t("qa_no_more_deferred", lang),
            stage="QA",
            event_kind="finding",
        )

    # --- answer to current finding ---
    if cursor.current_id is None:
        # No active finding; prompt done
        return ReviewReply(
            text=_i18n_t("qa_done_prompt", lang),
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
            ingest_sources=state.ingest_sources,
            ingest_errors=state.ingest_errors,
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

    # v1.3.2 H5: conservative default. Pre-fix default was
    # READY_WITH_OPEN_ITEMS — if the gate LLM crashed or returned
    # malformed JSON, the review silently passed. New default = FAIL so
    # a broken gate forces escalation back to QA (or eventual rounds
    # exhaustion → FORCED_PARTIAL) instead of fabricating a pass.
    verdict = gate_result.get("verdict", "FAIL")

    if verdict == "FAIL":
        # Intent gate failed.
        # v1.3.5 fix: pre-existing bug surfaced by v1.3.2 H5 (which made
        # FAIL the default verdict on gate failures, so this path now
        # actually runs). Old code set state.verdict="FAIL" THEN called
        # transition("QA") — but transition() validates the new stage
        # against the CURRENT verdict, and QA only accepts verdict=PENDING.
        # InvalidStateError raised every time. Fix: rounds-exhausted path
        # → force-close FORCED_PARTIAL; otherwise reset verdict to PENDING
        # BEFORE transitioning back to QA.
        state.rounds += 1
        rationale = gate_result.get("rationale", "")

        if state.rounds >= state.max_rounds:
            # No more rounds left — force-close as partial. v1.3.6 fix:
            # also write the 6-section brief + audit + archive so the
            # owner gets the same deliverable as the READY path. Pre-fix
            # the rounds-exhausted close returned only the 1-line "FAIL
            # after N rounds" string and skipped deliver(), leaving the
            # session dir un-archived and the owner without findings
            # detail.
            state.verdict = "FORCED_PARTIAL"
            state.forced = True
            state.forced_reason = "rounds_exhausted"
            state.closed_at = _now_iso()
            transition(state, "CLOSED")
            state.last_event_kind = "close_propose"
            save_state(state)

            sessions_root = _storage.PAID_DIR / "review" / "sessions"
            try:
                delivered = _deliver.deliver(
                    sid_dir, sessions_root,
                    subject=subject,
                    junior_name=state.cp_id,
                    junior_platform=state.platform,
                    rounds=state.rounds, verdict=state.verdict,
                    document=document,
                    forced_reason=f"rounds_exhausted (gate rationale: {rationale})",
                    ingest_sources=state.ingest_sources,
                    ingest_errors=state.ingest_errors,
                )
                brief_text = delivered["summary"]
            except Exception as exc:
                logger.warning(
                    "_do_gate_and_close rounds_exhausted: deliver crashed "
                    "sid=%s: %s", state.sid, exc,
                )
                state.delivery_failed = True
                try:
                    save_state(state)
                except Exception:
                    pass
                brief_text = (
                    f"⚠️ Review session {state.sid} closed FORCED_PARTIAL after "
                    f"{state.rounds} rounds, but summary write failed: {exc}\n\n"
                    f"Gate rationale: {rationale}\n"
                    f"原始材料 + annotations 已在 session 目录留存，请联系 admin 取回。"
                )

            return ReviewReply(
                text=brief_text,
                stage="CLOSED",
                event_kind="close_propose",
                closed=True,
            )

        # Still have rounds — go back to QA with verdict reset.
        state.verdict = "PENDING"
        state.last_event_kind = "gate_fail"
        transition(state, "QA")
        save_state(state)
        return ReviewReply(
            text=(
                f"Final gate FAIL — Intent pillar 没通过。本轮 rounds={state.rounds}/{state.max_rounds}。\n"
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

    # v1.3.2 C3: deliver() can raise on disk full / Lark API outage /
    # filesystem perms. Pre-fix the exception bubbled up after state
    # was already saved CLOSED — junior got nothing, owner got
    # nothing, and PAID had to be debugged via SSH. Now we catch, mark
    # state.delivery_failed=True, and surface a degraded ReviewReply
    # the plugin glue can route to the owner alert path.
    sessions_root = _storage.PAID_DIR / "review" / "sessions"
    try:
        delivered = _deliver.deliver(
            sid_dir, sessions_root,
            subject=subject,
            junior_name=state.cp_id,
            junior_platform=state.platform,
            rounds=state.rounds, verdict=state.verdict,
            document=document,
            ingest_sources=state.ingest_sources,
            ingest_errors=state.ingest_errors,
        )
        brief_text = delivered["summary"]
    except Exception as exc:
        logger.warning(
            "_do_gate_and_close: deliver crashed sid=%s verdict=%s: %s",
            state.sid, state.verdict, exc,
        )
        state.delivery_failed = True
        try:
            save_state(state)
        except Exception:
            pass
        brief_text = (
            f"⚠️ Review session {state.sid} 关闭了 verdict={state.verdict}，"
            f"但 summary 落盘/归档失败：{exc}\n\n"
            f"原始材料 + annotations 已在 session 目录留存，需要人工 recover — "
            f"请联系 admin。"
        )

    return ReviewReply(
        text=brief_text,             # owner gets the brief (plugin glue routes)
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
        # v1.5: pass lark_client singleton so URL-driven backends can
        # fetch Lark Doc/Wiki content. Singleton fails gracefully when
        # FEISHU_APP_ID/SECRET aren't set — ingest still runs text-only.
        lark_client_obj = None
        try:
            from paid.lark_client import get_lark_client
            lark_client_obj = get_lark_client()
        except Exception as lc_exc:
            logger.warning(
                "[review] sid=%s no LarkClient (Lark URL ingest disabled): %s",
                sid, lc_exc,
            )

        result = _ingest_mod.ingest(
            initial_message,
            attachments or [],
            sid_dir,
            lark_client=lark_client_obj,
        )
        (sid_dir / "normalized.md").write_text(
            result.normalized_text or (initial_message or ""),
            encoding="utf-8",
        )
        if result.errors:
            logger.warning(
                "[review] sid=%s ingest had %d errors", sid, len(result.errors)
            )
        # v1.5: persist ingest audit to SessionState so build_summary
        # can render Sources footer + ⚠️ ingest_errors header in brief.
        try:
            st = load_state(sid)
            if st is not None:
                st.ingest_sources = list(result.sources or [])
                st.ingest_errors = list(result.errors or [])
                save_state(st)
        except Exception as exc:
            logger.warning(
                "[review] sid=%s persisting ingest audit failed: %s", sid, exc,
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


def add_attachments_to_session(
    sid: str, attachments: list[dict],
) -> dict:
    """v1.5.4 — append additional attachments to an active review session.

    Called from ``__init__.py::on_pre_gateway_dispatch`` when a cp with an
    active session sends a media-only inbound (Lark splits ``/review`` text
    and image into two events — the second event lands here).

    Behavior:
      - Re-runs ``paid_review.ingest.ingest`` on the new attachments only,
        with ``initial_message=""``. New normalized text is APPENDED to
        the session's ``normalized.md`` (with a ``---`` separator if there
        was existing content).
      - Extends ``state.ingest_sources`` and ``state.ingest_errors``.
      - Updates ``state.last_inbound_at`` so TTL pruning sees fresh activity.
      - Stamps ``state.updated_at``.

    Refuses (returns ok=False) when session is CLOSED or missing — caller
    can fall back to ``buffer.add()`` to await the next ``/review``.

    Returns dict::

        {
          "ok": True/False,
          "added_sources": int,   # new ingest_sources entries
          "added_errors": int,    # new ingest_errors entries
          "appended_chars": int,  # how much normalized.md grew
          "reason": str,          # set when ok=False
        }
    """
    if not attachments:
        return {"ok": False, "reason": "no attachments to add"}
    state = load_state(sid)
    if state is None:
        return {"ok": False, "reason": "session not found"}
    if state.stage == "CLOSED":
        return {"ok": False, "reason": "session already closed"}

    sid_dir = session_dir(sid)

    # Try to import the LarkClient singleton; tolerate absence (tests, no
    # FEISHU env). LarkDocBackend is the only attachment-backend that
    # needs it, and current callers only pass image/PDF paths so absence
    # is non-fatal.
    lark_client = None
    try:
        from paid.lark_client import get_lark_client
        lark_client = get_lark_client()
    except Exception:
        lark_client = None

    from paid_review import ingest as _ingest

    try:
        result = _ingest.ingest(
            initial_message="",
            attachments=attachments,
            sid_dir=sid_dir,
            lark_client=lark_client,
        )
    except Exception as exc:
        logger.warning("add_attachments_to_session: ingest crashed sid=%s: %s", sid, exc)
        return {"ok": False, "reason": f"ingest crashed: {exc}"}

    # Append to normalized.md
    norm = sid_dir / "normalized.md"
    appended_chars = 0
    if result.normalized_text and result.normalized_text.strip():
        existing = norm.read_text(encoding="utf-8") if norm.exists() else ""
        sep = "\n\n---\n\n" if existing.strip() else ""
        new_content = existing + sep + result.normalized_text
        norm.write_text(new_content, encoding="utf-8")
        appended_chars = len(new_content) - len(existing)

    # Extend state audit
    state.ingest_sources.extend(result.sources)
    state.ingest_errors.extend(result.errors)
    state.last_inbound_at = _now_iso()
    state.updated_at = _now_iso()
    save_state(state)

    return {
        "ok": True,
        "added_sources": len(result.sources),
        "added_errors": len(result.errors),
        "appended_chars": appended_chars,
    }
