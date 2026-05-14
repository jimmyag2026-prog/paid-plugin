"""paid_review.core.deliver — write artifacts + archive (Sprint C).

When GATE → CLOSED happens (or any force_close), deliver:
  1. Write summary.md (LLM 6-section brief) to sessions/<sid>/
  2. Write summary_audit.md (deterministic) to sessions/<sid>/
  3. Move sessions/<sid>/ → sessions/_closed/<YYYY-MM>/<sid>/

Spec §6 約定: skill 永不直接 send_dm. The IM push to owner / junior is
the plugin glue's job — deliver returns the brief text via ReviewReply
and lets api → plugin do the actual send.

So this module is purely **filesystem operations** + invoking
build_summary + build_audit. No IM I/O.
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from paid_review.core.annotation import Annotation, load_annotations
from paid_review.core.build_audit import build_audit
from paid_review.core.build_summary import build_summary

logger = logging.getLogger(__name__)


def _archive_month(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m")


def write_artifacts(sid_dir: Path, *,
                    subject: str, junior_name: str, junior_platform: str,
                    rounds: int, verdict: str,
                    document: str,
                    forced_reason: str = "",
                    ingest_sources: list[dict] | None = None,
                    ingest_errors: list[str] | None = None) -> dict[str, str]:
    """Write summary.md + summary_audit.md inside sid_dir.

    v1.5: optional ingest_sources / ingest_errors are propagated into the
    brief — caller (api.py) reads them off SessionState and passes here.
    Defaults preserve pre-v1.5 callers that don't know about ingest audit.

    Returns the textual contents (for caller to put in ReviewReply.text
    so owner gets the brief in IM).
    """
    annotations = load_annotations(sid_dir)

    summary_text = build_summary(
        subject=subject, junior_name=junior_name,
        junior_platform=junior_platform,
        rounds=rounds, verdict=verdict,
        document=document, annotations=annotations,
        ingest_sources=ingest_sources,
        ingest_errors=ingest_errors,
    )
    audit_text = build_audit(
        subject=subject, junior_name=junior_name,
        junior_platform=junior_platform,
        rounds=rounds, verdict=verdict,
        annotations=annotations, forced_reason=forced_reason,
    )

    sid_dir.mkdir(parents=True, exist_ok=True)
    (sid_dir / "summary.md").write_text(summary_text, encoding="utf-8")
    (sid_dir / "summary_audit.md").write_text(audit_text, encoding="utf-8")

    return {"summary": summary_text, "audit": audit_text}


def archive(sid_dir: Path, sessions_root: Path) -> Path:
    """Move sid_dir → sessions_root/_closed/<YYYY-MM>/<sid>/.

    Returns the new path. Idempotent: if destination already exists
    (rare race), append a timestamp suffix.
    """
    sid = sid_dir.name
    dest_root = sessions_root / "_closed" / _archive_month()
    dest_root.mkdir(parents=True, exist_ok=True)
    dest = dest_root / sid
    if dest.exists():
        dest = dest_root / f"{sid}.{int(datetime.now(timezone.utc).timestamp())}"
    shutil.move(str(sid_dir), str(dest))
    return dest


def deliver(sid_dir: Path, sessions_root: Path, *,
            subject: str, junior_name: str, junior_platform: str,
            rounds: int, verdict: str,
            document: str,
            forced_reason: str = "",
            do_archive: bool = True,
            ingest_sources: list[dict] | None = None,
            ingest_errors: list[str] | None = None) -> dict[str, object]:
    """Write artifacts + (optionally) archive.

    Returns:
      {
        "summary": <markdown text for owner brief>,
        "audit":   <markdown text>,
        "archived_at": <Path or None>,
      }

    `do_archive=False` is for tests + force_close paths that want the
    files written but skip the move (e.g. if archiving is risky in test
    fixtures).

    v1.5: ingest_sources / ingest_errors propagate to the brief via
    write_artifacts → build_summary.
    """
    artifacts = write_artifacts(
        sid_dir,
        subject=subject, junior_name=junior_name,
        junior_platform=junior_platform,
        rounds=rounds, verdict=verdict,
        document=document, forced_reason=forced_reason,
        ingest_sources=ingest_sources,
        ingest_errors=ingest_errors,
    )

    archived_at: Path | None = None
    if do_archive:
        try:
            archived_at = archive(sid_dir, sessions_root)
        except Exception as exc:
            logger.warning("deliver: archive failed (artifacts written but "
                           "directory not moved): %s", exc)

    return {
        "summary": artifacts["summary"],
        "audit": artifacts["audit"],
        "archived_at": str(archived_at) if archived_at else None,
    }
