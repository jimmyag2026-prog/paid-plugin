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


# --- resolve_owner_lark_target — Lark routing helper ---------------------

import os
import pytest


@pytest.fixture(autouse=False)
def _clear_feishu_home(monkeypatch):
    monkeypatch.delenv("FEISHU_HOME_CHANNEL", raising=False)
    yield


def test_resolve_lark_target_passthrough_when_caller_uid_is_routable(
    paid_tmp, _clear_feishu_home
):
    """If caller already has a routable id (ou_/oc_/email), return it
    unchanged — fastest path and avoids touching disk."""
    assert identity.resolve_owner_lark_target("ou_abc123") == "ou_abc123"
    assert identity.resolve_owner_lark_target("oc_chat99") == "oc_chat99"
    assert identity.resolve_owner_lark_target("user@example.com") == "user@example.com"


def test_resolve_lark_target_finds_ou_in_owner_when_caller_bare(
    paid_tmp, _clear_feishu_home
):
    """Caller passes bare hex (legacy v1 user_id). Owner.json has a
    second feishu identity with ou_ form. Helper must find and return
    the routable one."""
    _seed_owner(paid_tmp, [
        {"platform": "feishu", "user_id": "8ea86e3b"},
        {"platform": "feishu", "user_id": "ou_8580f481"},
    ])
    assert identity.resolve_owner_lark_target("8ea86e3b") == "ou_8580f481"


def test_resolve_lark_target_skips_disabled_identities(
    paid_tmp, _clear_feishu_home
):
    """Disabled identities don't count — owner muted that platform."""
    _seed_owner(paid_tmp, [
        {"platform": "feishu", "user_id": "8ea86e3b"},
        {"platform": "feishu", "user_id": "ou_disabled", "enabled": False},
    ])
    monkey_env_set("FEISHU_HOME_CHANNEL", "oc_fallback")  # fallback should kick in
    # Without env var, we'd return bare; with it, we'd return env.
    # Confirm env wins because no routable identity exists.
    os.environ["FEISHU_HOME_CHANNEL"] = "oc_fallback"
    try:
        assert identity.resolve_owner_lark_target("8ea86e3b") == "oc_fallback"
    finally:
        os.environ.pop("FEISHU_HOME_CHANNEL", None)


def monkey_env_set(k, v):
    """tiny shim because the test above mixes fixtures."""
    os.environ[k] = v


def test_resolve_lark_target_ignores_env_when_owner_has_routable_id(
    paid_tmp, monkeypatch
):
    """The bug we fixed: FEISHU_HOME_CHANNEL must NOT override a valid
    routable identity in owner.json."""
    _seed_owner(paid_tmp, [
        {"platform": "feishu", "user_id": "ou_correct_target"},
    ])
    monkeypatch.setenv("FEISHU_HOME_CHANNEL", "oc_wrong_target_999")
    assert identity.resolve_owner_lark_target("ou_correct_target") == "ou_correct_target"
    # And same result when caller passes a bare hex but owner has ou_:
    assert identity.resolve_owner_lark_target("anything") == "ou_correct_target"


def test_resolve_lark_target_falls_back_to_env_when_no_routable_anywhere(
    paid_tmp, monkeypatch
):
    """Legacy v1 owner.json: only bare-hex identity. /sethome wrote
    FEISHU_HOME_CHANNEL — preserve that fallback path."""
    _seed_owner(paid_tmp, [{"platform": "feishu", "user_id": "8ea86e3b"}])
    monkeypatch.setenv("FEISHU_HOME_CHANNEL", "oc_legacy_home")
    assert identity.resolve_owner_lark_target("8ea86e3b") == "oc_legacy_home"


def test_resolve_lark_target_returns_input_when_nothing_else(
    paid_tmp, monkeypatch
):
    """No routable owner identity, no env var → echo caller's id back.
    Lark will reject with [230001] and the caller will surface the
    error — but the helper itself doesn't hide the misconfig."""
    _seed_owner(paid_tmp, [{"platform": "feishu", "user_id": "8ea86e3b"}])
    monkeypatch.delenv("FEISHU_HOME_CHANNEL", raising=False)
    assert identity.resolve_owner_lark_target("8ea86e3b") == "8ea86e3b"


def test_resolve_lark_target_no_owner_json(paid_tmp, monkeypatch):
    """No owner.json at all — helper must not crash; falls through to
    env or caller id."""
    monkeypatch.setenv("FEISHU_HOME_CHANNEL", "oc_env_only")
    assert identity.resolve_owner_lark_target("anything") == "oc_env_only"


def test_resolve_lark_target_only_lark_identities_count(paid_tmp, monkeypatch):
    """Telegram identity's user_id must not be considered for Lark routing
    even if it happens to look like a number."""
    _seed_owner(paid_tmp, [
        {"platform": "telegram", "user_id": "ou_oddly_named_tg"},  # mis-named, ignore
        {"platform": "feishu", "user_id": "8ea86e3b"},
    ])
    monkeypatch.setenv("FEISHU_HOME_CHANNEL", "oc_correct_fallback")
    # No routable feishu identity → env fallback.
    assert identity.resolve_owner_lark_target("8ea86e3b") == "oc_correct_fallback"
