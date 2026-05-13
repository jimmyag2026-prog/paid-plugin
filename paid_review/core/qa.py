"""paid_review.core.qa — Q&A loop helpers (Sprint B).

Two functions for the QA stage:

  render_finding(ann, idx, total, lang) — produce the IM text shown to
    the junior for ONE finding. Wraps with options block + (a/b/c/skip).
    Replaces api._finding_text with proper i18n + severity surfacing.

  classify_reply(text, finding) — classify a junior's free-text reply
    into one of {accepted, rejected, modified, unresolvable}. Fast path:
    short codes (a/b/c/skip + zh/en synonyms) match without LLM. Slow
    path: free text → LLM via prompts/classify_reply.md.

Why two-tier (short-code → LLM):
  - 95% of replies are short codes (cheap + deterministic + offline)
  - Free-text reply is meaningful and needs comprehension; we pay LLM
    for it but only when needed
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

from paid_review.core.annotation import Annotation

logger = logging.getLogger(__name__)


_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


Status = Literal["accepted", "rejected", "modified", "unresolvable"]


# ---------------------------------------------------------------------------
# Short-code fast path
# ---------------------------------------------------------------------------

# Map junior reply text → status. Case-insensitive; whitespace stripped.
# Order matters only for exact-match disambiguation; we hit the first
# matching set for any given normalized input.
_SHORT_CODES: dict[str, Status] = {
    # accepted
    "a":          "accepted",
    "accept":     "accepted",
    "approved":   "accepted",
    "ok":         "accepted",
    "okay":       "accepted",
    "yes":        "accepted",
    "y":          "accepted",
    "接受":       "accepted",
    "同意":       "accepted",
    "改":         "accepted",
    # rejected (with reason — short codes carry the intent; if junior just
    # types 'b' the prompt instructed them to add a reason. If they don't,
    # we still record rejected; downstream 'open_items' logic handles it)
    "b":          "rejected",
    "reject":     "rejected",
    "no":         "rejected",
    "n":          "rejected",
    "保留异议":   "rejected",
    "不同意":     "rejected",
    "dissent":    "rejected",
    # unresolvable
    "c":              "unresolvable",
    "unresolvable":   "unresolvable",
    "无解":           "unresolvable",
    "标为无解":       "unresolvable",
    "不知道":         "unresolvable",
    # modified treated as 'skip' (cursor advances; finding stays open)
    "skip":           "modified",
    "跳过":           "modified",
    "pass":           "modified",
}


def _short_code(text: str) -> Status | None:
    return _SHORT_CODES.get(text.strip().lower())


# ---------------------------------------------------------------------------
# Free-text LLM classifier
# ---------------------------------------------------------------------------


def _load_prompt() -> str:
    path = _PROMPTS_DIR / "classify_reply.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt template missing: {path}")
    return path.read_text(encoding="utf-8")


def _call_llm(prompt: str, system: str = "") -> str:
    from paid import hermes_io
    return hermes_io.call_llm(
        prompt=prompt,
        system=system or "Classify the reply as instructed.",
    )


def _parse_status_json(raw: str) -> Status:
    """Parse classify_reply.md output. Falls back to 'modified' on any
    parse failure (the safest non-blocking advance)."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("qa.classify_reply: JSON parse failed; raw=%r", raw[:200])
        return "modified"

    status = data.get("status") if isinstance(data, dict) else None
    if status not in ("accepted", "rejected", "modified", "unresolvable"):
        logger.warning("qa.classify_reply: invalid status %r", status)
        return "modified"
    return status  # type: ignore[return-value]


def classify_reply(text: str, finding: Annotation) -> Status:
    """Classify junior's reply text against `finding`.

    Fast path: short code match → return immediately (no LLM call).
    Slow path: LLM via classify_reply.md prompt → returns status.

    Always returns a valid Status. On LLM failure, returns 'modified'
    (lets the cursor advance instead of stalling — open finding stays
    open and shows up in close_propose review).
    """
    short = _short_code(text)
    if short is not None:
        return short

    # Free-text path
    template = _load_prompt()
    prompt = (
        template
        .replace("{pillar}", finding.pillar)
        .replace("{severity}", "IMPROVEMENT")  # severity not stored on Annotation v1
        .replace("{issue}", finding.text.split("\n", 1)[0])  # first line is the issue
        .replace("{suggest}", "(see issue line)")
        .replace("{reply}", text)
    )

    try:
        raw = _call_llm(prompt)
    except Exception as exc:
        logger.warning("classify_reply: LLM call failed: %s", exc)
        return "modified"

    return _parse_status_json(raw)


# ---------------------------------------------------------------------------
# Finding renderer
# ---------------------------------------------------------------------------


# i18n options text
_OPTIONS_ZH = (
    "(a) 接受 — 我会改\n"
    "(b) 保留异议 — 我有不同看法\n"
    "(c) 标为无解 — 我处理不了，留给 owner\n"
    "(skip) 跳过这条\n"
    "(自由文本) 直接说你的回复"
)

_OPTIONS_EN = (
    "(a) accept — I'll fix\n"
    "(b) reject — I have a counter\n"
    "(c) unresolvable — I can't address; leave for owner\n"
    "(skip) skip this one\n"
    "(free text) just type your reply"
)


def render_finding(ann: Annotation, index: int, total: int,
                   lang: str = "zh") -> str:
    """Format ONE finding as an IM message for the junior.

    Layout:
      【Finding N/M · Pillar】
      <issue + 💡 suggest>

      <options block in lang>
    """
    options = _OPTIONS_EN if lang == "en" else _OPTIONS_ZH
    return (
        f"【Finding {index}/{total} · {ann.pillar}】\n"
        f"{ann.text}\n\n"
        f"{options}"
    )
