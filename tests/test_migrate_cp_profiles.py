"""Tests for bin/migrate_cp_profiles.py — pure-function upgrade_cp_payload
plus end-to-end migrate_one / migrate_all."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "paid_migrate_cp", _REPO / "bin" / "migrate_cp_profiles.py"
)
_mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mig)


# --------------------------------------------------------------------------
# upgrade_cp_payload — pure function
# --------------------------------------------------------------------------


def test_upgrade_v1_adds_schema_version():
    data = {
        "cp_id": "telegram_99",
        "platform": "telegram",
        "user_id": "99",
        "display_name": "Old Cp",
        "role": "junior",
        "topics_allowed": [],
        "topics_always_escalate": [],
        "web_search_allowed": True,
        "notes": "",
    }
    new, changes = _mig.upgrade_cp_payload(data)
    assert new["schema_version"] == 2
    assert any("schema_version" in c for c in changes)


def test_upgrade_backfills_all_v2_default_fields():
    data = {"cp_id": "x", "platform": "tg", "user_id": "1"}
    new, changes = _mig.upgrade_cp_payload(data)
    assert new["ignore_reason"] == ""
    assert new["ignore_set_at"] == ""
    assert new["discovery_notified_at"] == ""
    assert new["active_review_session"] == ""
    assert new["review_history"] == []
    # Each backfill emits a change-log entry.
    assert len(changes) >= 6  # schema_version + 5 backfills


def test_upgrade_preserves_existing_v2_fields():
    data = {
        "schema_version": 2,
        "cp_id": "x", "platform": "tg", "user_id": "1",
        "ignore_reason": "spam",
        "ignore_set_at": "2026-04-01T00:00:00",
        "discovery_notified_at": "",
        "active_review_session": "sess-abc",
        "review_history": [{"sid": "old", "verdict": "READY"}],
        # v1.4.5: blacklist_action added; include here so the test is
        # "all v2-incl-1.4.5 fields present" and remains a true no-op.
        "blacklist_action": "decline",
    }
    new, changes = _mig.upgrade_cp_payload(data)
    assert changes == []  # already v2 + all fields present
    assert new["ignore_reason"] == "spam"
    assert new["active_review_session"] == "sess-abc"
    assert len(new["review_history"]) == 1
    assert new["blacklist_action"] == "decline"


def test_upgrade_preserves_existing_some_v2_fields_partial():
    """v1 record that already had ignore_reason but not the rest — only
    backfill what's missing, don't clobber the explicit field."""
    data = {
        "cp_id": "x", "platform": "tg", "user_id": "1",
        "ignore_reason": "blocked-already",
    }
    new, _ = _mig.upgrade_cp_payload(data)
    assert new["ignore_reason"] == "blocked-already"  # not clobbered
    assert new["active_review_session"] == ""        # backfilled
    assert new["review_history"] == []               # backfilled


def test_upgrade_v2_record_is_noop():
    data = {
        "schema_version": 2,
        "cp_id": "x", "platform": "tg", "user_id": "1",
        "ignore_reason": "", "ignore_set_at": "",
        "discovery_notified_at": "",
        "active_review_session": "", "review_history": [],
        # v1.4.5: include the new field so the record is "fully v1.4.5";
        # without it the migration would non-trivially add the field.
        "blacklist_action": "decline",
    }
    new, changes = _mig.upgrade_cp_payload(data)
    assert changes == []
    assert new == data


def test_upgrade_v1_4_4_record_gets_blacklist_action_backfilled():
    """A profile written by pre-v1.4.5 (schema_version=2 but missing
    blacklist_action) gets the new field added with default 'decline'.
    This is the canonical v1.4.4 → v1.4.5 migration path."""
    data = {
        "schema_version": 2,
        "cp_id": "x", "platform": "tg", "user_id": "1",
        "ignore_reason": "", "ignore_set_at": "",
        "discovery_notified_at": "",
        "active_review_session": "", "review_history": [],
        # No blacklist_action — pre-v1.4.5
    }
    new, changes = _mig.upgrade_cp_payload(data)
    assert new["blacklist_action"] == "decline"
    assert any("blacklist_action" in c for c in changes)


def test_upgrade_review_history_uses_fresh_list_per_record():
    """Defensive: the default [] in _V2_DEFAULTS must not leak across
    records (would silently share state if migrate_one returned dict
    references)."""
    a, _ = _mig.upgrade_cp_payload({"cp_id": "a"})
    b, _ = _mig.upgrade_cp_payload({"cp_id": "b"})
    a["review_history"].append({"sid": "x"})
    assert b["review_history"] == []


def test_upgrade_rejects_non_dict_root():
    with pytest.raises(ValueError):
        _mig.upgrade_cp_payload(["not", "a", "dict"])


# --------------------------------------------------------------------------
# migrate_one — file I/O
# --------------------------------------------------------------------------


def _write_v1_profile(paid_tmp: Path, cp_id: str, **fields) -> Path:
    cp_dir = paid_tmp / "counterparties" / cp_id
    cp_dir.mkdir(parents=True)
    base = {
        "cp_id": cp_id, "platform": cp_id.split("_")[0],
        "user_id": cp_id.split("_", 1)[1],
        "display_name": cp_id, "role": "junior",
        "topics_allowed": [], "topics_always_escalate": [],
        "web_search_allowed": True, "notes": "",
    }
    base.update(fields)
    profile = cp_dir / "profile.json"
    profile.write_text(json.dumps(base))
    return profile


def test_migrate_one_writes_backup_and_new_file(paid_tmp):
    profile = _write_v1_profile(paid_tmp, "telegram_99")
    changed, log = _mig.migrate_one(profile, dry_run=False)
    assert changed is True
    assert log
    bak = profile.with_suffix(".json.v1.bak")
    assert bak.exists()
    new_data = json.loads(profile.read_text())
    assert new_data["schema_version"] == 2
    assert "active_review_session" in new_data


def test_migrate_one_dry_run_does_not_write(paid_tmp):
    profile = _write_v1_profile(paid_tmp, "telegram_99")
    original = profile.read_text()
    changed, log = _mig.migrate_one(profile, dry_run=True)
    assert changed is True
    assert log
    assert profile.read_text() == original
    assert not profile.with_suffix(".json.v1.bak").exists()


def test_migrate_one_v2_file_is_noop(paid_tmp):
    profile = _write_v1_profile(
        paid_tmp, "lark_x", schema_version=2,
        ignore_reason="", ignore_set_at="",
        discovery_notified_at="",
        active_review_session="", review_history=[],
        # v1.4.5: must include for true no-op
        blacklist_action="decline",
    )
    before = profile.read_text()
    changed, log = _mig.migrate_one(profile, dry_run=False)
    assert changed is False
    assert log == []
    assert profile.read_text() == before
    assert not profile.with_suffix(".json.v1.bak").exists()


def test_migrate_one_invalid_json_raises(paid_tmp):
    cp_dir = paid_tmp / "counterparties" / "broken"
    cp_dir.mkdir(parents=True)
    (cp_dir / "profile.json").write_text("not-valid-json{{{")
    with pytest.raises(ValueError):
        _mig.migrate_one(cp_dir / "profile.json")


# --------------------------------------------------------------------------
# migrate_all — directory walk
# --------------------------------------------------------------------------


def test_migrate_all_walks_counterparties_dir(paid_tmp, capsys):
    # 2 v1 profiles, 1 already-v1.4.5, 1 broken
    _write_v1_profile(paid_tmp, "telegram_1")
    _write_v1_profile(paid_tmp, "lark_2")
    _write_v1_profile(
        paid_tmp, "telegram_3", schema_version=2,
        ignore_reason="", ignore_set_at="",
        discovery_notified_at="",
        active_review_session="", review_history=[],
        # v1.4.5: must include for true skip
        blacklist_action="decline",
    )
    bad_dir = paid_tmp / "counterparties" / "broken"
    bad_dir.mkdir()
    (bad_dir / "profile.json").write_text("nope")

    rc = _mig.migrate_all(paid_tmp / "counterparties", dry_run=False)
    captured = capsys.readouterr()
    # 2 upgraded, 1 skipped, 1 failed → return = -1 (failed > 0)
    assert "2 upgraded, 1 skipped, 1 failed" in captured.out
    assert rc == -1


def test_migrate_all_missing_dir_returns_zero(paid_tmp, capsys):
    rc = _mig.migrate_all(paid_tmp / "no-such-dir", dry_run=False)
    assert rc == 0


def test_migrate_all_skips_non_dir_entries(paid_tmp):
    # Stray file under counterparties/ — must be ignored, not crash.
    cp_root = paid_tmp / "counterparties"
    cp_root.mkdir()
    (cp_root / "stray-file.txt").write_text("x")
    rc = _mig.migrate_all(cp_root, dry_run=True)
    assert rc == 0
