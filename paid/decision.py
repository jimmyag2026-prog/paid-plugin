"""Module D — 3-state decision rules + context shaping for pre_llm_call.

Implements the L5 -> J2 routing logic from the v1 PRD: every junior message
becomes one of three states (direct / request / decline), and the corresponding
context string is what the pre_llm_call hook returns to Hermes.

Two layers of guardrails sit ahead of the classifier-based rules:

1. **Hard topic blacklist**: keyword scan on the raw user message. If the user
   asks about credentials, finances, equity, hiring, legal, etc., we force a
   `request` (escalate) regardless of what the classifier said. Catches the
   "wifi password classified as low-stakes" failure mode where an overconfident
   LLM marks a sensitive topic as in-scope.

2. **Sender language**: shape_context emits Chinese or English copy based on
   the detected language of the user's message, so the junior receives a
   reply in the language they wrote in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


# ---------------------------------------------------------------------------
# Tunables. Loaded from ``~/.hermes/paid/settings.json`` via ``paid.settings``;
# the constant below is a fallback used when settings.json is absent at module
# import time (e.g. unit tests that haven't set up PAID_DIR).
# ---------------------------------------------------------------------------

_CONFIDENCE_THRESHOLD_DIRECT_DEFAULT: float = 0.75


def _confidence_threshold_direct() -> float:
    """Resolve threshold via settings.json if available, else default.
    Lazy-imported so plain ``import paid.decision`` doesn't pull settings/storage
    in test environments that don't need them."""
    try:
        from . import settings as _settings  # noqa: WPS433 — intentionally lazy
        return _settings.confidence_threshold_direct()
    except Exception:
        return _CONFIDENCE_THRESHOLD_DIRECT_DEFAULT


# ---------------------------------------------------------------------------
# Hard global blacklist — applied before classifier rules.
# Keywords that ALWAYS force escalation, regardless of classifier confidence.
# ---------------------------------------------------------------------------

_HARD_BLACKLIST_KEYWORDS: tuple[str, ...] = (
    # credentials
    "password", "passwd", "credential", "api key", "api_key", "secret",
    "token", "ssh key", "private key", "2fa", "otp",
    "密码", "凭证", "密钥", "口令", "登录密码",
    # wifi (review §5 wifi-password example)
    "wifi", "wi-fi", "无线密码", "网络密码",
    # finance / money
    "salary", "compensation", "bonus", "payroll", "invoice", "wire transfer",
    "bank account", "routing number",
    "工资", "薪水", "薪资", "奖金", "报销", "发票", "转账", "银行账户",
    # equity / vesting
    "equity", "vesting", "cliff", "rsu", "option grant", "stock option",
    "期权", "股权", "归属", "兑现",
    # hiring / firing
    "hire", "hiring", "firing", "fire someone", "lay off", "termination",
    "招聘", "解雇", "辞退", "裁员",
    # legal
    "lawsuit", "subpoena", "nda", "non-disclosure", "lawyer", "legal advice",
    "诉讼", "传票", "保密协议", "法律意见", "律师",
    # personal / health
    "medical", "diagnosis", "therapy",
    "病情", "诊断",
)


def _has_blacklist_topic(text: str) -> str | None:
    """Return the matched keyword if `text` triggers the hard blacklist, else None."""
    if not text:
        return None
    lowered = text.lower()
    for kw in _HARD_BLACKLIST_KEYWORDS:
        if kw in lowered:
            return kw
    return None


# ---------------------------------------------------------------------------
# Language detection (CN vs EN) — tiny CJK-ratio heuristic, no deps.
# ---------------------------------------------------------------------------

_CJK_RE = re.compile(r"[一-鿿]")


def detect_lang(text: str) -> str:
    """Return "zh" if text looks Chinese, else "en". Empty → "en"."""
    if not text:
        return "en"
    cjk = len(_CJK_RE.findall(text))
    return "zh" if cjk >= 2 else "en"


# ---------------------------------------------------------------------------
# Action dataclass
# ---------------------------------------------------------------------------


@dataclass
class Action:
    """Output of decide_action()."""

    state: str  # "direct" | "request" | "decline" | "review"
    reason: str  # human-readable rationale (for audit log)


# States the plugin glue must know how to dispatch on. The "review" state
# is a hand-off marker — when set, the plugin should route to the
# paid-review skill instead of using shape_context() output directly.
ACTION_STATES = ("direct", "request", "decline", "review")


def is_review_state(action: "Action") -> bool:
    """True if `action` should hand off to the paid-review skill.

    Centralised so callers don't pattern-match the string literal.
    """
    return action.state == "review"


# ---------------------------------------------------------------------------
# Decision rules
# ---------------------------------------------------------------------------


def _review_min_stakes() -> str:
    """Minimum classifier stakes that allows auto-review trigger.

    Reads ``settings.review.auto_trigger_min_stakes`` (one of ``"low"``,
    ``"medium"``, ``"high"``, ``"off"``); defaults to ``"medium"`` so a
    junior simply venting low-stakes feelings doesn't open a review session.
    Returning ``"off"`` disables auto-review entirely (skill is then only
    triggered by explicit ``/review`` commands).
    """
    try:
        from . import settings as _settings  # lazy
        cfg = _settings.load().get("review", {}) if hasattr(_settings, "load") else {}
        val = str(cfg.get("auto_trigger_min_stakes", "medium")).lower()
        if val in {"low", "medium", "high", "off"}:
            return val
    except Exception:
        pass
    return "medium"


def _stakes_meets(stakes: str, threshold: str) -> bool:
    """True when *stakes* >= *threshold* in the low<medium<high ordering."""
    order = {"low": 0, "medium": 1, "high": 2}
    return order.get(stakes, 1) >= order.get(threshold, 1)


def decide_action(
    classification: Any,
    counterparty: Any,
    user_message: str = "",
) -> Action:
    """Apply the 4-state rules to classifier output.

    Order:
      0. Hard global blacklist on user_message     -> request (escalate)
      1. is_blacklisted=True                       -> decline
      1.5 needs_review=True AND stakes >= min      -> review (hand off)
      2. in_scope=False                            -> request
      3. stakes=="high"                            -> request
      4. confidence > threshold AND stakes=="low" AND in_scope=True -> direct
      5. otherwise                                 -> request (conservative)

    The "review" branch (1.5) is intentionally placed BEFORE the in_scope
    check: a draft / proposal that needs structured review is worth opening
    a session for even if the topic isn't on the counterparty's allow-list,
    since the review session itself involves the owner. is_blacklisted still
    wins so a "review my equity grant" ask still declines on rule 1.

    `classification` and `counterparty` are duck-typed; we only read attrs.
    """
    # 0. Hard global blacklist — bypass classifier judgment entirely.
    matched = _has_blacklist_topic(user_message)
    if matched is not None:
        return Action(
            state="request",
            reason=f"hard blacklist keyword match: {matched!r}",
        )

    # Defensive attribute access — classifier output is duck-typed.
    is_blacklisted = bool(getattr(classification, "is_blacklisted", False))
    in_scope = bool(getattr(classification, "in_scope", False))
    stakes = str(getattr(classification, "stakes", "medium")).lower()
    confidence = float(getattr(classification, "confidence", 0.0))
    needs_review = bool(getattr(classification, "needs_review", False))

    if is_blacklisted:
        return Action(state="decline", reason="counterparty blacklisted topic")

    # 1.5 review hand-off (only if review subsystem is enabled).
    if needs_review:
        threshold = _review_min_stakes()
        if threshold != "off" and _stakes_meets(stakes, threshold):
            return Action(
                state="review",
                reason=(
                    f"needs_review=True, stakes={stakes} >= "
                    f"min={threshold}; hand off to paid-review skill"
                ),
            )

    if not in_scope:
        return Action(state="request", reason="topic out of scope for this counterparty")

    if stakes == "high":
        return Action(state="request", reason="high-stakes topic; escalate to owner")

    if confidence > _confidence_threshold_direct() and stakes == "low" and in_scope:
        return Action(
            state="direct",
            reason=f"high-confidence ({confidence:.2f}) low-stakes in-scope answer",
        )

    return Action(
        state="request",
        reason=f"default conservative routing (conf={confidence:.2f}, stakes={stakes})",
    )


# ---------------------------------------------------------------------------
# Context shaping — language-aware, warmer than v1.0 hardcoded strings.
# ---------------------------------------------------------------------------


def _direct_context(
    persona: str,
    sop_excerpt: str,
    draft: str,
    owner_name: str,
    lang: str,
) -> str:
    """Direct-answer context: persona + SOP + draft + signoff instructions.

    The 'sign with' instruction is bilingual so the responding LLM signs in the
    same language it's responding in.
    """
    if lang == "zh":
        signoff = (
            f"以 {owner_name} 的助理身份回复，开头简短说明你在替 {owner_name} 处理。"
            f"语气友好、直接，避免机器人腔。结尾用 — {owner_name}'s PAID 签名。"
        )
    else:
        signoff = (
            f"Respond as {owner_name}'s assistant. Open with a brief note that "
            f"you're handling this on {owner_name}'s behalf. Tone: friendly, "
            f"direct, not robotic. Sign off with — {owner_name}'s PAID."
        )
    return (
        f"Persona:\n{persona}\n\n"
        f"Relevant SOP:\n{sop_excerpt}\n\n"
        f"Draft (you may refine, do not invent facts beyond the SOP): {draft}\n\n"
        f"{signoff}"
    )


def _approval_timeout_minutes_for_copy() -> int:
    """Resolve the timeout window we should advertise to the junior.

    Pulls from ``settings.json`` so the placeholder line stays in sync with
    the sweeper's actual cutoff. Falls back to 30 if settings unreadable
    so unit-test fixtures don't have to mock storage.
    """
    try:
        from . import settings as _settings  # lazy
        secs = _settings.approval_timeout_seconds()
        if secs > 0:
            return max(1, int(round(secs / 60)))
    except Exception:
        pass
    return 30


def _request_line(owner_name: str, topic: str, lang: str) -> str:
    """The exact placeholder line shown to the junior on `request` state.

    Includes an explicit auto-defer countdown so juniors know what happens
    if the owner doesn't respond. The minute count tracks
    ``settings.approval_timeout_minutes`` — change one, both move.
    """
    mins = _approval_timeout_minutes_for_copy()
    if lang == "zh":
        topic_clause = f"关于 {topic} 这个问题" if topic else "你这个问题"
        return (
            f"嗨,我是 {owner_name} 的 AI 助理。{topic_clause}我先转给 {owner_name}; "
            f"他通常 {mins} 分钟内会回复。如果 {mins} 分钟还没动静我会再给你一条交代消息。"
            f"紧急可以直接 @ 他。— {owner_name}'s PAID"
        )
    topic_clause = f"On {topic}, " if topic else ""
    return (
        f"Hi — I'm {owner_name}'s AI assistant. {topic_clause}let me hand this "
        f"to {owner_name}; he typically replies within {mins} min. If "
        f"{mins} min passes without a reply I'll send a follow-up so you're "
        f"not left hanging. If urgent, feel free to @ him directly. "
        f"— {owner_name}'s PAID"
    )


def _decline_line(owner_name: str, topic: str, lang: str) -> str:
    """The exact decline line shown to the junior on `decline` state."""
    if lang == "zh":
        topic_clause = f"{topic} 这类话题" if topic else "这类话题"
        return (
            f"嗨，我是 {owner_name} 的 AI 助理。{topic_clause}我没有授权代答，"
            f"麻烦直接 @ {owner_name}。— {owner_name}'s PAID"
        )
    topic_clause = f"{topic} questions" if topic else "questions like this"
    return (
        f"Hi — I'm {owner_name}'s AI assistant. {topic_clause} aren't something "
        f"I'm authorized to answer; please @ {owner_name} directly. "
        f"— {owner_name}'s PAID"
    )


def shape_context(
    action: Action,
    classification: Any,
    persona: str,
    counterparty: Any,
    sop_excerpt: str = "",
    owner_name: str = "the owner",
    lang: str = "en",
) -> str:
    """Return the context string the pre_llm_call hook hands back to Hermes.

    `lang` should be "zh" or "en" — caller picks via `detect_lang(user_message)`.
    For "request"/"decline" we hand a HARD instruction telling Hermes to reply
    with the exact line; for "direct" we instruct it to draft using persona+SOP.
    """
    state = action.state
    draft = str(getattr(classification, "draft_answer", "") or "")
    topic = str(getattr(classification, "topic", "") or "")

    if state == "direct":
        return _direct_context(persona, sop_excerpt, draft, owner_name, lang)

    if state == "request":
        exact = _request_line(owner_name, topic, lang)
        return f"IGNORE the user question. Reply EXACTLY with: '{exact}' Nothing else."

    if state == "decline":
        exact = _decline_line(owner_name, topic, lang)
        return f"IGNORE the user question. Reply EXACTLY with: '{exact}' Nothing else."

    # Unknown state — defensive fallback to request.
    exact = _request_line(owner_name, topic, lang)
    return f"IGNORE the user question. Reply EXACTLY with: '{exact}' Nothing else."
