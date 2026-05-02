"""Module R — simple keyword retrieval over sop.md.

v1 sprint: substring keyword scoring against paragraphs (split by blank lines).
FTS5 is a stretch goal we're deferring to keep risk low.

Tokenization handles BOTH ASCII words and CJK characters:
  - ASCII runs (letters/digits) become whole tokens
  - Each CJK char becomes its own token (no jieba dep; per-char + stopwords
    is good enough for sop.md sized corpora)

The CJK split was missing in the first cut, which made *all* Chinese queries
score 0 and silently fall back to the first paragraph (paid-may_review_v1
§3.2). The current tokenizer fixes that.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import storage


# Tiny CN/EN stopword list — enough to keep query keywords meaningful for v1.
_STOPWORDS: frozenset[str] = frozenset(
    {
        # CN — common particles
        "的", "是", "吗", "吧", "了", "和", "也", "在", "我", "你", "他",
        "她", "它", "这", "那", "有", "就", "都", "啊", "呀", "呢", "么",
        # CN — common interrogatives (so "怎么", "什么" don't drown signal)
        "怎", "么", "什", "多", "少", "哪", "几", "谁", "为", "如", "何",
        "请", "能", "可",
        # EN
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "and", "or", "of", "to", "in", "on", "for", "with", "at", "by",
        "this", "that", "it", "i", "you", "he", "she", "we", "they",
        "do", "does", "did", "have", "has", "had", "can", "could", "would",
        "should", "will", "may", "might", "what", "how", "when", "where",
        "why", "who",
    }
)


# CJK Unified Ideographs (basic block). Sufficient for Chinese; covers most
# Japanese kanji incidentally. We don't try to handle hiragana/katakana —
# Phase 1 is CN+EN.
_CJK_RE = re.compile(r"[一-鿿]")
_ASCII_WORD_RE = re.compile(r"[a-z0-9]+")


def _sop_path() -> Path:
    return storage.PAID_DIR / "sop.md"


def _tokenize(query: str) -> list[str]:
    """Split into per-char CJK tokens + whole-word ASCII tokens, drop stopwords.

    Examples:
        "怎么退款"        -> ["退", "款"]              (怎,么 stopword'd)
        "How do refunds"  -> ["refunds"]              (how,do stopword'd)
        "退款 policy"     -> ["退", "款", "policy"]
    """
    if not query:
        return []
    lowered = query.lower()
    out: list[str] = []
    # Per-character CJK
    for ch in _CJK_RE.findall(lowered):
        if ch in _STOPWORDS:
            continue
        out.append(ch)
    # Whole-word ASCII
    for word in _ASCII_WORD_RE.findall(lowered):
        if word in _STOPWORDS:
            continue
        # Drop 1-char ASCII noise (single letters/digits)
        if len(word) == 1:
            continue
        out.append(word)
    return out


def _score(paragraph: str, keywords: list[str]) -> int:
    """Count case-insensitive keyword occurrences in paragraph."""
    if not keywords:
        return 0
    lower = paragraph.lower()
    return sum(lower.count(kw) for kw in keywords)


def retrieve_sop_context(query: str, max_chars: int = 2000) -> str:
    """Return concatenated relevant SOP paragraphs (best-match first).

    - Reads ~/.hermes/paid/sop.md via storage.
    - Splits on blank lines (double newline).
    - Scores each paragraph by keyword-occurrence count.
    - Returns top paragraphs joined by `\\n\\n`, truncated to max_chars.
    - If no keyword matches: returns the first paragraph (general overview).
    - Returns "" if file missing or empty.
    """
    raw = storage.read_text(_sop_path())
    if not raw:
        return ""

    paragraphs = [p.strip() for p in raw.split("\n\n") if p.strip()]
    if not paragraphs:
        return ""

    keywords = _tokenize(query)
    scored = [(idx, _score(p, keywords), p) for idx, p in enumerate(paragraphs)]
    matched = [s for s in scored if s[1] > 0]

    if not matched:
        # Fallback: assume the first paragraph is a general overview.
        first = paragraphs[0]
        return first[:max_chars]

    # Sort by score desc, then original order asc for stable output.
    matched.sort(key=lambda s: (-s[1], s[0]))

    out_parts: list[str] = []
    used = 0
    for _idx, _score_val, para in matched:
        chunk = para if not out_parts else "\n\n" + para
        if used + len(chunk) > max_chars:
            remaining = max_chars - used
            if remaining > 0:
                out_parts.append(chunk[:remaining])
            break
        out_parts.append(chunk)
        used += len(chunk)

    return "".join(out_parts)
