"""Tests for paid_review.core.scan (Sprint B 4-pillar + Responder Sim).

Covers:
  - run_four_pillar parses canned LLM JSON into Annotation list
  - run_responder_sim same with `simulated_question` fallback
  - run_full_scan merges both layers + dedups overlapping findings
  - Empty / malformed LLM responses → [] (not exception)
  - Markdown-fenced JSON unwrap
  - Pillar normalization (case-insensitive)
  - LLM exception → [] (logged, not raised)
"""

from __future__ import annotations

import json

import pytest

from paid_review.core import scan
from paid_review.core.annotation import Annotation


# --------------------------------------------------------------------------
# fake LLM helper
# --------------------------------------------------------------------------


def _patch_llm(monkeypatch, return_value: str | Exception):
    """Replace scan._call_llm with a fake. value=str returns; value=Exception raises."""
    if isinstance(return_value, Exception):
        def boom(*a, **kw):
            raise return_value
        monkeypatch.setattr(scan, "_call_llm", boom)
    else:
        monkeypatch.setattr(scan, "_call_llm", lambda *a, **kw: return_value)


# --------------------------------------------------------------------------
# JSON parsing helpers
# --------------------------------------------------------------------------


def test_parse_findings_strips_markdown_fence():
    raw = "```json\n[{\"id\":\"p1\",\"pillar\":\"Intent\",\"issue\":\"x\"}]\n```"
    items = scan._parse_findings_json(raw)
    assert len(items) == 1
    assert items[0]["id"] == "p1"


def test_parse_findings_handles_plain_json():
    raw = '[{"id":"p1","pillar":"Materials","issue":"missing data"}]'
    items = scan._parse_findings_json(raw)
    assert len(items) == 1


def test_parse_findings_returns_empty_on_garbage():
    assert scan._parse_findings_json("not json") == []
    assert scan._parse_findings_json("") == []
    assert scan._parse_findings_json("```\nincomplete") == []


def test_parse_findings_returns_empty_on_non_list():
    assert scan._parse_findings_json('{"a": 1}') == []


def test_parse_findings_drops_non_dict_items():
    raw = '[{"id":"p1","issue":"ok"}, "string", null, 42]'
    items = scan._parse_findings_json(raw)
    assert len(items) == 1
    assert items[0]["id"] == "p1"


# --------------------------------------------------------------------------
# annotation_from_dict
# --------------------------------------------------------------------------


def test_annotation_from_dict_normalizes_pillar_case():
    ann = scan._annotation_from_dict(
        {"issue": "x", "pillar": "INTENT"},
        source="four_pillar", default_id="p1",
    )
    assert ann is not None
    assert ann.pillar == "Intent"


def test_annotation_appends_suggest_to_text():
    ann = scan._annotation_from_dict(
        {"issue": "missing source", "suggest": "add reference link"},
        source="four_pillar", default_id="p1",
    )
    assert "missing source" in ann.text
    assert "💡" in ann.text
    assert "add reference link" in ann.text


def test_annotation_falls_back_to_simulated_question():
    """responder_sim items use `simulated_question` not `issue`."""
    ann = scan._annotation_from_dict(
        {"simulated_question": "what's the customer count?",
         "pillar": "Materials"},
        source="responder_sim", default_id="r1",
    )
    assert ann is not None
    assert "customer count" in ann.text


def test_annotation_returns_none_when_no_text_at_all():
    ann = scan._annotation_from_dict(
        {"pillar": "Intent"}, source="four_pillar", default_id="p1",
    )
    assert ann is None


def test_annotation_default_pillar_when_missing():
    ann = scan._annotation_from_dict(
        {"issue": "x"}, source="four_pillar", default_id="p1",
    )
    assert ann.pillar == "Materials"  # default


# --------------------------------------------------------------------------
# run_four_pillar
# --------------------------------------------------------------------------


def test_run_four_pillar_returns_annotations(monkeypatch):
    fake = json.dumps([
        {"id": "p1", "pillar": "Intent", "issue": "vague ask",
         "suggest": "rewrite as 'approve X by Y'"},
        {"id": "p2", "pillar": "Materials", "issue": "no source for $240k",
         "suggest": "add CFO memo link"},
    ])
    _patch_llm(monkeypatch, fake)
    out = scan.run_four_pillar(subject="Q3 budget", document="...")
    assert len(out) == 2
    assert all(isinstance(a, Annotation) for a in out)
    assert out[0].id == "p1"
    assert out[0].pillar == "Intent"
    assert "vague ask" in out[0].text
    assert "rewrite as" in out[0].text  # suggest appended


def test_run_four_pillar_assigns_default_ids(monkeypatch):
    """LLM may forget to set id; scan auto-assigns p1, p2, ..."""
    fake = json.dumps([
        {"pillar": "Intent", "issue": "x"},
        {"pillar": "Materials", "issue": "y"},
    ])
    _patch_llm(monkeypatch, fake)
    out = scan.run_four_pillar(subject="x", document="y")
    assert [a.id for a in out] == ["p1", "p2"]


def test_run_four_pillar_returns_empty_on_llm_error(monkeypatch):
    _patch_llm(monkeypatch, RuntimeError("simulated LLM outage"))
    out = scan.run_four_pillar(subject="x", document="y")
    assert out == []


def test_run_four_pillar_returns_empty_on_garbage(monkeypatch):
    _patch_llm(monkeypatch, "not valid JSON at all")
    out = scan.run_four_pillar(subject="x", document="y")
    assert out == []


def test_run_four_pillar_loads_prompt_template():
    """Sanity: prompt template file exists + has key sections."""
    template = scan._load_prompt("four_pillar")
    assert "{subject}" in template
    assert "{document}" in template
    assert "Background" in template
    assert "Materials" in template
    assert "Framework" in template
    assert "Intent" in template


# --------------------------------------------------------------------------
# run_responder_sim
# --------------------------------------------------------------------------


def test_run_responder_sim_returns_annotations(monkeypatch):
    fake = json.dumps([
        {"id": "r1", "pillar": "Materials",
         "simulated_question": "what's the customer churn rate?",
         "issue": "doc doesn't show churn",
         "suggest": "add Q2 churn from Mixpanel",
         "priority": 1},
    ])
    _patch_llm(monkeypatch, fake)
    out = scan.run_responder_sim(
        subject="x", document="y",
        owner_name="Jimmy",
        responder_profile="Cares about retention metrics.",
    )
    assert len(out) == 1
    assert out[0].id == "r1"
    assert "churn" in out[0].text


def test_run_responder_sim_handles_empty_profile(monkeypatch):
    fake = json.dumps([])  # owner profile empty + doc clean → 0 findings
    _patch_llm(monkeypatch, fake)
    out = scan.run_responder_sim(subject="x", document="y")
    assert out == []


def test_run_responder_sim_returns_empty_on_llm_error(monkeypatch):
    _patch_llm(monkeypatch, RuntimeError("network"))
    out = scan.run_responder_sim(subject="x", document="y")
    assert out == []


def test_run_responder_sim_loads_prompt_template():
    template = scan._load_prompt("responder_sim")
    assert "{subject}" in template
    assert "{document}" in template
    assert "{owner_name}" in template
    assert "{responder_profile}" in template


# --------------------------------------------------------------------------
# run_full_scan (both layers merged)
# --------------------------------------------------------------------------


def test_run_full_scan_merges_both_layers(monkeypatch):
    """4-pillar + responder_sim findings concatenated, p* before r*."""
    fp_fake = json.dumps([{"id": "p1", "pillar": "Intent", "issue": "vague"}])
    rs_fake = json.dumps([{"id": "r1", "pillar": "Materials",
                           "simulated_question": "where's the data"}])
    calls = {"n": 0}
    def fake_llm(prompt, system=""):
        calls["n"] += 1
        # First call = four_pillar; second = responder_sim
        return fp_fake if calls["n"] == 1 else rs_fake
    monkeypatch.setattr(scan, "_call_llm", fake_llm)

    out = scan.run_full_scan(subject="x", document="y", owner_name="J")
    assert len(out) == 2
    assert out[0].id == "p1"
    assert out[1].id == "r1"
    assert calls["n"] == 2  # both layers really called


def test_run_full_scan_dedups_responder_sim_against_four_pillar(monkeypatch):
    """If responder_sim's primary issue text overlaps a 4-pillar finding,
    the responder_sim version is dropped."""
    fp = json.dumps([
        {"id": "p1", "pillar": "Materials",
         "issue": "no source for the $240k figure"},
    ])
    rs = json.dumps([
        {"id": "r1", "pillar": "Materials",
         "issue": "no source for the $240k figure"},  # same text
        {"id": "r2", "pillar": "Background",
         "issue": "missing competitor comparison"},  # unique
    ])
    calls = {"n": 0}
    def fake_llm(prompt, system=""):
        calls["n"] += 1
        return fp if calls["n"] == 1 else rs
    monkeypatch.setattr(scan, "_call_llm", fake_llm)

    out = scan.run_full_scan(subject="x", document="y")
    # p1 + r2 (r1 dropped as duplicate of p1)
    assert len(out) == 2
    ids = [a.id for a in out]
    assert "p1" in ids and "r2" in ids
    assert "r1" not in ids


def test_run_full_scan_empty_when_both_fail(monkeypatch):
    monkeypatch.setattr(scan, "_call_llm",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("x")))
    out = scan.run_full_scan(subject="x", document="y")
    assert out == []


def test_run_full_scan_one_layer_succeeds_other_fails(monkeypatch):
    """Independence: 4-pillar works, responder_sim fails → still get 4-pillar findings."""
    fp = json.dumps([{"id": "p1", "pillar": "Intent", "issue": "vague"}])
    calls = {"n": 0}
    def fake_llm(prompt, system=""):
        calls["n"] += 1
        if calls["n"] == 1:
            return fp
        raise RuntimeError("responder sim LLM down")
    monkeypatch.setattr(scan, "_call_llm", fake_llm)

    out = scan.run_full_scan(subject="x", document="y")
    assert len(out) == 1
    assert out[0].id == "p1"


# ----- v1.3.2 B4: run_full_scan_with_errors reports per-layer failure -----


def test_run_full_scan_with_errors_both_ok(monkeypatch):
    """Both LLMs succeed → empty failed_layers list."""
    fp = json.dumps([{"id": "p1", "pillar": "Intent", "issue": "vague"}])
    rs = json.dumps([{"id": "r1", "pillar": "Background", "issue": "no anchor"}])
    calls = {"n": 0}
    def fake_llm(prompt, system=""):
        calls["n"] += 1
        return fp if calls["n"] == 1 else rs
    monkeypatch.setattr(scan, "_call_llm", fake_llm)

    annotations, failed = scan.run_full_scan_with_errors(subject="x", document="y")
    assert len(annotations) == 2
    assert failed == []


def test_run_full_scan_with_errors_both_fail_reports_both(monkeypatch):
    """Both LLMs raise → annotations=[] AND failed=['four_pillar','responder_sim'].
    Caller (api._handle_subject) keys off this to escalate instead of
    silently closing as READY (v1.3.2 B4 fix)."""
    monkeypatch.setattr(
        scan, "_call_llm",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("rate limit")),
    )
    annotations, failed = scan.run_full_scan_with_errors(subject="x", document="y")
    assert annotations == []
    assert "four_pillar" in failed and "responder_sim" in failed


def test_run_full_scan_with_errors_partial_fail_reports_one(monkeypatch):
    """4-pillar succeeds, responder_sim fails → failed=['responder_sim'],
    annotations contains 4-pillar findings (partial coverage usable)."""
    fp = json.dumps([{"id": "p1", "pillar": "Intent", "issue": "v"}])
    calls = {"n": 0}
    def fake_llm(prompt, system=""):
        calls["n"] += 1
        if calls["n"] == 1:
            return fp
        raise RuntimeError("responder sim LLM down")
    monkeypatch.setattr(scan, "_call_llm", fake_llm)

    annotations, failed = scan.run_full_scan_with_errors(subject="x", document="y")
    assert len(annotations) == 1
    assert failed == ["responder_sim"]
