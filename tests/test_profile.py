"""Tests for paid.profile schema + load/save (v1.6.0)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


from paid import profile as p
from paid import storage


@pytest.fixture
def paid_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "PAID_DIR", tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Default construction
# ---------------------------------------------------------------------------


def test_owner_profile_defaults_safe():
    prof = p.OwnerProfile(owner_id="x")
    assert prof.name == ""
    assert prof.preferred_language == "auto"
    assert prof.voice.tone == "direct-friendly"
    assert prof.voice.do_not_say == []
    # Default always_escalate matches v1.4.x baseline
    assert "salary" in prof.topics.always_escalate
    assert "hiring" in prof.topics.always_escalate
    assert prof.topics.default_blacklist_action == "decline"
    assert prof.preferences.daily_cost_cap_usd == 5.0
    assert prof.preferences.review_max_rounds == 3
    assert prof.preferences.ocr_languages == "chi_sim+eng"
    assert prof.observed.approval_rate == 0.0
    assert prof.schema_version == p.PROFILE_SCHEMA_VERSION


def test_new_profile_founder_preset():
    prof = p.new_profile("jimmy", name="Jimmy Yin")
    assert prof.owner_id == "jimmy"
    assert prof.name == "Jimmy Yin"
    assert prof.voice.tone == "direct-friendly"
    assert "按规定" in prof.voice.do_not_say


def test_new_profile_minimal_preset():
    prof = p.new_profile("anyone", voice_preset="minimal")
    assert prof.voice.tone == "minimal"
    assert prof.voice.do_not_say == []


def test_new_profile_unknown_preset_falls_back_founder():
    prof = p.new_profile("anyone", voice_preset="does-not-exist")
    assert prof.voice.tone == "direct-friendly"  # founder default


def test_list_voice_presets():
    presets = p.list_voice_presets()
    assert "founder" in presets
    assert "professional" in presets
    assert "casual" in presets
    assert "minimal" in presets


# ---------------------------------------------------------------------------
# Round-trip persistence
# ---------------------------------------------------------------------------


def test_save_and_load_roundtrip(paid_tmp):
    prof = p.new_profile("jimmy", name="Jimmy Yin")
    prof.identities = [
        {"platform": "feishu", "user_id": "ou_x", "enabled": True}
    ]
    prof.topics.always_escalate.append("custom_topic")
    prof.preferences.model_primary = "deepseek-v4-pro"
    p.save_profile(prof)

    loaded = p.load_profile()
    assert loaded is not None
    assert loaded.owner_id == "jimmy"
    assert loaded.name == "Jimmy Yin"
    assert loaded.identities[0]["platform"] == "feishu"
    assert "custom_topic" in loaded.topics.always_escalate
    assert loaded.preferences.model_primary == "deepseek-v4-pro"


def test_save_stamps_timestamps_and_schema(paid_tmp):
    prof = p.OwnerProfile(owner_id="x")
    saved = p.save_profile(prof)
    assert saved.created_at  # non-empty
    assert saved.updated_at
    assert saved.schema_version == p.PROFILE_SCHEMA_VERSION


def test_save_preserves_created_at_on_repeat(paid_tmp):
    prof = p.OwnerProfile(owner_id="x")
    p.save_profile(prof)
    first_ts = prof.created_at

    # Mutate + re-save; created_at should stay, updated_at advances
    prof.name = "Newer"
    p.save_profile(prof)
    assert prof.created_at == first_ts


def test_load_missing_file_returns_none(paid_tmp):
    assert p.load_profile() is None


def test_load_malformed_json_returns_none(paid_tmp):
    (paid_tmp / "owner_profile.json").write_text("{not json}", encoding="utf-8")
    assert p.load_profile() is None


# ---------------------------------------------------------------------------
# Backward-compat — old/missing fields fill defaults
# ---------------------------------------------------------------------------


def test_load_v0_minimal_dict_fills_defaults(paid_tmp):
    """A skeleton profile with just owner_id should load with all defaults."""
    (paid_tmp / "owner_profile.json").write_text(
        json.dumps({"owner_id": "jimmy"}), encoding="utf-8",
    )
    loaded = p.load_profile()
    assert loaded.owner_id == "jimmy"
    assert loaded.voice.tone == "direct-friendly"
    assert loaded.topics.always_escalate  # default 5 fill in


def test_load_tolerates_extra_unknown_fields(paid_tmp):
    """Future schema_version files have extra fields — must not crash."""
    (paid_tmp / "owner_profile.json").write_text(
        json.dumps({
            "owner_id": "x",
            "future_field": "ignore me",
            "voice": {"tone": "x", "future_voice_field": 1},
        }), encoding="utf-8",
    )
    loaded = p.load_profile()
    assert loaded.owner_id == "x"
    assert loaded.voice.tone == "x"


def test_load_handles_null_nested_dicts_gracefully(paid_tmp):
    """profile.json sometimes has explicit nulls in nested dicts after manual edit."""
    (paid_tmp / "owner_profile.json").write_text(
        json.dumps({
            "owner_id": "x",
            "voice": None,
            "topics": None,
            "preferences": None,
            "observed": None,
        }), encoding="utf-8",
    )
    loaded = p.load_profile()
    assert loaded is not None
    assert loaded.voice.tone == "direct-friendly"
    assert loaded.topics.default_blacklist_action == "decline"


# ---------------------------------------------------------------------------
# Type coercion (JSON loads strings as strings, ensure we coerce numbers right)
# ---------------------------------------------------------------------------


def test_load_coerces_numeric_strings(paid_tmp):
    """Numeric prefs stored as string (e.g. owner hand-edit) should coerce."""
    (paid_tmp / "owner_profile.json").write_text(
        json.dumps({
            "owner_id": "x",
            "preferences": {
                "daily_cost_cap_usd": "10.5",
                "review_max_rounds": "4",
            },
        }), encoding="utf-8",
    )
    loaded = p.load_profile()
    assert loaded.preferences.daily_cost_cap_usd == 10.5
    assert loaded.preferences.review_max_rounds == 4
