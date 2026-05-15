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

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
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
    card_formatters,
    card_spec,
    classifier,
    decision,
    group_routing,
    hermes_io,
    identity,
    retrieval,
    safety,
    storage,
)
from paid import setup_wizard as _setup_wizard
from paid import conv_capture as _conv_capture


# ---------------------------------------------------------------------------
# In-process session metadata cache.
#
# hermes' post_llm_call hook does NOT receive sender_id / platform in its
# kwargs (verified against hermes-agent v0.12.0 — only pre_llm_call carries
# them). Without a way to identify the sender at post-hook time, we can't
# (a) early-return for owner messages so we don't audit-log their replies,
# or (b) scope Layer 4 cross-cp checks to the right counterparty.
#
# Workaround: at pre_llm_call we cache (session_id → {platform, sender_id,
# cp_id}); post_llm_call resolves by session_id. The cache is in-memory only;
# losing it across restarts is acceptable — the worst outcome is one extra
# audit row for an in-flight message.
#
# Bounded LRU keeps memory in check on long-running gateways.
# ---------------------------------------------------------------------------

from collections import OrderedDict

_SESSION_META_CACHE: OrderedDict[str, dict[str, str]] = OrderedDict()
_SESSION_META_CACHE_MAX = 256


def _cache_session_meta(session_id: str, platform: str, sender_id: str, cp_id: str) -> None:
    if not session_id:
        return
    _SESSION_META_CACHE[session_id] = {
        "platform": platform or "",
        "sender_id": sender_id or "",
        "cp_id": cp_id or "",
    }
    _SESSION_META_CACHE.move_to_end(session_id)
    while len(_SESSION_META_CACHE) > _SESSION_META_CACHE_MAX:
        _SESSION_META_CACHE.popitem(last=False)


def _lookup_session_meta(session_id: str) -> dict[str, str]:
    if not session_id:
        return {}
    return _SESSION_META_CACHE.get(session_id, {})


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


# Per-process alert debounce — keyed on (channel, reason) so a fatal that
# repeats every inbound (e.g. busted hermes config) doesn't spam the owner's
# IM. fatal_alerts.jsonl is NOT debounced — every entry lands for forensics.
_ALERT_IM_DEBOUNCE_SECONDS = 600   # 10 min between IM alerts for same reason
_ALERT_MAIL_DEBOUNCE_SECONDS = 1800  # 30 min between mails for same reason
_ALERT_LAST_SENT: dict[tuple[str, str], float] = {}


def _alert_recently_sent(channel: str, reason: str) -> bool:
    import time as _time
    key = (channel, reason)
    last = _ALERT_LAST_SENT.get(key, 0.0)
    window = (
        _ALERT_IM_DEBOUNCE_SECONDS if channel == "im"
        else _ALERT_MAIL_DEBOUNCE_SECONDS
    )
    return (_time.time() - last) < window


def _mark_alert_sent(channel: str, reason: str) -> None:
    import time as _time
    _ALERT_LAST_SENT[(channel, reason)] = _time.time()


def _alert_owner(reason: str, detail: str) -> None:
    """Surface a fatal-class event to the owner.

    Layered best-effort, ordered by reliability + reach:
      1. plugin_runtime.log   (always — handled by _safe_log already at call site)
      2. fatal_alerts.jsonl   (durable record a tail-watcher can pick up)
      3. IM DM via send_dm    (fastest channel — owner sees it on phone within seconds;
                               falls back to outbound_queue.jsonl on adapter failure)
      4. ~/bin/send_mail      (slower but survives hermes/IM outages — only if installed)

    Every layer wrapped in try/except: alert path MUST NOT raise into the hook
    that called us, otherwise a transient exception while reporting an exception
    creates an unrecoverable loop. The mail / IM steps each have short timeouts.

    To avoid an alert storm if the same fatal repeats every inbound (e.g. a
    busted hermes config), we de-bounce IM-channel alerts to one per
    ``_ALERT_IM_DEBOUNCE_SECONDS`` window keyed on (reason); fatal_alerts.jsonl
    still gets every entry for forensics. send_mail also de-bounces — same key,
    same window.
    """
    ts = datetime.now(timezone.utc).isoformat()
    entry = {"ts": ts, "reason": reason, "detail": detail[:2000]}

    # Layer 2 — durable JSONL.
    try:
        storage.append_jsonl(storage.PAID_DIR / "fatal_alerts.jsonl", entry)
    except Exception:
        pass

    # Layer 3 — IM DM. Best-effort; debounced to avoid spamming the owner if
    # the same fatal repeats. send_dm itself falls back to outbound_queue on
    # adapter failure so even when the gateway is down, a record reaches disk.
    #
    # Identity selection: use owner.preferred_identity() (Owner v2) so the
    # alert lands on the platform the owner actually watches. Falls back to
    # legacy _owner_primary_identity for owners whose v2 profile somehow has
    # no enabled identities (defensive — shouldn't happen in practice).
    try:
        if not _alert_recently_sent("im", reason):
            owner = identity.load_owner()
            pref = owner.preferred_identity() if owner else None
            if pref is not None:
                plat, uid = pref.platform, pref.home_chat_id
            else:
                target = _owner_primary_identity(owner)
                if target is None:
                    raise RuntimeError("no owner identity")
                plat, uid = target
            # For Lark/feishu, resolve to a directly-routable identity
            # (ou_/oc_/email) when owner.json has one. Falls back to
            # FEISHU_HOME_CHANNEL only when no routable identity exists.
            # Single source of truth — _notify_owner_about_request and
            # sweep_pending.py use the same helper.
            if plat in ("feishu", "lark"):
                uid = identity.resolve_owner_lark_target(uid)
            short_detail = (detail or "").strip().splitlines()[0][:300]
            body = (
                f"⚠️ PAID fatal alert\n"
                f"reason: {reason}\n"
                f"ts: {ts}\n"
                f"detail: {short_detail}\n"
                f"(ask your operator for the full trace if needed)"
            )
            hermes_io.send_dm(plat, uid, body, fallback_to_queue=True)
            _mark_alert_sent("im", reason)
    except Exception:
        pass

    # Layer 4 — send_mail (slower but survives hermes outage).
    send_mail = shutil.which("send_mail")
    if not send_mail:
        return
    try:
        if _alert_recently_sent("mail", reason):
            return
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
        _mark_alert_sent("mail", reason)
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


# NOTE: _confidence_badge / _stakes_badge moved to paid/card_spec.py
# (single source of truth for visual hints across formatters).
# _format_lark_approval_card moved to paid/card_formatters.format_lark.
# _format_pending_card moved to paid/card_formatters.format_plain.


def _resolve_owner_send_target(platform: str, user_id: str) -> str:
    """Pick the right receive_id for owner DM/card.

    Lark/feishu: delegates to ``identity.resolve_owner_lark_target`` so
    a routable ``ou_``/``oc_``/email identity in owner.json wins over
    the ``FEISHU_HOME_CHANNEL`` env var. Other platforms: unchanged.

    Pre-v1.2.4 this unconditionally returned FEISHU_HOME_CHANNEL for
    Lark, which silently misrouted J3 cards when /sethome had been run
    from the wrong chat. The new helper still falls back to the env
    var when owner.json has only bare-hex (non-routable) identities,
    preserving the legacy /sethome path for backward compat.
    """
    if platform in ("feishu", "lark"):
        return identity.resolve_owner_lark_target(user_id)
    return user_id


def _notify_owner_about_request(req: approval.PendingApproval) -> None:
    """Push the approval card to the owner. Failures fall back to local queue.

    Dispatches by the owner's preferred platform identity (Owner v2):
      - feishu / lark → send_lark_card with interactive card JSON
      - telegram      → send_telegram_card with InlineKeyboardMarkup
      - slack         → send_slack_block with Block Kit blocks
      - any other     → plain-text card via send_dm

    Card path failure (e.g. no live gateway, adapter not connected) falls
    through to the plain-text path so the recipient still sees the body
    even when the rich UI fails.
    """
    owner = identity.load_owner()
    pref = owner.preferred_identity() if owner else None
    if pref is None:
        # Backward compat: try legacy primary-identity helper for v1
        # owner.json files where preferred_identity returns None due to
        # all-disabled (shouldn't happen in practice but defensive).
        target = _owner_primary_identity(owner)
        if target is None:
            _safe_log(f"[approval] no owner identity — skipping DM for #{req.request_id}")
            return
        plat, uid = target
    else:
        plat = pref.platform
        uid = pref.home_chat_id
    # FEISHU_HOME_CHANNEL env still wins for Lark (operator-set runtime
    # override that pre-dates the schema-v2 home_chat_id field).
    receive_target = _resolve_owner_send_target(plat, uid)

    # Build platform-agnostic spec; each platform formatter consumes the same.
    try:
        from paid import settings as _settings
        timeout_min = max(1, int(_settings.approval_timeout_seconds() / 60))
    except Exception:
        timeout_min = 30
    spec = card_spec.ApprovalCardSpec.from_pending_approval(
        req, timeout_min=timeout_min,
    )

    # Platform-specific rich card path. On any failure, fall through to the
    # plain-text path below so the owner still gets the card body.
    if plat in ("feishu", "lark"):
        try:
            card = card_formatters.format_lark(spec)
            result = hermes_io.send_lark_card(
                plat, receive_target, card, fallback_to_queue=True
            )
            _safe_log(
                f"[approval] notify owner #{req.request_id} via {plat}:{receive_target} "
                f"(interactive card) → {result}"
            )
            if result.get("ok"):
                return
            _safe_log(
                f"[approval] interactive card failed, falling back to text for "
                f"#{req.request_id}"
            )
        except Exception as exc:
            _safe_log(
                f"[approval] interactive card EXC #{req.request_id}: {exc} "
                f"— falling back to text"
            )
    elif plat == "telegram":
        try:
            payload = card_formatters.format_telegram(spec)
            keyboard = (payload.get("reply_markup") or {}).get("inline_keyboard")
            result = hermes_io.send_telegram_card(
                receive_target,
                payload["text"],
                keyboard=keyboard,
                parse_mode=payload.get("parse_mode", "Markdown"),
                fallback_to_queue=True,
            )
            _safe_log(
                f"[approval] notify owner #{req.request_id} via tg:{receive_target} "
                f"(inline kbd) → {result}"
            )
            if result.get("ok"):
                return
            _safe_log(
                f"[approval] TG inline-kbd card failed, falling back to text for "
                f"#{req.request_id}"
            )
        except Exception as exc:
            _safe_log(
                f"[approval] TG card EXC #{req.request_id}: {exc} "
                f"— falling back to text"
            )
    elif plat == "slack":
        try:
            payload = card_formatters.format_slack(spec)
            result = hermes_io.send_slack_block(
                receive_target,
                payload["blocks"],
                fallback_text=payload.get("text", ""),
                fallback_to_queue=True,
            )
            _safe_log(
                f"[approval] notify owner #{req.request_id} via slack:{receive_target} "
                f"(block kit) → {result}"
            )
            if result.get("ok"):
                return
            _safe_log(
                f"[approval] Slack block-kit failed, falling back to text for "
                f"#{req.request_id}"
            )
        except Exception as exc:
            _safe_log(
                f"[approval] Slack card EXC #{req.request_id}: {exc} "
                f"— falling back to text"
            )

    # Text fallback (other platforms or rich-card failure).
    body = card_formatters.format_plain(spec)
    try:
        result = hermes_io.send_dm(plat, receive_target, body, fallback_to_queue=True)
        _safe_log(
            f"[approval] notify owner #{req.request_id} via {plat}:{receive_target} (text) → {result}"
        )
    except Exception as exc:
        _safe_log(f"[approval] notify owner #{req.request_id} EXC {exc}")


# ---------------------------------------------------------------------------
# Discovery card — first inbound message from unknown sender (J4 entry)
# ---------------------------------------------------------------------------

def _format_discovery_card(
    cp: identity.Counterparty,
    user_message: str,
    impersonates: identity.Counterparty | None,
) -> str:
    impersonation_note = ""
    if impersonates is not None:
        impersonation_note = (
            f"\n⚠️ Possible impersonation: a counterparty named "
            f"'{impersonates.display_name}' already exists on "
            f"{impersonates.platform} (cp_id={impersonates.cp_id}). "
            f"Verify before granting trust."
        )
    return (
        f"👋 PAID discovery — first contact from unknown sender\n"
        f"  cp_id:   {cp.cp_id}\n"
        f"  display: {cp.display_name or '(none)'}\n"
        f"  platform: {cp.platform}  user_id: {cp.user_id}\n"
        f"  first message: {user_message[:300]!r}\n"
        f"{impersonation_note}\n"
        f"\n"
        f"Three ways to respond on the host:\n"
        f"  1️⃣ TRUST  — let PAID auto-answer their topic\n"
        f"     python3 -m paid add-counterparty {cp.platform} {cp.user_id} "
        f"--name '<display>' --role junior --topic-allow <topic>\n"
        f"  2️⃣ ASK    — bounce a clarifying question to them before deciding\n"
        f"     python3 -m paid ask-counterparty {cp.platform} {cp.user_id} "
        f"'<your question>'\n"
        f"  3️⃣ IGNORE — silent reply, no escalations\n"
        f"     python3 -m paid ignore-counterparty {cp.platform} {cp.user_id} "
        f"--reason '<why>'"
    )


def _notify_owner_about_unknown_sender(
    cp: identity.Counterparty, user_message: str
) -> None:
    """Push a discovery DM to the owner when *cp* is a freshly-created pending
    role and we haven't notified yet. Idempotent — sets discovery_notified_at
    on success so repeated inbounds from the same unknown sender don't spam.
    """
    if cp.discovery_notified_at:
        return  # already pinged owner about this one

    owner = identity.load_owner()
    target = _owner_primary_identity(owner)
    if target is None:
        _safe_log(f"[discovery] no owner identity — skipping for {cp.cp_id}")
        return
    plat, uid = target
    receive_target = _resolve_owner_send_target(plat, uid)

    impersonates = identity.detect_impersonation(cp)
    body = _format_discovery_card(cp, user_message, impersonates)

    try:
        result = hermes_io.send_dm(plat, receive_target, body, fallback_to_queue=True)
        _safe_log(
            f"[discovery] notify owner about {cp.cp_id} via {plat}:{receive_target} → "
            f"{result}"
            + (f" [IMPERSONATION_FLAG cp={impersonates.cp_id}]" if impersonates else "")
        )
    except Exception as exc:
        _safe_log(f"[discovery] notify EXC for {cp.cp_id}: {exc}")
        return

    # Mark as notified regardless of delivery success — failed sends already
    # land in outbound_queue.jsonl, no need to retry on every subsequent
    # message from the same unknown sender.
    cp.discovery_notified_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    identity.save_counterparty(cp)


# ---------------------------------------------------------------------------
# Hook handlers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Sprint D — review skill routing (spec §6 plugin glue contract)
# ---------------------------------------------------------------------------

# v0.1 only the explicit /review trigger opens a session — classifier's
# needs_review path is intentionally dead code (M1.7 evaluates flipping
# this on after pilot). spec §2 + design/05_backlog.md M1 §2.

def _wrap_reply_for_hermes(text: str) -> dict:
    """ReviewReply.text → hermes context override that forces an exact reply.

    v1.4.4 (backlog v1.4.2): now delegates to the canonical wrap helper
    in ``paid.decision.wrap_exact_reply`` so we don't keep two parallel
    formats. Same external behavior as pre-v1.4.4 Format B.
    """
    from paid.decision import wrap_exact_reply
    return {"context": wrap_exact_reply(text)}


def _maybe_route_to_review_skill(cp, user_message: str,
                                 hook_kwargs: dict) -> dict | None:
    """Route junior inbound to paid_review skill if applicable.

    Returns:
      - None: not for review skill; caller (on_pre_llm_call) falls through to J2
      - {"context": "..."}: skill consumed the message; hermes should reply
        exactly with the skill's text (no LLM thinking on top)

    Routing rules (spec §6 entry table):
      1. /review cancel             → force_close any active session
      2. /review or /r <subject>    → intake (refused if active exists)
      3. cp.active_review_session   → handle_inbound (regular QA tick)
      4. else                       → None (J2 path)
    """
    text = (user_message or "").strip()
    text_lower = text.lower()

    has_active = bool(getattr(cp, "active_review_session", "") or "")
    starts_just_r = text_lower in ("/r", "/review")  # bare /r needs a subject
    is_review_cmd = (
        text_lower.startswith("/review")
        or text_lower.startswith("/r ")
        or starts_just_r
    )

    # v1.5.3 fix #5: accept common cancel synonyms — users typed close/stop/
    # abort/end in live test and got new sessions opened with those as
    # content. Also accept the "/r" short variants for consistency.
    _CANCEL_VARIANTS = {
        "/review cancel", "/review close", "/review stop",
        "/review abort", "/review end", "/review exit", "/review quit",
        "/r cancel", "/r close", "/r stop",
        "/r abort", "/r end", "/r exit", "/r quit",
    }

    # v1.5.3: detect lang from inbound for cp-facing reply strings.
    try:
        from paid_review.i18n import detect_lang as _detect_lang
        from paid_review.i18n import t as _t
        _lang = _detect_lang(text)
    except Exception:
        _detect_lang = None
        _t = None
        _lang = "zh"

    # ---- Path 1: /review cancel (+ synonyms) ----
    if text_lower.strip() in _CANCEL_VARIANTS:
        if not has_active:
            return _wrap_reply_for_hermes(
                _t("cancel_no_active", _lang) if _t else
                "你目前没有进行中的 review session 可以取消。发 /review <subject> 开新一轮。"
            )
        try:
            from paid_review import api as _review_api
            msg = _review_api.force_close(
                cp.active_review_session, reason="junior_cancel",
            )
        except Exception as exc:
            _safe_log(f"[review] cancel EXC sid={cp.active_review_session}: {exc}")
            msg = f"取消失败: {exc}"
        try:
            identity.clear_active_review_session(cp, archive={
                "sid": cp.active_review_session,
                "closed_reason": "junior_cancel",
            })
        except Exception:
            pass
        return _wrap_reply_for_hermes(
            _t("cancel_done", _lang, detail=msg) if _t else
            f"已关闭，发 /review <subject> 开新一轮。\n\n{msg}"
        )

    # ---- Path 2: /review <subject> or /r <subject> ----
    if is_review_cmd and text_lower.strip() not in _CANCEL_VARIANTS:
        if has_active:
            return _wrap_reply_for_hermes(
                _t("review_already_active", _lang, sid=cp.active_review_session) if _t else
                f"你已有进行中的 review session: {cp.active_review_session}。先 /review cancel 再开新的。"
            )
        # Strip command prefix (v1.5.3 also tolerates CJK directly after,
        # e.g. `/review看一下` → "看一下")
        if text_lower.startswith("/review"):
            initial = text[len("/review"):].lstrip(" :：")
        else:
            initial = text[len("/r"):].lstrip(" :：")
        if not initial and starts_just_r:
            return _wrap_reply_for_hermes(
                _t("review_need_subject", _lang) if _t else
                "请告诉我要 review 的 subject。例: /review Q3 OKR 草稿"
            )
        # v1.5.4: drain any media buffered for this cp (sent before
        # /review). Combine with whatever attachments hermes already
        # gave us in hook_kwargs.
        explicit_attachments = list(hook_kwargs.get("attachments", []) or [])
        buffered_attachments: list[dict] = []
        try:
            from paid_review import attachment_buffer as _buf
            buffered_attachments = _buf.drain(cp.platform, cp.user_id)
            if buffered_attachments:
                _safe_log(
                    f"[review attach] drained {len(buffered_attachments)} "
                    f"buffered media for cp={cp.cp_id} at /review intake"
                )
        except Exception as exc:
            _safe_log(f"[review attach] drain EXC cp={cp.cp_id}: {exc}")
        all_attachments = explicit_attachments + buffered_attachments

        # Run intake
        try:
            from paid_review import api as _review_api
            sid = _review_api.intake(
                cp=cp, initial_message=initial or text,
                attachments=all_attachments,
            )
            identity.set_active_review_session(cp, sid)
        except Exception as exc:
            _safe_log(f"[review] intake EXC cp={cp.cp_id}: {exc}")
            return _wrap_reply_for_hermes(
                f"开 review session 失败: {exc}。请重试或换个 subject。"
            )
        # Drive the state machine ONE step (intake side-effect already
        # wrote SUBJECT stage; handle_inbound with empty text returns the
        # subject_ask reply). intake() in api.py already does the SUBJECT
        # transition + saves subject_candidates, so we synthesize the
        # subject_ask reply by calling handle_inbound with the empty
        # trigger placeholder.
        try:
            reply = _review_api.handle_inbound(sid, "", hook_kwargs)
        except Exception as exc:
            _safe_log(f"[review] post-intake handle_inbound EXC sid={sid}: {exc}")
            return _wrap_reply_for_hermes(
                f"已创建 session {sid} 但首条响应失败: {exc}"
            )
        # Defensive: if intake-triggered first step already closes (e.g.
        # SCAN no_findings short-circuit), route the brief to owner not
        # junior — same split as the QA-driven close path below.
        if reply.closed:
            junior_close_text = _dispatch_review_close_to_owner(cp, reply)
            return _wrap_reply_for_hermes(junior_close_text)
        return _wrap_reply_for_hermes(reply.text)

    # ---- Path 3: active session, non-/review message ----
    if has_active:
        try:
            from paid_review import api as _review_api
            reply = _review_api.handle_inbound(
                cp.active_review_session, user_message, hook_kwargs,
            )
        except Exception as exc:
            _safe_log(
                f"[review] handle_inbound EXC sid={cp.active_review_session}: {exc}"
            )
            return _wrap_reply_for_hermes(
                "review skill 暂时出错，可以发 /review cancel 退出再试。"
            )

        # spec §6 plugin职责 #5: closed=True → clear active_session +
        # dispatch the 6-section brief to OWNER (the brief is the owner's
        # deliverable; junior must NOT see the internal findings/verdict).
        # SCAN progress: do NOT clear; session continues — just relay text.
        if reply.closed:
            junior_close_text = _dispatch_review_close_to_owner(cp, reply)
            return _wrap_reply_for_hermes(junior_close_text)
        return _wrap_reply_for_hermes(reply.text)

    # ---- Path 4: not a review-skill message ----
    return None


def _dispatch_review_close_to_owner(cp, reply) -> str:
    """On review session close: push the 6-section brief to owner via DM,
    clear the cp's active_review_session, and return a short confirmation
    string for the junior.

    The brief contains internal findings + verdict + recommendations the
    junior must not see — that's why we split the channels here rather
    than just returning reply.text (which would send the brief to the
    junior, the original /review caller).

    All steps are best-effort: owner send failure falls back to
    outbound_queue.jsonl + fatal_alerts.jsonl (via existing _alert_owner
    debounce) so the brief is recoverable even when IM is down. The
    session is always closed on disk regardless of delivery outcome,
    so a fresh /review can be opened.
    """
    sid = cp.active_review_session
    brief = reply.text or ""

    # 1) Clear active session bookkeeping first — even if owner-send
    #    fails, the cp must be able to open a new /review.
    try:
        identity.clear_active_review_session(cp, archive={
            "sid": sid,
            "stage": reply.stage,
            "event_kind": reply.event_kind,
        })
    except Exception as exc:
        _safe_log(f"[review] clear_active EXC sid={sid}: {exc}")

    # 2) Resolve owner identity. No owner = silent log + ask junior to
    #    follow up directly (degraded but not crashed).
    owner = identity.load_owner()
    pref = owner.preferred_identity() if owner else None
    if pref is None:
        # Log retains server path for operator forensics; user-facing
        # return text only references the sid.
        _safe_log(
            f"[review] close sid={sid}: no owner identity — brief stayed on "
            f"disk only (~/.hermes/paid/review/sessions/_closed/.../{sid}/)"
        )
        return (
            "Review 已完成，但 owner 端没收到通知（系统未配置 owner 身份）。"
            f"请直接 ping owner 让他来取 summary (sid: {sid})。— PAID"
        )

    plat = pref.platform
    uid = pref.home_chat_id
    if plat in ("feishu", "lark"):
        uid = identity.resolve_owner_lark_target(uid)

    # 3) Send brief to owner. Long briefs may need chunking on some
    #    platforms (Lark text DM ~30k char ceiling; TG ~4k). Naive
    #    single-send is fine for v0.1 (briefs are typically <2k); add
    #    chunking later if we hit a real ceiling.
    cp_name = getattr(cp, "display_name", "") or cp.cp_id
    brief_with_header = (
        f"📋 Quality Audit complete — sid `{sid}`\n"
        f"From: {cp_name} ({cp.platform})\n"
        f"Verdict: {reply.stage} / {getattr(reply, 'event_kind', '')}\n"
        f"\n{brief}\n"
        f"\n_Session ID: {sid} (full archive available from your operator)_"
        f"\n_Note: v1 ships without MERGE — junior did NOT revise the doc. "
        f"Audit is on the original draft._"
    )
    try:
        result = hermes_io.send_dm(plat, uid, brief_with_header, fallback_to_queue=True)
        _safe_log(
            f"[review] close sid={sid}: brief → {plat}:{uid} → ok={result.get('ok')}"
        )
    except Exception as exc:
        _safe_log(f"[review] close sid={sid}: send brief EXC: {exc}")
        try:
            _alert_owner(
                "review_brief_send_failed",
                f"sid={sid} cp={cp.cp_id} target={plat}:{uid}: {exc}"
            )
        except Exception:
            pass

    # 4) Short close message for junior — must NOT leak brief contents.
    return (
        "Review session 已完成 ✅ Summary 已发给 owner，他会跟你直接对接后续。"
        "谢谢配合 — PAID"
    )


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

        # Defense in depth: pre_gateway_dispatch may not have fired (older
        # hermes / unusual route). Try again from here on TG events.
        if platform == "telegram":
            _ensure_telegram_callback_registered()

        if identity.is_owner(platform, sender_id):
            _safe_log(f"[pre_llm] owner pass-through platform={platform} sender={sender_id}")
            return None

        # Counterparty resolution -----------------------------------------------
        cp = identity.ensure_counterparty(platform, sender_id)

        # Cache (session_id → metadata) so post_llm_call can resolve sender
        # identity even though hermes drops platform/sender_id from its kwargs.
        _cache_session_meta(session_id, platform, sender_id, cp.cp_id)

        # First inbound from an unknown sender → notify owner exactly once.
        # role="pending" + no discovery_notified_at = fresh; once notified we
        # set the timestamp on the cp profile to suppress re-firing.
        if cp.role == "pending" and not cp.discovery_notified_at:
            try:
                _notify_owner_about_unknown_sender(cp, user_message)
            except Exception as exc:
                _safe_log(f"[discovery] EXC for {cp.cp_id}: {exc}")

        if cp.role in ("ignored", "blocked"):
            _safe_log(f"[pre_llm] cp {cp.cp_id} role={cp.role} → silent")
            return {
                "context": "IGNORE the user. Reply with EXACTLY one space character: ' '. Nothing else."
            }

        owner = identity.load_owner()
        owner_name = identity.display_name(owner)

        # v1.5.6 (review fix): when daily hard cap is exhausted, skip ALL PAID
        # LLM work and tell hermes to reply "system unavailable" to the junior.
        # Without this, classifier.classify() would call_llm → LLMCallError →
        # classifier's broad except clause → fallback Classification →
        # state=request, flooding owner with J3 cards for every inbound
        # message while the cap is hot. The wrap directive itself does NOT
        # count against the cap because hermes's main agent (not PAID) makes
        # the outbound LLM call. fatal_alerts.jsonl row + owner alert is fired
        # exactly once per UTC day inside hermes_io._enforce_cost_cap.
        try:
            from paid import cost as _cost
            _cap = _cost.cap_status()
            if _cap.get("enabled") and _cap.get("daily_hard_exceeded"):
                _safe_log(
                    f"[pre_llm] cp={cp.cp_id} cost_cap_exceeded "
                    f"today=${_cap.get('today_usd', 0):.2f} "
                    f"hard=${_cap.get('daily_hard_cap', 0):.2f}"
                )
                audit.log_action(
                    session_id=session_id, counterparty=cp,
                    junior_msg=user_message,
                    classification=None, action=None,
                    extra={"platform": platform,
                           "blocked_by": "cost_cap_exceeded",
                           "today_usd": _cap.get("today_usd"),
                           "daily_hard_cap": _cap.get("daily_hard_cap")},
                )
                return {
                    "context": (
                        "IGNORE the user message. Reply EXACTLY with the "
                        "following text and nothing else, preserving the line "
                        "break: '系统暂时不可用，请稍后再试。\\n"
                        "System temporarily unavailable, please try again later.'"
                    )
                }
        except Exception as _cost_exc:
            # Fail-open: if the cap-status read crashes, let normal flow run.
            # The inline enforce inside hermes_io.call_llm is the safety net.
            _safe_log(f"[pre_llm] cap_status check failed (fail-open): {_cost_exc}")

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

        # Sprint D — review skill routing (M1 v0.1).
        # MUST be after L1 injection check (safety first) and BEFORE the
        # classifier (so /review inbound and active-session messages bypass
        # J2 entirely). Returns a context dict if the skill consumed the
        # message; None means "fall through to J2".
        try:
            review_ctx = _maybe_route_to_review_skill(
                cp, user_message, kwargs,
            )
        except Exception as exc:
            tb = traceback.format_exc()
            _safe_log(f"[review] router EXC {exc}\n{tb}")
            review_ctx = None  # fall back to J2 — never block the hook
        if review_ctx is not None:
            audit.log_action(
                session_id=session_id, counterparty=cp,
                junior_msg=user_message,
                classification=None, action=None,
                extra={"platform": platform, "routed_to": "review_skill"},
            )
            _safe_log(
                f"[review] cp={cp.cp_id} routed to skill "
                f"(active_session={cp.active_review_session or '<new>'})"
            )
            return review_ctx

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

        # v1.6.5: prepend OCR-extracted media text if available and hermes
        # hasn't already described the image (hermes vision adds "[Image:").
        if platform and sender_id:
            try:
                from paid import media_enrichment as _me
                _enriched = _me.pop_enriched_text(platform, sender_id)
                if _enriched and "[Image:" not in (user_message or "")[:120]:
                    context = (
                        f"[Extracted text from CP-sent media via OCR]\n"
                        f"{_enriched}\n"
                        f"[End of extracted media text]\n\n"
                        f"{context}"
                    )
                    _safe_log(
                        f"[media_enrich] injected {len(_enriched)}ch for cp={cp.cp_id}"
                    )
            except Exception as _me_exc:
                _safe_log(f"[media_enrich] inject EXC: {_me_exc}")

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


def _bump_l4_incident_counter(cp_id: str) -> None:
    """Append to ~/.hermes/paid/l4_incidents.jsonl so /paid-status can
    surface 'cp X had Y L4 hits in the last 7d'. We append rather than
    mutate cp.profile.json to avoid file-lock contention with intake
    and to keep cp profile schema stable. v1.3.2 C5 mitigation."""
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "cp_id": cp_id,
    }
    try:
        storage.append_jsonl(storage.PAID_DIR / "l4_incidents.jsonl", rec)
    except Exception:
        pass


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

        # hermes-agent v0.12.0's post_llm_call kwargs do NOT include
        # platform/sender_id (only session_id is reliable). Fall back to the
        # cache populated by pre_llm_call so we can still: (a) early-return
        # on owner replies and (b) scope L4 to the right counterparty.
        if not platform or not sender_id:
            cached = _lookup_session_meta(session_id)
            platform = platform or cached.get("platform", "")
            sender_id = sender_id or cached.get("sender_id", "")

        if sender_id and platform and identity.is_owner(platform, sender_id):
            return

        # Resolve current counterparty for cross-cp leakage scoping.
        cp_id = ""
        if sender_id and platform:
            cp = identity.load_counterparty(platform, sender_id)
            cp_id = cp.cp_id if cp else f"{platform}_{sender_id}"
        elif session_id:
            cp_id = _lookup_session_meta(session_id).get("cp_id", "")

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
                        # v1.6.8: include the rest of L4 detail so silent
                        # empty-leak alerts (like the v1.6.7 L4d false-positive)
                        # can be diagnosed instead of leaving the operator
                        # guessing which detector fired.
                        "unsourced_claims": l4.get("unsourced_claims", []),
                        "llm_concerns": l4.get("llm_concerns", []),
                        "response_preview": response[:500],
                    },
                    ensure_ascii=False,
                ),
            )
            # Best-effort corrective DM: hermes 0.12.0 has no outbound mutation
            # hook, so the leaked response has already been sent to the
            # counterparty. We fire a follow-up message asking them to
            # disregard so the operator at least has a paper-trail of "PAID
            # noticed and tried to contain it" rather than a silent leak.
            # Use any-CJK heuristic to pick the language.
            #
            # v1.3.2 C5 partial: strengthened the corrective wording (was
            # "may have contained" — too soft for safety message); added a
            # crisp placeholder so the counterparty has a clear substitute
            # to act on. True C5 (block dispatch before send) blocked on
            # hermes upstream (see backlog M2.5).
            owner_name_for_msg = identity.display_name(identity.load_owner())
            if platform and sender_id:
                try:
                    is_cjk = any(c >= "一" and c <= "鿿" for c in response)
                    correction = (
                        f"⚠️ 上一条回复包含了不该自动发出的信息，请忽略它。"
                        f"我已通知 {owner_name_for_msg}，他会直接回复你。"
                        f"— {owner_name_for_msg}'s PAID"
                        if is_cjk else
                        f"⚠️ Please disregard my previous reply — it contained "
                        f"information that shouldn't have been sent automatically. "
                        f"I've flagged {owner_name_for_msg} to follow up with you directly."
                        f" — {owner_name_for_msg}'s PAID"
                    )
                    hermes_io.send_dm(platform, sender_id, correction, fallback_to_queue=True)
                except Exception as exc:
                    _safe_log(f"[L4-LEAK] corrective send EXC: {exc}")

            # Track per-cp L4 incident so repeat offenders are visible in
            # /paid-status. Best-effort; failure must not break the post
            # hook.
            try:
                if cp_id:
                    _bump_l4_incident_counter(cp_id)
            except Exception as exc:
                _safe_log(f"[L4-LEAK] incident counter EXC: {exc}")

        # v1.6.7: pass cp identification so audit.log_action routes the row
        # to counterparties/<cp_id>/audit.jsonl instead of legacy audit_log.jsonl.
        # cp_id was already resolved above (line ~1065-1070).
        cp_for_audit = (
            {"cp_id": cp_id, "platform": platform}
            if cp_id else None
        )
        audit.log_action(
            session_id=session_id,
            counterparty=cp_for_audit,
            junior_msg="",
            classification=None,
            action=None,
            extra={
                "platform": platform,
                "assistant_response_preview": response[:300],
                "l4_ok": l4["ok"],
                "l4_name_leakage": l4["name_leakage"],
                "l4_pii": l4["pii"],
                # v1.6.8: surface L4d/L4c in audit so empty-leak rows
                # are diagnosable.
                "l4_unsourced_claims": l4.get("unsourced_claims", []),
                "l4_llm_concerns": l4.get("llm_concerns", []),
            },
        )
    except Exception as exc:
        _safe_log(f"[post_llm] EXC {exc}")


# ---------------------------------------------------------------------------
# Pre-gateway dispatch — gate input BEFORE it ever reaches the LLM.
#
# hermes-agent v0.12.0's `pre_gateway_dispatch` fires per inbound MessageEvent
# AFTER the internal-event guard but BEFORE auth/pairing/agent dispatch.
# Returning ``{"action": "skip"}`` drops the message (no reply). We use that
# to short-circuit Layer 1 (prompt-injection) hits — the LLM never sees the
# attempt, saving tokens AND making jailbreaks harder (no chance for the LLM
# to be coaxed before our regex screen runs).
#
# We DON'T move owner short-circuit / counterparty resolution / classifier
# here — keep those in pre_llm_call. This hook is single-purpose: a
# pre-emptive injection guard.
#
# (Outbound interception — true Layer 4 redaction — would need a hook that
# hermes 0.12.0 does NOT expose. See README "Hermes upstream gaps".)
# ---------------------------------------------------------------------------

def on_pre_gateway_dispatch(**kwargs) -> dict | None:
    """Drop inbound messages that trip the L1 injection regex AND route
    counterparty-issued /review and /r slash commands directly to the
    paid_review skill (bypassing hermes' slash dispatcher).

    Routing /review here instead of via ctx.register_command is the only
    way to get sender identity into the handler. The register_command
    handler signature is ``fn(raw_args: str) -> str`` — no source, no
    platform, no user_id — and the ``HERMES_GATEWAY_*`` env vars that
    older PAID code assumed exist are not actually set by hermes.
    pre_gateway_dispatch DOES receive ``event.source`` with full
    platform + user_id, so review routing must live here.

    ``kwargs``: event (MessageEvent), gateway (GatewayRunner), session_store.
    Returns ``{"action": "skip"}`` to drop; None to let dispatch proceed.
    """
    try:
        event = kwargs.get("event")
        if event is None:
            return None
        # Owner messages bypass — owner can paste anything they want.
        source = getattr(event, "source", None)
        platform = ""
        sender_id = ""
        if source is not None:
            plat_val = getattr(source, "platform", None)
            platform = getattr(plat_val, "value", str(plat_val)) if plat_val else ""
            sender_id = str(getattr(source, "user_id", "") or "")

        # First TG inbound after gateway boot: attach our paid_* button
        # callback handler to the live PTB Application. Idempotent — only
        # the first call does real work.
        if platform == "telegram":
            _ensure_telegram_callback_registered()

        # v1.5.4: attachment-binding for two-event Lark delivery.
        # Lark splits "/review <text>" + image attachment into two
        # separate inbound events (4-10 sec apart). We bind the media
        # back to the cp's review session in two ways:
        #
        #  (a) Active session: when cp has an open review session, this
        #      event has media but no /review prefix → call
        #      paid_review.api.add_attachments_to_session() directly to
        #      append the media into the session's ingest pipeline.
        #      Returns {action:skip,reason:paid_review_attachment_bound}.
        #
        #  (b) No active session: passively buffer the media path in
        #      ``paid_review.attachment_buffer`` (90s TTL, per-cp).
        #      A subsequent /review intake call drains the buffer and
        #      includes the buffered attachments. Do NOT skip here —
        #      let hermes' main agent continue normal handling, since
        #      we don't know yet whether a /review is coming.
        #
        # Owner-side media is ignored — owners can DM the bot images and
        # we want hermes' vision flow to handle them normally (J0 path).
        _event_media_urls = list(getattr(event, "media_urls", []) or [])
        _event_media_types = list(getattr(event, "media_types", []) or [])
        _event_text_for_media = str(getattr(event, "text", "") or "").strip()
        _has_media = bool(_event_media_urls)
        _is_owner_now = bool(
            platform and sender_id
            and identity.is_owner(platform, sender_id)
        )
        # Only handle media-only messages (no /review|/r prefix). Messages
        # that bundle text + media (e.g., a Lark caption with an image)
        # flow through the normal /review handler with attachments=[]
        # in hook_kwargs — fix in _maybe_route_to_review_skill (Path 2)
        # drains the buffer there.
        _is_media_only = (
            _has_media
            and not _event_text_for_media.startswith("/review")
            and not _event_text_for_media.startswith("/r ")
            and _event_text_for_media not in ("/r", "/review")
        )
        if _is_media_only and platform and sender_id and not _is_owner_now:
            _attachments_from_event = [
                {
                    "path": p,
                    "mimetype": (_event_media_types[i] if i < len(_event_media_types) else ""),
                    "name": os.path.basename(p) if p else f"media_{i}",
                }
                for i, p in enumerate(_event_media_urls)
                if p
            ]
            _active_sid = ""
            try:
                _cp_quick = identity.load_counterparty(platform, sender_id)
                if _cp_quick and _cp_quick.active_review_session:
                    _active_sid = _cp_quick.active_review_session
            except Exception:
                pass

            if _active_sid:
                # (a) bind to active session
                try:
                    from paid_review import api as _review_api
                    _bind_res = _review_api.add_attachments_to_session(
                        _active_sid, _attachments_from_event,
                    )
                    _safe_log(
                        f"[review attach] bound {len(_attachments_from_event)} "
                        f"to active sid={_active_sid[:8]}: {_bind_res}"
                    )
                    return {
                        "action": "skip",
                        "reason": "paid_review_attachment_bound",
                    }
                except Exception as exc:
                    _safe_log(
                        f"[review attach] bind to active sid={_active_sid[:8]} "
                        f"EXC: {exc}"
                    )
                    # fall through — don't block hermes' own media handling
            else:
                # (b) buffer for upcoming /review (passive)
                try:
                    from paid_review import attachment_buffer as _buf
                    for a in _attachments_from_event:
                        _buf.add(
                            platform, sender_id,
                            path=a["path"], mime=a["mimetype"], name=a["name"],
                        )
                    _safe_log(
                        f"[review attach] buffered {len(_attachments_from_event)} "
                        f"media for cp={platform}:{sender_id[:8]} "
                        f"(awaiting /review within TTL)"
                    )
                except Exception as exc:
                    _safe_log(f"[review attach] buffer add EXC: {exc}")
                # Do NOT return skip — let hermes continue its routing.
                # The buffer is a passive memo; hermes can still vision-
                # analyze the image normally if cp never /reviews.

        # v1.6.5: non-review media enrichment — run OCR/pdftotext on CP
        # media that is NOT bound to a /review session, so the extracted
        # text can be injected into the LLM context in on_pre_llm_call.
        # Skip owner messages (they go through hermes' own vision flow).
        if _has_media and platform and sender_id and not _is_owner_now:
            _is_review_media = (
                _event_text_for_media.startswith("/review")
                or _event_text_for_media.startswith("/r ")
                or _event_text_for_media in ("/r", "/review")
            )
            if not _is_review_media:
                try:
                    from paid import media_enrichment as _me
                    from paid import settings as _settings
                    _enrich_mode = _settings.media_enrichment_mode()
                    if _enrich_mode != "off":
                        _me.enrich_media_for_cp(
                            platform, sender_id,
                            _event_media_urls, _event_media_types, _enrich_mode,
                        )
                        _safe_log(
                            f"[media_enrich] mode={_enrich_mode} "
                            f"cp={platform}:{sender_id[:8]} "
                            f"files={len(_event_media_urls)}"
                        )
                except Exception as _me_exc:
                    _safe_log(f"[media_enrich] EXC: {_me_exc}")

        # v1.5 Phase 6: group routing gate. By default group chats are
        # NOT enabled — owner must opt in per group via /paid-enable-group
        # (Phase 7). Until then, any group message is silently dropped to
        # prevent the bot from auto-replying in groups it was added to.
        # Owner-issued /paid-* commands inside groups are always allowed
        # through so Phase 7 self-service can run from the group itself.
        #
        # v1.5.2 fix: hermes feishu adapter synthesizes card-action button
        # clicks into `/card button {json}` MessageEvents with
        # event_chat_type="group" HARDCODED (gateway/platforms/feishu.py
        # ``_handle_card_action_event``). On a Lark bot↔owner DM this is
        # technically still oc_*-prefixed chat_id, and the synthetic
        # chat_type="group" made Phase 6 misclassify the click as a group
        # message and drop it with `paid_group_not_enabled`. The whole
        # /card slash command path is owner-tools-only and routes to
        # ``_cmd_card`` regardless of chat type — let it bypass the
        # routing gate entirely. (Regression introduced in v1.5.0 when
        # Phase 6 wiring landed; latent because lark card clicks weren't
        # tested live until v1.5.1 manual smoke. Both paid uid 1002 and
        # paid-jelabs uid 1004 logs show `_handle_card_action_event`
        # firing correctly but PAID dropping the synthetic command at
        # the routing gate.)
        event_text_peek = str(getattr(event, "text", "") or "")
        if event_text_peek.lstrip().startswith("/card "):
            _routing = "p2p"
        else:
            try:
                _routing = group_routing.classify_routing(event, text=event_text_peek)
            except Exception:
                _routing = "p2p"  # fail-open
        if _routing != "p2p":
            owner_in_group_command = (
                platform and sender_id
                and identity.is_owner(platform, sender_id)
                and event_text_peek.lstrip().startswith("/paid-")
            )
            if _routing == "group_disabled" and not owner_in_group_command:
                return {"action": "skip", "reason": "paid_group_not_enabled"}
            if _routing in ("group_everyday", "group_both"):
                # v1.6.17b: everyday/both now actually wire the Claude flow
                # (was reserved/dropped pre-v1.6.17 — jelabs pilot day-1:
                # owner set mode=both, cp's "请问团队开会时间…" was silently
                # dropped with paid_group_mode_reserved_group_both, bot
                # looked dead in the group).
                #
                # A non-command group message in everyday/both falls
                # through to the same J2 cp pipeline a P2P DM uses
                # (classify → decide → approval card to owner DM → reply
                # back to the group). /paid-* and /review keep their
                # existing downstream handlers.
                #
                # Mention gating: we rely on hermes' FEISHU_ALLOW_BOTS=
                # mentions contract — only @-mentioned group messages are
                # delivered to the agent pipeline, so anything reaching
                # here is already addressed to the bot. We do NOT re-parse
                # mention markup (brittle across platforms); if an operator
                # sets ALLOW_BOTS=all they explicitly opted into the bot
                # seeing all group chatter in everyday mode.
                _grp_chat_id = (
                    str(getattr(source, "chat_id", "") or "")
                    if source is not None else ""
                )
                _safe_log(
                    f"[group_routing] everyday/both fall-through "
                    f"routing={_routing} chat={_grp_chat_id} "
                    f"text='{event_text_peek[:50]}'"
                )
                # fall through — owner-command / review / cp-classify
                # branches below handle it exactly as P2P.
            if _routing == "group_review_strict" and not owner_in_group_command:
                # v1.5.1 fix (audit Critical #5): review-only group, sender
                # typed a non-command message. Only let through if THIS
                # sender has an active review session in progress — that
                # makes the message a QA continuation. Otherwise drop so
                # the bot doesn't auto-reply to everyday chatter.
                has_active = False
                if platform and sender_id:
                    try:
                        cp_check = identity.load_counterparty(platform, sender_id)
                        has_active = bool(
                            cp_check and cp_check.active_review_session
                        )
                    except Exception as exc:
                        _safe_log(f"[group_routing] active-session check EXC: {exc}")
                if not has_active:
                    return {
                        "action": "skip",
                        "reason": "paid_group_review_only_non_review_message",
                    }
                # else: fall through — downstream has_active_review branch
                # routes the message through the review skill.
            # _routing == "group_review" (command-prefixed) falls through —
            # existing /review interception handles it as P2P would.

        if platform and sender_id and identity.is_owner(platform, sender_id):
            # v1.5 Phase 7: owner-issued /paid-*-group commands. Intercept
            # before the awaiting_input check so the slash text doesn't get
            # eaten as a captured reply. Only fires for /paid-* prefixes
            # the group-command handler knows about.
            owner_text_peek = str(getattr(event, "text", "") or "").strip()
            if owner_text_peek.startswith("/paid-"):
                try:
                    group_rv = _handle_group_command_in_pre_gateway(
                        event, platform, sender_id, owner_text_peek,
                    )
                except Exception as exc:
                    _safe_log(f"[group_cmd] handler EXC: {exc}")
                    group_rv = None
                if group_rv is not None:
                    return group_rv

            # v1.6.0: /paid-setup wizard. Owner DMs the bot to configure
            # profile via 5-question state machine (or edit-mode menu when
            # profile already exists). Subsequent plain-text replies from
            # the owner WHILE the wizard is active are captured as answers
            # — see the "owner in wizard" branch below.
            try:
                wiz_rv = _handle_setup_command_in_pre_gateway(
                    event, platform, sender_id, owner_text_peek,
                )
            except Exception as exc:
                _safe_log(f"[setup_wizard] handler EXC: {exc}")
                wiz_rv = None
            if wiz_rv is not None:
                return wiz_rv

            # v1.6.1: doc confirm reply capture (mode="doc_confirm")
            if (
                owner_text_peek
                and not owner_text_peek.startswith("/")
                and _setup_wizard.is_doc_confirm_active(platform, sender_id)
            ):
                _chat_id_dc = ""
                if source is not None:
                    _cid_dc = getattr(source, "chat_id", None)
                    _chat_id_dc = str(_cid_dc) if _cid_dc else ""
                try:
                    _dc_reply, _dc_done = _setup_wizard.consume_doc_confirm(
                        platform, sender_id, owner_text_peek,
                    )
                except Exception as exc:
                    _safe_log(f"[doc_confirm] EXC: {exc}")
                    _dc_reply, _dc_done = (
                        f"文档确认出错 — {exc}。",
                        True,
                    )
                _send_setup_wizard_reply(platform, sender_id, _chat_id_dc, _dc_reply)
                return {"action": "skip", "reason": "paid_doc_confirm"}

            # v1.6.0: owner in wizard → capture next plain text as answer
            if (
                owner_text_peek
                and not owner_text_peek.startswith("/")
                and _setup_wizard.is_active(platform, sender_id)
            ):
                _chat_id_for_send = ""
                if source is not None:
                    _cid_w = getattr(source, "chat_id", None)
                    _chat_id_for_send = str(_cid_w) if _cid_w else ""
                try:
                    reply_text, _wiz_done = _setup_wizard.consume(
                        platform, sender_id, owner_text_peek,
                    )
                except Exception as exc:
                    _safe_log(f"[setup_wizard] consume EXC: {exc}")
                    reply_text, _wiz_done = (
                        f"PAID setup: 出错了 — {exc}。发 /paid-setup cancel 退出。",
                        False,
                    )
                _send_setup_wizard_reply(platform, sender_id, _chat_id_for_send, reply_text)
                return {
                    "action": "skip",
                    "reason": ("paid_setup_done" if _wiz_done else "paid_setup_step"),
                }
            # v1.6.2: conversation capture confirm reply
            if (
                owner_text_peek
                and not owner_text_peek.startswith("/")
                and _conv_capture.has_pending(platform, sender_id)
            ):
                _safe_log(
                    f"[conv_capture] apply path: pending exists for "
                    f"{platform}:{sender_id[:8]}, reply='{owner_text_peek[:40]}'"
                )
                _chat_id_cc = ""
                if source is not None:
                    _cid_cc = getattr(source, "chat_id", None)
                    _chat_id_cc = str(_cid_cc) if _cid_cc else ""
                try:
                    _cc_reply = _conv_capture.apply_confirmed(
                        platform, sender_id, owner_text_peek,
                    )
                    _safe_log(f"[conv_capture] apply done: '{_cc_reply[:60]}'")
                except Exception as exc:
                    _safe_log(f"[conv_capture] apply EXC: {exc}")
                    _cc_reply = f"Profile 更新确认出错 — {exc}。"
                _send_setup_wizard_reply(platform, sender_id, _chat_id_cc, _cc_reply)
                return {"action": "skip", "reason": "paid_conv_capture_confirm"}

            # v1.6.2: detect profile-update signals in owner chat (side-effect)
            # Message still passes through to Claude; this fires asynchronously.
            if owner_text_peek and not owner_text_peek.startswith("/"):
                try:
                    _safe_log(
                        f"[conv_capture] detect entry: plat={platform} "
                        f"sender={sender_id[:8]} text='{owner_text_peek[:60]}'"
                    )
                    _prof_for_cc = None
                    try:
                        from paid import profile as _profile_mod
                        _prof_for_cc = _profile_mod.load_profile()
                    except Exception as _pf_exc:
                        _safe_log(f"[conv_capture] profile load EXC: {_pf_exc}")
                    if _prof_for_cc is None:
                        _safe_log("[conv_capture] detect skip: profile is None")
                    else:
                        _scan_ok = _conv_capture.should_scan(owner_text_peek)
                        _rate_limited = _conv_capture.is_rate_limited(
                            platform, sender_id,
                        )
                        _safe_log(
                            f"[conv_capture] gates: should_scan={_scan_ok} "
                            f"rate_limited={_rate_limited}"
                        )
                        _cc_proposals = _conv_capture.extract_from_message(
                            owner_text_peek, _prof_for_cc, platform, sender_id,
                        )
                        _safe_log(
                            f"[conv_capture] extract result: "
                            f"proposals={len(_cc_proposals)} "
                            f"fields={[p.field for p in _cc_proposals]}"
                        )
                        if _cc_proposals:
                            _conv_capture.store_pending(platform, sender_id, _cc_proposals)
                            _cc_prompt = _conv_capture.format_confirm(
                                _cc_proposals,
                            )
                            _chat_id_ccp = ""
                            if source is not None:
                                _cid_ccp = getattr(source, "chat_id", None)
                                _chat_id_ccp = str(_cid_ccp) if _cid_ccp else ""
                            _safe_log(
                                f"[conv_capture] sending confirm: "
                                f"chat_id='{_chat_id_ccp}' "
                                f"target={_chat_id_ccp or sender_id} "
                                f"prompt_len={len(_cc_prompt)}"
                            )
                            # Inline the send so we can log the actual result
                            # from hermes_io.send_dm (the helper swallows it).
                            _cc_send_target = _chat_id_ccp or sender_id
                            try:
                                _cc_send_result = hermes_io.send_dm(
                                    platform, _cc_send_target, _cc_prompt,
                                    fallback_to_queue=True,
                                )
                                _safe_log(
                                    f"[conv_capture] send_dm result: "
                                    f"ok={_cc_send_result.get('ok')} "
                                    f"msg_id={_cc_send_result.get('msg_id')} "
                                    f"queued={_cc_send_result.get('queued')} "
                                    f"err={_cc_send_result.get('error')}"
                                )
                            except Exception as _cc_send_exc:
                                _safe_log(
                                    f"[conv_capture] send_dm EXC: "
                                    f"{type(_cc_send_exc).__name__}: {_cc_send_exc}"
                                )
                except Exception as exc:
                    _safe_log(
                        f"[conv_capture] detect EXC: {type(exc).__name__}: {exc}"
                    )

            # Owner-side messages normally pass through to hermes / Claude.
            # BUT: if the owner clicked ✅ / ✏️ on a card and we armed
            # awaiting_input, the next plain-text reply in the same chat
            # should be captured as the answer to forward to the junior —
            # not handed to the LLM.
            owner_text = str(getattr(event, "text", "") or "").strip()
            chat_id_val = ""
            if source is not None:
                _cid = getattr(source, "chat_id", None)
                chat_id_val = str(_cid) if _cid else ""
            if (
                owner_text
                and not owner_text.startswith("/")
                and (platform, sender_id) in _AWAITING_INPUT
            ):
                rid = _consume_awaiting_input(platform, sender_id, chat_id=chat_id_val or None)
                if rid is not None:
                    try:
                        result_text = _do_approve(rid, override_text=owner_text)
                    except Exception as exc:
                        _safe_log(f"[card] owner-input do_approve EXC #{rid}: {exc}")
                        result_text = f"PAID: #{rid} — internal error: {exc}"
                    _send_owner_inline_message(
                        f"✅ #{rid} approved with your reply.\n{result_text}"
                    )
                    _safe_log(
                        f"[card] case-2 captured owner input for #{rid}; "
                        f"text_len={len(owner_text)} → {result_text[:120]!r}"
                    )
                    return {"action": "skip", "reason": "paid_owner_input_consumed"}
            return None

        text = str(getattr(event, "text", "") or "")
        if not text:
            return None

        # /review and /r interception — before L1 so injection guard
        # still applies to the rest of the message but we don't need to
        # match "/review" against injection patterns (it's our own
        # command syntax).
        stripped = text.lstrip()
        # v1.5.3 fix #6: CJK-friendly review-command match. Accepts whitespace,
        # EOL, OR any non-letter char (so `/review看一下` recognizes; `/reviewing`
        # still does not).
        def _is_review_prefix(s: str, n: int) -> bool:
            return s.startswith(s[:n]) and (
                len(s) == n
                or s[n] in " \n\t"
                or not s[n].isalpha()
            )
        is_review_cmd = (
            (stripped.startswith("/review") and _is_review_prefix(stripped, 7))
            or (stripped.startswith("/r") and _is_review_prefix(stripped, 2))
        )

        # Active review session: route ALL inbound (not just /review prefix)
        # directly here, bypassing pre_llm_call. Reason: when active-session
        # Q&A goes through pre_llm_call, the reply is wrapped in
        # "IGNORE the user question. Reply EXACTLY with: '<text>'" for the
        # LLM. ~20% of the time the LLM doesn't comply and free-styles —
        # which leaks the IGNORE wrapper instruction text to the cp.
        # Confirmed in 2026-05-12 dogfood (orphan J3 ca58f7e3 had the raw
        # IGNORE wrapper as its junior_question). Direct send_dm here
        # eliminates the LLM-compliance dependency entirely.
        has_active_review = False
        if platform and sender_id and not is_review_cmd:
            try:
                cp_quick = identity.load_counterparty(platform, sender_id)
                if cp_quick and cp_quick.active_review_session:
                    has_active_review = True
            except Exception:
                pass

        if is_review_cmd or has_active_review:
            if platform and sender_id:
                return _handle_review_in_pre_gateway(
                    platform, sender_id, stripped, event=event,
                )

        l1_hit, l1_labels = safety.detect_prompt_injection(text)
        if not l1_hit:
            return None

        _safe_log(
            f"[pre_gw L1-INJECTION-EARLY] platform={platform} sender={sender_id} "
            f"labels={l1_labels} msg={text[:120]!r}"
        )
        try:
            storage.append_jsonl(
                storage.PAID_DIR / "fatal_alerts.jsonl",
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "reason": "layer_1_prompt_injection_early",
                    "platform": platform,
                    "sender_id": sender_id,
                    "labels": l1_labels,
                    "snippet": text[:300],
                },
            )
        except Exception:
            pass

        # Best-effort canned decline back to the sender. send_dm tolerates
        # gateway-not-ready by falling through to outbound_queue.jsonl.
        owner = identity.load_owner()
        owner_name = identity.display_name(owner)
        decline = (
            f"我没办法处理这个请求，请直接 @ {owner_name}." if any(c >= "一" for c in text)
            else f"I can't process that request — please contact {owner_name} directly."
        )
        try:
            hermes_io.send_dm(platform, sender_id, decline, fallback_to_queue=True)
        except Exception as exc:
            _safe_log(f"[pre_gw L1] decline send EXC: {exc}")

        return {"action": "skip", "reason": "layer_1_prompt_injection"}
    except Exception as exc:
        _safe_log(f"[pre_gw] FATAL: {exc}\n{traceback.format_exc()}")
        return None  # fail-open — let hermes proceed


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

    # v1.3.2 H2: surface classifier fallback rate so owner can spot
    # silent degradation. Pre-fix, if classifier was failing every call
    # (network / API key bad), every inbound just got conservatively
    # routed to request — the owner sees more J3 cards but has no
    # signal that the classifier itself is broken.
    health_lines = []
    try:
        from paid.classifier import fallback_rate_recent
        fb, total, ratio = fallback_rate_recent()
        if total >= 5:  # don't fire alarms before there's signal
            pct = int(round(ratio * 100))
            marker = " ⚠️" if ratio >= 0.20 else ""
            health_lines.append(
                f"Classifier fallback rate (last {total}): {pct}% ({fb}/{total}){marker}"
            )
    except Exception:
        pass

    if not pendings:
        body = ["PAID: no pending approvals."]
    else:
        body = ["PAID pending approvals:"] + [_format_pending_summary(r) for r in pendings]

    if health_lines:
        body = body + ["", "— Health —"] + health_lines
    return "\n".join(body)


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


def _do_approve(rid: str, override_text: str = "") -> str:
    """Approve a pending request and dispatch the answer to the junior.

    Returns the operator-facing summary string. Caller is responsible for
    owner identity verification — used by three caller paths:
      - /paid-approve slash command (env-gated)
      - Lark card-button click via _cmd_card
      - TG inline-keyboard callback (gated by query.from_user.id against
        owner.json)
    """
    req = approval.get(rid)
    if req is None:
        return f"PAID: unknown request id #{rid}"
    if req.status != "pending":
        return f"PAID: #{rid} already {req.status} (resolved at {req.ts_resolved})"

    final_text = (override_text or "").strip() or (req.draft_answer or "").strip()
    if not final_text:
        return (
            f"PAID: #{rid} has no draft and no override text was given. "
            f"Use the inline approve flow or /paid-approve {rid} <your answer>."
        )

    owner = identity.load_owner()
    owner_name = identity.display_name(owner)
    decorated = f"{owner_name} 看了你的问题：\n\n{final_text}"

    delivery = _dispatch_to_junior(req, decorated)
    approval.set_status(rid, "approved", final_text=decorated)
    return f"PAID: #{rid} approved → {delivery}"


def _do_reject(rid: str) -> str:
    """Reject a pending request. See :func:`_do_approve` for caller contract."""
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


def _cmd_approve(raw_args: str) -> str:
    if not _is_caller_owner_via_env():
        return ""
    try:
        rid, extra = _resolve_request(raw_args)
    except ValueError as exc:
        return f"PAID: {exc} — usage: /paid-approve <id> [optional override text]"
    return _do_approve(rid, override_text=extra)


def _cmd_reject(raw_args: str) -> str:
    if not _is_caller_owner_via_env():
        return ""
    try:
        rid, _ = _resolve_request(raw_args)
    except ValueError as exc:
        return f"PAID: {exc} — usage: /paid-reject <id>"
    return _do_reject(rid)


# ---------------------------------------------------------------------------
# Inline approve flow — case 1 (direct) / case 2 (collect-then-send)
#
# Problem v1.3.x had: clicking ✅ Approve on a Lark / TG card returned a
# string asking the owner to type ``/paid-approve <id> <text>``, but
# (a) that return string is delivered by hermes' synthetic-command
# reply path which doesn't reliably reach the owner's chat (verified
# on VPS — 8/8 historical button clicks logged but zero visible
# response to owner), and (b) even when it does reach, the owner
# expectation is "click ✅ → it's done" or "click ✏️ Reply → ask me
# what to say". Forcing them to switch to slash command syntax breaks
# that.
#
# This module fixes both:
#   - all card-click outcomes are pushed via send_dm (not relying on
#     synthetic-command reply delivery)
#   - ✅ Approve always dispatches immediately: with the draft if there
#     is one, else with a language-matched default agreement
#   - ✏️ Reply records an "awaiting input" state for this owner and DMs
#     them an inline prompt; the owner's NEXT plain-text reply (within
#     30 min, same owner chat) becomes the answer
#
# State is module-level (in-memory only). If hermes restarts mid-flow,
# the owner clicks ✏️ Reply again to re-arm. Acceptable trade-off vs
# the complexity of persisting a new approval substate.
# ---------------------------------------------------------------------------

import time as _time

_AWAITING_INPUT_TTL_SEC = 30 * 60  # matches J3 approval timeout default
_AWAITING_INPUT: dict[tuple[str, str], dict] = {}
# key:   (owner platform, owner home_chat_id / user_id) — whichever id is
#        the source.user_id when owner DMs the bot (we store both keys for
#        Lark where home_chat_id may differ from owner identity user_id).
# value: {"rid": str, "since_ts": float, "expected_chat_id": str | None}


def _awaiting_keys_for_owner(owner) -> list[tuple[str, str]]:
    """All (platform, id) pairs that should match an owner's own messages
    in pre_gateway_dispatch — to be safe across Lark home_chat_id vs
    user_id ambiguity."""
    if owner is None:
        return []
    out: list[tuple[str, str]] = []
    for ident in getattr(owner, "identities", []) or []:
        if not isinstance(ident, dict):
            continue
        if ident.get("enabled") is False:
            continue
        plat = str(ident.get("platform", "")).strip()
        uid = str(ident.get("user_id", "")).strip()
        hcid = str(ident.get("home_chat_id", "")).strip()
        if plat and uid:
            out.append((plat, uid))
        if plat and hcid and hcid != uid:
            out.append((plat, hcid))
    return out


def _record_awaiting_input(rid: str, expected_chat_id: str | None = None) -> None:
    """Arm the owner-input capture for the owner's preferred identity.

    Indexes by every (platform, id) pair the owner might appear as in
    inbound events; the consumer pops by whichever key matches the
    incoming MessageEvent.source.
    """
    owner = identity.load_owner()
    keys = _awaiting_keys_for_owner(owner)
    if not keys:
        _safe_log(f"[card] cannot arm awaiting_input for #{rid}: no owner identities")
        return
    entry = {
        "rid": rid,
        "since_ts": _time.time(),
        "expected_chat_id": expected_chat_id,
    }
    for key in keys:
        _AWAITING_INPUT[key] = entry
    _safe_log(f"[card] armed awaiting_input for #{rid} on owner keys={keys}")


def _consume_awaiting_input(
    platform: str, sender_id: str, chat_id: str | None = None,
) -> str | None:
    """If this owner has an active awaiting_input slot within TTL, return
    its rid and clear ALL its keys. Otherwise return None.

    chat_id is checked when present on both sides: only consume when
    the inbound chat matches the chat where the card was clicked. This
    keeps a stale awaiting_input from eating an owner's casual reply in
    some unrelated chat.
    """
    key = (platform, sender_id)
    entry = _AWAITING_INPUT.get(key)
    if entry is None:
        return None
    if _time.time() - entry["since_ts"] > _AWAITING_INPUT_TTL_SEC:
        # Expired — clear and don't consume.
        _clear_awaiting_input_keys(entry["rid"])
        return None
    expected = entry.get("expected_chat_id")
    if expected and chat_id and str(expected) != str(chat_id):
        # Owner replied in a different chat than where they clicked — leave
        # the slot armed for when they DO reply in the right chat.
        return None
    rid = entry["rid"]
    _clear_awaiting_input_keys(rid)
    return rid


def _clear_awaiting_input_keys(rid: str) -> None:
    """Remove every key that points to this rid (since we duplicate)."""
    stale = [k for k, v in _AWAITING_INPUT.items() if v.get("rid") == rid]
    for k in stale:
        _AWAITING_INPUT.pop(k, None)


def _default_approve_text(req) -> str:
    """Generic "yes/agree" reply for the empty-draft approve path.

    Picks language by inspecting the junior's question (zh vs en) so the
    deflection text matches what the junior wrote in. This is reached
    only when ✅ Approve is clicked on a card where the classifier
    didn't ground a draft — owner is saying "yes, I agree with the
    request as stated".
    """
    try:
        lang = decision.detect_lang(req.junior_question or "")
    except Exception:
        lang = "zh"
    return "Approved." if lang == "en" else "可以的。"


def _send_owner_inline_message(text: str) -> bool:
    """Best-effort push a short status message to the owner's preferred
    identity. Returns True if the underlying send_dm reported success
    (or fell through to outbound_queue).
    """
    owner = identity.load_owner()
    pref = owner.preferred_identity() if owner else None
    if pref is None:
        target = _owner_primary_identity(owner)
        if target is None:
            _safe_log(f"[card] cannot push owner notice: no owner identity")
            return False
        plat, uid = target
    else:
        plat, uid = pref.platform, pref.home_chat_id
    receive_target = _resolve_owner_send_target(plat, uid)
    try:
        result = hermes_io.send_dm(plat, receive_target, text, fallback_to_queue=True)
    except Exception as exc:
        _safe_log(f"[card] owner notice send_dm EXC: {exc}")
        return False
    if not isinstance(result, dict):
        return False
    return bool(result.get("ok") or result.get("queued"))


def _unwrap_hermes_context(ctx_str: str) -> str:
    """Pull the actual reply text back out of a _wrap_reply_for_hermes
    output. PAID has TWO wrap formats in production (verified in
    grep 2026-05-13):

      Format A — paid/decision.py request/decline/direct contexts:
          IGNORE the user question. Reply EXACTLY with: '<text>' Nothing else.

      Format B — __init__.py:_wrap_reply_for_hermes (review skill):
          IGNORE the user message. Reply EXACTLY with the following text
          and nothing else, preserving all line breaks: '<text>'

    Pre-v1.3.8 this function matched only Format A → Format B fell
    through → the wrapper text leaked verbatim to the cp in
    pre_gateway_dispatch direct-send paths (confirmed in 2026-05-13
    dogfood when Evie saw the IGNORE instruction in her chat after
    a /review intake). Now handles both.
    """
    # Format A
    mark_a = "EXACTLY with: '"
    end_a = "' Nothing else."
    if mark_a in ctx_str and end_a in ctx_str:
        start = ctx_str.index(mark_a) + len(mark_a)
        end = ctx_str.rindex(end_a)
        inner = ctx_str[start:end]
        return inner.replace("\\\\", "\\").replace("\\'", "'")

    # Format B
    mark_b = "preserving all line breaks: '"
    if mark_b in ctx_str:
        start = ctx_str.index(mark_b) + len(mark_b)
        # Format B ends at the LAST quote (no trailing " Nothing else.")
        trimmed = ctx_str.rstrip()
        if trimmed.endswith("'"):
            end = trimmed.rfind("'", start)
            if end > start:
                inner = ctx_str[start:end]
                return inner.replace("\\\\", "\\").replace("\\'", "'")

    return ctx_str


def _handle_review_in_pre_gateway(
    platform: str, sender_id: str, text: str,
    *, event=None,
) -> dict:
    """Intercept /review and /r from a counterparty in pre_gateway_dispatch.

    Resolves the cp, routes through ``_maybe_route_to_review_skill``,
    sends the reply directly via ``hermes_io.send_dm`` (since
    pre_gateway_dispatch returns dispatcher metadata, not message
    content), and returns ``{"action": "skip"}`` to suppress hermes'
    own slash dispatcher from also responding.

    Called only when the inbound is from a non-owner — owner check
    happens in the caller (on_pre_gateway_dispatch).

    Returns the action dict for pre_gateway_dispatch — always "skip"
    on this path, since the message is now fully handled.

    v1.5.3 fix #7: when /review was sent in a group chat, the reply
    routes back to the group chat (not the cp's DM with the bot). Per
    Round 2 manual-test feedback — cp expects to see bot's response
    where they typed. ``event`` (optional) is the inbound MessageEvent
    we use to pull source.chat_id + chat_type. When chat_type indicates
    group, send back to chat_id; otherwise keep DM behavior.
    """
    try:
        cp = identity.ensure_counterparty(platform, sender_id)
    except Exception as exc:
        _safe_log(f"[review pre_gw] ensure_counterparty EXC plat={platform} sid={sender_id}: {exc}")
        # Don't suppress — let normal dispatch handle the unknown sender.
        return None

    try:
        result = _maybe_route_to_review_skill(cp, text, {"attachments": []})
    except Exception as exc:
        _safe_log(f"[review pre_gw] router EXC cp={cp.cp_id}: {exc}")
        result = None

    if result is None:
        reply_text = (
            "review skill 没接住这条消息。"
            "请直接发 /review <要 review 的草稿正文>。"
        )
    else:
        ctx_str = result.get("context", "") if isinstance(result, dict) else ""
        reply_text = _unwrap_hermes_context(ctx_str)

    # v1.5.3 fix #7: pick reply target — group chat_id when in group, cp DM otherwise.
    reply_target = sender_id
    reply_dest = "dm"
    if event is not None:
        try:
            src = getattr(event, "source", None)
            ct = (getattr(src, "chat_type", "") or "").lower() if src else ""
            chat_id = str(getattr(src, "chat_id", "") or "") if src else ""
            if chat_id and ct in ("group", "supergroup"):
                reply_target = chat_id
                reply_dest = "group"
        except Exception:
            pass

    try:
        hermes_io.send_dm(platform, reply_target, reply_text, fallback_to_queue=True)
    except Exception as exc:
        _safe_log(f"[review pre_gw] send_dm EXC cp={cp.cp_id}: {exc}")

    _safe_log(
        f"[review pre_gw] handled /review for cp={cp.cp_id} "
        f"reply_dest={reply_dest} target={reply_target[:30]} reply_len={len(reply_text)}"
    )
    return {"action": "skip", "reason": "paid_review_routed"}


# ---------------------------------------------------------------------------
# Group self-service commands (v1.5 Phase 7)
#
# These commands are intercepted in pre_gateway_dispatch (NOT via
# register_command) because they need to know which group the owner
# is calling from — and only pre_gateway_dispatch sees event.source.chat_id.
#
# Recognized prefixes:
#   /paid-enable-group [mode]   — enable current group (default review-only)
#   /paid-disable-group          — disable current group
#   /paid-set-group-mode <mode>  — change mode (review-only|everyday|both)
#   /paid-set-group-name <name>  — set display name
#   /paid-group-status           — show current group's config
#   /paid-list-groups            — list all configured groups (works in DM)
#
# All commands require owner identity. Group-bound commands (everything
# except /paid-list-groups) require being invoked from inside a group chat.
# ---------------------------------------------------------------------------


def _handle_group_command_in_pre_gateway(event, platform: str, sender_id: str,
                                          text: str) -> dict | None:
    """Parse and execute group self-service commands from inside the hook.

    Returns a {"action": "skip", ...} dict when the command was handled
    (so hermes doesn't double-respond), or None to fall through.
    Replies to the owner go via hermes_io.send_dm to whatever chat the
    command originated in (group or DM).
    """
    stripped = text.lstrip()
    chat_id = ""
    if event is not None and getattr(event, "source", None) is not None:
        chat_id = str(getattr(event.source, "chat_id", "") or "")

    parts = stripped.split(None, 1)
    cmd = parts[0] if parts else ""
    args = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/paid-enable-group", "/paid-disable-group",
               "/paid-set-group-mode", "/paid-set-group-name",
               "/paid-group-status"):
        # These need group context — refuse in DM
        if group_routing.classify_chat(event) != "group":
            _send_group_reply(platform, sender_id, chat_id,
                              "PAID: this command must be run inside a group chat.")
            return {"action": "skip", "reason": "paid_group_cmd_not_in_group"}

        group_key = group_routing.get_group_key(event)
        if not group_key:
            _send_group_reply(platform, sender_id, chat_id,
                              "PAID: couldn't identify this group's chat_id.")
            return {"action": "skip", "reason": "paid_group_cmd_no_key"}

        group_id = chat_id  # group_id == platform-native chat_id

        if cmd == "/paid-enable-group":
            mode = (args.split(None, 1)[0] if args else "review-only").lower()
            if mode not in ("review-only", "everyday", "both"):
                _send_group_reply(platform, sender_id, chat_id,
                                  f"PAID: unknown mode '{mode}'. "
                                  "Use review-only | everyday | both.")
                return {"action": "skip", "reason": "paid_group_bad_mode"}
            existing = group_routing.load_group_config(group_key)
            cfg = group_routing.GroupConfig(
                group_key=group_key,
                platform=platform,
                group_id=group_id,
                enabled=True,
                mode=mode,
                owner_user_id=sender_id,
                display_name=existing.display_name if existing else "",
                created_at=existing.created_at if existing else "",
            )
            group_routing.save_group_config(cfg)
            _send_group_reply(
                platform, sender_id, chat_id,
                f"✅ PAID enabled in this group (mode={mode}).\n"
                f"group_key={group_key}",
            )
            return {"action": "skip", "reason": "paid_group_enabled"}

        if cmd == "/paid-disable-group":
            cfg = group_routing.load_group_config(group_key)
            if cfg is None or not cfg.enabled:
                _send_group_reply(platform, sender_id, chat_id,
                                  "PAID: this group is not currently enabled.")
                return {"action": "skip", "reason": "paid_group_already_disabled"}
            cfg.enabled = False
            group_routing.save_group_config(cfg)
            _send_group_reply(platform, sender_id, chat_id,
                              "✅ PAID disabled in this group. Group config kept "
                              "(re-enable any time with /paid-enable-group).")
            return {"action": "skip", "reason": "paid_group_disabled"}

        if cmd == "/paid-set-group-mode":
            if not args:
                _send_group_reply(platform, sender_id, chat_id,
                                  "PAID: usage /paid-set-group-mode "
                                  "<review-only|everyday|both>")
                return {"action": "skip", "reason": "paid_group_set_mode_no_arg"}
            new_mode = args.split(None, 1)[0].lower()
            if new_mode not in ("review-only", "everyday", "both"):
                _send_group_reply(platform, sender_id, chat_id,
                                  f"PAID: unknown mode '{new_mode}'. "
                                  "Use review-only | everyday | both.")
                return {"action": "skip", "reason": "paid_group_bad_mode"}
            cfg = group_routing.load_group_config(group_key)
            if cfg is None:
                _send_group_reply(platform, sender_id, chat_id,
                                  "PAID: this group is not configured. "
                                  "Run /paid-enable-group first.")
                return {"action": "skip", "reason": "paid_group_not_configured"}
            cfg.mode = new_mode
            group_routing.save_group_config(cfg)
            _send_group_reply(platform, sender_id, chat_id,
                              f"✅ Mode updated to '{new_mode}'.")
            return {"action": "skip", "reason": "paid_group_mode_set"}

        if cmd == "/paid-set-group-name":
            if not args:
                _send_group_reply(platform, sender_id, chat_id,
                                  "PAID: usage /paid-set-group-name <display name>")
                return {"action": "skip", "reason": "paid_group_set_name_no_arg"}
            cfg = group_routing.load_group_config(group_key)
            if cfg is None:
                _send_group_reply(platform, sender_id, chat_id,
                                  "PAID: this group is not configured. "
                                  "Run /paid-enable-group first.")
                return {"action": "skip", "reason": "paid_group_not_configured"}
            cfg.display_name = args[:120]
            group_routing.save_group_config(cfg)
            _send_group_reply(platform, sender_id, chat_id,
                              f"✅ Group name set to: {cfg.display_name}")
            return {"action": "skip", "reason": "paid_group_name_set"}

        if cmd == "/paid-group-status":
            cfg = group_routing.load_group_config(group_key)
            if cfg is None:
                _send_group_reply(platform, sender_id, chat_id,
                                  "PAID: this group is not configured.")
                return {"action": "skip", "reason": "paid_group_status_none"}
            status = "enabled" if cfg.enabled else "disabled"
            name_line = f"\nname: {cfg.display_name}" if cfg.display_name else ""
            _send_group_reply(
                platform, sender_id, chat_id,
                f"PAID group status:\n"
                f"key: {cfg.group_key}\n"
                f"status: {status}\n"
                f"mode: {cfg.mode}"
                f"{name_line}\n"
                f"updated_at: {cfg.updated_at or '(never)'}",
            )
            return {"action": "skip", "reason": "paid_group_status_reported"}

    if cmd == "/paid-list-groups":
        configs = group_routing.list_group_configs()
        if not configs:
            _send_group_reply(platform, sender_id, chat_id,
                              "PAID: no groups configured.")
            return {"action": "skip", "reason": "paid_group_list_empty"}
        lines = ["PAID groups:"]
        for c in configs:
            badge = "ON " if c.enabled else "off"
            label = c.display_name or c.group_id
            lines.append(f"  [{badge}] {label} — mode={c.mode}  ({c.group_key})")
        _send_group_reply(platform, sender_id, chat_id, "\n".join(lines))
        return {"action": "skip", "reason": "paid_group_list_reported"}

    return None


def _send_group_reply(platform: str, sender_id: str, chat_id: str,
                      text: str) -> None:
    """Send a group-command reply. Lark/feishu group chats need
    receive_id_type=chat_id, so prefer the chat_id when available; fall
    back to direct user_id otherwise (DM-context list_groups)."""
    target = chat_id or sender_id
    try:
        hermes_io.send_dm(platform, target, text, fallback_to_queue=True)
    except Exception as exc:
        _safe_log(f"[group_cmd] reply send EXC plat={platform} target={target}: {exc}")


# ---------------------------------------------------------------------------
# v1.6.0 setup wizard intercept
# ---------------------------------------------------------------------------


def _handle_setup_command_in_pre_gateway(
    event, platform: str, sender_id: str, text: str,
) -> dict | None:
    """Handle ``/paid-setup`` and ``/paid-resync`` slash commands from owner.

    Returns a ``{"action": "skip", ...}`` dict when handled, else None
    (let other handlers continue). Replies go directly via send_dm so
    hermes' slash dispatcher doesn't also try to handle them.
    """
    stripped = text.lstrip()
    chat_id = ""
    if event is not None and getattr(event, "source", None) is not None:
        chat_id = str(getattr(event.source, "chat_id", "") or "")

    parts = stripped.split(None, 1)
    cmd = parts[0] if parts else ""

    if cmd == "/paid-setup":
        sub = (parts[1].strip() if len(parts) > 1 else "")
        sub_lower = sub.lower()
        if sub_lower in ("cancel", "exit", "quit", "abort"):
            reply = _setup_wizard.cancel(platform, sender_id)
            _send_setup_wizard_reply(platform, sender_id, chat_id, reply)
            return {"action": "skip", "reason": "paid_setup_cancelled"}
        # v1.6.1: "/paid-setup add-doc <url>"
        if sub_lower.startswith("add-doc"):
            url_part = sub[len("add-doc"):].strip()
            if not url_part:
                _send_setup_wizard_reply(
                    platform, sender_id, chat_id,
                    "用法：`/paid-setup add-doc <url>`\n"
                    "例：`/paid-setup add-doc https://example.feishu.cn/docx/abc`",
                )
                return {"action": "skip", "reason": "paid_setup_add_doc_usage"}
            reply = _setup_wizard.start_doc_ingest(platform, sender_id, url_part)
            _send_setup_wizard_reply(platform, sender_id, chat_id, reply)
            return {"action": "skip", "reason": "paid_setup_add_doc"}
        # Start wizard (first-time or edit mode)
        reply = _setup_wizard.start(platform, sender_id)
        _send_setup_wizard_reply(platform, sender_id, chat_id, reply)
        return {"action": "skip", "reason": "paid_setup_started"}

    if cmd == "/paid-resync":
        reply = _setup_wizard.resync()
        _send_setup_wizard_reply(platform, sender_id, chat_id, reply)
        return {"action": "skip", "reason": "paid_resync_done"}

    return None


def _send_setup_wizard_reply(
    platform: str, sender_id: str, chat_id: str, text: str,
) -> None:
    """Send wizard reply. Owner DM (not group) is the primary path.
    chat_id used when platform routes via chat_id (Lark)."""
    target = chat_id or sender_id
    try:
        hermes_io.send_dm(platform, target, text, fallback_to_queue=True)
    except Exception as exc:
        _safe_log(
            f"[setup_wizard] reply send EXC plat={platform} "
            f"target={target}: {exc}"
        )


# ---------------------------------------------------------------------------
# Telegram inline-keyboard button callback routing (M3.5.C)
#
# Goal: owner clicks ✅ Approve / ❌ Reject on the TG approval card and PAID
# actually acts on it — instead of falling back to ``/paid-approve <id>``
# slash command, which was the v1.2 UX gap (5/4 dogfood: buttons rendered
# but click did nothing).
#
# Approach (design 08 §1 path C): lazy-grab the live ``adapter._app``
# (python-telegram-bot Application) on first hook fire for platform=telegram
# and add our own ``CallbackQueryHandler`` filtered to ``^paid_``.
#
# Trade-offs:
#   - We don't fork hermes or wait on upstream. Same compatibility bet as
#     ``send_telegram_card`` which already reaches into ``adapter._bot``.
#   - We register in PTB ``group=-1`` so we run BEFORE hermes' catch-all
#     ``_handle_callback_query`` (group 0). After we handle a paid_* event,
#     the catch-all still runs but matches no prefix branch and no-ops.
#   - Registration is double-checked-lock idempotent. Failure is loud
#     (fatal_alert + log) — never silent, per memory feedback_im_bot_api_traps.
#   - Approve/reject dispatch off the event loop via run_in_executor so the
#     blocking ``send_dm`` (which uses ``fut.result()`` on the same loop)
#     can't deadlock.
# ---------------------------------------------------------------------------

_CALLBACK_LOCK = threading.Lock()
_callback_registered: dict[str, bool] = {"telegram": False}


def _ensure_telegram_callback_registered() -> None:
    """Best-effort, idempotent. Attach PAID's CallbackQueryHandler to the
    live TG adapter so paid_* button clicks route back to PAID.

    Called on every TG-platform pre_llm_call / pre_gateway_dispatch — the
    first successful call wins; subsequent calls are O(1) flag check.

    Failures (no adapter, no _app, python-telegram-bot missing, add_handler
    raises) set the flag anyway and emit a fatal_alert so the operator
    sees that buttons WON'T work this run, and PAID falls back to slash
    commands. We don't keep retrying because retrying a broken import on
    every inbound message is just noise.
    """
    if _callback_registered.get("telegram"):
        return
    with _CALLBACK_LOCK:
        if _callback_registered.get("telegram"):
            return

        # Locate live adapter
        try:
            adapter = hermes_io._get_gateway_adapter("telegram")
        except hermes_io.SendDmError:
            # Gateway not ready yet OR telegram adapter not loaded — keep
            # retrying on subsequent hooks (don't set the flag).
            return
        except Exception as exc:
            _safe_log(f"[tg-callback] adapter lookup EXC: {exc}")
            return

        app = getattr(adapter, "_app", None)
        if app is None:
            # Adapter exists but PTB Application not built yet (race between
            # adapter constructor and connect()). Retry next hook.
            return

        # Locate PTB CallbackQueryHandler — module is vendored with hermes.
        try:
            from telegram.ext import CallbackQueryHandler  # type: ignore
        except Exception as exc:
            _callback_registered["telegram"] = True  # don't keep retrying a broken import
            detail = (
                f"python-telegram-bot CallbackQueryHandler import failed: {exc}. "
                f"TG button clicks will NOT route to PAID this run; use "
                f"/paid-approve / /paid-reject slash commands. Upgrade hermes "
                f"or its python-telegram-bot dep."
            )
            _safe_log(f"[tg-callback] ⚠️  {detail}")
            try:
                _alert_owner(reason="paid_tg_callback_register", detail=detail)
            except Exception:
                pass
            return

        try:
            app.add_handler(
                CallbackQueryHandler(_on_paid_telegram_callback, pattern=r"^paid_"),
                group=-1,
            )
            _callback_registered["telegram"] = True
            _safe_log(
                "[tg-callback] registered paid_* CallbackQueryHandler on "
                "adapter._app (group=-1 — runs before hermes catch-all)"
            )
        except Exception as exc:
            _callback_registered["telegram"] = True
            detail = (
                f"add_handler raised: {exc}. TG button clicks will NOT route "
                f"to PAID this run; use slash commands instead."
            )
            _safe_log(f"[tg-callback] ⚠️  {detail}")
            try:
                _alert_owner(reason="paid_tg_callback_register", detail=detail)
            except Exception:
                pass


def _parse_paid_callback_data(data: str) -> tuple[str, str] | None:
    """Parse ``paid_<action>:<rid>`` callback_data. Returns (action, rid)
    or None for malformed input.

    Whitelisted actions only: approve / reject / reply / edit (legacy
    alias for reply, accepted for cards rendered before v1.4 rename) /
    opt. Anything else is treated as malformed so a future prefix
    collision can't accidentally fire approval logic.
    """
    if not data or not data.startswith("paid_"):
        return None
    body = data[len("paid_"):]
    if ":" not in body:
        return None
    action, rid = body.split(":", 1)
    action = action.strip().lower()
    rid = rid.strip()
    if not action or not rid:
        return None
    if action not in {"approve", "reject", "reply", "edit", "opt"}:
        return None
    return action, rid


async def _on_paid_telegram_callback(update, context) -> None:
    """PTB CallbackQueryHandler for paid_* inline-keyboard buttons.

    Owner-gates by ``query.from_user.id`` against owner.json identities —
    independent of the env-var gating used for slash commands.

    Acks the click immediately (``query.answer()``) so the TG spinner
    clears, then runs the actual approve/reject dispatch off the event
    loop via ``run_in_executor`` to avoid deadlocking on hermes_io.send_dm.
    On completion, edits the original card to show the resolution and
    removes the inline keyboard.
    """
    query = update.callback_query
    if not query or not query.data:
        return

    data = query.data
    user_id = str(getattr(query.from_user, "id", ""))

    # Owner check — independent from slash command env-var path.
    if not identity.is_owner("telegram", user_id):
        try:
            await query.answer(text="⛔ Not authorized.")
        except Exception:
            pass
        _safe_log(f"[tg-callback] unauthorized click data={data!r} tg_user={user_id}")
        return

    parsed = _parse_paid_callback_data(data)
    if parsed is None:
        _safe_log(f"[tg-callback] malformed data={data!r}")
        try:
            await query.answer()
        except Exception:
            pass
        return
    action, rid = parsed

    # Ack first so the TG client spinner clears even on slow paths.
    try:
        await query.answer()
    except Exception:
        pass

    # Dispatch the sync handler off-loop to avoid deadlocking on
    # hermes_io.send_dm, which uses run_coroutine_threadsafe + .result()
    # on the same gateway loop we're running on.
    loop = asyncio.get_event_loop()
    try:
        if action == "approve":
            # Mirrors Lark _cmd_card: direct dispatch, with a language-
            # matched default agreement when the classifier didn't ground
            # a draft. ✏️ Reply is the path for custom answers.
            req_obj = approval.get(rid)
            override = ""
            if req_obj is not None and not (req_obj.draft_answer or "").strip():
                override = _default_approve_text(req_obj)
            result_text = await loop.run_in_executor(
                None, lambda: _do_approve(rid, override_text=override)
            )
        elif action == "reject":
            result_text = await loop.run_in_executor(None, lambda: _do_reject(rid))
        elif action in ("reply", "edit"):
            # ✏️ Reply ("edit" still accepted as legacy alias for cards
            # rendered before v1.4 rename). Arm awaiting_input scoped to
            # this owner's TG chat; on_pre_gateway_dispatch consumes the
            # next plain-text inbound from the same chat.
            expected_chat_id = (
                str(getattr(query.message, "chat_id", "") or "") or None
            )
            _record_awaiting_input(rid, expected_chat_id=expected_chat_id)
            junior_label = "the requester"
            req_obj = approval.get(rid)
            if req_obj is not None:
                junior_label = (
                    req_obj.counterparty_display
                    or req_obj.counterparty_user_id
                    or junior_label
                )
            result_text = (
                f"✏️ #{rid} 等你输入答复给 {junior_label}（30 分钟内有效）。\n"
                f"接下来你在本聊天发的下一条普通文字会作为答复转给 ta。\n"
                f"发 /paid-cancel-input 取消，或 /paid-reject {rid} 拒绝。"
            )
        else:
            # _parse_paid_callback_data whitelists; this branch is defensive.
            _safe_log(f"[tg-callback] unhandled action={action!r}")
            return
    except Exception as exc:
        _safe_log(f"[tg-callback] dispatch EXC action={action} rid={rid}: {exc}")
        result_text = f"PAID: #{rid} — internal error while handling {action}: {exc}"

    _safe_log(f"[tg-callback] handled action={action} rid={rid} → {result_text[:120]!r}")

    # Update the card to reflect resolution + remove keyboard. Fall back to
    # a fresh message if edit fails (e.g. message > 48h old, original
    # deleted).
    original_text = ""
    try:
        original_text = (query.message.text or "")
    except Exception:
        pass
    new_text = (
        (original_text + "\n\n— — —\n" if original_text else "")
        + result_text
    )
    try:
        await query.edit_message_text(text=new_text, reply_markup=None)
    except Exception as exc:
        _safe_log(f"[tg-callback] edit_message_text failed: {exc}; sending fallback")
        try:
            chat_id = query.message.chat_id if query.message else user_id
            await context.bot.send_message(chat_id=chat_id, text=result_text)
        except Exception as exc2:
            _safe_log(f"[tg-callback] fallback send_message also failed: {exc2}")


def _cmd_card(raw_args: str) -> str:
    """Handle Lark card-button clicks routed by hermes as ``/card <tag> <json>``.

    The feishu adapter (``gateway/platforms/feishu.py::_handle_card_action_event``)
    creates a synthetic command of the form::

        /card button {"paid_action":"approve","request_id":"abc12345"}

    when the operator clicks one of our approval card's buttons.

    Why this returns ``""`` instead of a status string
    ---------------------------------------------------
    Pre-v1.4.0, this handler returned a status string ("PAID: #rid
    approved → delivered to ...") relying on hermes to deliver that
    return value back to the owner's Lark chat. VPS evidence (8/8
    historical clicks logged but zero visible owner-side response)
    showed that path is unreliable for synthetic commands. We now
    always return ``""`` and push every outcome via ``send_dm`` directly.

    Case 1 vs case 2
    ----------------
    Click ✅ Approve:
      - Draft is non-empty (PAID could ground a reply from SOP) → dispatch
        immediately to the junior; DM the owner with a confirmation.
      - Draft is empty (J3 out-of-scope / sensitive topic) → arm an
        "awaiting input" slot for the owner and DM them an inline prompt
        asking for their reply. The owner's next plain-text message in
        the same chat (intercepted in :func:`on_pre_gateway_dispatch`)
        becomes the answer.

    Click ❌ Reject:
      - Always direct: dispatch the deflection text to the junior and
        DM the owner with a confirmation.

    Click ✏️ Edit:
      - Same as approve case 2: arm awaiting input + DM inline prompt.
    """
    if not _is_caller_owner_via_env():
        return ""  # silent for non-owners
    if not raw_args.strip():
        return ""

    parts = raw_args.strip().split(maxsplit=1)
    if len(parts) < 2:
        return ""  # not enough: missing JSON payload — ignore quietly
    payload_str = parts[1]

    try:
        payload = json.loads(payload_str)
    except Exception as exc:
        _safe_log(f"[card] JSON parse fail: {exc} payload={payload_str[:200]!r}")
        return ""
    if not isinstance(payload, dict):
        return ""

    action = str(payload.get("paid_action") or payload.get("action") or "").strip().lower()
    rid = str(payload.get("request_id") or "").strip()
    if not action or not rid:
        return ""

    req = approval.get(rid)
    if req is None:
        _send_owner_inline_message(f"PAID: unknown request id #{rid}")
        return ""
    if req.status != "pending":
        _send_owner_inline_message(
            f"PAID: #{rid} already {req.status} (resolved at {req.ts_resolved})."
        )
        return ""

    junior_label = (req.counterparty_display or req.counterparty_user_id or "the sender")

    if action == "approve":
        # ✅ Approve always means "yes, agree" — dispatch immediately to
        # the junior. If the classifier didn't draft a reply (J3 cases
        # where SOP doesn't cover), fall back to a language-matched
        # default agreement ("可以的" / "Approved") so the click ALWAYS
        # does something useful. Owner can use ✏️ Reply for a custom
        # answer instead.
        override = ""
        if not (req.draft_answer or "").strip():
            override = _default_approve_text(req)
        result_text = _do_approve(rid, override_text=override)
        _send_owner_inline_message(f"✅ #{rid} approved.\n{result_text}")
        _safe_log(
            f"[card] approve #{rid} "
            f"({'draft' if not override else 'default-agree'}) → "
            f"{result_text[:120]!r}"
        )
        return ""

    if action in ("reply", "edit"):
        # ✏️ Reply (legacy alias: "edit") — the only path that asks the
        # owner to type a custom response. Arms an in-memory slot
        # consumed by on_pre_gateway_dispatch when owner's next plain
        # text arrives.
        _record_awaiting_input(rid)
        _send_owner_inline_message(
            f"✏️ #{rid} 等你输入答复给 {junior_label}（30 分钟内有效）。\n"
            f"\n"
            f"⚠️ 接下来你在本聊天发的下一条普通文字会作为答复转给 ta。\n"
            f"如果暂时不想回，发 /paid-cancel-input 取消，"
            f"或发 /paid-reject {rid} 拒绝。"
        )
        _safe_log(f"[card] reply #{rid} — armed awaiting_input")
        return ""

    if action == "reject":
        result_text = _do_reject(rid)
        _send_owner_inline_message(f"❌ #{rid} rejected.\n{result_text}")
        _safe_log(f"[card] reject #{rid} → {result_text[:120]!r}")
        return ""

    _safe_log(f"[card] unknown paid_action={action!r} for #{rid}")
    return ""


def _cmd_paid_cancel_input(raw_args: str) -> str:
    """``/paid-cancel-input`` — clear any pending awaiting_input slot for
    the calling owner.

    Useful when the owner armed inline input by clicking ✅ / ✏️ but
    decided not to reply right now — they don't want their next casual
    message accidentally captured as the answer.
    """
    if not _is_caller_owner_via_env():
        return ""
    owner = identity.load_owner()
    keys = _awaiting_keys_for_owner(owner)
    rids_cleared: set[str] = set()
    for key in keys:
        entry = _AWAITING_INPUT.pop(key, None)
        if entry is not None:
            rids_cleared.add(entry["rid"])
    if not rids_cleared:
        return "PAID: no pending input."
    return f"PAID: cancelled pending input for #{', #'.join(sorted(rids_cleared))}"


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


def _cmd_paid_doctor(raw_args: str) -> str:
    """``/paid-doctor`` — run 7 health checks (v1.5.5 A1).

    On Lark we push an interactive card to the owner's preferred channel
    and return a one-line ack as the slash reply. On TG/Slack/CLI we
    return the plain-text report directly.
    """
    if not _is_caller_owner_via_env():
        return ""
    from paid import doctor as _doctor
    rows = _doctor.run_checks()
    n_pass = _doctor.n_passed(rows)
    n_total = len(rows)
    summary = f"PAID doctor: {n_pass}/{n_total} checks passed"

    plat = os.environ.get("HERMES_GATEWAY_PLATFORM", "").strip().lower()
    if plat in ("feishu", "lark"):
        # Push a Lark card; the slash reply becomes a short ack.
        try:
            owner = identity.load_owner()
            pref = owner.preferred_identity() if owner else None
            if pref is None:
                target = _owner_primary_identity(owner)
                if target is None:
                    return _doctor.format_plain_text(rows)  # fall back to text
                pref_plat, uid = target
            else:
                pref_plat = pref.platform
                uid = pref.home_chat_id
            if pref_plat in ("feishu", "lark"):
                receive_target = _resolve_owner_send_target(pref_plat, uid)
                card = card_formatters.format_doctor_card_lark(rows)
                hermes_io.send_lark_card(
                    pref_plat, receive_target, card, fallback_to_queue=True,
                )
                _safe_log(f"[doctor] pushed Lark card to {pref_plat}:{receive_target} ({summary})")
                fail_ids = [r["id"] for r in rows if not r.get("ok")]
                if fail_ids:
                    return f"{summary} (failing: {', '.join(fail_ids)}) — see card."
                return f"{summary} — see card."
        except Exception as exc:
            _safe_log(f"[doctor] Lark card path failed: {exc}; falling back to text")
            # fall through to plain-text return

    return _doctor.format_plain_text(rows)


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

_MINIMUM_HERMES_VERSION = "0.11.0"


def _check_hermes_capability(ctx) -> dict:
    """Verify the running hermes exposes the plugin surface PAID needs.

    PAID's owner-facing CLI (5 slash commands) was wired in v0.5+ on top of
    hermes v0.11's ``register_command`` plugin API (#10626). Before v0.11,
    that method did not exist — PAID would silently NOT register its
    /paid-pending /paid-approve /paid-reject /paid-status /card commands,
    leaving the owner with a J3 dead-end and no obvious failure signal.

    Returns a dict with the diagnostic. Caller decides whether to alert.
    """
    has_register_command = hasattr(ctx, "register_command")
    has_register_hook = hasattr(ctx, "register_hook")
    return {
        "has_register_command": has_register_command,
        "has_register_hook": has_register_hook,
        "hermes_version_ok": has_register_command and has_register_hook,
        "minimum_required": _MINIMUM_HERMES_VERSION,
    }


def register(ctx) -> None:
    storage.ensure_dirs()
    _safe_log("=" * 60)
    _safe_log(f"PAID v1 plugin registering (path: {ctx.manifest.path})")

    # Capability check — refuse to half-load on too-old hermes.
    cap = _check_hermes_capability(ctx)
    if not cap["hermes_version_ok"]:
        msg = (
            f"hermes is missing required plugin surface for PAID: "
            f"register_command={cap['has_register_command']} "
            f"register_hook={cap['has_register_hook']}; "
            f"need hermes >= {_MINIMUM_HERMES_VERSION}. "
            f"PAID will skip slash-command registration; owner CLI "
            f"(/paid-pending /paid-approve /paid-reject /paid-status /card) "
            f"will be unavailable. Upgrade hermes-agent to v0.11+ to enable "
            f"the full PAID surface."
        )
        _safe_log(f"⚠️  {msg}")
        try:
            _alert_owner(reason="paid_minimum_hermes_version", detail=msg)
        except Exception:
            # _alert_owner depends on hermes-side bits; if even that fails on
            # the very-old hermes, the file log + jsonl entry are the
            # forensic record.
            pass
        if not cap["has_register_hook"]:
            # Without register_hook we can't even wire J2 — bail out.
            return
        # else: fall through and register hooks; just skip slash commands.

    # pre_gateway_dispatch (hermes 0.12.0+): early-exit on prompt-injection
    # before the LLM is ever invoked. Older hermes versions ignore the
    # registration if the hook isn't in their VALID_HOOKS set, so this is
    # safe to call unconditionally.
    try:
        ctx.register_hook("pre_gateway_dispatch", on_pre_gateway_dispatch)
        _safe_log("registered: pre_gateway_dispatch")
    except Exception as exc:
        _safe_log(f"pre_gateway_dispatch registration skipped: {exc}")

    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("post_llm_call", on_post_llm_call)

    if cap["has_register_command"]:
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
        ctx.register_command(
            "paid-cancel-input", _cmd_paid_cancel_input,
            description="Cancel a pending inline-input slot (after clicking ✅/✏️ on a card).",
        )
        ctx.register_command(
            "paid-doctor", _cmd_paid_doctor,
            description="Run PAID health checks (7 items: config, owner, hermes, timers, files, settings, errors).",
        )
        # `/card` intercepts hermes feishu adapter's synthetic command for
        # interactive-card button clicks. Lark sends button click events as
        # ``/card button {json}``; we parse and route to approve/reject.
        try:
            ctx.register_command(
                "card", _cmd_card,
                description="(internal) Lark card button click handler — used by PAID interactive cards.",
            )
            _safe_log("registered: /card (Lark interactive card handler)")
        except Exception as exc:
            _safe_log(f"/card registration skipped: {exc}")

        # /review + /r are routed via pre_gateway_dispatch (see
        # on_pre_gateway_dispatch), NOT registered as hermes slash
        # commands. Reason: hermes' register_command handler signature
        # is fn(raw_args:str)->str — no sender context. pre_gateway_dispatch
        # IS given the full MessageEvent with platform + user_id, which
        # we need to resolve the counterparty correctly. (v1.3.3 first
        # attempted register_command + env-var lookup; the env vars
        # don't actually exist in hermes — caught in second dogfood.)

        _safe_log("hooks: pre_llm_call, post_llm_call, pre_gateway_dispatch")
        _safe_log("commands: /paid-pending /paid-approve /paid-reject /paid-status /paid-cancel-input /paid-doctor /card")
        _safe_log("pre_gateway_dispatch routes: /review /r (paid_review skill, cp-side)")
    else:
        _safe_log("hooks: pre_llm_call, post_llm_call (commands skipped — hermes < 0.11)")

    # v1.4.2: dry-run the classifier LLM path at startup so silent-failure
    # mode (yaml missing model.api_key, env var missing) is surfaced
    # immediately as a fatal_alert + owner DM, instead of every cp message
    # falling through to fallback `request` while owner wonders why
    # auto-answer isn't firing. JELabs pilot 2026-05-13 root cause.
    _classifier_health_check()


def _classifier_health_check() -> None:
    """Best-effort one-shot LLM ping; failure → loud fatal_alert + owner DM.

    Why: PAID classifier reads ``config.yaml model.api_key`` (now also env
    vars per v1.4.2). If neither is set, every cp message lands in the
    ``[fallback]`` branch — owner sees no auto-answer, no error in any
    UI, just silent degradation. Detect it at register-time, surface
    once, let operator fix before pilot suffers.

    All paths wrapped in try/except: the health-check MUST NOT raise into
    plugin registration. Failure is informational, not fatal to startup.
    """
    try:
        from paid.hermes_io import call_llm, HermesConfigError
    except Exception as exc:
        _safe_log(
            f"[health-check] cannot import hermes_io ({exc}); classifier ping skipped"
        )
        return
    try:
        reply = call_llm("ping", system="Reply with the single word: ok")
        _safe_log(
            f"[health-check] classifier dry-run OK (reply preview: {reply[:60]!r})"
        )
    except HermesConfigError as exc:
        msg = (
            "PAID classifier dry-run FAILED — all cp messages will fall through "
            "to fallback `request` (every inbound becomes an approval card). "
            f"Root cause: {exc}"
        )
        _safe_log(f"[health-check] {msg}")
        try:
            _alert_owner(reason="classifier_config_invalid", detail=str(exc))
        except Exception as alert_exc:
            _safe_log(
                f"[health-check] _alert_owner also failed: {alert_exc}"
            )
    except Exception as exc:
        # LLM call itself failed (network, 4xx from provider, etc.).
        msg = f"PAID classifier dry-run FAILED at LLM call: {exc}"
        _safe_log(f"[health-check] {msg}")
        try:
            _alert_owner(
                reason="classifier_llm_unreachable", detail=str(exc)[:500]
            )
        except Exception as alert_exc:
            _safe_log(
                f"[health-check] _alert_owner also failed: {alert_exc}"
            )
