"""Module Sa — Layer 1 (input) + Layer 4 (output) safety regex.

Conservative regex layers for the J2 pipeline (PRD §J2 / §L4):

  Layer 1 INPUT  — detect_prompt_injection(user_message)
  Layer 4a       — detect_cross_cp_name_leakage(response, current_cp_id)
  Layer 4b       — detect_pii(response)

Layers 4c (LLM post-check) and 4d (source attribution) intentionally deferred
to Week 2 (see ``design/01_review_decisions.md §2.6``).

Design choices:
  * No third-party deps — pure stdlib re.
  * Functions return ``(hit: bool, matches: list[str])`` so callers can decide
    policy (block vs. flag vs. log).
  * Patterns are intentionally tight — false negatives are acceptable for v0.5;
    a noisy false-positive on a friendly tester is a worse failure mode.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import storage


# ---------------------------------------------------------------------------
# Layer 1 — Prompt injection in junior input
# ---------------------------------------------------------------------------

# Each pattern is paired with a short label so the audit log explains *why*
# we flagged. Order matters only for the first-match-wins ``label`` return.
_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "ignore-prior-instructions",
        re.compile(
            r"\b(ignore|disregard|forget|override)\b.{0,40}\b("
            r"previous|prior|above|earlier|all)\b.{0,40}\b"
            r"(instruction|prompt|rule|message|system)s?\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "ignore-prior-instructions-zh",
        re.compile(
            r"(忽略|无视|清除|忘记).{0,20}(之前|前面|以上|所有)?"
            r".{0,20}(指令|提示|规则|系统|prompt)",
            re.IGNORECASE,
        ),
    ),
    (
        "system-prompt-extraction",
        re.compile(
            r"(reveal|show|print|repeat|leak|dump)\b.{0,40}\b"
            r"(system|developer)\s*(prompt|message|instruction)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "role-reset",
        re.compile(
            r"\byou\s+are\s+now\b.{0,80}\b(new|different|jailbroken|admin|root)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "fake-system-tag",
        re.compile(
            r"<\s*(system|admin|developer)\s*>",
            re.IGNORECASE,
        ),
    ),
    (
        "fake-instruction-block",
        re.compile(
            r"\b(BEGIN|START|NEW)\s+(INSTRUCTION|PROMPT|SYSTEM)S?\b",
        ),
    ),
    (
        "act-as-bypass",
        re.compile(
            r"\b(act|pretend|roleplay)\s+as\b.{0,40}\b"
            r"(no\s+restrictions?|jailbroken|DAN|developer\s+mode)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
]


def detect_prompt_injection(user_message: str) -> tuple[bool, list[str]]:
    """Return (hit, labels) for any matched injection patterns.

    ``labels`` is the list of pattern labels that fired, in pattern order.
    The first hit alone is enough for an upstream block; the full list is
    handy for the audit log.
    """
    if not user_message:
        return False, []
    hits: list[str] = []
    for label, pat in _INJECTION_PATTERNS:
        if pat.search(user_message):
            hits.append(label)
    return (bool(hits), hits)


# ---------------------------------------------------------------------------
# Layer 4a — Cross-counterparty name leakage in outbound response
# ---------------------------------------------------------------------------


def _read_other_cp_names(current_cp_id: str) -> list[str]:
    """Return display_names of all known counterparties EXCEPT current.

    Reads ``~/.hermes/paid/counterparties/*/profile.json`` lazily; failures are
    swallowed so safety checks degrade open. Empty / single-token names <2
    chars are skipped to avoid trivially false-positive on stop-words.
    """
    cp_root: Path = storage.PAID_DIR / "counterparties"
    if not cp_root.exists():
        return []
    names: list[str] = []
    for child in cp_root.iterdir():
        if not child.is_dir():
            continue
        if child.name == current_cp_id:
            continue
        prof = storage.read_json(child / "profile.json")
        if not prof:
            continue
        dn = str(prof.get("display_name") or "").strip()
        if len(dn) >= 2 and dn.lower() not in {"unknown", "tester", "user"}:
            names.append(dn)
    return names


def detect_cross_cp_name_leakage(
    response: str,
    current_cp_id: str,
) -> tuple[bool, list[str]]:
    """True if *response* mentions another known counterparty's display name.

    Word-boundary match (case-insensitive) so substring noise is filtered
    (matches "Alice" but not "alphabetical" / "calibration"). Pure-CJK names
    fall through ``\\b`` so we use a lookaround that still works.
    """
    if not response:
        return False, []
    other_names = _read_other_cp_names(current_cp_id)
    hits: list[str] = []
    for name in other_names:
        # Use a CJK-tolerant substring search: ASCII names get word-boundary,
        # CJK names get plain substring (re-tokenizing CJK is out of scope).
        if any(ord(c) > 0x2E80 for c in name):
            if name in response:
                hits.append(name)
        else:
            if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", response, re.IGNORECASE):
                hits.append(name)
    return (bool(hits), hits)


# ---------------------------------------------------------------------------
# Layer 4b — PII regex
# ---------------------------------------------------------------------------

_PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Email — RFC-pragmatic (not RFC-perfect; cheap precision).
    (
        "email",
        re.compile(
            r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
        ),
    ),
    # US SSN: nnn-nn-nnnn or 9 contiguous digits with - separators.
    (
        "ssn-us",
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    ),
    # CN ID 18-digit (loose: 17 digits + check char 0-9/X). Front-anchored on word.
    (
        "cn-idcard",
        re.compile(r"(?<!\d)[1-9]\d{16}[\dXx](?!\d)"),
    ),
    # Mainland CN mobile: 1[3-9]xxxxxxxxx (11 digits).
    (
        "phone-cn",
        re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    ),
    # E.164-style international phone (loose).
    (
        "phone-e164",
        re.compile(r"\+\d{1,3}[\s\-]?\d{4,14}\b"),
    ),
    # Money amounts ≥ $1000 (signal sensitive financial leakage). Flags the
    # number-only chunk so the audit log doesn't echo full sentences.
    (
        "money-usd-large",
        re.compile(
            r"\$\s?\d{1,3}(?:,\d{3})+(?:\.\d{2})?(?!\d)"
            r"|\$\s?\d{4,}(?:\.\d{2})?(?!\d)",
        ),
    ),
    # CN money in 万 / 亿 notation (e.g. "100万", "1.2亿").
    (
        "money-cn-large",
        re.compile(r"\d+(?:\.\d+)?\s*(?:万|亿)"),
    ),
    # Credit-card-ish 13-19 digit run (loose; common card lengths).
    (
        "card-number",
        re.compile(r"(?<!\d)\d{13,19}(?!\d)"),
    ),
]


def detect_pii(response: str) -> tuple[bool, list[str]]:
    """Return (hit, labels) for any matched PII patterns in *response*.

    Labels list is order-preserving; duplicates are collapsed.
    """
    if not response:
        return False, []
    seen: set[str] = set()
    hits: list[str] = []
    for label, pat in _PII_PATTERNS:
        if pat.search(response) and label not in seen:
            seen.add(label)
            hits.append(label)
    return (bool(hits), hits)


# ---------------------------------------------------------------------------
# Combined output check — convenience for the post-LLM hook
# ---------------------------------------------------------------------------


def check_output(response: str, current_cp_id: str) -> dict:
    """Run Layer 4a + 4b together; return a structured result.

    Shape::

        {
            "ok": bool,
            "name_leakage": [...],
            "pii": [...],
        }

    ``ok`` is False if either layer hit. The caller decides redaction policy.
    """
    name_hit, names = detect_cross_cp_name_leakage(response, current_cp_id)
    pii_hit, pii = detect_pii(response)
    return {
        "ok": not (name_hit or pii_hit),
        "name_leakage": names,
        "pii": pii,
    }
