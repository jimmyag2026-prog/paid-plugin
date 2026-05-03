"""Module Sa — Layer 1 (input) + Layer 4 (output) safety regex + LLM check.

Conservative regex layers for the J2 pipeline (PRD §J2 / §L4):

  Layer 1 INPUT  — detect_prompt_injection(user_message)
  Layer 4a       — detect_cross_cp_name_leakage(response, current_cp_id)
  Layer 4b       — detect_pii(response)
  Layer 4c       — detect_via_llm(response, persona, sop_excerpt)   [W2]
  Layer 4d       — detect_unsourced_claims(response)                 [W2]

Design choices:
  * Layers 1/4a/4b/4d use pure-stdlib regex; no third-party deps.
  * Layer 4c is opt-in (uses an LLM call) — wired via settings.safety.l4c_enabled
    so a default install pays no extra cost.
  * Functions return ``(hit: bool, matches: list[str])`` so callers can decide
    policy (block vs. flag vs. log). Layer 4c returns
    ``(suspicious, concerns)`` with concerns as short strings.
  * Patterns are intentionally tight — false negatives are acceptable for v0.5;
    a noisy false-positive on a friendly tester is a worse failure mode.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import hermes_io, storage


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
# Layer 4d — Source attribution heuristic (regex-only, no LLM)
# ---------------------------------------------------------------------------

# Words that, appearing within a small window of a numeric / quoted claim,
# count as "this claim has a source". We're permissive — any reasonable
# attribution lets the claim through.
_SOURCE_HINT_TOKENS: tuple[str, ...] = (
    "source", "per ", "according to", "based on", "ref:", "see ",
    "[", "(source", "—", "–", "cited", "from ", "citing",
    "来源", "出处", "依据", "根据", "参考", "引自", "据 ",
    "据财", "据报", "据数据", "据资", "据消息", "据公告",
)

# A "large" number worth attributing. We deliberately skip small integers
# (< 1000) and obvious non-claim numbers (years, percentages, page refs).
# Match digits with optional thousands separators or decimals.
_LARGE_NUMBER_RE = re.compile(
    r"\b(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d{4,}(?:\.\d+)?)\b"
)
# Year-like 4-digit numbers between 1900 and 2099 are rarely the kind of
# claim that needs sourcing (they're contextual). Skip them.
_YEAR_LIKE_RE = re.compile(r"^(?:19|20)\d{2}$")
# Currency symbols suggest an explicit money claim — those especially
# benefit from a source. Bonus signal beyond bare digits.
_MONEY_PREFIX_RE = re.compile(r"[$¥€£]\s?\d")


def _has_source_nearby(text: str, position: int, window: int = 80) -> bool:
    """True if any ``_SOURCE_HINT_TOKENS`` appear within *window* chars of
    *position* in *text* (case-insensitive)."""
    start = max(0, position - window)
    end = min(len(text), position + window)
    snippet = text[start:end].lower()
    return any(tok.lower() in snippet for tok in _SOURCE_HINT_TOKENS)


def detect_unsourced_claims(response: str) -> tuple[bool, list[str]]:
    """Return (hit, claims) where claims is a list of unsourced numeric or
    monetary statements found in *response*.

    Heuristic, **not** a fact-checker — flags numbers that look like
    substantive claims (large counts / money) without a nearby attribution
    token. False negatives are fine; the goal is to catch obviously-bare
    "我们 GMV 增长了 3 倍" / "users grew to 12,500" output where the
    response should have cited where the number came from.
    """
    if not response:
        return False, []
    claims: list[str] = []
    seen: set[str] = set()

    # Money first — every $X / ¥Y / €Z without nearby source is a claim.
    for m in _MONEY_PREFIX_RE.finditer(response):
        if not _has_source_nearby(response, m.start()):
            text = response[m.start(): m.start() + 12].rstrip(" ,.")
            if text not in seen:
                seen.add(text)
                claims.append(text)

    # Then bare large numbers (>=1000) that aren't years.
    for m in _LARGE_NUMBER_RE.finditer(response):
        token = m.group(0)
        if _YEAR_LIKE_RE.match(token.replace(",", "")):
            continue
        if _has_source_nearby(response, m.start()):
            continue
        if token in seen:
            continue
        seen.add(token)
        claims.append(token)

    return (bool(claims), claims)


# ---------------------------------------------------------------------------
# Layer 4c — LLM-based post-check (opt-in)
# ---------------------------------------------------------------------------

_L4C_SYSTEM_PROMPT = (
    "You are PAID's output auditor. You receive ONE proposed reply that PAID's "
    "AI assistant is about to send to a junior on the owner's behalf, plus the "
    "owner's persona / SOP excerpt that grounds what the assistant is allowed "
    "to say. Decide if the reply has any of these PROBLEMS: "
    "(1) invents facts not in the SOP; "
    "(2) makes commitments on the owner's behalf the SOP doesn't authorise "
    "(scheduling, money, hiring, equity, legal); "
    "(3) tone problems — pretends to be the owner, signs as if the owner wrote "
    "it, or uses high-pressure / sycophantic language; "
    "(4) leaks instructions or system prompt content. "
    "Return ONLY a single JSON object — no markdown, no fences."
)

_L4C_USER_TEMPLATE = """\
PERSONA:
---
{persona}
---

SOP EXCERPT:
---
{sop_excerpt}
---

PROPOSED REPLY:
---
{response}
---

Return JSON: {{"suspicious": bool, "concerns": [string, ...]}}
- "concerns" lists short (<=15 word) descriptions of any (1)-(4) problems found.
- If reply looks fine, return {{"suspicious": false, "concerns": []}}.
"""


def _l4c_enabled() -> bool:
    """Read settings.safety.l4c_enabled (default False — opt-in)."""
    try:
        from . import settings as _settings  # lazy
        cfg = _settings.load().get("safety", {}) if hasattr(_settings, "load") else {}
        return bool(cfg.get("l4c_enabled", False))
    except Exception:
        return False


def detect_via_llm(
    response: str,
    persona: str = "",
    sop_excerpt: str = "",
    *,
    enabled_override: bool | None = None,
) -> tuple[bool, list[str]]:
    """Optional LLM-based output check (Layer 4c).

    Returns (suspicious, concerns) where concerns is a short string list.
    Defaults to disabled — caller (or settings.json) must opt-in. On any
    LLM failure we **fail open** (return ``(False, [])``) so a flaky
    auditor doesn't silently block all replies — combine with regex layers.

    Args:
        response: the assistant draft about to be sent.
        persona / sop_excerpt: grounding context the auditor compares against.
        enabled_override: bypass settings (mainly for tests / forced runs).
    """
    if not response:
        return False, []
    enabled = _l4c_enabled() if enabled_override is None else enabled_override
    if not enabled:
        return False, []

    prompt = _L4C_USER_TEMPLATE.format(
        persona=persona.strip() or "(none)",
        sop_excerpt=sop_excerpt.strip() or "(none)",
        response=response,
    )
    try:
        raw = hermes_io.call_llm(
            prompt=prompt,
            system=_L4C_SYSTEM_PROMPT,
            json_mode=True,
            temperature=0.0,
            timeout=20.0,
        )
    except Exception:
        # Auditor failures are silent — never block a reply because the
        # checker LLM was down. Caller can still run regex layers (4a/4b/4d).
        return False, []

    import json as _json
    try:
        data = _json.loads(raw.strip().lstrip("`").rstrip("`").strip())
    except Exception:
        return False, []
    if not isinstance(data, dict):
        return False, []
    suspicious = bool(data.get("suspicious", False))
    raw_concerns = data.get("concerns", []) or []
    concerns = [str(c)[:200] for c in raw_concerns if c is not None][:5]
    return (suspicious, concerns)


# ---------------------------------------------------------------------------
# Combined output check — convenience for the post-LLM hook
# ---------------------------------------------------------------------------


def check_output(
    response: str,
    current_cp_id: str,
    *,
    persona: str = "",
    sop_excerpt: str = "",
    run_l4c: bool | None = None,
    run_l4d: bool = True,
) -> dict:
    """Run Layer 4a + 4b + (optional) 4c + 4d together; return a structured result.

    Shape::

        {
            "ok": bool,
            "name_leakage": [...],
            "pii": [...],
            "unsourced_claims": [...],   # may be absent if run_l4d=False
            "llm_concerns": [...],       # only present when L4c ran AND fired
        }

    ``ok`` is False if any enabled layer hit. The caller decides redaction policy.

    L4c is opt-in (settings.safety.l4c_enabled, default False) — pass
    ``run_l4c=True`` to force it on a single call (e.g. for spot-checking),
    or False to disable even if settings says enabled. Defaults to None
    (use settings).

    L4d is on by default (regex, free); set ``run_l4d=False`` to skip.
    """
    name_hit, names = detect_cross_cp_name_leakage(response, current_cp_id)
    pii_hit, pii = detect_pii(response)

    out: dict = {
        "ok": True,
        "name_leakage": names,
        "pii": pii,
    }
    bad = bool(name_hit or pii_hit)

    if run_l4d:
        unsourced_hit, unsourced = detect_unsourced_claims(response)
        out["unsourced_claims"] = unsourced
        bad = bad or unsourced_hit

    # L4c only runs when explicitly requested or enabled in settings.
    run_l4c_resolved = (
        run_l4c if run_l4c is not None else _l4c_enabled()
    )
    if run_l4c_resolved:
        l4c_hit, concerns = detect_via_llm(
            response, persona=persona, sop_excerpt=sop_excerpt,
            enabled_override=True,
        )
        if l4c_hit:
            out["llm_concerns"] = concerns
            bad = True

    out["ok"] = not bad
    return out
