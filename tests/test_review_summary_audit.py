"""Tests for paid_review.core.build_summary + build_audit (Sprint C)."""

from __future__ import annotations

from paid_review.core import build_audit, build_summary
from paid_review.core.annotation import Annotation


def _make_anns():
    return [
        Annotation(id="p1", pillar="Intent",   text="vague ask", status="accepted"),
        Annotation(id="p2", pillar="Materials", text="no source", status="rejected"),
        Annotation(id="p3", pillar="Materials", text="missing comp", status="unresolvable"),
        Annotation(id="r1", pillar="Background", text="missing why-now", status="open"),
    ]


# ==========================================================================
# build_audit (deterministic; no LLM)
# ==========================================================================


def test_audit_renders_header_with_subject_junior_verdict():
    out = build_audit.build_audit(
        subject="Q3 plan", junior_name="Evie",
        junior_platform="feishu",
        rounds=2, verdict="READY_WITH_OPEN_ITEMS",
        annotations=_make_anns(),
    )
    assert "# Review Audit Trail — Q3 plan" in out
    assert "Evie (feishu)" in out
    assert "Rounds: 2" in out
    assert "READY_WITH_OPEN_ITEMS" in out


def test_audit_includes_status_count_table():
    out = build_audit.build_audit(
        subject="x", junior_name="J", junior_platform="lark",
        rounds=1, verdict="READY",
        annotations=_make_anns(),
    )
    assert "## 4 柱 × status 计数表" in out
    assert "| Pillar |" in out
    # Each pillar present in table
    assert "Intent" in out
    assert "Materials" in out
    assert "Background" in out


def test_audit_groups_findings_by_status():
    out = build_audit.build_audit(
        subject="x", junior_name="J", junior_platform="tg",
        rounds=1, verdict="READY",
        annotations=_make_anns(),
    )
    assert "已接受" in out
    assert "保留异议" in out
    assert "已 modified" in out  # empty list shown
    assert "无解" in out
    assert "未闭合" in out
    # Specific finding text present in correct section
    assert "vague ask" in out      # accepted section
    assert "no source" in out      # rejected section
    assert "missing comp" in out   # unresolvable section


def test_audit_handles_empty_annotations():
    out = build_audit.build_audit(
        subject="x", junior_name="J", junior_platform="lark",
        rounds=0, verdict="READY", annotations=[],
    )
    assert "(no findings)" in out
    assert "(none)" in out  # all status sections empty


def test_audit_includes_forced_reason():
    out = build_audit.build_audit(
        subject="x", junior_name="J", junior_platform="lark",
        rounds=1, verdict="FORCED_PARTIAL", annotations=[],
        forced_reason="rounds_exhausted",
    )
    assert "rounds_exhausted" in out


# ==========================================================================
# build_summary (LLM-driven; fallback paths critical)
# ==========================================================================


def _patch_llm(monkeypatch, value):
    if isinstance(value, Exception):
        def boom(*a, **kw):
            raise value
        monkeypatch.setattr(build_summary, "_call_llm", boom)
    else:
        monkeypatch.setattr(build_summary, "_call_llm", lambda *a, **kw: value)


def test_summary_uses_llm_output_when_starts_with_header(monkeypatch):
    fake_brief = (
        "# 会前简报 — Q3 plan\n\n"
        "_Junior: Evie · Rounds: 2_\n\n"
        "## 1. 议题摘要\n要 owner 决定是否批 Q3 预算 240k\n"
    )
    _patch_llm(monkeypatch, fake_brief)
    out = build_summary.build_summary(
        subject="Q3 plan", junior_name="Evie",
        junior_platform="feishu", rounds=2, verdict="READY",
        document="...", annotations=_make_anns(),
    )
    assert out == fake_brief.strip()  # build_summary strips trailing whitespace
    assert "议题摘要" in out


def test_summary_falls_back_when_llm_doesnt_start_with_header(monkeypatch):
    """If LLM returns garbage / wrong format, ship the heuristic skeleton."""
    _patch_llm(monkeypatch, "not a brief at all just random text")
    out = build_summary.build_summary(
        subject="Q3 plan", junior_name="Evie",
        junior_platform="feishu", rounds=2, verdict="READY_WITH_OPEN_ITEMS",
        document="...", annotations=_make_anns(),
    )
    assert out.startswith("# 会前简报")
    assert "fallback" in out.lower()
    assert "READY_WITH_OPEN_ITEMS" in out


def test_summary_fallback_on_llm_exception(monkeypatch):
    _patch_llm(monkeypatch, RuntimeError("LLM API down"))
    out = build_summary.build_summary(
        subject="Q3", junior_name="J", junior_platform="lark",
        rounds=1, verdict="FORCED_PARTIAL",
        document="x", annotations=_make_anns(),
    )
    assert out.startswith("# 会前简报")
    assert "fallback" in out.lower()
    # Heuristic includes finding counts
    assert "接受: 1" in out
    assert "保留异议: 1" in out


def test_summary_template_has_all_placeholders():
    template = build_summary._load_prompt()
    for ph in ("{subject}", "{junior_name}", "{junior_platform}",
               "{rounds}", "{verdict}", "{ts}", "{document}",
               "{findings_summary}", "{dissent_log}"):
        assert ph in template, f"summary.md missing {ph}"
