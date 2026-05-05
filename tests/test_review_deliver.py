"""Tests for paid_review.core.deliver (Sprint C filesystem ops)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from paid_review.core import build_summary, deliver
from paid_review.core.annotation import Annotation, append_annotation


def _patch_llm(monkeypatch, value):
    """summary.md LLM gets a canned brief; gate calls don't fire here."""
    monkeypatch.setattr(build_summary, "_call_llm", lambda *a, **kw: value)


def _seed_session(paid_tmp: Path, sid: str, *, anns: list[Annotation] | None = None):
    """Create a sessions/<sid>/ with normalized.md + optional annotations."""
    sid_dir = paid_tmp / "review" / "sessions" / sid
    sid_dir.mkdir(parents=True)
    (sid_dir / "normalized.md").write_text("draft document body", encoding="utf-8")
    for a in (anns or []):
        append_annotation(sid_dir, a)
    return sid_dir


# ==========================================================================
# write_artifacts
# ==========================================================================


def test_write_artifacts_creates_summary_and_audit(paid_tmp, monkeypatch):
    _patch_llm(monkeypatch, "# 会前简报 — Q3\n\n## 1. 议题摘要\nx\n")
    sid_dir = _seed_session(paid_tmp, "abc123",
                            anns=[Annotation(id="p1", pillar="Intent",
                                             text="vague", status="accepted")])
    out = deliver.write_artifacts(
        sid_dir,
        subject="Q3", junior_name="Evie", junior_platform="feishu",
        rounds=2, verdict="READY", document="x",
    )
    assert "summary" in out and "audit" in out
    summary_file = sid_dir / "summary.md"
    audit_file = sid_dir / "summary_audit.md"
    assert summary_file.exists() and audit_file.exists()
    assert "议题摘要" in summary_file.read_text()
    assert "Audit Trail" in audit_file.read_text()


def test_write_artifacts_includes_forced_reason_in_audit(paid_tmp, monkeypatch):
    _patch_llm(monkeypatch, "# 会前简报\n## 1.")
    sid_dir = _seed_session(paid_tmp, "force1")
    deliver.write_artifacts(
        sid_dir,
        subject="x", junior_name="J", junior_platform="tg",
        rounds=3, verdict="FORCED_PARTIAL", document="x",
        forced_reason="rounds_exhausted",
    )
    audit_text = (sid_dir / "summary_audit.md").read_text()
    assert "rounds_exhausted" in audit_text


# ==========================================================================
# archive
# ==========================================================================


def test_archive_moves_sid_dir_to_closed_month(paid_tmp, monkeypatch):
    _patch_llm(monkeypatch, "# 会前简报\n## 1.")
    sid_dir = _seed_session(paid_tmp, "arc1")
    sessions_root = paid_tmp / "review" / "sessions"

    new_path = deliver.archive(sid_dir, sessions_root)

    assert not sid_dir.exists()        # original moved
    assert new_path.exists()           # new location exists
    assert new_path.parent.parent.name == "_closed"
    # Month dir name = current YYYY-MM (we don't pin date in test)
    assert (new_path / "normalized.md").exists()


def test_archive_idempotent_on_collision(paid_tmp, monkeypatch):
    """If destination already exists, archive must not silently overwrite."""
    sid = "collide1"
    sid_dir = _seed_session(paid_tmp, sid)
    sessions_root = paid_tmp / "review" / "sessions"

    # Pre-create the destination
    dest_root = sessions_root / "_closed" / "2099-12"
    dest_root.mkdir(parents=True)
    pre_existing = dest_root / sid
    pre_existing.mkdir()
    (pre_existing / "marker.txt").write_text("pre-existing")

    # Force the archive path to use 2099-12 by monkey-patching _archive_month
    monkeypatch.setattr(deliver, "_archive_month", lambda now=None: "2099-12")

    new_path = deliver.archive(sid_dir, sessions_root)
    assert new_path != pre_existing       # got a different (timestamped) dir
    assert pre_existing.exists()          # original NOT clobbered
    assert (pre_existing / "marker.txt").exists()
    assert new_path.exists()


# ==========================================================================
# deliver (full)
# ==========================================================================


def test_deliver_writes_then_archives(paid_tmp, monkeypatch):
    _patch_llm(monkeypatch, "# 会前简报\n## 1. 议题摘要\nx\n")
    sid_dir = _seed_session(paid_tmp, "full1",
                            anns=[Annotation(id="p1", pillar="Intent",
                                             text="x", status="accepted")])
    sessions_root = paid_tmp / "review" / "sessions"

    out = deliver.deliver(
        sid_dir, sessions_root,
        subject="x", junior_name="J", junior_platform="lark",
        rounds=1, verdict="READY", document="x",
    )

    assert out["summary"].startswith("# 会前简报")
    assert "Audit Trail" in out["audit"]
    assert out["archived_at"] is not None
    archived = Path(out["archived_at"])
    assert archived.exists()
    assert (archived / "summary.md").exists()
    assert (archived / "summary_audit.md").exists()
    # Original sid_dir gone
    assert not sid_dir.exists()


def test_deliver_skip_archive_keeps_files_in_place(paid_tmp, monkeypatch):
    _patch_llm(monkeypatch, "# 会前简报\n## 1.")
    sid_dir = _seed_session(paid_tmp, "noarc1")
    sessions_root = paid_tmp / "review" / "sessions"

    out = deliver.deliver(
        sid_dir, sessions_root,
        subject="x", junior_name="J", junior_platform="lark",
        rounds=0, verdict="READY", document="x",
        do_archive=False,
    )
    assert out["archived_at"] is None
    assert sid_dir.exists()
    assert (sid_dir / "summary.md").exists()


def test_deliver_archive_failure_doesnt_lose_artifacts(paid_tmp, monkeypatch):
    """If archive() raises, summary + audit still got written; caller can
    inspect sid_dir manually."""
    _patch_llm(monkeypatch, "# 会前简报\n## 1.")
    sid_dir = _seed_session(paid_tmp, "fail_arc")
    sessions_root = paid_tmp / "review" / "sessions"

    monkeypatch.setattr(deliver, "archive",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))

    out = deliver.deliver(
        sid_dir, sessions_root,
        subject="x", junior_name="J", junior_platform="lark",
        rounds=0, verdict="READY", document="x",
    )
    assert out["archived_at"] is None  # archive bailed
    assert sid_dir.exists()             # original kept
    assert (sid_dir / "summary.md").exists()  # but artifacts written
