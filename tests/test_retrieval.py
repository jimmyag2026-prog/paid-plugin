"""Tests for Module R — paid.retrieval."""

from __future__ import annotations

import pytest

from paid.retrieval import retrieve_sop_context


SAMPLE_SOP = """\
# PAID SOP — overview

This is the general overview paragraph that describes how PAID works at a high level.

## Refunds

Refunds are processed within 7 business days. Always ask for the order ID before approving a refund.

## Shipping

Shipping defaults to standard ground. International shipping requires manager approval.

## Pricing

Pricing changes must be approved in writing. Never quote a custom price without confirmation.
"""


def _write_sop(paid_tmp, content: str) -> None:
    (paid_tmp / "sop.md").write_text(content, encoding="utf-8")


def test_retrieve_returns_empty_when_file_missing(paid_tmp):
    # No sop.md written.
    out = retrieve_sop_context("anything")
    assert out == ""


def test_retrieve_returns_empty_when_file_empty(paid_tmp):
    _write_sop(paid_tmp, "")
    out = retrieve_sop_context("anything")
    assert out == ""


def test_retrieve_finds_relevant_paragraph_by_keyword(paid_tmp):
    _write_sop(paid_tmp, SAMPLE_SOP)
    out = retrieve_sop_context("How do I handle a refund?")
    assert "refund" in out.lower()
    # Refunds paragraph should rank above shipping/pricing.
    assert "7 business days" in out


def test_retrieve_falls_back_to_first_paragraph_when_no_match(paid_tmp):
    _write_sop(paid_tmp, SAMPLE_SOP)
    out = retrieve_sop_context("zzzzz nonexistent topic qqqq")
    # First paragraph (the H1 + overview) used as fallback.
    assert "overview" in out.lower() or "PAID" in out


def test_retrieve_truncates_to_max_chars(paid_tmp):
    _write_sop(paid_tmp, SAMPLE_SOP)
    out = retrieve_sop_context("shipping pricing refund", max_chars=80)
    assert len(out) <= 80
    assert len(out) > 0


def test_retrieve_higher_score_paragraph_first(paid_tmp):
    # 'shipping' appears once in shipping paragraph; with query 'shipping shipping'
    # the shipping paragraph should clearly dominate.
    _write_sop(paid_tmp, SAMPLE_SOP)
    out = retrieve_sop_context("shipping international")
    # Shipping paragraph should be present and appear before pricing paragraph
    # (if pricing is even included).
    assert "International shipping" in out or "Shipping" in out
    # The shipping content must come before pricing content if both present.
    if "Pricing changes" in out:
        assert out.index("Shipping") < out.index("Pricing changes")


# ---------------------------------------------------------------------------
# CJK tokenization regression tests (paid-may_review_v1 §3.2).
# Pre-fix, all Chinese queries silently fell back to the first paragraph
# because `query.lower().split()` does not break CJK runs.
# ---------------------------------------------------------------------------


CN_SOP = """\
# PAID 操作手册

这是顶层概览段落，介绍 PAID 的整体设计。

## 退款流程

退款将在 7 个工作日内处理完毕。请先确认订单号。

## 物流安排

国内物流默认走标准陆运。国际物流需要 manager 批准。

## 定价规则

任何定价变更都必须有书面确认。不要随意报价。
"""


def test_retrieve_chinese_query_finds_chinese_paragraph(paid_tmp):
    """Bug fix: '怎么退款' must score the 退款 paragraph above the rest."""
    _write_sop(paid_tmp, CN_SOP)
    out = retrieve_sop_context("怎么退款")
    assert "退款" in out
    assert "7 个工作日" in out
    # Must NOT silently fall back to the overview paragraph
    assert "顶层概览" not in out


def test_retrieve_chinese_interrogatives_are_stopworded(paid_tmp):
    """`怎么` etc. should not pollute scoring with high-frequency noise."""
    _write_sop(paid_tmp, CN_SOP)
    # Ambiguous-looking query; the discriminating signal is "物流"
    out = retrieve_sop_context("怎么 安排 物流")
    assert "物流" in out
    assert "标准陆运" in out


def test_retrieve_mixed_cn_en_query(paid_tmp):
    """Mixed-language queries must tokenize both halves."""
    sop = SAMPLE_SOP + "\n\n## 退款\n\n中文退款条款相同。\n"
    _write_sop(paid_tmp, sop)
    out = retrieve_sop_context("refund 退款")
    # Either / both paragraphs ought to be present; the key requirement is
    # "we do not fall back to overview".
    assert ("refund" in out.lower()) or ("退款" in out)
    assert "general overview paragraph" not in out.lower()


def test_retrieve_chinese_query_no_match_falls_back(paid_tmp):
    """CJK query with no real signal still falls back gracefully.

    Picking truly out-of-vocabulary CJK chars (chess/biology) so per-char
    matching can't accidentally hit anything in the SOP.
    """
    _write_sop(paid_tmp, CN_SOP)
    out = retrieve_sop_context("象棋 蚯蚓 zzzzz")
    # Fallback returns the first paragraph (H1 "# PAID 操作手册" in this fixture)
    assert "操作手册" in out or "PAID" in out


# ---------------------------------------------------------------------------
# Bigram tokenizer regression — long-form Chinese SOP paragraphs that
# would have scored 0 under the pre-v0.8 single-char tokenizer when they
# happen to share many common chars across all paragraphs.
# ---------------------------------------------------------------------------


CN_SOP_LONG = """\
# PAID 个人助理操作手册

PAID 是 Jimmy 的代理 AI，按授权处理来访者的消息。整体设计是三态：直接答 / 请示
Jimmy / 拒答。这一段是顶层概览，不应该被任何具体问题命中。

## 工作时间与时区

Jimmy 默认工作时间是周一到周五上午十点到下午六点半，时区是 Asia/Hong_Kong (UTC+8)。
紧急情况除外，下班后不一定及时回。

## 退款流程

国内订单退款通常 7 个工作日内处理完毕。需要订单号和发票编号才能开启流程。

## 会议安排

默认会议长度 25 分钟，前后保留 5 分钟缓冲。约一对一去 calendly.com/jimmy 看空档，
临时改期至少提前 4 小时。
"""


def test_bigram_finds_specific_chinese_paragraph(paid_tmp):
    """Long-form Chinese: question about working hours must hit the
    'working hours' paragraph at the top of the result."""
    _write_sop(paid_tmp, CN_SOP_LONG)
    out = retrieve_sop_context("Jimmy 工作时间是几点？")
    assert "上午十点" in out and "下午六点" in out
    # Ranking check: the working-hours content must appear BEFORE the
    # overview content if both are included.
    if "顶层概览" in out:
        assert out.index("上午十点") < out.index("顶层概览")


def test_bigram_disambiguates_meeting_vs_refund(paid_tmp):
    _write_sop(paid_tmp, CN_SOP_LONG)
    out = retrieve_sop_context("会议默认多久")
    assert "25 分钟" in out
    assert "退款" not in out  # must not bleed in via shared chars


def test_bigram_refund_query_hits_refund_section(paid_tmp):
    _write_sop(paid_tmp, CN_SOP_LONG)
    out = retrieve_sop_context("怎么处理退款 需要什么材料")
    assert "退款" in out
    assert "订单号" in out


def test_tokenize_bigram_pure_unit():
    """Direct unit test on _tokenize: bigrams emerge for pure CJK input."""
    from paid.retrieval import _tokenize
    toks = _tokenize("工作时间")
    # bigrams must be present
    assert "工作" in toks
    assert "时间" in toks
