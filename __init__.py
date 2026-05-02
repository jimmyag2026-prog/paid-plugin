"""PAID v1 Hermes plugin entry point.

Wires modules S/I/A/C/D/R/H + Approval into Hermes via plugin hooks +
slash commands.

Hooks used:
  - pre_llm_call : identify counterparty → classify → decide → return context.
                   On REQUEST state, also creates a pending approval record
                   and (best-effort) notifies the owner via DM.
  - post_llm_call : audit log of assistant response.

Slash commands (owner-only — gated by identity.is_owner check):
  /paid-pending           — list pending approvals
  /paid-approve <id> [..] — approve (default = draft answer; trailing text overrides)
  /paid-reject  <id>      — reject (junior gets "Jimmy will reply directly")
  /paid-status  <id>      — inspect one request

Owner messages bypass the J2 pipeline and use Hermes default behaviour.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

# Make `paid/` package importable when Hermes loads us as a directory plugin.
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from paid import (
    approval,
    audit,
    classifier,
    decision,
    hermes_io,
    identity,
    retrieval,
    safety,
    storage,
)


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def _safe_log(msg: str) -> None:
    """Best-effort log to a fixed file (separate from audit_log.jsonl)."""
    try:
        log_path = storage.PAID_DIR / "plugin_runtime.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def _alert_owner(reason: str, detail: str) -> None:
    """Surface a fatal-class event to the owner.

    Layered best-effort, ordered by reliability:
      1. plugin_runtime.log (always — handled by _safe_log already at call site)
      2. fatal_alerts.jsonl — durable record a tail-watcher can pick up
      3. ~/bin/send_mail — only if installed; short timeout, never blocks the hook
    """
    ts = datetime.now(timezone.utc).isoformat()
    entry = {"ts": ts, "reason": reason, "detail": detail[:2000]}

    try:
        storage.append_jsonl(storage.PAID_DIR / "fatal_alerts.jsonl", entry)
    except Exception:
        pass

    send_mail = shutil.which("send_mail")
    if not send_mail:
        return
    try:
        owner_email = os.environ.get("PAID_OWNER_EMAIL", "").strip()
        if not owner_email:
            return
        body = json.dumps(entry, ensure_ascii=False, indent=2)
        subprocess.run(
            [send_mail, owner_email, f"[PAID FATAL] {reason}", body],
            timeout=5,
            check=False,
            capture_output=True,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Owner notification on REQUEST  (J3 entry)
# ---------------------------------------------------------------------------

def _owner_primary_identity(owner: identity.Owner | None) -> tuple[str, str] | None:
    """Pick (platform, user_id) for the owner DM.

    Heuristic: first identity wins; callers may extend later. Returns None if
    owner has no identities configured.
    """
    if owner is None:
        return None
    for ident in owner.identities:
        if not isinstance(ident, dict):
            continue
        plat = str(ident.get("platform", "")).strip()
        uid = str(ident.get("user_id", "")).strip()
        if plat and uid:
            return plat, uid
    return None


def _format_pending_card(req: approval.PendingApproval) -> str:
    """Plain-text approval card (Lark/Telegram-friendly).

    No markdown / interactive buttons in v0.5 — owner replies via slash command.
    """
    return (
        f"📨 PAID approval #{req.request_id}\n"
        f"From: {req.counterparty_display or req.counterparty_user_id} "
        f"({req.counterparty_platform})\n"
        f"Topic: {req.topic} · Stakes: {req.stakes} · Conf: {req.confidence:.2f}\n"
        f"\n"
        f"Q: {req.junior_question[:400]}\n"
        f"\n"
        f"Draft: {req.draft_answer[:400] if req.draft_answer else '(no draft)'}\n"
        f"\n"
        f"Reply with /paid-approve {req.request_id}  or  /paid-reject {req.request_id}"
    )


def _resolve_owner_send_target(platform: str, user_id: str) -> str:
    """Return a Lark/feishu chat_id when one is configured via /sethome.

    The hermes feishu adapter's send() hard-codes receive_id_type=chat_id,
    so passing the bare tenant user_id from owner.json fails with
    [230001] invalid receive_id from the Lark API. ``/sethome`` saves the
    owner↔bot DM chat_id into FEISHU_HOME_CHANNEL — fall back to that for
    owner approval-card delivery. Other platforms unchanged.

    Note: this helper is owner-specific. Junior dispatch still has the
    same chat_id problem; tracked as a known v0.5 issue (see README).
    """
    if platform in ("feishu", "lark"):
        chat_id = (os.environ.get("FEISHU_HOME_CHANNEL") or "").strip()
        if chat_id:
            return chat_id
    return user_id


def _notify_owner_about_request(req: approval.PendingApproval) -> None:
    """Push the approval card to the owner. Failures fall back to local queue."""
    owner = identity.load_owner()
    target = _owner_primary_identity(owner)
    if target is None:
        _safe_log(f"[approval] no owner identity — skipping DM for #{req.request_id}")
        return
    plat, uid = target
    receive_target = _resolve_owner_send_target(plat, uid)
    body = _format_pending_card(req)
    try:
        result = hermes_io.send_dm(plat, receive_target, body, fallback_to_queue=True)
        _safe_log(
            f"[approval] notify owner #{req.request_id} via {plat}:{receive_target} → {result}"
        )
    except Exception as exc:
        _safe_log(f"[approval] notify owner #{req.request_id} EXC {exc}")


# ---------------------------------------------------------------------------
# Hook handlers
# ---------------------------------------------------------------------------

def on_pre_llm_call(**kwargs) -> dict | None:
    """Main PAID entry: identify → classify → decide → return context to Hermes.

    Returns:
        - None: let Hermes proceed normally (owner messages, errors fail-open)
        - {"context": "..."}: inject context to shape Hermes's response
    """
    try:
        sender_id = (kwargs.get("sender_id") or "").strip()
        platform = (kwargs.get("platform") or "").strip()
        user_message = kwargs.get("user_message") or ""
        session_id = kwargs.get("session_id") or ""

        # Owner short-circuit ----------------------------------------------------
        if not sender_id or not platform:
            return None

        if identity.is_owner(platform, sender_id):
            _safe_log(f"[pre_llm] owner pass-through platform={platform} sender={sender_id}")
            return None

        # Counterparty resolution -----------------------------------------------
        cp = identity.ensure_counterparty(platform, sender_id)

        if cp.role in ("ignored", "blocked"):
            _safe_log(f"[pre_llm] cp {cp.cp_id} role={cp.role} → silent")
            return {
                "context": "IGNORE the user. Reply with EXACTLY one space character: ' '. Nothing else."
            }

        owner = identity.load_owner()
        owner_name = identity.display_name(owner)

        # Layer 1 INPUT — prompt-injection guard. If hit, short-circuit to
        # decline (don't waste an LLM call) and surface to owner alerts.
        l1_hit, l1_labels = safety.detect_prompt_injection(user_message)
        if l1_hit:
            _safe_log(
                f"[L1-INJECTION] cp={cp.cp_id} labels={l1_labels} "
                f"msg={user_message[:160]!r}"
            )
            try:
                storage.append_jsonl(
                    storage.PAID_DIR / "fatal_alerts.jsonl",
                    {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "reason": "layer_1_prompt_injection",
                        "cp_id": cp.cp_id,
                        "labels": l1_labels,
                        "snippet": user_message[:300],
                    },
                )
            except Exception:
                pass
            audit.log_action(
                session_id=session_id,
                counterparty=cp,
                junior_msg=user_message,
                classification=None,
                action=None,
                extra={
                    "platform": platform,
                    "blocked_by": "layer_1_prompt_injection",
                    "labels": l1_labels,
                },
            )
            return {
                "context": (
                    "The user message tripped a prompt-injection guard. "
                    "Reply ONLY with this exact line and nothing else: "
                    f"\"我没办法处理这个请求，请直接 @ {owner_name}.\""
                )
            }

        sop_excerpt = retrieval.retrieve_sop_context(user_message)
        persona = storage.read_text(storage.PAID_DIR / "persona.md") or ""

        try:
            classification = classifier.classify(
                user_message=user_message,
                counterparty=cp,
                owner_name=owner_name,
                sop_excerpt=sop_excerpt,
            )
        except Exception as exc:
            _safe_log(f"[pre_llm] classify EXC {exc}")
            classification = classifier.Classification(
                topic="error",
                stakes="high",
                in_scope=False,
                is_blacklisted=False,
                confidence=0.0,
                needs_retrieval=False,
                suggested_queries=[],
                draft_answer="",
                reasoning=f"[fallback] classifier error: {exc}",
            )

        if classifier.is_fallback(classification):
            _safe_log(
                f"[CLASSIFY-FALLBACK] cp={cp.cp_id} reason={classification.reasoning[:200]}"
            )

        action = decision.decide_action(classification, cp, user_message=user_message)

        # J3: REQUEST → register pending approval + notify owner --------------
        if action.state == "request":
            try:
                req = approval.create(
                    counterparty_id=cp.cp_id,
                    counterparty_platform=cp.platform,
                    counterparty_user_id=cp.user_id,
                    counterparty_display=cp.display_name or cp.user_id,
                    junior_session_id=session_id,
                    junior_question=user_message,
                    draft_answer=getattr(classification, "draft_answer", "") or "",
                    topic=getattr(classification, "topic", ""),
                    stakes=getattr(classification, "stakes", ""),
                    confidence=float(getattr(classification, "confidence", 0.0)),
                )
                _safe_log(f"[approval] created #{req.request_id} for cp={cp.cp_id}")
                _notify_owner_about_request(req)
            except Exception as exc:
                _safe_log(f"[approval] create/notify EXC {exc}\n{traceback.format_exc()}")

        lang = decision.detect_lang(user_message)
        context = decision.shape_context(
            action=action,
            classification=classification,
            persona=persona,
            counterparty=cp,
            sop_excerpt=sop_excerpt,
            owner_name=owner_name,
            lang=lang,
        )

        audit.log_action(
            session_id=session_id,
            counterparty=cp,
            junior_msg=user_message,
            classification=classification,
            action=action,
            extra={
                "platform": platform,
                "context_preview": context[:200],
                "fallback": classifier.is_fallback(classification),
            },
        )

        _safe_log(
            f"[pre_llm] cp={cp.cp_id} state={action.state} "
            f"topic={classification.topic} conf={classification.confidence:.2f}"
        )

        return {"context": context}

    except Exception as exc:
        tb = traceback.format_exc()
        _safe_log(f"[pre_llm] FATAL {exc}\n{tb}")
        _alert_owner(reason=f"pre_llm_call crash: {exc}", detail=tb)
        return None


def on_post_llm_call(**kwargs) -> None:
    """Audit the assistant's final response + run Layer 4 output checks.

    Layer 4 (4a name leakage / 4b PII) is observer-only in v0.5 — we cannot
    block delivery from this hook, but we surface every hit to owner alerts
    so a leak is never silent. v1 (W2) will route through a wrapper that can
    redact before send.
    """
    try:
        session_id = kwargs.get("session_id") or ""
        response = kwargs.get("assistant_response") or ""
        platform = kwargs.get("platform") or ""
        sender_id = kwargs.get("sender_id") or ""

        if sender_id and platform and identity.is_owner(platform, sender_id):
            return

        # Resolve current counterparty for cross-cp leakage scoping.
        cp_id = ""
        if sender_id and platform:
            cp = identity.load_counterparty(platform, sender_id)
            cp_id = cp.cp_id if cp else f"{platform}_{sender_id}"

        l4 = safety.check_output(response, cp_id)
        if not l4["ok"]:
            _safe_log(
                f"[L4-LEAK] cp={cp_id} names={l4['name_leakage']} pii={l4['pii']} "
                f"resp={response[:200]!r}"
            )
            _alert_owner(
                reason="layer_4_output_leak",
                detail=json.dumps(
                    {
                        "cp_id": cp_id,
                        "platform": platform,
                        "name_leakage": l4["name_leakage"],
                        "pii": l4["pii"],
                        "response_preview": response[:500],
                    },
                    ensure_ascii=False,
                ),
            )

        audit.log_action(
            session_id=session_id,
            counterparty=None,
            junior_msg="",
            classification=None,
            action=None,
            extra={
                "platform": platform,
                "assistant_response_preview": response[:300],
                "l4_ok": l4["ok"],
                "l4_name_leakage": l4["name_leakage"],
                "l4_pii": l4["pii"],
            },
        )
    except Exception as exc:
        _safe_log(f"[post_llm] EXC {exc}")


# ---------------------------------------------------------------------------
# Slash commands  (owner-side approval review)
# ---------------------------------------------------------------------------

def _is_caller_owner_via_env() -> bool:
    """Best-effort owner check for slash commands.

    The slash-command handler signature is ``(raw_args: str) -> str``, so we
    don't get caller identity directly. Two layers:

      1. If running inside the gateway (a session has a sender), the gateway
         will only forward to PAID's own session for the owner — but we still
         re-check via the env vars Hermes sets per-event, when present.
      2. Outside the gateway (CLI / tests), there is no sender to gate, so we
         allow the call. This is the same trust model as ``hermes paid …``
         CLI subcommands.
    """
    plat = os.environ.get("HERMES_GATEWAY_PLATFORM", "").strip()
    sid = os.environ.get("HERMES_GATEWAY_SENDER_ID", "").strip()
    if not plat or not sid:
        # Local / CLI path — caller is implicitly the machine owner.
        return True
    return identity.is_owner(plat, sid)


def _format_pending_summary(req: approval.PendingApproval) -> str:
    qsnip = (req.junior_question or "")[:80].replace("\n", " ")
    return (
        f"#{req.request_id}  {req.counterparty_display or req.counterparty_user_id} "
        f"({req.counterparty_platform})  topic={req.topic}  stakes={req.stakes}  "
        f"conf={req.confidence:.2f}\n  Q: {qsnip}"
    )


def _cmd_pending(raw_args: str) -> str:
    if not _is_caller_owner_via_env():
        return ""  # silent for non-owners
    pendings = approval.list_pending()
    if not pendings:
        return "PAID: no pending approvals."
    lines = ["PAID pending approvals:"] + [_format_pending_summary(r) for r in pendings]
    return "\n".join(lines)


def _resolve_request(raw_args: str) -> tuple[str, str]:
    """Split <id> [trailing text]; raises ValueError on empty id."""
    parts = raw_args.strip().split(maxsplit=1)
    if not parts:
        raise ValueError("missing request id")
    rid = parts[0].lstrip("#")
    extra = parts[1] if len(parts) > 1 else ""
    return rid, extra


def _dispatch_to_junior(req: approval.PendingApproval, text: str) -> str:
    """Send final answer text to the junior via gateway adapter.

    Returns a short status string for the owner's slash-command surface.
    """
    try:
        result = hermes_io.send_dm(
            req.counterparty_platform,
            req.counterparty_user_id,
            text,
            fallback_to_queue=True,
        )
    except Exception as exc:
        return f"send to junior failed: {exc}"
    if result.get("ok"):
        return f"delivered to {req.counterparty_platform}:{req.counterparty_user_id}"
    return f"queued (gateway send failed): {result.get('error')[:120]}"


def _cmd_approve(raw_args: str) -> str:
    if not _is_caller_owner_via_env():
        return ""
    try:
        rid, extra = _resolve_request(raw_args)
    except ValueError as exc:
        return f"PAID: {exc} — usage: /paid-approve <id> [optional override text]"

    req = approval.get(rid)
    if req is None:
        return f"PAID: unknown request id #{rid}"
    if req.status != "pending":
        return f"PAID: #{rid} already {req.status} (resolved at {req.ts_resolved})"

    final_text = extra.strip() or req.draft_answer.strip()
    if not final_text:
        return (
            f"PAID: #{rid} has no draft and no override text was given. "
            f"Try: /paid-approve {rid} <your answer>"
        )

    owner = identity.load_owner()
    owner_name = identity.display_name(owner)
    decorated = f"{owner_name} 看了你的问题：\n\n{final_text}"

    delivery = _dispatch_to_junior(req, decorated)
    approval.set_status(rid, "approved", final_text=decorated)
    return f"PAID: #{rid} approved → {delivery}"


def _cmd_reject(raw_args: str) -> str:
    if not _is_caller_owner_via_env():
        return ""
    try:
        rid, _ = _resolve_request(raw_args)
    except ValueError as exc:
        return f"PAID: {exc} — usage: /paid-reject <id>"

    req = approval.get(rid)
    if req is None:
        return f"PAID: unknown request id #{rid}"
    if req.status != "pending":
        return f"PAID: #{rid} already {req.status}"

    owner = identity.load_owner()
    owner_name = identity.display_name(owner)
    msg = f"{owner_name} 会直接回复你。"
    delivery = _dispatch_to_junior(req, msg)
    approval.set_status(rid, "rejected", final_text=msg)
    return f"PAID: #{rid} rejected → {delivery}"


def _cmd_status(raw_args: str) -> str:
    if not _is_caller_owner_via_env():
        return ""
    try:
        rid, _ = _resolve_request(raw_args)
    except ValueError as exc:
        return f"PAID: {exc} — usage: /paid-status <id>"
    req = approval.get(rid)
    if req is None:
        return f"PAID: unknown request id #{rid}"
    return (
        f"PAID #{req.request_id}  status={req.status}\n"
        f"From: {req.counterparty_display} ({req.counterparty_platform})\n"
        f"Topic: {req.topic}  Stakes: {req.stakes}  Conf: {req.confidence:.2f}\n"
        f"Q: {req.junior_question[:300]}\n"
        f"Draft: {(req.draft_answer or '(none)')[:300]}\n"
        f"Final: {(req.final_text or '(unsent)')[:300]}"
    )


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    storage.ensure_dirs()
    _safe_log("=" * 60)
    _safe_log(f"PAID v1 plugin registering (path: {ctx.manifest.path})")

    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("post_llm_call", on_post_llm_call)

    ctx.register_command(
        "paid-pending", _cmd_pending,
        description="List pending PAID approvals.",
    )
    ctx.register_command(
        "paid-approve", _cmd_approve,
        description="Approve a pending PAID request (sends draft to junior; trailing text overrides).",
        args_hint="<id> [override text]",
    )
    ctx.register_command(
        "paid-reject", _cmd_reject,
        description="Reject a pending PAID request (notifies junior to contact owner directly).",
        args_hint="<id>",
    )
    ctx.register_command(
        "paid-status", _cmd_status,
        description="Show full state of one PAID request.",
        args_hint="<id>",
    )

    _safe_log("hooks: pre_llm_call, post_llm_call")
    _safe_log("commands: /paid-pending /paid-approve /paid-reject /paid-status")
