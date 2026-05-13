"""Tests for paid_review.core.qa (Sprint B short-code + LLM reply classifier).

Covers:
  - Short-code matrix (a/b/c/skip + zh + en synonyms) → correct status
  - Free-text reply → LLM call → status parsed
  - LLM JSON parse failures → fallback 'modified' (advance, don't stall)
  - Markdown-fenced LLM output unwrap
  - Invalid status from LLM → fallback 'modified'
  - render_finding zh + en + layout
"""

from __future__ import annotations

import json

import pytest

from paid_review.core import qa
from paid_review.core.annotation import Annotation


def _make_finding(pillar="Intent", text="vague ask\n💡 建议: rewrite as 'approve X by Y'"):
    return Annotation(id="p1", pillar=pillar, text=text, status="open")


# --------------------------------------------------------------------------
# Short-code fast path (no LLM)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("a", "accepted"),
        ("A", "accepted"),
        ("accept", "accepted"),
        ("ok", "accepted"),
        ("yes", "accepted"),
        ("接受", "accepted"),
        ("同意", "accepted"),
        ("改", "accepted"),
        ("b", "rejected"),
        ("reject", "rejected"),
        ("no", "rejected"),
        ("保留异议", "rejected"),
        ("不同意", "rejected"),
        ("c", "unresolvable"),
        ("无解", "unresolvable"),
        ("不知道", "unresolvable"),
        ("skip", "modified"),
        ("跳过", "modified"),
        ("pass", "modified"),
    ],
)
def test_short_code_fast_path(text, expected, monkeypatch):
    """Short codes resolve without LLM call. monkeypatch _call_llm to
    raise — if any of these short codes hits the LLM path, test fails."""
    monkeypatch.setattr(qa, "_call_llm",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("LLM should not be called")))
    finding = _make_finding()
    assert qa.classify_reply(text, finding) == expected


def test_short_code_case_and_whitespace_insensitive(monkeypatch):
    monkeypatch.setattr(qa, "_call_llm",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("nope")))
    finding = _make_finding()
    assert qa.classify_reply("  A  ", finding) == "accepted"
    assert qa.classify_reply("ACCEPT", finding) == "accepted"
    assert qa.classify_reply("\tSKIP\n", finding) == "modified"


# --------------------------------------------------------------------------
# Free-text path (LLM-driven)
# --------------------------------------------------------------------------


def test_free_text_routes_to_llm(monkeypatch):
    """Anything not in short-code map → LLM call."""
    captured: dict = {}
    def fake(prompt, system=""):
        captured["prompt"] = prompt
        return json.dumps({"status": "rejected", "confidence": 0.9,
                           "rationale": "explicitly disagrees"})
    monkeypatch.setattr(qa, "_call_llm", fake)
    finding = _make_finding()
    out = qa.classify_reply("actually we already considered Y, so this finding doesn't apply", finding)
    assert out == "rejected"
    # Prompt must contain finding context + reply text
    assert "vague ask" in captured["prompt"]
    assert "considered Y" in captured["prompt"]


def test_free_text_llm_invalid_status_falls_back_to_modified(monkeypatch):
    monkeypatch.setattr(qa, "_call_llm",
                        lambda *a, **kw: json.dumps({"status": "garbage"}))
    out = qa.classify_reply("free text reply", _make_finding())
    assert out == "modified"


def test_free_text_llm_garbage_response_falls_back(monkeypatch):
    monkeypatch.setattr(qa, "_call_llm", lambda *a, **kw: "not json")
    out = qa.classify_reply("some custom rebuttal text here", _make_finding())
    assert out == "modified"


def test_free_text_llm_exception_falls_back(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("LLM down")
    monkeypatch.setattr(qa, "_call_llm", boom)
    out = qa.classify_reply("free text", _make_finding())
    assert out == "modified"


def test_free_text_llm_strips_markdown_fence(monkeypatch):
    fenced = "```json\n{\"status\": \"accepted\"}\n```"
    monkeypatch.setattr(qa, "_call_llm", lambda *a, **kw: fenced)
    out = qa.classify_reply("free text reply", _make_finding())
    assert out == "accepted"


def test_classify_returns_all_four_statuses(monkeypatch):
    """Sanity: each status is reachable via free-text + LLM response."""
    finding = _make_finding()
    for status in ("accepted", "rejected", "modified", "unresolvable"):
        monkeypatch.setattr(qa, "_call_llm",
                            lambda *a, _s=status, **kw: json.dumps({"status": _s}))
        assert qa.classify_reply("free text reply", finding) == status


# --------------------------------------------------------------------------
# render_finding
# --------------------------------------------------------------------------


def test_render_finding_zh_default():
    ann = _make_finding(pillar="Intent", text="ask is vague\n💡 建议: rewrite")
    out = qa.render_finding(ann, 1, 5)
    assert "Finding 1/5" in out
    assert "Intent" in out
    assert "ask is vague" in out
    assert "(a) 接受" in out
    assert "(b) 保留异议" in out
    assert "(c) 标为无解" in out
    assert "(skip)" in out
    assert "自由文本" in out


def test_render_finding_en():
    ann = _make_finding(pillar="Materials", text="missing source")
    out = qa.render_finding(ann, 2, 3, lang="en")
    assert "Finding 2/3" in out
    assert "Materials" in out
    assert "(a) accept" in out
    assert "(b) reject" in out
    assert "(c) unresolvable" in out
    assert "(skip)" in out
    assert "free text" in out


def test_render_finding_does_not_drop_emoji_suggest():
    """The 💡 建议 line is part of ann.text after scan; renderer must keep it."""
    ann = _make_finding(text="missing data\n💡 建议: add CFO link")
    out = qa.render_finding(ann, 1, 1)
    assert "💡" in out
    assert "add CFO link" in out


# --------------------------------------------------------------------------
# Prompt template loaded
# --------------------------------------------------------------------------


def test_classify_prompt_template_exists():
    template = qa._load_prompt()
    assert "{pillar}" in template
    assert "{issue}" in template
    assert "{reply}" in template
    assert "accepted" in template
    assert "rejected" in template
    assert "modified" in template
    assert "unresolvable" in template
