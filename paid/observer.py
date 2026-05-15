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
    """Scan audit + pending logs and return raw stats dict.

    v1.6.8: the compute functions were rewritten to read the real audit
    schema (classification.topic, action.state, extra.assistant_response_preview)
    and to read pending.jsonl for approval-rate + decision-window stats.
    Before this fix every stat was permanently 0/null in production because
    the compute functions looked for top-level ``decision`` / ``topics``
    keys that PAID has never written.

    Returns a dict with keys matching profile.observed.* fields plus
    intermediate data for digest generation.
    """
    if audit_path is None:
        from . import audit as _audit
        entries = _audit.read_all_entries(lookback_days=lookback_days)
        # Approval rate + decision window come from pending.jsonl events,
        # not audit rows. Read them here so callers don't have to.
        pending_events = _read_all_pending_events(lookback_days)
    else:
        # Direct path specified (tests / CLI pass-through). Tests that supply
        # a synthetic audit_path don't have pending logs — fall back to []
        # so the legacy test surface still works.
        if not audit_path.exists():
            return _empty_stats()
        cutoff = _cutoff_ts(lookback_days)
        entries = _read_entries(audit_path, cutoff)
        pending_events = []

    if not entries and not pending_events:
        return _empty_stats()

    approval_rate = _compute_approval_rate(pending_events)
    top_topics = _compute_top_topics(entries, n=_DEFAULT_TOP_N)
    avg_reply_len = _compute_avg_reply_len(entries)
    p75_window = _compute_decision_window_p75(pending_events)

    return {
        "approval_rate": approval_rate,
        "top_escalated_topics": top_topics,
        "avg_reply_length_chars": avg_reply_len,
        "preferred_decision_window_hrs": p75_window,
        "entries_scanned": len(entries),
        "pending_events_scanned": len(pending_events),
        "lookback_days": lookback_days,
    }


def _read_all_pending_events(lookback_days: int) -> list[dict]:
    """Read every pending event (create + status) across all per-cp dirs.

    v1.6.8: matches the v1.6.4 per-cp layout (counterparties/<cp>/pending.jsonl).
    Also reads legacy pending_approvals.jsonl for any grace-period rows
    that the migration left behind. Filters by lookback_days using each
    event's ``ts`` (which is a Unix float in pending.jsonl, not isoformat
    like audit.jsonl — both are handled).
    """
    from . import storage
    cutoff = _cutoff_ts(lookback_days)
    events: list[dict] = []

    candidates: list[Path] = []
    cp_dir = storage.PAID_DIR / "counterparties"
    if cp_dir.exists():
        candidates.extend(cp_dir.glob("*/pending.jsonl"))
    legacy = storage.PAID_DIR / "pending_approvals.jsonl"
    if legacy.exists():
        candidates.append(legacy)

    for p in candidates:
        try:
            with p.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(ev, dict):
                        continue
                    ts = ev.get("ts")
                    # pending.jsonl uses Unix float; tolerate ISO too.
                    try:
                        if isinstance(ts, (int, float)):
                            ts_unix = float(ts)
                        else:
                            ts_unix = datetime.fromisoformat(str(ts)).timestamp()
                    except (ValueError, TypeError):
                        ts_unix = None
                    if ts_unix is not None and ts_unix < cutoff:
                        continue
                    events.append(ev)
        except OSError:
            continue
    return events


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


def _compute_approval_rate(pending_events: list[dict]) -> float | None:
    """Fraction of resolved approvals owner approved.

    v1.6.8: reads pending.jsonl status events (the actual decision record).
    Denominator = approved + rejected + timed_out. ``timed_out`` counts
    against approval rate because those are owner ghosts. Returns None
    when no decisions have been resolved.
    """
    decided = 0
    approved = 0
    for ev in pending_events:
        if ev.get("type") != "status":
            continue
        status = ev.get("status")
        if status in ("approved", "rejected", "timed_out"):
            decided += 1
            if status == "approved":
                approved += 1
    if decided == 0:
        return None
    return round(approved / decided, 3)


def _compute_top_topics(entries: list[dict], n: int = 5) -> list[str]:
    """Top-N topics that hit the request (escalation) state.

    v1.6.8: reads ``classification.topic`` and filters by
    ``action.state == "request"``. Before this fix it looked for top-level
    ``topics`` / ``matched_topics`` keys that have never existed in the
    audit schema, so the result was always empty.
    """
    from collections import Counter
    topic_counts: Counter = Counter()
    for e in entries:
        action = e.get("action") or {}
        if not isinstance(action, dict):
            continue
        if action.get("state") != "request":
            continue
        cls = e.get("classification") or {}
        if not isinstance(cls, dict):
            continue
        topic = cls.get("topic")
        if isinstance(topic, str) and topic.strip():
            topic_counts[topic.strip()] += 1
    return [t for t, _ in topic_counts.most_common(n)]


def _compute_avg_reply_len(entries: list[dict]) -> float | None:
    """Mean length of PAID's actual outbound replies.

    v1.6.8: reads ``extra.assistant_response_preview`` from post_llm audit
    rows — that's the recorded text PAID actually sent. Falls back to
    ``classification.draft_answer`` for pre_llm rows where the post_llm
    row hasn't landed yet. Previous code looked for top-level
    ``draft_answer`` / ``answer`` which the audit schema doesn't have.
    """
    lengths = []
    for e in entries:
        extra = e.get("extra") or {}
        reply = extra.get("assistant_response_preview") if isinstance(extra, dict) else None
        if not reply:
            cls = e.get("classification") or {}
            if isinstance(cls, dict):
                reply = cls.get("draft_answer") or ""
        if reply and isinstance(reply, str):
            lengths.append(len(reply))
    if not lengths:
        return None
    return round(sum(lengths) / len(lengths), 1)


def _compute_decision_window_p75(pending_events: list[dict]) -> float | None:
    """75th percentile of (status_ts − create_ts), in hours.

    v1.6.8: rewritten to use pending.jsonl events. The audit log has no
    ``received_at`` / ``decided_at`` fields — those names were a guess in
    v1.6.3 that has never matched production data. Now we walk pending.jsonl
    pairing each ``type=create`` with its first non-``timed_out``
    ``type=status`` event by request_id.

    Timed-out approvals are excluded from the window because they reflect
    owner-never-saw-it latency, not decision speed.

    P75 uses linear interpolation (matches numpy.percentile) per v1.6.6 SF6.
    """
    creates: dict[str, float] = {}
    decisions: dict[str, float] = {}
    for ev in pending_events:
        rid = ev.get("request_id")
        ts = ev.get("ts")
        if not rid or not isinstance(ts, (int, float)):
            continue
        kind = ev.get("type")
        if kind == "create":
            creates[rid] = float(ts)
        elif kind == "status":
            status = ev.get("status")
            if status in ("approved", "rejected") and rid not in decisions:
                decisions[rid] = float(ts)

    windows: list[float] = []
    for rid, ct in creates.items():
        dt = decisions.get(rid)
        if dt is not None and dt > ct:
            windows.append((dt - ct) / 3600.0)

    if not windows:
        return None
    return round(_percentile_linear(windows, 0.75), 2)


def _percentile_linear(values: list[float], q: float) -> float:
    """Linear-interpolation percentile (matches numpy.percentile default).

    For sorted values v[0..n-1] and target quantile q ∈ [0,1]:
      pos = q * (n - 1)
      lo, hi = floor(pos), ceil(pos)
      return v[lo] + (v[hi] - v[lo]) * (pos - lo)
    """
    if not values:
        raise ValueError("percentile of empty sequence")
    s = sorted(values)
    n = len(s)
    if n == 1:
        return s[0]
    pos = q * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return s[lo] + (s[hi] - s[lo]) * frac


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

    # v1.6.8: always stamp last_updated_at when the scan runs (even if no
    # stat values changed). Before this fix, last_updated_at stayed empty
    # forever because the file wasn't persisted unless something changed,
    # making it impossible to tell whether the observer cron was working.
    prev_last = prof.observed.last_updated_at
    prof.observed.last_updated_at = now
    if changed or prev_last != now:
        _profile.save_profile(prof)
        logger.info(
            "observer: updated profile.observed (entries=%d, changed=%s)",
            stats.get("entries_scanned", 0), changed,
        )
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
