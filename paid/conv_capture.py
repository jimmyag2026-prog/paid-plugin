"""Module CC — Conversation Capture for Owner Profile (v1.6.2).

Detects when an owner's chat message implies a profile update
(e.g. "以后别说'按规定'" → voice.do_not_say, "客户问题 2 小时内回复" →
observed.preferred_decision_window_hrs) and offers a confirm prompt.

Design:
  1. Fast regex pre-filter runs on every owner message. If no trigger
     pattern, skip entirely (no LLM, zero latency cost).
  2. If a trigger pattern fires, call LLM with a compact extraction
     prompt. Rate-limited to once per 30 s per owner to avoid spam.
  3. Extracted proposals are offered to owner via DM confirm card
     (same format as doc_ingest, reuses parse_confirm_reply / apply_proposals).
  4. Owner replies yes/no/indices → apply → derive.

Integration point: called from ``on_pre_llm_call`` in ``__init__.py``,
only for owner-originating messages. cp messages are NEVER analyzed here.

Sensitive data:
  - NEVER extracted (same allowed-fields list as doc_ingest).
  - URL sharing by owner is routed to doc_ingest, not this module.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate limit: don't trigger extraction more than once per 30s per owner
# ---------------------------------------------------------------------------

_LAST_EXTRACT_TS: dict[str, float] = {}   # keyed by "{platform}:{owner_id}"
_EXTRACT_COOLDOWN_SEC: float = 30.0

# ---------------------------------------------------------------------------
# Trigger patterns — fast regex pre-filter (no LLM if no match)
#
# Patterns are intentionally broad; false positives are filtered by the LLM
# extraction step. False negatives just mean we miss an update (acceptable).
# ---------------------------------------------------------------------------

_TRIGGER_PATTERNS: list[re.Pattern] = [re.compile(p, re.IGNORECASE) for p in [
    # "以后别/不要/不用 X"
    r"以后.{0,10}别",
    r"以后.{0,10}不要",
    r"以后.{0,10}请",
    r"以后.{0,10}记得",
    # v1.6.9: "以后 X 直接/一律 拒/拒绝/不回/不答" — the natural Chinese
    # SOP-update phrasing that v1.6.2 missed entirely. The user's actual
    # live test "以后客户问 pricing 直接拒绝，我不想看" failed to fire any
    # pre-v1.6.9 pattern because no preset literal sat within 10 chars
    # of "以后" and the topic-routing regex required "我" within 10 chars
    # of an action verb (which it isn't in this phrasing).
    r"以后.{0,30}(?:直接|一律|马上|立刻).{0,15}(?:拒|拒绝|不回|不答|不理|不要回)",
    r"以后.{0,30}(?:都|全部|一律|统统).{0,15}(?:拒|拒绝|不回|不答|不理)",
    r"以后.{0,40}(?:不想|不愿|懒得|没空).{0,10}(?:看|理|回|应)",
    # "记住 X"
    r"记住.{0,20}",
    # English equivalents
    r"\bremember\b",
    r"\balways\b.{0,15}",
    r"\bnever\b.{0,15}",
    r"\bdon'?t\b.{0,15}",
    r"\bplease\b.{0,10}\b(always|never|don'?t)\b",
    r"\bfrom now on\b",
    # v1.6.9: "from now on X just reject" / "always decline"
    r"\b(?:from now on|going forward)\b.{0,40}\b(?:reject|decline|refuse|ignore|skip)\b",
    r"\b(?:always|never|just|please)\b.{0,15}\b(?:decline|reject|refuse|skip|ignore)\b",
    # Time windows
    r"\d+\s*(?:小时|hr|hour|分钟|min|minute).{0,10}(?:内|以内|以后|within|reply|回)",
    # Tone hints
    r"(?:语气|tone|风格|style).{0,20}",
    r"(?:短一点|简短|别废话|brief|shorter|concise)",
    # Phrase bans
    r"别说.{1,30}",
    r"不要说.{1,30}",
    r"禁止说.{1,30}",
    r"避免.{0,10}(?:说|用|提)",
    r"\bavoid\b.{0,20}\b(?:saying|using|mentioning)\b",
    # Topic routing hints
    r"(?:这类|这种).{0,10}(?:问题|话题|事情).{0,10}(?:直接|必须|一律)",
    r"(?:客户|投资|招聘|薪资|finances?).{0,20}(?:转|找|告诉|问).{0,10}我",
]]


def should_scan(text: str) -> bool:
    """True iff the message text contains at least one trigger pattern."""
    if not text or len(text) < 6:
        return False
    for pat in _TRIGGER_PATTERNS:
        if pat.search(text):
            return True
    return False


def is_rate_limited(platform: str, owner_id: str) -> bool:
    """True iff we extracted for this owner within the last 30s."""
    key = f"{platform}:{owner_id}"
    last = _LAST_EXTRACT_TS.get(key, 0.0)
    return (time.time() - last) < _EXTRACT_COOLDOWN_SEC


def _mark_extracted(platform: str, owner_id: str) -> None:
    key = f"{platform}:{owner_id}"
    _LAST_EXTRACT_TS[key] = time.time()


def _clear_rate_limit_for_tests() -> None:
    _LAST_EXTRACT_TS.clear()


# ---------------------------------------------------------------------------
# LLM extraction
# ---------------------------------------------------------------------------

_CC_SYSTEM = """\
You are a profile update extractor for an AI delegation system (PAID).
An owner said something in a chat message that may imply they want to
update their AI delegate's behaviour profile.

Allowed fields to extract:
  voice.do_not_say       — phrases to avoid (list of strings)
  voice.tone             — "direct-friendly" | "professional" | "casual" | "minimal" | freeform
  voice.style_notes      — freeform style notes (string)
  topics.always_escalate — topics that always need owner approval (list)
  topics.always_direct   — topics bot can handle without approval (list)
  topics.always_decline  — topics bot should reject outright without escalating (list)
  preferred_language     — "zh" | "en" | "ko" | "auto"
  preferences.daily_cost_cap_usd — number
  observed.preferred_decision_window_hrs — number

Distinguishing escalate vs decline:
- always_escalate = "bother the owner, let them decide each time"
  (e.g. "客户问 X 一定要告诉我")
- always_decline  = "reject outright, do NOT bother me about this again"
  (e.g. "客户问 X 直接拒绝，我不想看" / "ignore these going forward")

RULES:
- Extract ONLY if the owner's message explicitly states or very strongly implies a change.
- Return [] if message is casual chat with no profile-update intent.
- Do NOT include credentials, tokens, API keys, passwords, or email addresses.
- Rationale must quote the original phrasing.
- Return valid JSON array of {field, proposed, rationale}.
"""

_CC_USER_TMPL = """\
Owner message: {message}

Current profile snapshot:
{profile_summary}

Return JSON array of proposed profile updates ([] if none):
"""


def extract_from_message(
    text: str,
    profile: Any,
    platform: str,
    owner_id: str,
) -> list:
    """Run LLM extraction on an owner message. Returns list[UpdateProposal].

    Returns [] on rate limit, no trigger match, LLM error, or empty result.
    """
    from . import doc_ingest as _di
    from . import hermes_io

    if not should_scan(text):
        return []
    if is_rate_limited(platform, owner_id):
        logger.debug("conv_capture: rate limited for %s:%s", platform, owner_id)
        return []

    _mark_extracted(platform, owner_id)

    profile_summary = _di._summarize_profile(profile)
    prompt = _CC_USER_TMPL.format(message=text, profile_summary=profile_summary)
    try:
        # v1.6.9 fix: hermes_io.call_llm takes (prompt, system=...), NOT
        # messages=[]. Pre-v1.6.9 every invocation raised TypeError which
        # the broad except clause below swallowed → silently returned [].
        # Result: conv_capture LLM extraction never ran in production since
        # v1.6.2 shipped. Same bug exists in doc_ingest (also fixed).
        raw = hermes_io.call_llm(
            prompt=prompt,
            system=_CC_SYSTEM,
            temperature=0.1,
        )
    except Exception as e:
        logger.warning("conv_capture: LLM error: %s", e)
        return []

    proposals = _di._parse_proposals(raw, profile)
    return proposals


# ---------------------------------------------------------------------------
# Pending confirm state — lightweight, no threading needed
# (pre_llm_call is sequential within a single request; state is short-lived)
# v1.6.6: TTL prune added so an owner who never replies doesn't leak the
# entry forever (mirrors setup_wizard's _WIZARD_STATE TTL behaviour).
# ---------------------------------------------------------------------------

_PENDING_TTL_SEC = 60 * 60  # 1 hour — owner has this long to reply

# Keyed by (platform, owner_id) → (proposals, stored_at_unix_ts)
_PENDING: dict[tuple[str, str], tuple[list, float]] = {}


def _prune_expired_pending() -> None:
    """Drop entries older than _PENDING_TTL_SEC. Uses time.time() (Unix epoch)
    to stay consistent with setup_wizard's TTL clock — see v1.6.1 incident
    where monotonic/time mixing prune-killed states on every read."""
    cutoff = time.time() - _PENDING_TTL_SEC
    stale = [k for k, (_, ts) in _PENDING.items() if ts < cutoff]
    for k in stale:
        _PENDING.pop(k, None)


def store_pending(platform: str, owner_id: str, proposals: list) -> None:
    _prune_expired_pending()
    _PENDING[(platform, owner_id)] = (proposals, time.time())


def pop_pending(platform: str, owner_id: str) -> list:
    _prune_expired_pending()
    entry = _PENDING.pop((platform, owner_id), None)
    if entry is None:
        return []
    proposals, _ts = entry
    return proposals


def has_pending(platform: str, owner_id: str) -> bool:
    _prune_expired_pending()
    return (platform, owner_id) in _PENDING


def clear_pending_for_tests() -> None:
    _PENDING.clear()


# ---------------------------------------------------------------------------
# Format confirm prompt (reuses doc_ingest's format + parse)
# ---------------------------------------------------------------------------

def format_confirm(proposals: list) -> str:
    """DM-ready confirm prompt for conversation-captured updates."""
    from . import doc_ingest as _di
    if not proposals:
        return ""
    header = "💡 我注意到你可能想更新以下 profile 设置，确认一下？\n"
    body = _di.format_confirm_prompt(proposals)
    # Replace the generic header from format_confirm_prompt
    body = body.replace(
        "📋 从文档提取到以下 profile 更新建议，回复序号确认（`all` = 全接受，`none` = 全拒绝）：",
        "",
    ).lstrip()
    return header + body


def apply_confirmed(
    platform: str,
    owner_id: str,
    reply: str,
) -> str:
    """Owner replied to a capture confirm. Apply, clear pending, return summary."""
    from . import doc_ingest as _di
    from . import profile as _profile

    proposals = pop_pending(platform, owner_id)
    if not proposals:
        return "没有待确认的 profile 更新。"

    accepted_indices = _di.parse_confirm_reply(reply, len(proposals))
    for i, prop in enumerate(proposals):
        prop.accepted = (i + 1) in accepted_indices

    prof = _profile.load_profile()
    if prof is None:
        return "profile 丢失，请重新 `/paid-setup`。"

    _di.apply_proposals(prof, proposals)
    n = sum(1 for p in proposals if p.accepted)
    return f"✓ 接受了 {n}/{len(proposals)} 条 profile 更新，已保存。"
