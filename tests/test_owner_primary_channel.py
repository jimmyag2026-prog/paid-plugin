"""Owner primary IM channel (v1.7.0 / §1.B).

Covers the four parts of §1.B end-to-end:
  - schema: `OwnerProfile.preferred_platform` persists across save/load
  - sync: `profile_sync._write_owner_json` honours the explicit field
    (zero-regression: falls back to first-enabled when empty)
  - command: `_cmd_set_primary` happy path + owner-gating + validation
  - doctor: `_check_primary_channel` warns when multi-channel + unset,
    silent on single-channel or correctly set
  - wizard: `_ensure_caller_identity` auto-adds; Q6 only fires for ≥2
    channels; `_maybe_ask_preferred_platform` decision matrix

`paid_tmp` (from conftest) redirects PAID_DIR to a tmp dir so we can
write/read owner_profile.json + owner.json without touching the real
home directory.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# Schema — preferred_platform round-trips
# ---------------------------------------------------------------------------


def test_profile_persists_preferred_platform(paid_tmp):
    from paid import profile as _profile

    prof = _profile.new_profile(owner_id="o1", name="Jimmy")
    prof.preferred_platform = "slack"
    prof.identities = [
        {"platform": "slack", "user_id": "U_A", "enabled": True},
        {"platform": "lark", "user_id": "ou_B", "enabled": True},
    ]
    _profile.save_profile(prof)

    loaded = _profile.load_profile()
    assert loaded is not None
    assert loaded.preferred_platform == "slack"


def test_profile_missing_field_defaults_to_empty(paid_tmp):
    """Loading a v1.6 owner_profile.json (no preferred_platform key) must
    not crash — field defaults to empty, falling back to legacy logic."""
    from paid import profile as _profile, storage

    (storage.PAID_DIR / "owner_profile.json").write_text(json.dumps({
        "owner_id": "o1",
        "name": "Jimmy",
        # no preferred_platform key — simulates v1.6 file
        "identities": [{"platform": "lark", "user_id": "ou_A", "enabled": True}],
        "schema_version": 1,
    }))

    loaded = _profile.load_profile()
    assert loaded is not None
    assert loaded.preferred_platform == ""


def test_allowed_fields_includes_preferred_platform():
    from paid import profile as _profile
    assert "preferred_platform" in _profile.ALLOWED_PROFILE_FIELDS


# ---------------------------------------------------------------------------
# profile_sync — explicit field wins, fallback preserved
# ---------------------------------------------------------------------------


def test_sync_prefers_explicit_preferred_platform_over_first_identity(paid_tmp):
    from paid import profile as _profile, profile_sync as _sync, storage

    prof = _profile.new_profile(owner_id="o1", name="Jimmy")
    prof.identities = [
        {"platform": "lark", "user_id": "ou_A", "enabled": True},
        {"platform": "slack", "user_id": "U_B", "enabled": True},
    ]
    prof.preferred_platform = "slack"

    _sync.derive_from_profile(prof)
    owner_json = json.loads((storage.PAID_DIR / "owner.json").read_text())
    assert owner_json["preferred_platform"] == "slack"


def test_sync_falls_back_when_preferred_empty(paid_tmp):
    """Zero-regression: profiles without preferred_platform keep v1.6
    behaviour (first enabled identity)."""
    from paid import profile as _profile, profile_sync as _sync, storage

    prof = _profile.new_profile(owner_id="o1", name="Jimmy")
    prof.identities = [
        {"platform": "lark", "user_id": "ou_A", "enabled": True},
        {"platform": "slack", "user_id": "U_B", "enabled": True},
    ]
    prof.preferred_platform = ""  # explicit empty

    _sync.derive_from_profile(prof)
    owner_json = json.loads((storage.PAID_DIR / "owner.json").read_text())
    assert owner_json["preferred_platform"] == "lark"  # first enabled


def test_sync_ignores_invalid_preferred_platform(paid_tmp):
    """If preferred_platform names a platform that isn't in identities[]
    (or is disabled), fall back to first enabled rather than persist a
    broken pointer. /paid-doctor surfaces the inconsistency separately."""
    from paid import profile as _profile, profile_sync as _sync, storage

    prof = _profile.new_profile(owner_id="o1", name="Jimmy")
    prof.identities = [
        {"platform": "lark", "user_id": "ou_A", "enabled": True},
    ]
    prof.preferred_platform = "slack"  # not in identities

    _sync.derive_from_profile(prof)
    owner_json = json.loads((storage.PAID_DIR / "owner.json").read_text())
    assert owner_json["preferred_platform"] == "lark"


# ---------------------------------------------------------------------------
# /paid-set-primary command
# ---------------------------------------------------------------------------


def _fresh_plugin_module():
    spec = importlib.util.spec_from_file_location(
        "paid_v1_primary_test_module", _ROOT / "__init__.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed_profile(storage_mod, *, identities=None, preferred=""):
    """Write a minimal owner_profile.json + return loaded profile."""
    from paid import profile as _profile
    prof = _profile.new_profile(owner_id="o1", name="Jimmy")
    prof.identities = list(identities or [])
    prof.preferred_platform = preferred
    _profile.save_profile(prof)
    return prof


def test_set_primary_command_owner_only_silent(monkeypatch, paid_tmp):
    plugin = _fresh_plugin_module()
    monkeypatch.setattr(plugin, "_is_caller_owner_via_env", lambda: False)
    out = plugin._cmd_set_primary("slack")
    assert out == ""  # silent for non-owners


def test_set_primary_command_no_args_prints_usage(monkeypatch, paid_tmp):
    plugin = _fresh_plugin_module()
    monkeypatch.setattr(plugin, "_is_caller_owner_via_env", lambda: True)
    out = plugin._cmd_set_primary("")
    assert "usage" in out.lower()
    assert "lark" in out
    assert "telegram" in out
    assert "slack" in out


def test_set_primary_command_no_profile(monkeypatch, paid_tmp):
    plugin = _fresh_plugin_module()
    monkeypatch.setattr(plugin, "_is_caller_owner_via_env", lambda: True)
    out = plugin._cmd_set_primary("slack")
    assert "no owner_profile" in out.lower() or "paid-setup" in out.lower()


def test_set_primary_command_happy(monkeypatch, paid_tmp):
    from paid import profile as _profile, storage
    _seed_profile(storage, identities=[
        {"platform": "lark", "user_id": "ou_A", "enabled": True},
        {"platform": "slack", "user_id": "U_B", "enabled": True},
    ])
    plugin = _fresh_plugin_module()
    monkeypatch.setattr(plugin, "_is_caller_owner_via_env", lambda: True)

    out = plugin._cmd_set_primary("slack")
    assert "slack" in out.lower()
    assert "set" in out.lower() or "已" in out

    # Persisted in profile + derived owner.json
    loaded = _profile.load_profile()
    assert loaded.preferred_platform == "slack"
    owner_json = json.loads((storage.PAID_DIR / "owner.json").read_text())
    assert owner_json["preferred_platform"] == "slack"


def test_set_primary_command_feishu_aliases_to_lark(monkeypatch, paid_tmp):
    from paid import profile as _profile, storage
    _seed_profile(storage, identities=[
        {"platform": "lark", "user_id": "ou_A", "enabled": True},
        {"platform": "slack", "user_id": "U_B", "enabled": True},
    ])
    plugin = _fresh_plugin_module()
    monkeypatch.setattr(plugin, "_is_caller_owner_via_env", lambda: True)

    plugin._cmd_set_primary("feishu")
    assert _profile.load_profile().preferred_platform == "lark"


def test_set_primary_command_rejects_unknown_platform(monkeypatch, paid_tmp):
    from paid import profile as _profile, storage
    _seed_profile(storage, identities=[
        {"platform": "lark", "user_id": "ou_A", "enabled": True},
    ])
    plugin = _fresh_plugin_module()
    monkeypatch.setattr(plugin, "_is_caller_owner_via_env", lambda: True)

    out = plugin._cmd_set_primary("slack")  # not in identities
    assert "not in" in out.lower() or "enabled" in out.lower()
    # Profile unchanged
    assert _profile.load_profile().preferred_platform == ""


def test_set_primary_command_auto_clears(monkeypatch, paid_tmp):
    from paid import profile as _profile, storage
    _seed_profile(storage, identities=[
        {"platform": "lark", "user_id": "ou_A", "enabled": True},
        {"platform": "slack", "user_id": "U_B", "enabled": True},
    ], preferred="slack")
    plugin = _fresh_plugin_module()
    monkeypatch.setattr(plugin, "_is_caller_owner_via_env", lambda: True)

    out = plugin._cmd_set_primary("auto")
    assert _profile.load_profile().preferred_platform == ""
    # Fallback now uses first-enabled (lark)
    owner_json = json.loads((storage.PAID_DIR / "owner.json").read_text())
    assert owner_json["preferred_platform"] == "lark"


# ---------------------------------------------------------------------------
# doctor — primary_channel check
# ---------------------------------------------------------------------------


def test_doctor_primary_channel_warns_when_multi_unset(paid_tmp):
    from paid import doctor, storage
    (storage.PAID_DIR / "owner.json").write_text(json.dumps({
        "schema_version": 2,
        "preferred_platform": "",
        "identities": [
            {"platform": "lark", "user_id": "ou_A", "enabled": True},
            {"platform": "slack", "user_id": "U_B", "enabled": True},
        ],
    }))
    rows = doctor.run_checks()
    row = next(r for r in rows if r["id"] == "primary_channel")
    assert row["ok"] is False
    assert "no primary" in row["detail"].lower() or "default" in row["detail"].lower()
    assert "/paid-set-primary" in row["fix_hint"]


def test_doctor_primary_channel_silent_when_single_channel(paid_tmp):
    from paid import doctor, storage
    (storage.PAID_DIR / "owner.json").write_text(json.dumps({
        "schema_version": 2,
        "preferred_platform": "",
        "identities": [
            {"platform": "lark", "user_id": "ou_A", "enabled": True},
        ],
    }))
    rows = doctor.run_checks()
    row = next(r for r in rows if r["id"] == "primary_channel")
    assert row["ok"] is True
    assert "single-channel" in row["detail"]


def test_doctor_primary_channel_pass_when_set_correctly(paid_tmp):
    from paid import doctor, storage
    (storage.PAID_DIR / "owner.json").write_text(json.dumps({
        "schema_version": 2,
        "preferred_platform": "slack",
        "identities": [
            {"platform": "lark", "user_id": "ou_A", "enabled": True},
            {"platform": "slack", "user_id": "U_B", "enabled": True},
        ],
    }))
    rows = doctor.run_checks()
    row = next(r for r in rows if r["id"] == "primary_channel")
    assert row["ok"] is True
    assert "slack" in row["detail"]


def test_doctor_primary_channel_warns_when_pointing_at_disabled(paid_tmp):
    """Preferred names a platform not in enabled list → warn + fix hint."""
    from paid import doctor, storage
    (storage.PAID_DIR / "owner.json").write_text(json.dumps({
        "schema_version": 2,
        "preferred_platform": "telegram",  # not in identities
        "identities": [
            {"platform": "lark", "user_id": "ou_A", "enabled": True},
            {"platform": "slack", "user_id": "U_B", "enabled": True},
        ],
    }))
    rows = doctor.run_checks()
    row = next(r for r in rows if r["id"] == "primary_channel")
    assert row["ok"] is False
    assert "not in" in row["detail"]


# ---------------------------------------------------------------------------
# wizard — auto-add identity + Q6
# ---------------------------------------------------------------------------


def test_wizard_ensure_caller_identity_adds_when_missing(paid_tmp):
    from paid import profile as _profile, setup_wizard

    prof = _profile.new_profile(owner_id="o1")
    state = setup_wizard.WizardState(platform="slack", owner_id="U_OWNER")
    setup_wizard._ensure_caller_identity(prof, state)
    assert len(prof.identities) == 1
    assert prof.identities[0]["platform"] == "slack"
    assert prof.identities[0]["user_id"] == "U_OWNER"
    assert prof.identities[0]["enabled"] is True


def test_wizard_ensure_caller_identity_idempotent(paid_tmp):
    """Re-running wizard must not duplicate identity rows."""
    from paid import profile as _profile, setup_wizard

    prof = _profile.new_profile(owner_id="o1")
    state = setup_wizard.WizardState(platform="slack", owner_id="U_OWNER")
    setup_wizard._ensure_caller_identity(prof, state)
    setup_wizard._ensure_caller_identity(prof, state)
    setup_wizard._ensure_caller_identity(prof, state)
    assert len(prof.identities) == 1


def test_wizard_finalize_first_time_auto_adds_caller(monkeypatch, paid_tmp):
    """End-to-end: completing the 5-question wizard from a Slack DM
    persists profile.identities with the caller's Slack id."""
    from paid import profile as _profile, setup_wizard

    state = setup_wizard.WizardState(
        platform="slack", owner_id="U_OWNER", mode="first_time", step=5,
        answers={
            "name": "Jimmy", "voice_preset": "founder",
            "always_escalate": ["equity"], "preferred_language": "auto",
            "daily_cost_cap_usd": 5.0,
        },
    )
    reply, done = setup_wizard._finalize_first_time(state)
    assert done is True

    loaded = _profile.load_profile()
    assert loaded is not None
    assert any(
        i.get("platform") == "slack" and i.get("user_id") == "U_OWNER"
        for i in loaded.identities
    )


def test_wizard_q6_skipped_for_single_channel(monkeypatch, paid_tmp):
    """Only 1 enabled identity → finalize directly, no Q6 prompt."""
    from paid import setup_wizard

    state = setup_wizard.WizardState(
        platform="slack", owner_id="U_OWNER", mode="first_time", step=5,
        answers={"name": "Jimmy"},
    )
    reply, done = setup_wizard._maybe_ask_preferred_platform(state)
    assert done is True  # finalized
    assert state.awaiting_preferred_platform is False


def test_wizard_q6_asked_when_multi_channel(monkeypatch, paid_tmp):
    """Existing profile already has Lark identity; wizard caller adds
    Slack as second → Q6 must fire."""
    from paid import profile as _profile, setup_wizard

    prof = _profile.new_profile(owner_id="o1")
    prof.identities = [{"platform": "lark", "user_id": "ou_A", "enabled": True}]
    _profile.save_profile(prof)

    state = setup_wizard.WizardState(
        platform="slack", owner_id="U_OWNER", mode="first_time", step=5,
        answers={"name": "Jimmy"},
    )
    reply, done = setup_wizard._maybe_ask_preferred_platform(state)
    assert done is False
    assert state.awaiting_preferred_platform is True
    assert "lark" in reply
    assert "slack" in reply
    assert "auto" in reply


def test_wizard_q6_skipped_when_preferred_already_set(monkeypatch, paid_tmp):
    """If owner already set preferred_platform (e.g. via /paid-set-primary),
    re-running wizard doesn't badger them with Q6."""
    from paid import profile as _profile, setup_wizard

    prof = _profile.new_profile(owner_id="o1")
    prof.identities = [
        {"platform": "lark", "user_id": "ou_A", "enabled": True},
        {"platform": "slack", "user_id": "U_OWNER", "enabled": True},
    ]
    prof.preferred_platform = "lark"
    _profile.save_profile(prof)

    state = setup_wizard.WizardState(
        platform="slack", owner_id="U_OWNER", mode="first_time", step=5,
        answers={"name": "Jimmy"},
    )
    reply, done = setup_wizard._maybe_ask_preferred_platform(state)
    assert done is True  # finalized without asking


def test_wizard_q6_answer_by_number(monkeypatch, paid_tmp):
    """Q6 reply '2' picks the 2nd platform from the rendered list."""
    from paid import profile as _profile, setup_wizard

    prof = _profile.new_profile(owner_id="o1")
    prof.identities = [{"platform": "lark", "user_id": "ou_A", "enabled": True}]
    _profile.save_profile(prof)

    state = setup_wizard.WizardState(
        platform="slack", owner_id="U_OWNER", mode="first_time",
        step=5, awaiting_preferred_platform=True,
        answers={"name": "Jimmy"},
    )
    reply, done = setup_wizard._consume_preferred_platform_answer(state, "2")
    assert done is True

    loaded = _profile.load_profile()
    assert loaded.preferred_platform == "slack"


def test_wizard_q6_answer_by_name(monkeypatch, paid_tmp):
    from paid import profile as _profile, setup_wizard

    prof = _profile.new_profile(owner_id="o1")
    prof.identities = [{"platform": "lark", "user_id": "ou_A", "enabled": True}]
    _profile.save_profile(prof)

    state = setup_wizard.WizardState(
        platform="slack", owner_id="U_OWNER", mode="first_time",
        step=5, awaiting_preferred_platform=True,
        answers={"name": "Jimmy"},
    )
    reply, done = setup_wizard._consume_preferred_platform_answer(state, "slack")
    assert done is True
    assert _profile.load_profile().preferred_platform == "slack"


def test_wizard_q6_answer_auto_leaves_empty(monkeypatch, paid_tmp):
    from paid import profile as _profile, setup_wizard

    prof = _profile.new_profile(owner_id="o1")
    prof.identities = [{"platform": "lark", "user_id": "ou_A", "enabled": True}]
    _profile.save_profile(prof)

    state = setup_wizard.WizardState(
        platform="slack", owner_id="U_OWNER", mode="first_time",
        step=5, awaiting_preferred_platform=True,
        answers={"name": "Jimmy"},
    )
    reply, done = setup_wizard._consume_preferred_platform_answer(state, "auto")
    assert done is True
    assert _profile.load_profile().preferred_platform == ""


def test_wizard_q6_invalid_input_reprompts(monkeypatch, paid_tmp):
    from paid import profile as _profile, setup_wizard

    prof = _profile.new_profile(owner_id="o1")
    prof.identities = [{"platform": "lark", "user_id": "ou_A", "enabled": True}]
    _profile.save_profile(prof)

    state = setup_wizard.WizardState(
        platform="slack", owner_id="U_OWNER", mode="first_time",
        step=5, awaiting_preferred_platform=True,
        answers={"name": "Jimmy"},
    )
    reply, done = setup_wizard._consume_preferred_platform_answer(state, "discord")
    assert done is False
    assert "无法识别" in reply or "请回" in reply
