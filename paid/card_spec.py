"""Module CS — platform-agnostic spec for approval cards.

Each platform-specific formatter (``_format_{lark,telegram,slack,plain}_
approval_card``) consumes the same ``ApprovalCardSpec`` and emits its own
native payload. This avoids leaky abstractions across three very different
card schemas (Lark interactive JSON, Telegram InlineKeyboardMarkup, Slack
Block Kit blocks) while keeping all the field-derivation logic in one
place — confidence badge, stakes badge, draft truncation, instructions
copy, etc.

Used by:
  - ``__init__._notify_owner_approval`` — builds spec then dispatches by
    owner identity's platform to the matching formatter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


_MAX_JUNIOR_MSG_CHARS = 600
_MAX_DRAFT_CHARS = 600


def _confidence_badge(c: float) -> str:
    """Visual confidence pill — shared across formatters so 🟢/🟡/🔴 is
    consistent regardless of platform."""
    if c >= 0.75:
        return "🟢"
    if c >= 0.5:
        return "🟡"
    return "🔴"


def _stakes_badge(s: str) -> str:
    """Visual stakes pill — same hint everywhere."""
    return {
        "high": "🚨 HIGH",
        "medium": "🟠 medium",
        "low": "🟢 low",
    }.get((s or "").lower(), s or "?")


@dataclass
class ApprovalCardSpec:
    """Platform-agnostic snapshot of one approval request.

    Constructed by ``ApprovalCardSpec.from_pending_approval(req, ...)`` so
    callers don't need to know about ``approval.PendingApproval`` field
    layout. Formatters depend ONLY on this spec — never on PendingApproval
    directly — so changes to the persisted approval schema don't leak into
    every formatter.
    """

    request_id: str
    junior_name: str
    junior_platform: str
    junior_msg: str            # already truncated to _MAX_JUNIOR_MSG_CHARS
    topic: str
    confidence: float          # 0.0 - 1.0
    stakes: str                # "low" | "medium" | "high"
    draft: str                 # already truncated; "" if no draft
    has_draft: bool
    timeout_min: int = 30
    instructions: str = ""     # call-to-action text shown on every card
    sources: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Computed display helpers — formatters call these so visual hints
    # stay identical across Lark / TG / Slack / plain.
    # ------------------------------------------------------------------

    def confidence_pill(self) -> str:
        return _confidence_badge(self.confidence)

    def confidence_label(self) -> str:
        """e.g. '🟡 0.65' — formatters embed verbatim."""
        return f"{_confidence_badge(self.confidence)} {self.confidence:.2f}"

    def stakes_pill(self) -> str:
        return _stakes_badge(self.stakes)

    def header_title(self) -> str:
        """Used by Lark header / TG first line / Slack header block."""
        return f"📨 PAID approval #{self.request_id}"

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_pending_approval(
        cls,
        req: Any,                          # approval.PendingApproval — duck-typed
        *,
        timeout_min: int = 30,
        instructions: str | None = None,
    ) -> "ApprovalCardSpec":
        """Build a spec from a ``PendingApproval``.

        ``instructions`` defaults to a sensible bilingual call-to-action
        ("操作: 回 /paid-approve <id> 或 /paid-reject <id>") — pass an
        explicit string to override per-platform if needed.
        """
        msg = (getattr(req, "junior_question", "") or "")[:_MAX_JUNIOR_MSG_CHARS]
        draft_raw = getattr(req, "draft_answer", "") or ""
        has_draft = bool(draft_raw.strip())
        draft = draft_raw[:_MAX_DRAFT_CHARS] if has_draft else ""
        if instructions is None:
            instructions = (
                f"操作: 回 /paid-approve {req.request_id} "
                f"或 /paid-reject {req.request_id}  ·  "
                f"Action: reply /paid-approve {req.request_id} or /paid-reject {req.request_id}"
            )
        return cls(
            request_id=req.request_id,
            junior_name=(
                getattr(req, "counterparty_display", "")
                or getattr(req, "counterparty_user_id", "")
                or "(unknown)"
            ),
            junior_platform=getattr(req, "counterparty_platform", "") or "",
            junior_msg=msg,
            topic=getattr(req, "topic", "") or "—",
            confidence=float(getattr(req, "confidence", 0.0) or 0.0),
            stakes=getattr(req, "stakes", "") or "",
            draft=draft,
            has_draft=has_draft,
            timeout_min=timeout_min,
            instructions=instructions,
        )
