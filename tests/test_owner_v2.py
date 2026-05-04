"""Tests for Owner schema v2 — home_chat_id / enabled / preferred_platform.

Backward-compat: v1 owner.json (no schema_version, identity entries with
just platform+user_id) must load cleanly and produce sensible defaults
(home_chat_id=user_id, enabled=True, preferred_platform="" → first
enabled identity).
"""

from __future__ import annotations

import json

import pytest

from paid import identity


# --------------------------------------------------------------------------
# OwnerIdentity dataclass
# --------------------------------------------------------------------------


def test_owner_identity_default_home_chat_id_equals_user_id():
    i = identity.OwnerIdentity(platform="telegram", user_id="12345")
    assert i.home_chat_id == "12345"
    assert i.enabled is True
    assert i.name == ""


def test_owner_identity_explicit_home_chat_id_kept():
    i = identity.OwnerIdentity(
        platform="lark", user_id="ou_xxx", home_chat_id="oc_yyy",
    )
    assert i.home_chat_id == "oc_yyy"


def test_owner_identity_disabled_flag():
    i = identity.OwnerIdentity(
        platform="slack", user_id="U1", enabled=False,
    )
    assert i.enabled is False


# --------------------------------------------------------------------------
# Owner v2 — iter / enabled / preferred
# --------------------------------------------------------------------------


def _seed_owner(paid_tmp, **fields):
    payload = {
        "owner_id": "owner_jimmy",
        "name": "Jimmy",
        "schema_version": 2,
        "preferred_platform": "",
        "identities": [],
    }
    payload.update(fields)
    (paid_tmp / "owner.json").write_text(json.dumps(payload))


def test_iter_identities_skips_malformed(paid_tmp):
    _seed_owner(paid_tmp, identities=[
        {"platform": "telegram", "user_id": "12345"},
        "not-a-dict",
        {"platform": "lark", "user_id": "ou_x"},
        None,
    ])
    owner = identity.load_owner()
    assert owner is not None
    ids = owner.iter_identities()
    assert len(ids) == 2
    assert {i.platform for i in ids} == {"telegram", "lark"}


def test_enabled_identities_filters_disabled(paid_tmp):
    _seed_owner(paid_tmp, identities=[
        {"platform": "telegram", "user_id": "1", "enabled": True},
        {"platform": "lark",     "user_id": "ou_x", "enabled": False},
        {"platform": "slack",    "user_id": "U1", "enabled": True},
    ])
    enabled = identity.load_owner().enabled_identities()
    platforms = [i.platform for i in enabled]
    assert platforms == ["telegram", "slack"]


def test_preferred_identity_uses_preferred_platform(paid_tmp):
    _seed_owner(paid_tmp,
        preferred_platform="slack",
        identities=[
            {"platform": "telegram", "user_id": "1"},
            {"platform": "slack",    "user_id": "U1"},
            {"platform": "lark",     "user_id": "ou_x"},
        ],
    )
    pref = identity.load_owner().preferred_identity()
    assert pref is not None
    assert pref.platform == "slack"
    assert pref.user_id == "U1"


def test_preferred_identity_falls_back_to_first_enabled(paid_tmp):
    _seed_owner(paid_tmp,
        preferred_platform="discord",   # not present
        identities=[
            {"platform": "telegram", "user_id": "1"},
            {"platform": "lark",     "user_id": "ou_x"},
        ],
    )
    pref = identity.load_owner().preferred_identity()
    assert pref.platform == "telegram"


def test_preferred_identity_skips_disabled_when_matching(paid_tmp):
    _seed_owner(paid_tmp,
        preferred_platform="lark",
        identities=[
            {"platform": "telegram", "user_id": "1", "enabled": True},
            {"platform": "lark",     "user_id": "ou_x", "enabled": False},
        ],
    )
    pref = identity.load_owner().preferred_identity()
    # lark is preferred but disabled → falls through to first enabled.
    assert pref.platform == "telegram"


def test_preferred_identity_none_when_no_enabled(paid_tmp):
    _seed_owner(paid_tmp, identities=[
        {"platform": "telegram", "user_id": "1", "enabled": False},
    ])
    assert identity.load_owner().preferred_identity() is None


# --------------------------------------------------------------------------
# Backward compat — v1 owner.json (no schema_version, no v2 identity fields)
# --------------------------------------------------------------------------


def test_load_owner_v1_no_schema_version_defaults_safe(paid_tmp):
    (paid_tmp / "owner.json").write_text(json.dumps({
        "owner_id": "owner_jimmy",
        "name": "Jimmy",
        "identities": [
            {"platform": "telegram", "user_id": "12345"},
            {"platform": "lark", "user_id": "ou_xxx"},
        ],
    }))
    owner = identity.load_owner()
    assert owner is not None
    assert owner.schema_version == 1
    assert owner.preferred_platform == ""
    # v1 identities still work — defaults applied per OwnerIdentity.
    ids = owner.iter_identities()
    assert len(ids) == 2
    assert all(i.enabled for i in ids)
    assert ids[0].home_chat_id == "12345"  # default = user_id
    assert ids[1].home_chat_id == "ou_xxx"


def test_load_owner_v1_preferred_identity_first_enabled(paid_tmp):
    (paid_tmp / "owner.json").write_text(json.dumps({
        "owner_id": "x", "identities": [
            {"platform": "telegram", "user_id": "1"},
            {"platform": "feishu", "user_id": "ou_y"},
        ],
    }))
    pref = identity.load_owner().preferred_identity()
    assert pref.platform == "telegram"  # insertion order


# --------------------------------------------------------------------------
# save_owner round-trip
# --------------------------------------------------------------------------


def test_save_owner_writes_v2_schema(paid_tmp):
    _seed_owner(paid_tmp, identities=[
        {"platform": "telegram", "user_id": "1"},
    ])
    owner = identity.load_owner()
    owner.preferred_platform = "telegram"
    identity.save_owner(owner)
    loaded = json.loads((paid_tmp / "owner.json").read_text())
    assert loaded["schema_version"] == 2
    assert loaded["preferred_platform"] == "telegram"
    assert loaded["owner_id"] == "owner_jimmy"


def test_load_save_round_trip_preserves_identities(paid_tmp):
    _seed_owner(paid_tmp, identities=[
        {"platform": "lark", "user_id": "ou_x", "home_chat_id": "oc_y", "enabled": True},
        {"platform": "slack", "user_id": "U1", "enabled": False},
    ])
    owner1 = identity.load_owner()
    identity.save_owner(owner1)
    owner2 = identity.load_owner()
    assert len(owner2.identities) == 2
    ids2 = owner2.iter_identities()
    assert ids2[0].home_chat_id == "oc_y"
    assert ids2[1].enabled is False


# --------------------------------------------------------------------------
# is_owner — must keep working with v2 (regression guard)
# --------------------------------------------------------------------------


def test_is_owner_with_v2_owner_json(paid_tmp):
    _seed_owner(paid_tmp, identities=[
        {"platform": "telegram", "user_id": "12345", "enabled": True},
        {"platform": "lark",     "user_id": "ou_y", "enabled": False},
    ])
    assert identity.is_owner("telegram", "12345") is True
    # is_owner does NOT filter by enabled — disabled identities still count
    # as "this is the owner" (they're just muted for outbound). This matches
    # v1 behaviour exactly so existing tests stay green.
    assert identity.is_owner("lark", "ou_y") is True
    assert identity.is_owner("slack", "U1") is False
