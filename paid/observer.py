"""Module OB — Usage Observation Learner (v1.6.3).

Scans audit_log.jsonl to compute observed usage statistics, writes them
to profile.observed.*, and generates a weekly digest for the owner.

Statistics computed:
  approval_rate           float   — fraction of decisions that were approved
  top_escalated_topics    list    — top-N topics most frequently escalated
  avg_reply_length_chars  float   — mean length of draft_answer strings
  preferred_decision_window_hrs float — 75th-pctile decision window (hrs)
  last_updated_at         str     — ISO timestamp of last scan

Auto-approve modes (configurable via preferences.observation_mode):
  "manual"     — default; never auto-apply; only weekly digest
  "suggested"  — DMs owner with suggestions, owner types yes/no
  "auto"       — applies all non-name/identity changes automatically

Cron setup: this module ships a ``bin/run_observer.py`` entry point
intended to be called via cron (e.g. daily at 02:00 UTC).

VPS cron line (add to reviewer or paid user crontab):
  0 2 * * * /home/paid/.hermes/plugins/paid-v1/bin/run_observer.py >> /home/paid/.hermes/paid/observer.log 2>&1
  0 9 * * 1 /home/paid/.hermes/plugins/paid-v1/bin/run_observer.py --weekly >> /home/paid/.hermes/paid/observer.log 2>&1
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import storage

logger = logging.getLogger(__name__)

_DEFAULT_TOP_N = 5
_DEFAULT_LOOKBACK_DAYS = 30


# ---------------------------------------------------------------------------
# Audit log scanning
# ---------------------------------------------------------------------------


def scan_audit_log(
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    audit_path: Path | None = None,
) -> dict:
    """Scan audit_log.jsonl and return raw stats dict.

    Returns a dict with keys matching profile.observed.* fields plus
    intermediate data for digest generation.
    """
    if audit_path is None:
        # v1.6.4: use merged read (per-cp + legacy)
        from . import audit as _audit
        entries = _audit.read_all_entries(lookback_days=lookback_days)
    else:
        # Direct path specified (tests / CLI pass-through)
        if not audit_path.exists():
            return _empty_stats()
        cutoff = _cutoff_ts(lookback_days)
        entries = _read_entries(audit_path, cutoff)

    if not entries:
        return _empty_stats()

    approval_rate = _compute_approval_rate(entries)
    top_topics = _compute_top_topics(entries, n=_DEFAULT_TOP_N)
    avg_reply_len = _compute_avg_reply_len(entries)
    p75_window = _compute_decision_window_p75(entries)

    return {
        "approval_rate": approval_rate,
        "top_escalated_topics": top_topics,
        "avg_reply_length_chars": avg_reply_len,
        "preferred_decision_window_hrs": p75_window,
        "entries_scanned": len(entries),
        "lookback_days": lookback_days,
    }


def _empty_stats() -> dict:
    return {
        "approval_rate": None,
        "top_escalated_topics": [],
        "avg_reply_length_chars": None,
        "preferred_decision_window_hrs": None,
        "entries_scanned": 0,
        "lookback_days": 0,
    }


def _cutoff_ts(days: int) -> float:
    """Unix timestamp for 'days ago'."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    return (now - timedelta(days=days)).timestamp()


def _read_entries(path: Path, cutoff_ts: float) -> list[dict]:
    entries: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                ts = _entry_ts(entry)
                if ts is not None and ts < cutoff_ts:
                    continue
                entries.append(entry)
    except OSError:
        pass
    return entries


def _entry_ts(entry: dict) -> float | None:
    """Extract Unix timestamp from an audit entry."""
    ts_str = entry.get("timestamp") or entry.get("ts") or entry.get("created_at")
    if ts_str is None:
        return None
    try:
        return datetime.fromisoformat(str(ts_str)).timestamp()
    except (ValueError, TypeError):
        return None


def _compute_approval_rate(entries: list[dict]) -> float | None:
    decisions = [e for e in entries if "decision" in e or "action" in e]
    if not decisions:
        return None
    approved = sum(
        1 for e in decisions
        if e.get("decision") in ("approved", "approve")
        or e.get("action") in ("approved", "approve")
    )
    return round(approved / len(decisions), 3)


def _compute_top_topics(entries: list[dict], n: int = 5) -> list[str]:
    from collections import Counter
    topic_counts: Counter = Counter()
    for e in entries:
        topics = e.get("topics") or e.get("matched_topics") or []
        if isinstance(topics, str):
            topics = [topics]
        for t in topics:
            if isinstance(t, str) and t.strip():
                topic_counts[t.strip()] += 1
    return [t for t, _ in topic_counts.most_common(n)]


def _compute_avg_reply_len(entries: list[dict]) -> float | None:
    lengths = []
    for e in entries:
        draft = e.get("draft_answer") or e.get("answer") or ""
        if draft and isinstance(draft, str):
            lengths.append(len(draft))
    if not lengths:
        return None
    return round(sum(lengths) / len(lengths), 1)


def _compute_decision_window_p75(entries: list[dict]) -> float | None:
    """75th percentile time between message receipt and owner decision (hrs)."""
    windows: list[float] = []
    for e in entries:
        received = e.get("received_at") or e.get("timestamp")
        decided = e.get("decided_at") or e.get("approved_at") or e.get("rejected_at")
        if received and decided:
            try:
                t_recv = datetime.fromisoformat(str(received)).timestamp()
                t_dec = datetime.fromisoformat(str(decided)).timestamp()
                if t_dec > t_recv:
                    windows.append((t_dec - t_recv) / 3600)
            except (ValueError, TypeError):
                pass
    if not windows:
        return None
    windows.sort()
    idx = max(0, int(len(windows) * 0.75) - 1)
    return round(windows[idx], 2)


# ---------------------------------------------------------------------------
# Write observed fields to profile
# ---------------------------------------------------------------------------


def update_profile_observed(stats: dict) -> bool:
    """Write stats into profile.observed.*. Returns True if profile updated."""
    from . import profile as _profile

    prof = _profile.load_profile()
    if prof is None:
        logger.info("observer: no profile to update")
        return False

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    changed = False

    def _set(attr: str, val: Any) -> None:
        nonlocal changed
        if val is not None and getattr(prof.observed, attr, None) != val:
            setattr(prof.observed, attr, val)
            changed = True

    _set("approval_rate", stats.get("approval_rate"))
    _set("top_escalated_topics", stats.get("top_escalated_topics") or [])
    _set("avg_reply_length_chars", stats.get("avg_reply_length_chars"))
    _set("preferred_decision_window_hrs", stats.get("preferred_decision_window_hrs"))
    prof.observed.last_updated_at = now

    if changed:
        _profile.save_profile(prof)
        logger.info("observer: updated profile.observed (entries=%d)", stats.get("entries_scanned", 0))
    return changed


# ---------------------------------------------------------------------------
# Weekly digest
# ---------------------------------------------------------------------------


def build_weekly_digest(stats: dict) -> str:
    """Build a human-readable weekly digest string for DM to owner."""
    lines = [
        "📊 PAID 本周观察（过去 30 天）\n",
    ]
    n = stats.get("entries_scanned", 0)
    if n == 0:
        return "📊 PAID 本周观察：过去 30 天没有 audit 记录，还没有数据。"

    lines.append(f"• 共处理 **{n}** 条记录")

    rate = stats.get("approval_rate")
    if rate is not None:
        lines.append(f"• 审批通过率：**{rate:.0%}**")

    avg_len = stats.get("avg_reply_length_chars")
    if avg_len is not None:
        lines.append(f"• 平均回复长度：**{avg_len:.0f}** 字符")

    p75 = stats.get("preferred_decision_window_hrs")
    if p75 is not None:
        lines.append(f"• 75% 决策窗口：**{p75:.1f}** 小时")

    topics = stats.get("top_escalated_topics") or []
    if topics:
        lines.append(f"• 高频 escalate 话题：{', '.join(topics[:5])}")

    lines.append("\n发 `/paid-setup` 查看 / 更新 profile 设置。")
    return "\n".join(lines)


def send_weekly_digest(platform: str, owner_id: str, chat_id: str = "") -> None:
    """Compute stats, update profile, send weekly digest DM to owner."""
    from . import hermes_io

    stats = scan_audit_log()
    update_profile_observed(stats)
    digest = build_weekly_digest(stats)
    target = chat_id or owner_id
    try:
        hermes_io.send_dm(platform, target, digest, fallback_to_queue=True)
        logger.info("observer: sent weekly digest to %s:%s", platform, owner_id)
    except Exception as e:
        logger.warning("observer: send_dm failed: %s", e)


# ---------------------------------------------------------------------------
# Entry point for cron
# ---------------------------------------------------------------------------


def run_daily(send_digest: bool = False) -> int:
    """Run daily observation pass. Returns 0 on success, 1 on error.

    If send_digest=True, also send weekly digest to the owner.
    """
    try:
        from . import identity as _identity
        stats = scan_audit_log()
        update_profile_observed(stats)
        logger.info(
            "observer: daily scan complete entries=%d",
            stats.get("entries_scanned", 0),
        )
        if send_digest:
            owner = _identity.load_owner()
            if owner:
                identities = getattr(owner, "identities", []) or []
                for ident in identities:
                    if isinstance(ident, dict) and ident.get("enabled"):
                        platform = ident.get("platform", "")
                        uid = ident.get("user_id", "")
                        if platform and uid:
                            send_weekly_digest(platform, uid)
                            break
        return 0
    except Exception as e:
        logger.error("observer: run_daily failed: %s", e, exc_info=True)
        return 1
