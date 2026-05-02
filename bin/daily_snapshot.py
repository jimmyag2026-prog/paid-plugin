#!/usr/bin/env python3
"""Roll up today's PAID activity into a single daily Markdown log.

Why: dogfood weeks generate noise across audit_log.jsonl, plugin_runtime.log,
fatal_alerts.jsonl, pending_approvals.jsonl. Eyeballing them at 11pm to find
"how did PAID actually do today" is painful. This script aggregates one day's
worth into ``~/.hermes/paid/daily/<YYYY-MM-DD>.md`` so review takes 2 min.

Usage::

    python3 bin/daily_snapshot.py                # today (UTC)
    python3 bin/daily_snapshot.py --date 2026-05-02
    python3 bin/daily_snapshot.py --since 7      # rolling 7-day digest

Output sections:
    - Summary counts (direct / request / decline / L1 hits / L4 hits)
    - Per-counterparty activity
    - Pending approvals still open
    - Top 5 question previews per state
    - Fatal alerts (verbatim)

The script is read-only; it never mutates state.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from paid import approval, storage  # noqa: E402


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _within(entry_ts: str, start: datetime, end: datetime) -> bool:
    dt = _parse_iso(entry_ts)
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return start <= dt < end


def _bucketize(audit: list[dict]) -> dict[str, Any]:
    """Compute summary counters + per-cp activity from a slice of audit rows."""
    state_counts: Counter[str] = Counter()
    cp_activity: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "states": Counter(), "last_q": "", "last_ts": ""}
    )
    l1_hits = 0
    l4_hits = 0
    fallback_hits = 0
    by_state_qs: dict[str, list[str]] = defaultdict(list)

    for row in audit:
        action = row.get("action") or {}
        state = (action or {}).get("state")
        cp = row.get("counterparty")
        ts = row.get("ts", "")
        msg = row.get("junior_msg", "")
        extra = row.get("extra") or {}

        if state:
            state_counts[state] += 1
            if msg and len(by_state_qs[state]) < 5:
                by_state_qs[state].append(msg[:120])

        if extra.get("blocked_by") == "layer_1_prompt_injection":
            l1_hits += 1
        if extra.get("l4_ok") is False:
            l4_hits += 1
        if extra.get("fallback") is True:
            fallback_hits += 1

        if cp:
            entry = cp_activity[cp]
            entry["count"] += 1
            if state:
                entry["states"][state] += 1
            if ts > entry["last_ts"]:
                entry["last_ts"] = ts
                if msg:
                    entry["last_q"] = msg[:120]

    return {
        "state_counts": state_counts,
        "cp_activity": cp_activity,
        "l1_hits": l1_hits,
        "l4_hits": l4_hits,
        "fallback_hits": fallback_hits,
        "by_state_qs": by_state_qs,
        "total_audit_rows": len(audit),
    }


def _render(start: datetime, end: datetime, bundle: dict, fatal: list[dict]) -> str:
    sc: Counter[str] = bundle["state_counts"]
    total_decisions = sum(sc.values())
    direct = sc.get("direct", 0)
    request = sc.get("request", 0)
    decline = sc.get("decline", 0)

    direct_rate = (direct / total_decisions * 100) if total_decisions else 0
    pending_now = approval.list_pending()

    lines: list[str] = []
    lines.append(f"# PAID daily snapshot — {start.date().isoformat()}")
    lines.append("")
    lines.append(f"_Window (UTC): {start.isoformat()} → {end.isoformat()}_")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- audit rows: **{bundle['total_audit_rows']}**")
    lines.append(
        f"- decisions: direct **{direct}** · request **{request}** · "
        f"decline **{decline}**"
    )
    lines.append(
        f"- direct-rate: **{direct_rate:.0f}%** "
        + ("✅ ≥ 50% target" if direct_rate >= 50 else "🔴 below 50% target")
    )
    lines.append(f"- L1 prompt-injection hits: **{bundle['l1_hits']}**")
    lines.append(f"- L4 output-leakage hits: **{bundle['l4_hits']}**")
    lines.append(f"- classifier fallback (LLM error) hits: **{bundle['fallback_hits']}**")
    lines.append(f"- pending approvals right now: **{len(pending_now)}**")
    lines.append("")

    cp_activity: dict = bundle["cp_activity"]
    if cp_activity:
        lines.append("## Per-counterparty activity")
        lines.append("")
        rows = sorted(
            cp_activity.items(), key=lambda kv: kv[1]["count"], reverse=True
        )
        for cp_id, info in rows:
            states = "/".join(f"{k}:{v}" for k, v in info["states"].most_common())
            lines.append(
                f"- `{cp_id}`  msgs={info['count']}  ({states})  "
                f"last: \"{info['last_q']}\""
            )
        lines.append("")

    if pending_now:
        lines.append("## Open pending approvals")
        lines.append("")
        for r in pending_now:
            age_min = int((datetime.now(timezone.utc).timestamp() - r.ts_created) / 60)
            lines.append(
                f"- #`{r.request_id}` {r.counterparty_display or r.counterparty_user_id} "
                f"({r.counterparty_platform}) topic={r.topic} stakes={r.stakes} "
                f"age={age_min}m"
            )
            lines.append(f"  > Q: {r.junior_question[:120]}")
        lines.append("")

    by_state_qs: dict = bundle["by_state_qs"]
    if any(by_state_qs.values()):
        lines.append("## Question samples by state")
        lines.append("")
        for state in ("direct", "request", "decline"):
            qs = by_state_qs.get(state) or []
            if not qs:
                continue
            lines.append(f"### {state}")
            for q in qs:
                lines.append(f"- {q}")
            lines.append("")

    if fatal:
        lines.append("## Fatal alerts (verbatim)")
        lines.append("")
        for entry in fatal[-20:]:  # cap noise
            lines.append(f"- `{entry.get('ts','')}`  {entry.get('reason','')}")
            detail = entry.get("detail")
            if detail:
                lines.append(f"  > {str(detail)[:240]}")
        lines.append("")

    lines.append("---")
    lines.append(
        f"_Generated by `bin/daily_snapshot.py` at "
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}_"
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--date", help="UTC date YYYY-MM-DD (default: today)")
    p.add_argument(
        "--since",
        type=int,
        metavar="N",
        help="Rolling N-day digest ending today (default: single day)",
    )
    p.add_argument(
        "--out",
        help="Output path (default: ~/.hermes/paid/daily/<date>.md)",
    )
    args = p.parse_args()

    if args.date:
        end_date = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        end_date = datetime.now(timezone.utc).date()
    end = datetime(end_date.year, end_date.month, end_date.day, tzinfo=timezone.utc) + timedelta(days=1)
    start = end - timedelta(days=(args.since or 1))

    audit = _read_jsonl(storage.PAID_DIR / "audit_log.jsonl")
    fatal = _read_jsonl(storage.PAID_DIR / "fatal_alerts.jsonl")

    audit_in_window = [r for r in audit if _within(r.get("ts", ""), start, end)]
    fatal_in_window = [r for r in fatal if _within(r.get("ts", ""), start, end)]

    bundle = _bucketize(audit_in_window)
    md = _render(start, end, bundle, fatal_in_window)

    out_path = Path(args.out) if args.out else (
        storage.PAID_DIR / "daily" / f"{end_date.isoformat()}.md"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    print(f"wrote {out_path}  ({len(md)} chars)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
