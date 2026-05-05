#!/usr/bin/env python3
"""scripts/doctor.py — paid-review skill health check.

Run after install or whenever the skill behaves oddly. Verifies:
  - PAID_DIR layout (review/ subdirs, owner.json)
  - All 5 prompt templates present
  - Active sessions: meta loadable, stage in legal set, no orphan active_review_session
  - Sweep cron entry installed (best-effort, /etc/cron.d/paid-review-sweep)
  - Last sweep log mtime within 2× TTL (= sweep is actually firing)

Exit codes:
  0 = all OK or warnings only
  1 = at least one ERROR (broken state)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


_REQUIRED_PROMPTS = (
    "four_pillar.md", "responder_sim.md", "classify_reply.md",
    "summary.md", "final_gate.md",
)
_LEGAL_STAGES = {"INTAKE", "SUBJECT", "SCAN", "QA", "MERGE", "GATE", "CLOSED"}
_DEFAULT_CRON_PATH = Path("/etc/cron.d/paid-review-sweep")


@dataclass
class Check:
    severity: str   # "ok" | "warn" | "error"
    label: str
    detail: str = ""

    @property
    def icon(self) -> str:
        return {"ok": "[OK]", "warn": "[WARN]", "error": "[ERR]"}[self.severity]


def check_paid_dir_layout() -> list[Check]:
    from paid import storage
    out: list[Check] = []
    paid_dir = storage.PAID_DIR
    if not paid_dir.exists():
        return [Check("error", "PAID_DIR missing", str(paid_dir))]
    out.append(Check("ok", "PAID_DIR exists", str(paid_dir)))

    owner = paid_dir / "owner.json"
    if owner.exists():
        out.append(Check("ok", "owner.json present"))
    else:
        out.append(Check("warn", "owner.json missing",
                         "review skill needs an owner — run `paid install`"))

    review_dir = paid_dir / "review"
    if not review_dir.exists():
        out.append(Check("warn", "review/ subdir not created yet",
                         "first /review run will create it"))
        return out
    out.append(Check("ok", "review/ exists"))

    sessions = review_dir / "sessions"
    if sessions.exists():
        out.append(Check("ok", f"sessions/ — {sum(1 for _ in sessions.iterdir())} entries"))
    else:
        out.append(Check("ok", "sessions/ empty (no review yet)"))

    return out


def check_prompts() -> list[Check]:
    out: list[Check] = []
    prompts_dir = _ROOT / "paid_review" / "prompts"
    if not prompts_dir.exists():
        return [Check("error", "paid_review/prompts/ missing")]
    for name in _REQUIRED_PROMPTS:
        p = prompts_dir / name
        if not p.exists():
            out.append(Check("error", f"prompt missing: {name}"))
        elif p.stat().st_size == 0:
            out.append(Check("error", f"prompt empty: {name}"))
        else:
            out.append(Check("ok", f"prompt: {name}"))
    return out


def check_active_sessions() -> list[Check]:
    """Walk active sessions; flag orphans, illegal stages, missing meta."""
    from paid import storage, identity
    from paid_review.core import state as state_mod
    out: list[Check] = []
    sessions = storage.PAID_DIR / "review" / "sessions"
    if not sessions.exists():
        return [Check("ok", "no active sessions to audit")]

    active_sids: set[str] = set()
    for entry in sessions.iterdir():
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        sid = entry.name
        meta = entry / "meta.json"
        if not meta.exists():
            out.append(Check("warn", f"sid={sid} no meta.json"))
            continue
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except Exception as exc:
            out.append(Check("error", f"sid={sid} meta unreadable: {exc}"))
            continue
        stage = data.get("stage")
        if stage not in _LEGAL_STAGES:
            out.append(Check("error", f"sid={sid} illegal stage: {stage!r}"))
        if stage and stage != "CLOSED":
            active_sids.add(sid)

    out.append(Check("ok", f"active sessions: {len(active_sids)}"))

    # Cross-check: every cp.active_review_session points to a real session
    cps_dir = storage.PAID_DIR / "counterparties"
    orphans = 0
    if cps_dir.exists():
        for cp_dir in cps_dir.iterdir():
            profile = cp_dir / "profile.json"
            if not profile.exists():
                continue
            try:
                pdata = json.loads(profile.read_text(encoding="utf-8"))
            except Exception:
                continue
            ars = pdata.get("active_review_session", "")
            if ars and ars not in active_sids:
                orphans += 1
                out.append(Check("warn",
                                 f"cp={cp_dir.name} active_review_session={ars!r} "
                                 "but session missing/closed — will self-heal on next inbound"))
    if orphans == 0:
        out.append(Check("ok", "no orphan active_review_session pointers"))

    return out


def check_cron_entry(cron_path: Path = _DEFAULT_CRON_PATH) -> list[Check]:
    if not cron_path.exists():
        return [Check("warn", f"sweep cron not installed at {cron_path}",
                      "run install.sh or place cron entry manually")]
    body = cron_path.read_text(encoding="utf-8", errors="replace")
    if "sweep_review_sessions.py" not in body:
        return [Check("warn", f"{cron_path} exists but doesn't reference sweep_review_sessions.py")]
    return [Check("ok", f"sweep cron entry present at {cron_path}")]


def check_sweep_log_freshness(log_path: Path | None = None,
                              ttl_hours: int = 24) -> list[Check]:
    """Sweep is hourly; if log mtime hasn't moved in 2× TTL hours, cron is dead."""
    if log_path is None:
        log_path = Path.home() / ".hermes" / "paid" / "review_sweep_cron.log"
    if not log_path.exists():
        return [Check("warn", f"sweep log not found: {log_path}",
                      "either cron hasn't fired yet or log path differs")]
    age = datetime.now(timezone.utc) - datetime.fromtimestamp(
        log_path.stat().st_mtime, tz=timezone.utc
    )
    if age > timedelta(hours=2 * ttl_hours):
        return [Check("error",
                      f"sweep log stale: {age.total_seconds()/3600:.1f}h old > 2×{ttl_hours}h",
                      "cron may be broken — check `sudo systemctl status cron`")]
    return [Check("ok", f"sweep log fresh ({age.total_seconds()/3600:.1f}h old)")]


def run_all(cron_path: Path = _DEFAULT_CRON_PATH,
            sweep_log_path: Path | None = None) -> list[Check]:
    checks: list[Check] = []
    checks += check_paid_dir_layout()
    checks += check_prompts()
    checks += check_active_sessions()
    checks += check_cron_entry(cron_path)
    checks += check_sweep_log_freshness(sweep_log_path)
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON instead of human text.")
    parser.add_argument("--cron-path", type=Path, default=_DEFAULT_CRON_PATH)
    parser.add_argument("--sweep-log", type=Path, default=None)
    args = parser.parse_args()

    try:
        checks = run_all(cron_path=args.cron_path,
                         sweep_log_path=args.sweep_log)
    except Exception as exc:
        msg = f"doctor crashed: {exc}"
        if args.json:
            print(json.dumps({"error": msg}))
        else:
            print(f"[ERR] {msg}")
        return 1

    error_count = sum(1 for c in checks if c.severity == "error")

    if args.json:
        print(json.dumps([
            {"severity": c.severity, "label": c.label, "detail": c.detail}
            for c in checks
        ], ensure_ascii=False))
    else:
        for c in checks:
            line = f"{c.icon} {c.label}"
            if c.detail:
                line += f"  — {c.detail}"
            print(line)
        print()
        print(f"{error_count} error(s), "
              f"{sum(1 for c in checks if c.severity == 'warn')} warning(s), "
              f"{sum(1 for c in checks if c.severity == 'ok')} OK")

    return 1 if error_count else 0


if __name__ == "__main__":
    sys.exit(main())
