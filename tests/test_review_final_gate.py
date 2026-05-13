"""Tests for paid_review.core.final_gate (Sprint C form check)."""

from __future__ import annotations

import json

import pytest

from paid_review.core import final_gate as fg
from paid_review.core.annotation import Annotation


def _patch_llm(monkeypatch, value):
    if isinstance(value, Exception):
        def boom(*a, **kw):
            raise value
        monkeypatch.setattr(fg, "_call_llm", boom)
    else:
        monkeypatch.setattr(fg, "_call_llm", lambda *a, **kw: value)


# --------------------------------------------------------------------------
# JSON parser + validator
# --------------------------------------------------------------------------


def test_parse_gate_json_strips_markdown_fence():
    raw = '```json\n{"verdict": "READY", "csw_gate_status": "pass"}\n```'
    data = fg._parse_gate_json(raw)
    assert data.get("verdict") == "READY"


def test_parse_gate_json_returns_empty_on_garbage():
    assert fg._parse_gate_json("not json") == {}
    assert fg._parse_gate_json("") == {}


def test_validate_csw_status_must_match_intent_pillar():
    """spec rule: csw_gate_status MUST equal pillar_verdict.Intent."""
    valid = {
        "verdict": "READY",
        "csw_gate_status": "pass",
        "pillar_verdict": {"Background": "pass", "Materials": "pass",
                           "Framework": "pass", "Intent": "pass"},
    }
    assert fg._validate_gate_output(valid) is True

    mismatched = dict(valid)
    mismatched["csw_gate_status"] = "fail"  # but Intent says pass
    assert fg._validate_gate_output(mismatched) is False


def test_validate_invalid_verdict_rejected():
    bad = {
        "verdict": "MAYBE",
        "csw_gate_status": "pass",
        "pillar_verdict": {"Intent": "pass"},
    }
    assert fg._validate_gate_output(bad) is False


# --------------------------------------------------------------------------
# Heuristic fallback
# --------------------------------------------------------------------------


def test_heuristic_intent_unresolved_returns_FAIL():
    anns = [Annotation(id="p1", pillar="Intent", text="vague ask", status="open")]
    out = fg._heuristic_fallback_verdict(anns)
    assert out["verdict"] == "FAIL"
    assert out["csw_gate_status"] == "fail"


def test_heuristic_open_non_intent_returns_with_open_items():
    anns = [
        Annotation(id="p1", pillar="Intent", text="ask is clear", status="accepted"),
        Annotation(id="p2", pillar="Materials", text="no source", status="rejected"),
    ]
    out = fg._heuristic_fallback_verdict(anns)
    assert out["verdict"] == "READY_WITH_OPEN_ITEMS"


def test_heuristic_all_resolved_returns_READY():
    anns = [
        Annotation(id="p1", pillar="Intent", text="x", status="accepted"),
        Annotation(id="p2", pillar="Materials", text="y", status="accepted"),
    ]
    out = fg._heuristic_fallback_verdict(anns)
    assert out["verdict"] == "READY"


def test_heuristic_unresolvable_intent_is_fail():
    """Intent finding with status=unresolvable still blocks (Intent is CSW gate)."""
    anns = [Annotation(id="p1", pillar="Intent", text="x", status="unresolvable")]
    out = fg._heuristic_fallback_verdict(anns)
    assert out["verdict"] == "FAIL"


# --------------------------------------------------------------------------
# run_final_gate (LLM path)
# --------------------------------------------------------------------------


def test_run_final_gate_parses_valid_llm(monkeypatch):
    valid = json.dumps({
        "verdict": "READY",
        "csw_gate_pillar": "Intent",
        "csw_gate_status": "pass",
        "pillar_verdict": {"Background": "pass", "Materials": "pass",
                           "Framework": "pass", "Intent": "pass"},
        "regressions": [],
        "rationale": "all pillars pass",
    })
    _patch_llm(monkeypatch, valid)
    out = fg.run_final_gate(
        subject="Q3 plan", final_document="...",
        annotations=[], rounds=2,
    )
    assert out["verdict"] == "READY"
    assert "rationale" in out


def test_run_final_gate_returns_FAIL_when_intent_fails(monkeypatch):
    fail_json = json.dumps({
        "verdict": "FAIL",
        "csw_gate_pillar": "Intent",
        "csw_gate_status": "fail",
        "pillar_verdict": {"Background": "pass", "Materials": "pass",
                           "Framework": "pass", "Intent": "fail"},
        "regressions": [],
        "rationale": "ask is still vague",
    })
    _patch_llm(monkeypatch, fail_json)
    out = fg.run_final_gate(
        subject="x", final_document="y", annotations=[], rounds=1,
    )
    assert out["verdict"] == "FAIL"
    assert out["csw_gate_status"] == "fail"


def test_run_final_gate_falls_back_on_invalid_llm_output(monkeypatch):
    """When LLM returns garbage, heuristic fallback fires (never returns {})."""
    _patch_llm(monkeypatch, "not json")
    anns = [Annotation(id="p1", pillar="Intent", text="x", status="accepted")]
    out = fg.run_final_gate(
        subject="x", final_document="y", annotations=anns, rounds=1,
    )
    assert out["verdict"] in ("READY", "READY_WITH_OPEN_ITEMS", "FAIL")
    assert "rationale" in out


def test_run_final_gate_falls_back_on_llm_exception(monkeypatch):
    _patch_llm(monkeypatch, RuntimeError("LLM down"))
    anns = [Annotation(id="p1", pillar="Materials", text="x", status="open")]
    out = fg.run_final_gate(
        subject="x", final_document="y", annotations=anns, rounds=1,
    )
    assert out["verdict"] == "READY_WITH_OPEN_ITEMS"  # heuristic: open Materials → with_open


def test_run_final_gate_csw_mismatch_falls_back(monkeypatch):
    """LLM returns valid-looking JSON but csw_gate_status disagrees with
    pillar_verdict.Intent — must reject + heuristic fallback."""
    bad = json.dumps({
        "verdict": "READY",
        "csw_gate_pillar": "Intent",
        "csw_gate_status": "fail",   # mismatch
        "pillar_verdict": {"Background": "pass", "Materials": "pass",
                           "Framework": "pass", "Intent": "pass"},
    })
    _patch_llm(monkeypatch, bad)
    out = fg.run_final_gate(
        subject="x", final_document="y", annotations=[], rounds=1,
    )
    # Heuristic fallback with empty annotations → READY
    assert out["verdict"] == "READY"
    assert "(heuristic fallback" in out.get("rationale", "")


def test_run_final_gate_template_loaded():
    template = fg._load_prompt()
    assert "{subject}" in template
    assert "{final_document}" in template
    assert "{findings_status}" in template
    assert "{rounds}" in template
