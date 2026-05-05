"""paid_review.core.build_summary — 6-section LLM-driven brief (Sprint C).

This is the main owner-facing artifact. spec §12 defines the 6 sections:
1. 议题摘要 / 2. 核心数据 / 3. 团队自检结果 / 4. 待决策事项 /
5. 建议时间分配 / 6. 风险提示

Caller (deliver.py) writes the returned markdown to
sessions/<sid>/summary.md. On LLM failure, falls back to a
deterministic skeleton (better to ship a thin brief than nothing).
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from paid_review.core.annotation import Annotation

logger = logging.getLogger(__name__)


_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _call_llm(prompt: str, system: str = "") -> str:
    from paid import hermes_io
    return hermes_io.call_llm(
        system_prompt=system or "You are writing a decision-ready brief.",
        user_message=prompt,
    )


def _load_prompt() -> str:
    path = _PROMPTS_DIR / "summary.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt template missing: {path}")
    return path.read_text(encoding="utf-8")


def _findings_summary_block(annotations: list[Annotation]) -> str:
    """Compact bullet list for §3 prompt input."""
    if not annotations:
        return "(no findings)"
    lines = []
    for a in annotations:
        snippet = a.text.split("\n", 1)[0][:140]
        lines.append(f"- id={a.id} pillar={a.pillar} status={a.status}: {snippet}")
    return "\n".join(lines)


def _dissent_log(annotations: list[Annotation]) -> str:
    """Just the rejected findings + their text (rebuttal placeholder)."""
    rejected = [a for a in annotations if a.status == "rejected"]
    if not rejected:
        return "(no dissent)"
    lines = []
    for a in rejected:
        snippet = a.text.replace("\n", " ")[:200]
        lines.append(f"- [{a.pillar}] {snippet}")
    return "\n".join(lines)


def _heuristic_fallback_brief(*, subject: str, junior_name: str,
                              rounds: int, verdict: str,
                              annotations: list[Annotation]) -> str:
    """Skeleton brief used when LLM call fails / parse-rejects.
    Better to ship something the owner can act on than to block delivery."""
    counts = Counter(a.status for a in annotations)
    return (
        f"# 会前简报 — {subject}\n\n"
        f"_Junior: {junior_name} · Rounds: {rounds} · 产出时间: {_now_iso()}_\n"
        f"_⚠️ 此 brief 由 fallback 模板生成 (LLM 不可用)；详细 audit 见 summary_audit.md_\n\n"
        f"## 1. 议题摘要\n(brief 主体生成失败；请直接读 junior 原文 + audit)\n\n"
        f"## 2. 核心数据\n(参 audit)\n\n"
        f"## 3. 团队自检结果\n"
        f"- Findings 总数: {len(annotations)}\n"
        f"- 接受: {counts.get('accepted', 0)} 条 / 保留异议: {counts.get('rejected', 0)} 条 / "
        f"无解: {counts.get('unresolvable', 0)} 条 / 未闭合: {counts.get('open', 0)} 条\n\n"
        f"## 4. 待决策事项\nVerdict: **{verdict}**。详见 audit。\n\n"
        f"## 5. 建议时间分配\n材料未达 LLM-summary 状态；建议异步对齐: 直接读 audit。\n\n"
        f"## 6. 风险提示\n⚠️ Brief 由 fallback 模板生成，owner 应直接核对 junior 原文。\n"
    )


def build_summary(*, subject: str, junior_name: str, junior_platform: str,
                  rounds: int, verdict: str,
                  document: str,
                  annotations: list[Annotation]) -> str:
    """Return the 6-section markdown brief.

    Falls back to deterministic skeleton on LLM failure (caller still
    writes summary.md so owner sees something).
    """
    template = _load_prompt()
    prompt = (
        template
        .replace("{subject}", subject)
        .replace("{junior_name}", junior_name)
        .replace("{junior_platform}", junior_platform)
        .replace("{rounds}", str(rounds))
        .replace("{verdict}", verdict)
        .replace("{ts}", _now_iso())
        .replace("{document}", document[:4000])
        .replace("{findings_summary}", _findings_summary_block(annotations))
        .replace("{dissent_log}", _dissent_log(annotations))
    )

    try:
        raw = _call_llm(prompt)
    except Exception as exc:
        logger.warning("build_summary: LLM call failed: %s", exc)
        return _heuristic_fallback_brief(
            subject=subject, junior_name=junior_name,
            rounds=rounds, verdict=verdict, annotations=annotations,
        )

    text = raw.strip()
    # If LLM didn't even start with the expected header, treat as garbage
    if not text.startswith("#"):
        logger.warning("build_summary: LLM output didn't start with markdown header; "
                       "raw=%r", text[:200])
        return _heuristic_fallback_brief(
            subject=subject, junior_name=junior_name,
            rounds=rounds, verdict=verdict, annotations=annotations,
        )

    return text
