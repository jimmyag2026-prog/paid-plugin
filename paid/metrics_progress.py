"""Module MP — six-indicator progress card (v1.5.5 A6).

Surfaces the master-design §6 hard indicators on dashboard so owner can
see at a glance which are checked off.

Six indicators (owner decision 2026-05-14: drop 7th TBD):
  1. ≥1 pilot 走完任务全周期         (derived)
  2. 周报自动生成 ≥2 周                (derived from weekly_reports/)
  3. 跨组织 demo                       (manual flag)
  4. Twitter 长文 + demo 视频          (manual flag)
  5. 5 个相关方深度私聊                (manual count, 0..5)
  6. README 给陌生人看                 (manual flag)

Manual flags live under ``settings.metrics_progress.*``. Reading settings
each call (no cache) so an operator can edit settings.json and refresh
the dashboard to see updates.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import identity, settings, storage


_WEEKLY_REPORTS_DIR = "weekly_reports"
_DEEP_CHATS_TARGET = 5


def _read_audit_log() -> list[dict[str, Any]]:
    # v1.6.4: merged read (per-cp + legacy)
    from . import audit as _audit
    return _audit.read_all_entries()


def _indicator_1_pilot_cycle() -> dict[str, Any]:
    """Indicator #1: ≥1 pilot 走完任务全周期.

    PAID currently has no first-class "task" concept (M7.1 not started),
    so fallback approximation: ≥1 cp with role=junior has received at
    least one `state=direct` decision. Honest about the approximation
    via the `detail` field.
    """
    cps = identity.list_all_counterparties()
    junior_cps = [c for c in cps if c.role == "junior"]
    if not junior_cps:
        return {
            "id": 1,
            "title": "≥1 pilot 走完任务全周期",
            "status": "pending",
            "detail": "no junior cp registered yet",
            "source": "derived",
        }
    junior_ids = {c.cp_id for c in junior_cps}
    audit = _read_audit_log()
    direct_juniors = set()
    for r in audit:
        cp_id = r.get("counterparty")
        if cp_id not in junior_ids:
            continue
        action = r.get("action") or {}
        if isinstance(action, dict) and action.get("state") == "direct":
            direct_juniors.add(cp_id)
    if direct_juniors:
        return {
            "id": 1,
            "title": "≥1 pilot 走完任务全周期",
            "status": "done",
            "detail": (
                f"{len(direct_juniors)} junior cp received ≥1 direct response "
                f"(approximate: PAID lacks task primitive — M7.1 will refine)"
            ),
            "source": "derived",
        }
    return {
        "id": 1,
        "title": "≥1 pilot 走完任务全周期",
        "status": "pending",
        "detail": (
            f"{len(junior_cps)} junior cp(s) registered, none yet received direct response"
        ),
        "source": "derived",
    }


def _indicator_2_weekly_reports() -> dict[str, Any]:
    """Indicator #2: 周报自动生成 ≥2 周.

    Counts files under ``~/.hermes/paid/weekly_reports/`` (any extension).
    M7.3 will define the canonical schema; this collector just counts.
    """
    root = storage.PAID_DIR / _WEEKLY_REPORTS_DIR
    if not root.exists():
        return {
            "id": 2,
            "title": "周报自动生成 ≥2 周",
            "status": "pending",
            "detail": "weekly_reports/ dir does not exist (M7.3 not implemented)",
            "source": "derived",
        }
    try:
        files = [f for f in root.iterdir() if f.is_file()]
    except Exception:
        files = []
    n = len(files)
    status = "done" if n >= 2 else "pending"
    return {
        "id": 2,
        "title": "周报自动生成 ≥2 周",
        "status": status,
        "detail": f"{n} weekly report(s) found",
        "source": "derived",
    }


def _manual_flag(cfg: dict, key: str, idx: int, title: str) -> dict[str, Any]:
    val = bool(cfg.get(key, False))
    return {
        "id": idx,
        "title": title,
        "status": "done" if val else "pending",
        "detail": "marked done in settings.metrics_progress" if val
                  else f"set settings.metrics_progress.{key}=true when done",
        "source": "manual",
    }


def _indicator_5_deep_chats(cfg: dict) -> dict[str, Any]:
    raw = cfg.get("deep_chats_count", 0)
    try:
        count = max(0, int(raw))
    except (TypeError, ValueError):
        count = 0
    return {
        "id": 5,
        "title": f"{_DEEP_CHATS_TARGET} 个相关方深度私聊",
        "status": "done" if count >= _DEEP_CHATS_TARGET else "pending",
        "detail": f"{count}/{_DEEP_CHATS_TARGET} logged",
        "source": "manual",
    }


def collect() -> list[dict[str, Any]]:
    """Return the 6 indicator rows."""
    cfg = settings.load().get("metrics_progress") or {}
    if not isinstance(cfg, dict):
        cfg = {}
    return [
        _indicator_1_pilot_cycle(),
        _indicator_2_weekly_reports(),
        _manual_flag(cfg, "cross_org_demo_done",      3, "跨组织 demo"),
        _manual_flag(cfg, "twitter_long_post_done",   4, "Twitter 长文 + demo 视频"),
        _indicator_5_deep_chats(cfg),
        _manual_flag(cfg, "readme_for_strangers_done", 6, "README 给陌生人看"),
    ]


def n_done(rows: list[dict[str, Any]]) -> int:
    return sum(1 for r in rows if r.get("status") == "done")
