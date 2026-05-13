"""paid_review.core.build_audit — deterministic summary_audit.md (Sprint C).

Generates the audit-trail companion to the LLM-built brief. NO LLM call;
purely templated from on-disk data. The owner uses this when they want
to see the raw decision history (which findings, what status, who said
what) without the LLM's editorial framing.

Spec reference: 04 §7 (output 规范) summary_audit.md.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from paid_review.core.annotation import Annotation


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _counts_table(annotations: list[Annotation]) -> str:
    by_pillar: dict[str, Counter] = {}
    for a in annotations:
        by_pillar.setdefault(a.pillar, Counter())[a.status] += 1
    if not by_pillar:
        return "(no findings)"
    lines = [
        "| Pillar | open | accepted | rejected | modified | unresolvable |",
        "|---|---|---|---|---|---|",
    ]
    for pillar in sorted(by_pillar.keys()):
        c = by_pillar[pillar]
        lines.append(
            f"| {pillar} | {c.get('open', 0)} | {c.get('accepted', 0)} | "
            f"{c.get('rejected', 0)} | {c.get('modified', 0)} | {c.get('unresolvable', 0)} |"
        )
    return "\n".join(lines)


def _section_findings(annotations: list[Annotation], target_status: str,
                      heading: str) -> str:
    matched = [a for a in annotations if a.status == target_status]
    if not matched:
        return f"## {heading}\n\n(none)\n"
    lines = [f"## {heading}\n"]
    for a in matched:
        snippet = a.text.replace("\n", "\n  ")
        lines.append(f"- **[{a.pillar}]** {snippet}\n  _id: {a.id}_\n")
    return "\n".join(lines)


def build_audit(*, subject: str, junior_name: str, junior_platform: str,
                rounds: int, verdict: str,
                annotations: list[Annotation],
                forced_reason: str = "") -> str:
    """Return the markdown body. Caller (deliver.py) writes to disk."""
    head = (
        f"# Review Audit Trail — {subject}\n\n"
        f"_Junior: {junior_name} ({junior_platform}) · "
        f"Rounds: {rounds} · Verdict: **{verdict}**"
    )
    if forced_reason:
        head += f" ({forced_reason})"
    head += f" · 产出时间: {_now_iso()}_\n"

    counts_section = (
        "\n## 4 柱 × status 计数表\n\n" + _counts_table(annotations) + "\n"
    )
    accepted = _section_findings(annotations, "accepted", "已接受 (将进 final 文档)")
    rejected = _section_findings(annotations, "rejected", "保留异议 (junior 不同意)")
    modified = _section_findings(annotations, "modified", "已 modified (junior 提了改版)")
    unresolvable = _section_findings(
        annotations, "unresolvable", "无解 (留 owner 决定)",
    )
    open_items = _section_findings(annotations, "open", "未闭合 (rounds 上限或 force-close)")

    return "\n".join([
        head,
        counts_section,
        accepted,
        rejected,
        modified,
        unresolvable,
        open_items,
    ])
