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


def _try_jieba_tokens(text: str) -> list[str] | None:
    """Use jieba for CJK word segmentation if available, else None.

    jieba is ~5MB and slow on first import; we keep it optional and degrade
    gracefully to char-bigrams when it's missing.
    """
    try:
        import jieba  # type: ignore
    except Exception:
        return None
    out: list[str] = []
    for tok in jieba.cut(text, cut_all=False):
        tok = tok.strip()
        if not tok or tok in _STOPWORDS:
            continue
        if len(tok) == 1 and tok.isascii():
            continue
        out.append(tok)
    return out


def _cjk_bigrams(text: str) -> list[str]:
    """Split CJK runs into bigrams + carry single-char fallback.

    Bigrams capture pairs that act like word stems in Chinese ("退款", "时区",
    "工作", "时间"), giving substring scoring real signal. Single chars also
    kept (low weight) so a query like "退" still matches "退款条款".
    """
    out: list[str] = []
    for run in re.findall(r"[一-鿿]+", text):
        if len(run) >= 2:
            for i in range(len(run) - 1):
                bg = run[i : i + 2]
                if bg in _STOPWORDS:
                    continue
                out.append(bg)
        # always keep singletons too — short-query escape hatch
        for ch in run:
            if ch in _STOPWORDS:
                continue
            out.append(ch)
    return out


def _tokenize(query: str) -> list[str]:
    """Tokenise a query for substring scoring against SOP paragraphs.

    Path:
      1. CJK runs → jieba words if available, else bigrams + singletons.
      2. ASCII runs → \\w+, drop stopwords + 1-char noise.

    Bigrams are the workhorse for Chinese; "工作时间" → ["工作", "作时", "时间", ...]
    so a paragraph mentioning "默认工作时间是上午十点" is reliably hit.
    """
    if not query:
        return []
    lowered = query.lower()

    # --- CJK ---
    cjk_tokens = _try_jieba_tokens(lowered)
    if cjk_tokens is None:
        cjk_tokens = _cjk_bigrams(lowered)

    out: list[str] = []
    out.extend(cjk_tokens)

    # --- ASCII ---
    for word in _ASCII_WORD_RE.findall(lowered):
        if word in _STOPWORDS:
            continue
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
