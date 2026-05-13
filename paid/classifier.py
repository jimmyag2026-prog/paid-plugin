"""Module C — Classifier.

Calls the Hermes-configured LLM with a structured-output prompt to classify
a junior's incoming message into a `Classification` dataclass that the
decision module then uses to pick one of three states (direct / request /
decline).

The classifier does NOT touch storage, identity, or retrieval. It receives
plain Counterparty-shaped data (duck-typed with ``.name`` etc.) plus an
SOP excerpt and the user message.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import hermes_io


# --------------------------------------------------------------------------
# Counterparty Protocol — duck-typed so we don't depend on identity.py
# --------------------------------------------------------------------------


class CounterpartyLike(Protocol):
    """Structural type for counterparty data the classifier consumes."""

    display_name: str
    role: str
    topics_allowed: list[str]
    topics_always_escalate: list[str]


# --------------------------------------------------------------------------
# Classification dataclass
# --------------------------------------------------------------------------


@dataclass
class Classification:
    """Structured classification output for one junior message."""

    topic: str = ""
    stakes: str = "medium"  # "low" | "medium" | "high"
    in_scope: bool = False
    is_blacklisted: bool = False
    confidence: float = 0.0  # 0.0 - 1.0
    needs_retrieval: bool = False
    suggested_queries: list[str] = field(default_factory=list)
    draft_answer: str = ""
    reasoning: str = ""
    # review-skill integration (added in W2 batch 1).
    # `needs_review` flips on when the inbound looks like a structured ask
    # (draft / proposal / agenda) that the owner should sign off on rather
    # than a one-shot question. The decision module routes such messages to
    # the "review" state which hands off to the paid-review skill.
    # `review_subject_hints` carries 2-4 candidate decision-subject phrases
    # the skill uses for its subject-confirmation step.
    needs_review: bool = False
    review_subject_hints: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Prompt construction
# --------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "You are PAID's classifier — a precise, conservative gating layer that "
    "decides whether the owner's AI delegate may answer a junior's question "
    "directly, must request the owner's approval, or must decline. "
    "You output ONLY a single JSON object that strictly conforms to the "
    "schema described in the user prompt. No prose, no markdown, no code "
    "fences. Be conservative: when uncertain, prefer in_scope=false and a "
    "lower confidence. Never invent facts in draft_answer; if you cannot "
    "ground it in the SOP excerpt, leave draft_answer empty."
)


_JSON_SCHEMA_DESCRIPTION = """\
You MUST return a JSON object with exactly these keys and types:

{
  "topic":                 string,         // short topic label, e.g. "vesting", "logistics"
  "stakes":                "low"|"medium"|"high",
  "in_scope":              boolean,        // true ONLY if topic clearly matches counterparty.topics_allowed
  "is_blacklisted":        boolean,        // true if topic matches counterparty.topics_always_escalate or is sensitive (legal/financial/HR/equity)
  "confidence":            number,         // 0.0..1.0, how sure you are about the classification
  "needs_retrieval":       boolean,        // true if more SOP/web context would help
  "suggested_queries":     [string, ...],  // up to 3 short search strings, [] if none
  "draft_answer":          string,         // proposed answer grounded in SOP excerpt; "" if cannot draft
  "reasoning":             string,         // <=300 chars rationale, manager-only
  "needs_review":          boolean,        // see Review-trigger rules below
  "review_subject_hints":  [string, ...]   // 2-4 candidate "decision subject" phrases when needs_review=true; [] otherwise
}

Rules:
- If the question is clearly outside topics_allowed, set in_scope=false.
- If the question matches topics_always_escalate, set is_blacklisted=true and stakes="high".
- If you would need facts not present in the SOP excerpt, leave draft_answer="" and set needs_retrieval=true.
- Output the JSON object only — no surrounding text, no markdown fences.

Review-trigger rules (set `needs_review=true` if AND only if ALL three hold):
  (a) The junior submitted a STRUCTURED ASK — draft / proposal / plan / budget /
      agenda / investment memo / OKR / roadmap / spec — not a one-shot question.
  (b) They expect the owner to APPROVE / REJECT / CHOOSE between options /
      give DIRECTIONAL feedback — not just answer a fact.
  (c) A single-sentence reply would NOT meaningfully advance the work.
  Even when stakes=="low" you may still set true; the decision module checks
  stakes separately. When `needs_review=true`, populate `review_subject_hints`
  with 2-4 short phrases (each <= 12 words) capturing distinct candidate
  "single decision the owner is being asked to make". When false, return [].
"""


def _build_prompt(
    *,
    user_message: str,
    counterparty: CounterpartyLike,
    owner_name: str,
    sop_excerpt: str,
) -> str:
    """Assemble the user-side prompt fed to the classifier LLM."""
    topics_allowed = ", ".join(counterparty.topics_allowed) or "(none specified)"
    topics_escalate = (
        ", ".join(counterparty.topics_always_escalate) or "(none specified)"
    )
    cp_name = getattr(counterparty, "display_name", "") or "(unnamed counterparty)"
    cp_role = getattr(counterparty, "role", "") or "(unknown role)"
    sop_block = sop_excerpt.strip() or "(no SOP excerpt available)"

    return (
        f"OWNER: {owner_name}\n"
        f"COUNTERPARTY:\n"
        f"  name: {cp_name}\n"
        f"  role: {cp_role}\n"
        f"  topics_allowed: {topics_allowed}\n"
        f"  topics_always_escalate: {topics_escalate}\n"
        f"\n"
        f"RELEVANT SOP EXCERPT:\n"
        f"---\n{sop_block}\n---\n"
        f"\n"
        f"USER QUESTION:\n"
        f"---\n{user_message}\n---\n"
        f"\n"
        f"OUTPUT FORMAT (STRICT):\n"
        f"{_JSON_SCHEMA_DESCRIPTION}"
    )


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _coerce_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x) for x in value if x is not None]
    return []


def _coerce_float_unit(value: Any) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    if f < 0.0:
        return 0.0
    if f > 1.0:
        return 1.0
    return f


def _coerce_stakes(value: Any) -> str:
    if isinstance(value, str) and value.lower() in {"low", "medium", "high"}:
        return value.lower()
    return "medium"


def _strip_code_fence(raw: str) -> str:
    """If the model wrapped JSON in ```...``` fences, strip them."""
    s = raw.strip()
    if s.startswith("```"):
        # drop the first fence line
        first_nl = s.find("\n")
        if first_nl != -1:
            s = s[first_nl + 1 :]
        if s.endswith("```"):
            s = s[: -3]
    return s.strip()


def _validate(c: Classification) -> Classification:
    """Sanity-check classifier output; downgrade confidence on contradictions.

    Catches internally inconsistent records — a class of jailbreak/model error
    where the LLM emits self-contradicting fields (e.g. blacklisted yet in_scope,
    or low-stakes yet blacklisted). A correctly confident "out-of-scope, high
    stakes, blacklisted" record (e.g. vesting question) is NOT suspicious — it's
    the desirable shape — so we only flag actual contradictions.
    """
    suspicious_reasons = []
    if c.is_blacklisted and c.in_scope:
        suspicious_reasons.append("blacklisted yet in_scope")
    if c.is_blacklisted and c.stakes == "low":
        suspicious_reasons.append("blacklisted yet stakes=low")
    if c.in_scope and c.draft_answer == "" and c.confidence > 0.9:
        # Confidently in-scope but produced no draft → model output mismatch.
        suspicious_reasons.append("in_scope + high confidence + empty draft")

    if suspicious_reasons:
        c.confidence = min(c.confidence, 0.5)
        flag = "[suspicious: " + "; ".join(suspicious_reasons) + "]"
        c.reasoning = f"{flag} {c.reasoning}".strip()
    return c


def _parse_classification(raw: str) -> Classification:
    """Parse the LLM JSON response into a Classification.

    Returns the conservative fallback Classification on any parse failure.
    Fallback Classifications carry a reasoning prefix `[fallback]` so the
    caller (and audit log) can spot silent degradation.
    """
    if not raw or not raw.strip():
        return Classification(reasoning="[fallback] empty LLM response")

    cleaned = _strip_code_fence(raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return Classification(reasoning="[fallback] malformed JSON from classifier")

    if not isinstance(data, dict):
        return Classification(reasoning="[fallback] classifier JSON was not an object")

    parsed = Classification(
        topic=str(data.get("topic", "")),
        stakes=_coerce_stakes(data.get("stakes")),
        in_scope=bool(data.get("in_scope", False)),
        is_blacklisted=bool(data.get("is_blacklisted", False)),
        confidence=_coerce_float_unit(data.get("confidence", 0.0)),
        needs_retrieval=bool(data.get("needs_retrieval", False)),
        suggested_queries=_coerce_str_list(data.get("suggested_queries")),
        draft_answer=str(data.get("draft_answer", "")),
        reasoning=str(data.get("reasoning", "")),
        needs_review=bool(data.get("needs_review", False)),
        review_subject_hints=_coerce_str_list(data.get("review_subject_hints"))[:4],
    )
    return _validate(parsed)


def is_fallback(c: Classification) -> bool:
    """True if this Classification came from a fallback path (parse/network error)."""
    return c.reasoning.startswith("[fallback]")


# v1.3.2 H2: rolling window of last N classifier outcomes (True=fallback).
# Lives in-process; per-hermes-restart resets. Surfaced via
# fallback_rate_recent() so /paid-status can show "classifier fallback
# rate: X% (last N)" — pre-fix, silent classifier outages just produced
# 100% request-state routing with no operator visibility.
import collections as _collections

_CLASSIFIER_HISTORY_MAX = 100
_classifier_history: _collections.deque[bool] = _collections.deque(maxlen=_CLASSIFIER_HISTORY_MAX)


def _record_classification(c: Classification) -> None:
    _classifier_history.append(is_fallback(c))


def fallback_rate_recent() -> tuple[int, int, float]:
    """Return (fallback_count, total_count, ratio_0_to_1).

    ``total_count`` ≤ ``_CLASSIFIER_HISTORY_MAX``. ``ratio`` is 0.0 on
    empty history (no classifications yet, not a 0% real signal).
    """
    total = len(_classifier_history)
    if total == 0:
        return (0, 0, 0.0)
    fb = sum(1 for x in _classifier_history if x)
    return (fb, total, fb / total)


def reset_classifier_history() -> None:
    """Test hook + manual reset; not used in normal operation."""
    _classifier_history.clear()


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


def classify(
    user_message: str,
    counterparty: CounterpartyLike,
    owner_name: str,
    sop_excerpt: str,
) -> Classification:
    """Classify a junior message into a structured `Classification`.

    On any failure (network, malformed JSON, etc.) returns a conservative
    fallback Classification (confidence=0, in_scope=False, draft_answer="")
    so the decision module routes to "request" by default.
    """
    prompt = _build_prompt(
        user_message=user_message,
        counterparty=counterparty,
        owner_name=owner_name,
        sop_excerpt=sop_excerpt,
    )
    try:
        raw = hermes_io.call_llm(
            prompt=prompt,
            system=_SYSTEM_PROMPT,
            json_mode=True,
            temperature=0.1,  # deterministic gating; not creative
        )
    except Exception as e:  # noqa: BLE001 — classifier never raises upward
        result = Classification(reasoning=f"[fallback] classifier LLM call failed: {e}")
        _record_classification(result)
        return result

    result = _parse_classification(raw)
    _record_classification(result)
    return result
