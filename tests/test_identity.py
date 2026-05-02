"""Tests for Module I (identity)."""

from __future__ import annotations

from pathlib import Path

from paid import identity, storage


def _seed_owner(paid_dir: Path, identities: list[dict]):
    storage.write_json(
        paid_dir / "owner.json",
        {"owner_id": "jimmy", "identities": identities},
    )


def test_load_owner_missing_returns_none(paid_tmp: Path):
    assert identity.load_owner() is None


def test_load_owner_and_is_owner(paid_tmp: Path):
    _seed_owner(
        paid_tmp,
        [
            {"platform": "telegram", "user_id": "854066391"},
            {"platform": "feishu", "user_id": "4ed67983"},
        ],
    )
    owner = identity.load_owner()
    assert owner is not None
    assert owner.owner_id == "jimmy"
    assert identity.is_owner("telegram", "854066391") is True
    assert identity.is_owner("feishu", "4ed67983") is True
    assert identity.is_owner("telegram", "999") is False
    assert identity.is_owner("whatsapp", "854066391") is False


def test_is_owner_when_no_owner_file(paid_tmp: Path):
    assert identity.is_owner("telegram", "x") is False


def test_ensure_counterparty_creates_default_profile(paid_tmp: Path):
    cp = identity.ensure_counterparty("telegram", "6914282833", display_name="LM")
    assert cp.cp_id == "telegram_6914282833"
    assert cp.platform == "telegram"
    assert cp.user_id == "6914282833"
    assert cp.display_name == "LM"
    assert cp.role == "pending"
    assert cp.topics_allowed == []
    assert cp.topics_always_escalate == [
        "equity",
        "salary",
        "hiring",
        "customer",
        "finance",
    ]
    assert cp.web_search_allowed is True
    assert cp.notes == ""
    # Profile written to expected path
    profile_path = paid_tmp / "counterparties" / "telegram_6914282833" / "profile.json"
    assert profile_path.exists()


def test_ensure_counterparty_idempotent(paid_tmp: Path):
    cp1 = identity.ensure_counterparty("feishu", "8ea86e3b")
    # Mutate role on disk to simulate prior classification
    profile_path = paid_tmp / "counterparties" / "feishu_8ea86e3b" / "profile.json"
    data = storage.read_json(profile_path)
    assert data is not None
    data["role"] = "junior"
    data["notes"] = "trusted intern"
    storage.write_json(profile_path, data)
    # Second call should preserve the existing role/notes
    cp2 = identity.ensure_counterparty("feishu", "8ea86e3b")
    assert cp2.role == "junior"
    assert cp2.notes == "trusted intern"
    assert cp2.cp_id == cp1.cp_id


def test_load_counterparty_missing_returns_none(paid_tmp: Path):
    assert identity.load_counterparty("telegram", "ghost") is None
