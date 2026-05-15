"""Tests for v1.6.4 per-cp physical isolation of audit + pending logs."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from paid import audit, approval, storage


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "PAID_DIR", tmp_path)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


# ---------------------------------------------------------------------------
# audit.log_action — per-cp write
# ---------------------------------------------------------------------------


def test_audit_writes_to_per_cp_dir(tmp_path):
    class FakeCp:
        cp_id = "feishu_ou_abc"
        platform = "feishu"

    audit.log_action("s1", FakeCp(), "hello", None, None)
    cp_audit = tmp_path / "counterparties" / "feishu_ou_abc" / "audit.jsonl"
    assert cp_audit.exists()
    rows = [json.loads(l) for l in cp_audit.read_text().splitlines() if l]
    assert len(rows) == 1
    assert rows[0]["counterparty"] == "feishu_ou_abc"


def test_audit_fallback_to_legacy_when_no_cp(tmp_path):
    audit.log_action("s1", None, "system event", None, None)
    legacy = tmp_path / "audit_log.jsonl"
    assert legacy.exists()
    rows = [json.loads(l) for l in legacy.read_text().splitlines() if l]
    assert len(rows) == 1


def test_audit_read_all_merges_per_cp_and_legacy(tmp_path):
    # Write to legacy
    _write_jsonl(
        tmp_path / "audit_log.jsonl",
        [{"ts": "2026-01-01T00:00:00", "counterparty": "old_cp", "session_id": "old1"}],
    )
    # Write to per-cp
    _write_jsonl(
        tmp_path / "counterparties" / "new_cp" / "audit.jsonl",
        [{"ts": "2026-02-01T00:00:00", "counterparty": "new_cp", "session_id": "new1"}],
    )
    rows = audit.read_all_entries()
    assert len(rows) == 2
    session_ids = {r["session_id"] for r in rows}
    assert "old1" in session_ids
    assert "new1" in session_ids


def test_audit_read_all_deduplicates(tmp_path):
    """Same entry in both legacy and per-cp → appears once."""
    entry = {"ts": "2026-01-01T00:00:00", "counterparty": "cp_x", "session_id": "dup1"}
    _write_jsonl(tmp_path / "audit_log.jsonl", [entry])
    _write_jsonl(tmp_path / "counterparties" / "cp_x" / "audit.jsonl", [entry])
    rows = audit.read_all_entries()
    assert len(rows) == 1


def test_audit_read_all_cp_filter(tmp_path):
    _write_jsonl(
        tmp_path / "counterparties" / "cp_a" / "audit.jsonl",
        [{"ts": "2026-01-01T00:00:00", "counterparty": "cp_a", "session_id": "a1"}],
    )
    _write_jsonl(
        tmp_path / "counterparties" / "cp_b" / "audit.jsonl",
        [{"ts": "2026-01-01T00:00:00", "counterparty": "cp_b", "session_id": "b1"}],
    )
    rows = audit.read_all_entries(cp_id="cp_a")
    assert len(rows) == 1
    assert rows[0]["counterparty"] == "cp_a"


def test_audit_list_known_cp_ids(tmp_path):
    _write_jsonl(tmp_path / "counterparties" / "cp_a" / "audit.jsonl", [{}])
    _write_jsonl(tmp_path / "counterparties" / "cp_b" / "audit.jsonl", [{}])
    ids = audit.list_known_cp_ids()
    assert set(ids) == {"cp_a", "cp_b"}


# ---------------------------------------------------------------------------
# approval — per-cp pending write
# ---------------------------------------------------------------------------


def _make_approval(**over):
    base = dict(
        counterparty_id="feishu_ou_x",
        counterparty_platform="feishu",
        counterparty_user_id="ou_x",
        counterparty_display="Test",
        junior_session_id="s1",
        junior_question="Q?",
        draft_answer="A",
        topic="test",
        stakes="low",
        confidence=0.9,
    )
    base.update(over)
    return approval.create(**base)


def test_approval_writes_to_per_cp_dir(tmp_path):
    _make_approval()
    cp_pending = tmp_path / "counterparties" / "feishu_ou_x" / "pending.jsonl"
    assert cp_pending.exists()
    rows = [json.loads(l) for l in cp_pending.read_text().splitlines() if l]
    assert len(rows) == 1
    assert rows[0]["type"] == "create"


def test_approval_reads_from_legacy_too(tmp_path):
    """Legacy pending_approvals.jsonl is still read (grace period)."""
    _write_jsonl(
        tmp_path / "pending_approvals.jsonl",
        [{"type": "create", "request_id": "ghost_abc", "ts": 1000.0,
          "counterparty_id": "old_cp", "counterparty_platform": "feishu",
          "counterparty_user_id": "ou_old", "counterparty_display": "Old",
          "junior_session_id": "s0", "junior_question": "Q", "draft_answer": "",
          "topic": "x", "stakes": "low", "confidence": 0.5}],
    )
    pendings = approval.list_pending()
    assert any(p.request_id == "ghost_abc" for p in pendings)


def test_approval_set_status_writes_to_per_cp(tmp_path):
    req = _make_approval()
    approval.set_status(req.request_id, "approved", final_text="ok")
    cp_pending = tmp_path / "counterparties" / "feishu_ou_x" / "pending.jsonl"
    lines = [json.loads(l) for l in cp_pending.read_text().splitlines() if l]
    types = [l["type"] for l in lines]
    assert types == ["create", "status"]


# ---------------------------------------------------------------------------
# Migration script
# ---------------------------------------------------------------------------


def test_migrate_audit_to_per_cp(tmp_path):
    from bin.migrate_to_per_cp_audit import migrate

    # Legacy audit with 2 different cps
    _write_jsonl(
        tmp_path / "audit_log.jsonl",
        [
            {"ts": "2026-01-01T00:00:00", "counterparty": "cp_a", "session_id": "a1"},
            {"ts": "2026-01-02T00:00:00", "counterparty": "cp_b", "session_id": "b1"},
            {"ts": "2026-01-03T00:00:00", "counterparty": "cp_a", "session_id": "a2"},
        ],
    )
    _write_jsonl(tmp_path / "pending_approvals.jsonl", [])  # empty

    rc = migrate(paid_dir=tmp_path, dry_run=False)
    assert rc == 0

    # Per-cp files created
    assert (tmp_path / "counterparties" / "cp_a" / "audit.jsonl").exists()
    assert (tmp_path / "counterparties" / "cp_b" / "audit.jsonl").exists()

    # cp_a has 2 entries
    rows_a = [json.loads(l) for l in (tmp_path / "counterparties" / "cp_a" / "audit.jsonl").read_text().splitlines() if l]
    assert len(rows_a) == 2

    # Legacy renamed
    assert (tmp_path / "audit_log.jsonl.migrated_v1.6.4").exists()
    assert not (tmp_path / "audit_log.jsonl").exists()


def test_migrate_dry_run_no_writes(tmp_path):
    from bin.migrate_to_per_cp_audit import migrate

    _write_jsonl(
        tmp_path / "audit_log.jsonl",
        [{"ts": "2026-01-01T00:00:00", "counterparty": "cp_a", "session_id": "a1"}],
    )
    _write_jsonl(tmp_path / "pending_approvals.jsonl", [])

    rc = migrate(paid_dir=tmp_path, dry_run=True)
    assert rc == 0

    # No per-cp files created in dry run
    assert not (tmp_path / "counterparties").exists() or not any(
        (tmp_path / "counterparties").rglob("audit.jsonl")
    )
    # Legacy NOT renamed
    assert (tmp_path / "audit_log.jsonl").exists()


def test_migrate_skips_if_already_done(tmp_path):
    from bin.migrate_to_per_cp_audit import migrate

    # Create marker files
    (tmp_path / "audit_log.jsonl.migrated_v1.6.4").touch()
    (tmp_path / "pending_approvals.jsonl.migrated_v1.6.4").touch()

    rc = migrate(paid_dir=tmp_path, dry_run=False)
    assert rc == 2  # already migrated
