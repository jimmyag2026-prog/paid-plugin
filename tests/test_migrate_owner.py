"""Tests for bin/migrate_owner_v1_to_v2.py — pure-function upgrade_payload."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "paid_migrate", _REPO / "bin" / "migrate_owner_v1_to_v2.py"
)
_mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mig)


def test_upgrade_v1_payload_adds_schema_version():
    data = {
        "owner_id": "owner_jimmy",
        "name": "Jimmy",
        "identities": [{"platform": "telegram", "user_id": "1"}],
    }
    new, changes = _mig.upgrade_payload(data)
    assert new["schema_version"] == 2
    assert any("schema_version" in c for c in changes)


def test_upgrade_backfills_home_chat_id_and_enabled():
    data = {
        "owner_id": "x",
        "identities": [
            {"platform": "telegram", "user_id": "12345"},
            {"platform": "lark", "user_id": "ou_x"},
        ],
    }
    new, _ = _mig.upgrade_payload(data)
    ids = new["identities"]
    assert ids[0]["home_chat_id"] == "12345"
    assert ids[0]["enabled"] is True
    assert ids[1]["home_chat_id"] == "ou_x"
    assert ids[1]["enabled"] is True


def test_upgrade_preserves_existing_home_chat_id():
    data = {
        "owner_id": "x",
        "identities": [
            {"platform": "lark", "user_id": "ou_y", "home_chat_id": "oc_z"},
        ],
    }
    new, _ = _mig.upgrade_payload(data)
    assert new["identities"][0]["home_chat_id"] == "oc_z"


def test_upgrade_picks_first_platform_as_preferred():
    data = {
        "owner_id": "x",
        "identities": [
            {"platform": "telegram", "user_id": "1"},
            {"platform": "lark", "user_id": "ou_y"},
        ],
    }
    new, _ = _mig.upgrade_payload(data)
    assert new["preferred_platform"] == "telegram"


def test_upgrade_v2_is_noop():
    data = {
        "schema_version": 2,
        "owner_id": "x",
        "preferred_platform": "telegram",
        "identities": [{"platform": "telegram", "user_id": "1",
                        "home_chat_id": "1", "enabled": True}],
    }
    new, changes = _mig.upgrade_payload(data)
    assert changes == []
    assert new == data


def test_upgrade_skips_malformed_identities():
    data = {
        "owner_id": "x",
        "identities": [
            {"platform": "telegram", "user_id": "1"},
            "not-a-dict",
            None,
        ],
    }
    new, _ = _mig.upgrade_payload(data)
    # Malformed entries dropped; only the dict survives.
    assert len(new["identities"]) == 1


def test_upgrade_handles_no_identities():
    data = {"owner_id": "x", "identities": []}
    new, _ = _mig.upgrade_payload(data)
    assert new["schema_version"] == 2
    assert new["preferred_platform"] == ""
    assert new["identities"] == []


def test_upgrade_rejects_non_dict_root():
    with pytest.raises(ValueError):
        _mig.upgrade_payload(["not", "a", "dict"])


def test_migrate_writes_backup_and_new_file(paid_tmp):
    """End-to-end migrate(): backup created, file rewritten."""
    op = paid_tmp / "owner.json"
    op.write_text(json.dumps({
        "owner_id": "x",
        "identities": [{"platform": "telegram", "user_id": "1"}],
    }))
    rc = _mig.migrate(op, dry_run=False)
    assert rc == 0
    bak = paid_tmp / "owner.json.v1.bak"
    assert bak.exists()
    new_data = json.loads(op.read_text())
    assert new_data["schema_version"] == 2
    assert new_data["preferred_platform"] == "telegram"
    assert new_data["identities"][0]["home_chat_id"] == "1"


def test_migrate_dry_run_does_not_write(paid_tmp):
    op = paid_tmp / "owner.json"
    original = json.dumps({
        "owner_id": "x",
        "identities": [{"platform": "telegram", "user_id": "1"}],
    })
    op.write_text(original)
    rc = _mig.migrate(op, dry_run=True)
    assert rc == 0
    assert op.read_text() == original
    assert not (paid_tmp / "owner.json.v1.bak").exists()


def test_migrate_v2_file_is_noop(paid_tmp):
    op = paid_tmp / "owner.json"
    op.write_text(json.dumps({
        "schema_version": 2, "owner_id": "x",
        "preferred_platform": "telegram",
        "identities": [{"platform": "telegram", "user_id": "1",
                        "home_chat_id": "1", "enabled": True}],
    }))
    before = op.read_text()
    rc = _mig.migrate(op, dry_run=False)
    assert rc == 0
    assert op.read_text() == before  # unchanged
    assert not (paid_tmp / "owner.json.v1.bak").exists()  # no backup needed


def test_migrate_missing_owner_json_zero_exit(paid_tmp):
    rc = _mig.migrate(paid_tmp / "owner.json", dry_run=False)
    assert rc == 0
