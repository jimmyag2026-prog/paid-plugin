"""paid_review.core.scan — 4-pillar + Responder Sim two-layer scan (Sprint B).

Replaces Sprint A's single-LLM-call _build_findings() stub in api.py.
Two distinct LLM calls produce two annotation streams that get merged:

  Layer A: 4-pillar scan (Background / Materials / Framework / Intent)
           emits findings with id="p1", "p2", ...
  Layer B: Responder simulation (owner persona projects their top-5
           questions; document gaps become findings)
           emits findings with id="r1", "r2", ...

Each layer is independent — failure of one doesn't block the other.
Both can return [] (no findings); the caller (api._handle_subject)
handles the no_findings short-circuit (Ⓜ17).

Prompts live in paid_review/prompts/{four_pillar,responder_sim}.md
and are read once at module load. Tests monkeypatch _call_llm.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from paid_review.core.annotation import Annotation

logger = logging.getLogger(__name__)


# Module-level prompt cache (read once)
_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def _load_prompt(name: str) -> str:
    path = _PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt template missing: {path}")
    return path.read_text(encoding="utf-8")


def _call_llm(prompt: str, system: str = "") -> str:
    """Lazy import so tests can monkeypatch paid.hermes_io.call_llm
    OR paid_review.core.scan._call_llm directly."""
    from paid import hermes_io
    return hermes_io.call_llm(
        system_prompt=system or "You are a critical reviewer.",
        user_message=prompt,
    )


def _parse_findings_json(raw: str) -> list[dict[str, Any]]:
    """Tolerant JSON parser. Strips markdown fences if present.
    Returns [] on any parse failure (logged)."""
    text = raw.strip()
    # Strip ```json ... ``` fence
    if text.startswith("```"):
        lines = text.splitlines()
        # drop first fence line
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        # drop trailing fence
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        items = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("scan: JSON parse failed (%s); raw=%r", exc, raw[:200])
        return []

    if not isinstance(items, list):
        logger.warning("scan: expected list, got %s", type(items).__name__)
        return []

    return [it for it in items if isinstance(it, dict)]


def _annotation_from_dict(item: dict[str, Any], *, source: str,
                          default_id: str) -> Annotation | None:
    """Construct an Annotation from a parsed JSON item; returns None if
    required fields missing.

    `source` records provenance ("four_pillar" / "responder_sim") so the
    summary builder (Sprint C) can group findings by source if needed.
    """
    text = str(item.get("issue", "") or item.get("text", "")).strip()
    if not text:
        # `simulated_question` (responder sim) is also acceptable as the
        # primary finding text — fall back so we don't lose the row.
        text = str(item.get("simulated_question", "")).strip()
    if not text:
        return None

    # Append suggest to the text so the junior sees the recommended fix
    suggest = str(item.get("suggest", "")).strip()
    if suggest:
        text = f"{text}\n💡 建议: {suggest}"

    pillar = str(item.get("pillar", "")).strip() or "Materials"
    # Normalize pillar capitalization (LLMs are inconsistent)
    pillar_lower = pillar.lower()
    pillar_map = {
        "background": "Background", "materials": "Materials",
        "framework": "Framework", "intent": "Intent",
    }
    pillar_norm = pillar_map.get(pillar_lower, pillar)

    return Annotation(
        id=str(item.get("id", default_id)),
        pillar=pillar_norm,
        text=text,
        status="open",
    )


# ---------------------------------------------------------------------------
# Public layer functions
# ---------------------------------------------------------------------------


def run_four_pillar(*, subject: str, document: str) -> list[Annotation]:
    """Layer A — 4-pillar critical scan.

    Returns a list of Annotation. Empty list on LLM failure / parse error
    (logged; caller decides whether 0 findings means short-circuit close
    or just retry).
    """
    template = _load_prompt("four_pillar")
    prompt = template.replace("{subject}", subject).replace("{document}", document)

    try:
        raw = _call_llm(prompt)
    except Exception as exc:
        logger.warning("run_four_pillar: LLM call failed: %s", exc)
        return []

    items = _parse_findings_json(raw)
    out: list[Annotation] = []
    for i, item in enumerate(items, start=1):
        ann = _annotation_from_dict(
            item, source="four_pillar", default_id=f"p{i}",
        )
        if ann is not None:
            out.append(ann)
    return out


def run_responder_sim(*, subject: str, document: str,
                      owner_name: str = "the owner",
                      responder_profile: str = "") -> list[Annotation]:
    """Layer B — simulate owner asking their top questions; emit findings
    for the questions the document fails to answer.

    Returns a list of Annotation. Empty list when the owner profile is
    minimal AND the document already answers the obvious questions —
    that's a fine outcome, not a bug.
    """
    template = _load_prompt("responder_sim")
    prompt = (
        template
        .replace("{subject}", subject)
        .replace("{document}", document)
        .replace("{owner_name}", owner_name)
        .replace("{responder_profile}", responder_profile.strip() or "(no profile yet)")
    )

    try:
        raw = _call_llm(prompt)
    except Exception as exc:
        logger.warning("run_responder_sim: LLM call failed: %s", exc)
        return []

    items = _parse_findings_json(raw)
    out: list[Annotation] = []
    for i, item in enumerate(items, start=1):
        ann = _annotation_from_dict(
            item, source="responder_sim", default_id=f"r{i}",
        )
        if ann is not None:
            out.append(ann)
    return out


def run_full_scan(*, subject: str, document: str,
                  owner_name: str = "the owner",
                  responder_profile: str = "") -> list[Annotation]:
    """Convenience: both layers, merged. Order: 4-pillar findings first
    (p1, p2, ...), responder sim findings after (r1, r2, ...).

    De-dup is best-effort — if both layers emit the same `text`
    substring, the second occurrence is dropped. This is the entry
    api._handle_subject calls.
    """
    a = run_four_pillar(subject=subject, document=document)
    b = run_responder_sim(
        subject=subject, document=document,
        owner_name=owner_name, responder_profile=responder_profile,
    )

    # Naive de-dup: skip a responder_sim finding whose text is fully
    # contained in any 4-pillar finding's text. Cheap, conservative.
    a_blob = "\n".join(ann.text for ann in a).lower()
    deduped_b = []
    for ann in b:
        snippet = ann.text.split("\n", 1)[0].lower()  # primary issue line
        if snippet and snippet in a_blob:
            logger.debug("scan: dropping duplicate responder_sim finding %s", ann.id)
            continue
        deduped_b.append(ann)

    return a + deduped_b
