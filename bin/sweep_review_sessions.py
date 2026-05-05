#!/usr/bin/env python3
"""sweep_review_sessions — hourly TTL sweep for paid-review sessions.

Spec §11 (R6 mitigation): scan ~/.hermes/paid/review/sessions/<sid>/meta.json
for any session with stage != CLOSED whose last_inbound_at is older than
PAID_REVIEW_SESSION_TTL_HOURS (default 24, hard ceiling 72). Force-close each
expired session with reason="TTL_expired". The cron entry runs hourly under
the paid user's hermes venv; output goes to ~/.hermes/paid/review_sweep_cron.log.

Exit 0 always (cron mustn't get e-mail spam from a flaky LLM); failures of
individual sessions are logged but don't fail the whole sweep.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make `paid` and `paid_review` importable when invoked via cron from anywhere
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

logger = logging.getLogger("paid_review.sweep")


_TTL_DEFAULT_HOURS = 24
_TTL_CEILING_HOURS = 72


def _ttl_hours_from_env() -> int:
    raw = os.environ.get("PAID_REVIEW_SESSION_TTL_HOURS", "")
    if not raw.strip():
        return _TTL_DEFAULT_HOURS
    try:
        n = int(raw)
    except ValueError:
        logger.warning("Invalid PAID_REVIEW_SESSION_TTL_HOURS=%r, using default", raw)
        return _TTL_DEFAULT_HOURS
    if n < 1:
        return 1
    if n > _TTL_CEILING_HOURS:
        return _TTL_CEILING_HOURS
    return n


def _parse_iso(stamp: str) -> datetime | None:
    if not stamp:
        return None
    try:
        # Tolerate both Z suffix and +00:00
        if stamp.endswith("Z"):
            stamp = stamp[:-1] + "+00:00"
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


def sweep(now: datetime | None = None, ttl_hours: int | None = None) -> dict:
    """Walk active sessions, force_close those past TTL.

    Returns a summary dict: {scanned, expired, errors, closed_sids}.
    Caller uses for logging; never raises out.
    """
    from paid import storage
    from paid_review import api as review_api
    from paid_review.core import state as state_mod

    now = now or datetime.now(timezone.utc)
    ttl = ttl_hours if ttl_hours is not None else _ttl_hours_from_env()
    cutoff = now - timedelta(hours=ttl)

    sessions_root = storage.PAID_DIR / "review" / "sessions"
    summary = {"scanned": 0, "expired": 0, "errors": 0,
               "closed_sids": [], "ttl_hours": ttl}

    if not sessions_root.exists():
        logger.info("[sweep] no sessions dir at %s", sessions_root)
        return summary

    for entry in sorted(sessions_root.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("_"):  # skip _closed/
            continue
        sid = entry.name
        meta = entry / "meta.json"
        if not meta.exists():
            continue
        summary["scanned"] += 1

        st = state_mod.load_state(sid)
        if st is None:
            logger.warning("[sweep] sid=%s meta unreadable, skipping", sid)
            continue
        if st.stage == "CLOSED":
            continue

        anchor = _parse_iso(st.last_inbound_at) or _parse_iso(st.updated_at) \
                 or _parse_iso(st.created_at)
        if anchor is None:
            logger.warning("[sweep] sid=%s no timestamp, skipping", sid)
            continue

        if anchor >= cutoff:
            continue  # still within TTL

        age_h = (now - anchor).total_seconds() / 3600.0
        logger.info(
            "[sweep] sid=%s stage=%s anchor=%s age=%.1fh > ttl=%dh → force_close",
            sid, st.stage, anchor.isoformat(), age_h, ttl,
        )
        try:
            review_api.force_close(sid, reason="TTL_expired")
            summary["expired"] += 1
            summary["closed_sids"].append(sid)
        except Exception as exc:
            logger.warning("[sweep] sid=%s force_close failed: %s", sid, exc)
            summary["errors"] += 1
            continue

        # Cron is out-of-band: no plugin to clear cp.active_review_session.
        # Mirror the side-effect manually so the junior can start a new session.
        try:
            from paid import identity as _identity
            cp = _identity.load_counterparty_by_cp_id(st.cp_id) \
                if hasattr(_identity, "load_counterparty_by_cp_id") else None
            if cp is None and st.platform and st.cp_id:
                # cp_id format: "<platform>_<user_id>" — strip prefix
                prefix = f"{st.platform}_"
                user_id = st.cp_id[len(prefix):] if st.cp_id.startswith(prefix) \
                    else st.cp_id
                cp = _identity.load_counterparty(st.platform, user_id)
            if cp is not None and getattr(cp, "active_review_session", "") == sid:
                _identity.clear_active_review_session(
                    cp,
                    archive={"sid": sid, "closed_reason": "TTL_expired",
                             "closed_at": now.isoformat()},
                )
        except Exception as exc:
            logger.warning(
                "[sweep] sid=%s force_close OK but cp clear failed: %s", sid, exc
            )

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ttl-hours", type=int, default=None,
        help="Override TTL (else PAID_REVIEW_SESSION_TTL_HOURS or 24).",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Only log warnings (cron noise reduction).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    try:
        summary = sweep(ttl_hours=args.ttl_hours)
    except Exception as exc:
        logger.error("[sweep] crashed: %s", exc, exc_info=True)
        return 0  # never fail loud — cron repeat next hour

    logger.info(
        "[sweep] done scanned=%d expired=%d errors=%d ttl=%dh",
        summary["scanned"], summary["expired"], summary["errors"],
        summary["ttl_hours"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
