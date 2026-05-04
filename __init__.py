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
    card_formatters,
    card_spec,
    classifier,
    decision,
    hermes_io,
    identity,
    retrieval,
    safety,
    storage,
)


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
            # If owner is on Lark/Feishu and FEISHU_HOME_CHANNEL is set,
            # prefer the chat_id (matches sweep_pending.py heuristic so
            # alerts land in the same surface).
            if plat in ("feishu", "lark"):
                home = (os.environ.get("FEISHU_HOME_CHANNEL") or "").strip()
                if home:
                    uid = home
            short_detail = (detail or "").strip().splitlines()[0][:300]
            body = (
                f"⚠️ PAID fatal alert\n"
                f"reason: {reason}\n"
                f"ts: {ts}\n"
                f"detail: {short_detail}\n"
                f"(see ~/.hermes/paid/fatal_alerts.jsonl for full trace)"
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
            if platform and sender_id:
                try:
                    is_cjk = any(c >= "一" and c <= "鿿" for c in response)
                    correction = (
                        "上一条回复可能包含了不该外发的信息，请忽略；让 Jimmy 直接回复你。"
                        if is_cjk else
                        "Heads-up: my previous reply may have contained sensitive "
                        "information that shouldn't have been sent. Please disregard it "
                        "— I'll let the owner respond directly."
                    )
                    hermes_io.send_dm(platform, sender_id, correction, fallback_to_queue=True)
                except Exception as exc:
                    _safe_log(f"[L4-LEAK] corrective send EXC: {exc}")

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
    """Drop inbound messages that trip the L1 injection regex.

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
        if platform and sender_id and identity.is_owner(platform, sender_id):
            return None

        text = str(getattr(event, "text", "") or "")
        if not text:
            return None

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


def _cmd_card(raw_args: str) -> str:
    """Handle Lark card-button clicks routed by hermes as ``/card <tag> <json>``.

    The feishu adapter creates a synthetic command of the form::

        /card button {"paid_action":"approve","request_id":"abc12345"}

    when the operator clicks one of our approval card's buttons (no
    ``hermes_action`` key, so it falls through to the generic dispatch).
    We parse the JSON, find the matching action, and dispatch to the
    existing ``_cmd_approve`` / ``_cmd_reject`` handlers — preserving a
    single canonical code path.

    Owner gating: hermes only routes the synthetic command when the
    button-clicker is an authorised user; ``_is_caller_owner_via_env``
    re-checks anyway so other plugins / future versions can't accidentally
    let a non-owner trigger an approve.
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

    if action == "approve":
        # Empty-draft handling: hard-blacklist topics (salary / equity / etc.)
        # come through with no classifier-generated draft, since we don't want
        # the LLM speculating on sensitive content. Clicking ✅ in that case
        # has nothing to send. Redirect the owner to the slash form with an
        # explicit override message rather than letting them click into a
        # silent failure.
        req = approval.get(rid)
        if req is not None and req.status == "pending" and not (req.draft_answer or "").strip():
            _safe_log(f"[card] approve clicked on #{rid} but draft empty — asking owner for override")
            return (
                f"PAID #{rid}: this is a sensitive-topic request and PAID didn't draft "
                f"an answer. To approve, reply with your answer:\n"
                f"  /paid-approve {rid} <your reply text>\n"
                f"Or reject with:\n"
                f"  /paid-reject {rid}"
            )
        return _cmd_approve(rid)
    if action == "reject":
        return _cmd_reject(rid)

    _safe_log(f"[card] unknown paid_action={action!r} for #{rid}")
    return ""


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

    _safe_log("hooks: pre_llm_call, post_llm_call, pre_gateway_dispatch")
    _safe_log("commands: /paid-pending /paid-approve /paid-reject /paid-status /card")
