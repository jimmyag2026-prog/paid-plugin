"""paid_review.core.final_gate — Sprint C form-check before close.

Re-scans the final material against the 4 pillars to decide:
  - READY: all pass, ready for owner
  - READY_WITH_OPEN_ITEMS: Intent passes, but some findings unresolved
    (kept as open_items in brief §4 / §6, NOT blocking close)
  - FAIL: Intent fails OR new BLOCKER regression → back to QA + rounds++

This is api._handle_qa(done) → MERGE/GATE path's destination. It is
PAID's own form check, not another junior-facing question (spec §6 Ⓜ14
clarification).

Strict separation: this function returns a dict only — it does NOT
mutate SessionState. api.py handles the state transition based on the
returned verdict.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Literal

from paid_review.core.annotation import Annotation

logger = logging.getLogger(__name__)


_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


GateVerdict = Literal["READY", "READY_WITH_OPEN_ITEMS", "FAIL"]


def _call_llm(prompt: str, system: str = "") -> str:
    from paid import hermes_io
    return hermes_io.call_llm(
        prompt=prompt,
        system=system or "You are reviewing material for decision-readiness.",
    )


def _load_prompt() -> str:
    path = _PROMPTS_DIR / "final_gate.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt template missing: {path}")
    return path.read_text(encoding="utf-8")


def _parse_gate_json(raw: str) -> dict[str, Any]:
    """Tolerant parser; returns dict or {} on failure."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("final_gate: JSON parse failed (%s); raw=%r", exc, raw[:200])
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _summarize_findings(annotations: list[Annotation]) -> str:
    """Build a compact 'findings status' bullet list for the prompt."""
    if not annotations:
        return "(no findings recorded)"
    lines = []
    for a in annotations:
        snippet = (a.text.split("\n", 1)[0])[:120]
        lines.append(f"- [{a.status}] {a.pillar}: {snippet}")
    return "\n".join(lines)


def _heuristic_fallback_verdict(annotations: list[Annotation]) -> dict[str, Any]:
    """If LLM fails to parse, fall back to a deterministic heuristic so
    we don't block the session indefinitely.

    Rules:
      - Any open finding with pillar='Intent' status≠accepted → FAIL
      - Any open finding (any pillar) → READY_WITH_OPEN_ITEMS
      - Otherwise → READY

    This is conservative — when in doubt, prefer READY_WITH_OPEN_ITEMS
    over READY so owner sees the unresolved items in §4.
    """
    intent_unresolved = any(
        a.pillar.lower() == "intent" and a.status in ("open", "unresolvable", "rejected")
        for a in annotations
    )
    if intent_unresolved:
        return {
            "verdict": "FAIL",
            "csw_gate_pillar": "Intent",
            "csw_gate_status": "fail",
            "pillar_verdict": {
                "Background": "pass", "Materials": "pass",
                "Framework": "pass", "Intent": "fail",
            },
            "regressions": [],
            "rationale": "(heuristic fallback — LLM gate unavailable; "
                         "Intent finding still unresolved)",
        }
    open_or_unresolvable = any(
        a.status in ("open", "unresolvable", "rejected") for a in annotations
    )
    if open_or_unresolvable:
        return {
            "verdict": "READY_WITH_OPEN_ITEMS",
            "csw_gate_pillar": "Intent",
            "csw_gate_status": "pass",
            "pillar_verdict": {
                "Background": "pass", "Materials": "pass",
                "Framework": "pass", "Intent": "pass",
            },
            "regressions": [],
            "rationale": "(heuristic fallback — Intent satisfied; some "
                         "non-blocking items remain)",
        }
    return {
        "verdict": "READY",
        "csw_gate_pillar": "Intent",
        "csw_gate_status": "pass",
        "pillar_verdict": {
            "Background": "pass", "Materials": "pass",
            "Framework": "pass", "Intent": "pass",
        },
        "regressions": [],
        "rationale": "(heuristic fallback — all findings resolved)",
    }


def _validate_gate_output(data: dict[str, Any]) -> bool:
    """csw_gate_status must equal pillar_verdict.Intent (spec self-check)."""
    if not isinstance(data, dict):
        return False
    if data.get("verdict") not in ("READY", "READY_WITH_OPEN_ITEMS", "FAIL"):
        return False
    pv = data.get("pillar_verdict", {})
    if not isinstance(pv, dict):
        return False
    intent_pv = pv.get("Intent")
    if intent_pv not in ("pass", "fail"):
        return False
    if data.get("csw_gate_status") != intent_pv:
        return False
    return True


def run_final_gate(*, subject: str, final_document: str,
                   annotations: list[Annotation],
                   rounds: int) -> dict[str, Any]:
    """Run the form check; return the verdict dict (also written to
    sessions/<sid>/final_gate.json by api.py).

    On LLM failure or invalid output → heuristic fallback (never returns
    {} or raises). Returns dict with at minimum:
      verdict / csw_gate_pillar / csw_gate_status / pillar_verdict /
      regressions / rationale
    """
    template = _load_prompt()
    findings_status = _summarize_findings(annotations)
    prompt = (
        template
        .replace("{subject}", subject)
        .replace("{final_document}", final_document)
        .replace("{findings_status}", findings_status)
        .replace("{rounds}", str(rounds))
    )

    try:
        raw = _call_llm(prompt)
    except Exception as exc:
        logger.warning("run_final_gate: LLM call failed: %s", exc)
        return _heuristic_fallback_verdict(annotations)

    data = _parse_gate_json(raw)
    if not _validate_gate_output(data):
        logger.warning("run_final_gate: invalid LLM output, using heuristic; raw=%r",
                       raw[:200])
        return _heuristic_fallback_verdict(annotations)

    return data
