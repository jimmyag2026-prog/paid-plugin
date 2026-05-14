"""Tests for paid.profile_sync.derive_from_profile (v1.6.0)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


from paid import profile as p
from paid import profile_sync as ps
from paid import storage


@pytest.fixture
def paid_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "PAID_DIR", tmp_path)
    return tmp_path


def _fresh_profile(**over) -> p.OwnerProfile:
    prof = p.new_profile(
        over.pop("owner_id", "jimmy"),
        name=over.pop("name", "Jimmy Yin"),
    )
    prof.identities = [
        {"platform": "feishu", "user_id": "ou_test", "enabled": True}
    ]
    for k, v in over.items():
        setattr(prof, k, v)
    return prof


# ---------------------------------------------------------------------------
# All 4 files get written
# ---------------------------------------------------------------------------


def test_derive_writes_all_four_files(paid_tmp):
    prof = _fresh_profile()
    audit = ps.derive_from_profile(prof)
    assert sorted(audit["wrote"]) == sorted([
        "owner.json", "persona.md", "sop.md", "settings.json",
    ])
    for f in ("owner.json", "persona.md", "sop.md", "settings.json"):
        assert (paid_tmp / f).exists()


def test_derive_stamps_synced_to_files_at(paid_tmp):
    prof = _fresh_profile()
    ps.derive_from_profile(prof)
    # Re-load + verify timestamp populated
    loaded = p.load_profile()
    assert loaded.synced_to_files_at


# ---------------------------------------------------------------------------
# owner.json shape — matches paid.identity v2 schema reader
# ---------------------------------------------------------------------------


def test_owner_json_shape_matches_identity_v2(paid_tmp):
    prof = _fresh_profile()
    prof.identities = [
        {"platform": "feishu", "user_id": "ou_abc", "enabled": True},
        {"platform": "telegram", "user_id": "12345", "enabled": False},
    ]
    ps.derive_from_profile(prof)

    data = json.loads((paid_tmp / "owner.json").read_text())
    assert data["schema_version"] == 2
    assert data["owner_id"] == "jimmy"
    assert data["name"] == "Jimmy Yin"
    assert data["preferred_platform"] == "feishu"  # first enabled
    assert len(data["identities"]) == 2
    assert data["identities"][0]["platform"] == "feishu"
    assert data["identities"][1]["enabled"] is False


def test_owner_json_preferred_platform_picks_first_enabled(paid_tmp):
    """First disabled identity should be skipped when picking preferred."""
    prof = _fresh_profile()
    prof.identities = [
        {"platform": "slack", "user_id": "S1", "enabled": False},
        {"platform": "feishu", "user_id": "ou_x", "enabled": True},
    ]
    ps.derive_from_profile(prof)
    data = json.loads((paid_tmp / "owner.json").read_text())
    assert data["preferred_platform"] == "feishu"


def test_owner_json_home_chat_id_defaults_to_user_id(paid_tmp):
    prof = _fresh_profile()
    prof.identities = [
        {"platform": "feishu", "user_id": "ou_x", "enabled": True}
    ]
    ps.derive_from_profile(prof)
    data = json.loads((paid_tmp / "owner.json").read_text())
    assert data["identities"][0]["home_chat_id"] == "ou_x"


# ---------------------------------------------------------------------------
# persona.md content reflects voice fields
# ---------------------------------------------------------------------------


def test_persona_md_contains_voice_fields(paid_tmp):
    prof = _fresh_profile()
    prof.voice.self_description = "Founder of Y, infra background"
    prof.voice.do_not_say = ["按规定", "依据条款"]
    ps.derive_from_profile(prof)

    text = (paid_tmp / "persona.md").read_text()
    assert "direct-friendly" in text
    assert "Founder of Y" in text
    assert "按规定" in text
    assert "依据条款" in text
    assert "<!-- paid:profile-managed:start -->" in text
    assert "<!-- paid:profile-managed:end -->" in text


def test_persona_md_preserves_hand_edited_prose(paid_tmp):
    """Owner has pre-existing persona.md with custom prose — derive() should
    NOT delete it; profile fields go inside markers."""
    legacy = (paid_tmp / "persona.md")
    legacy.write_text(
        "# My custom persona\n\nLong-form hand-written prose.\n"
        "Examples of how I write: ...\n\n",
        encoding="utf-8",
    )
    prof = _fresh_profile()
    ps.derive_from_profile(prof)

    text = legacy.read_text()
    assert "Long-form hand-written prose" in text
    assert "Examples of how I write" in text
    assert "<!-- paid:profile-managed:start -->" in text  # new managed block added


def test_persona_md_idempotent(paid_tmp):
    """Two derive() calls in a row produce identical output."""
    prof = _fresh_profile()
    ps.derive_from_profile(prof)
    first = (paid_tmp / "persona.md").read_text()

    # Reload profile (sync stamps updated_at, but the rendered content
    # is independent of timestamps — we only test the persona.md text).
    prof2 = p.load_profile()
    ps.derive_from_profile(prof2)
    second = (paid_tmp / "persona.md").read_text()
    assert first == second


def test_persona_md_managed_block_replaced_on_change(paid_tmp):
    prof = _fresh_profile()
    ps.derive_from_profile(prof)
    first = (paid_tmp / "persona.md").read_text()
    assert "casual-friendly" not in first

    # Change voice, re-derive
    prof.voice.tone = "casual-friendly"
    ps.derive_from_profile(prof)
    second = (paid_tmp / "persona.md").read_text()
    assert "casual-friendly" in second
    assert "direct-friendly" not in second  # old tone removed
    # markers still exactly once each
    assert second.count("<!-- paid:profile-managed:start -->") == 1
    assert second.count("<!-- paid:profile-managed:end -->") == 1


# ---------------------------------------------------------------------------
# sop.md content
# ---------------------------------------------------------------------------


def test_sop_md_includes_all_topic_lists(paid_tmp):
    prof = _fresh_profile()
    prof.topics.always_direct = ["scheduling"]
    prof.topics.always_decline = ["legal advice"]
    ps.derive_from_profile(prof)

    text = (paid_tmp / "sop.md").read_text()
    assert "scheduling" in text
    assert "salary" in text  # default escalate
    assert "legal advice" in text
    assert "default_blacklist_action" not in text  # human-readable, not field name


def test_sop_md_preserves_legacy_content(paid_tmp):
    legacy = paid_tmp / "sop.md"
    legacy.write_text(
        "# Custom SOP\n\n## Tone notes\n\nBe concise.\n",
        encoding="utf-8",
    )
    prof = _fresh_profile()
    ps.derive_from_profile(prof)
    text = legacy.read_text()
    assert "Be concise" in text
    assert "Topic policy" in text  # managed section added


# ---------------------------------------------------------------------------
# settings.json content
# ---------------------------------------------------------------------------


def test_settings_json_renders_preferences(paid_tmp):
    prof = _fresh_profile()
    prof.preferences.daily_cost_cap_usd = 10.0
    prof.preferences.review_max_rounds = 4
    prof.preferences.ocr_languages = "jpn+eng"
    prof.preferences.model_primary = "deepseek-v4-pro"
    ps.derive_from_profile(prof)

    data = json.loads((paid_tmp / "settings.json").read_text())
    assert data["daily_cost_cap_usd"] == 10.0
    assert data["review_max_rounds"] == 4
    assert data["ocr_languages"] == "jpn+eng"
    assert data["model_override"] == "deepseek-v4-pro"


def test_settings_json_preserves_unmanaged_fields(paid_tmp):
    """settings.json has fields PAID's other modules write that aren't in
    profile (l4c_enabled, llm_retry_backoffs_seconds, custom flags).
    derive() must preserve them."""
    settings_path = paid_tmp / "settings.json"
    settings_path.write_text(json.dumps({
        "l4c_enabled": True,
        "llm_retry_backoffs_seconds": [1, 5, 30],
        "custom_pilot_flag": "x",
    }), encoding="utf-8")

    prof = _fresh_profile()
    ps.derive_from_profile(prof)

    data = json.loads(settings_path.read_text())
    # Unmanaged fields preserved
    assert data["l4c_enabled"] is True
    assert data["llm_retry_backoffs_seconds"] == [1, 5, 30]
    assert data["custom_pilot_flag"] == "x"
    # Managed fields written
    assert data["daily_cost_cap_usd"] == 5.0


# ---------------------------------------------------------------------------
# End-to-end save→derive→reload→derive consistency
# ---------------------------------------------------------------------------


def test_derive_then_reload_then_derive_again_is_stable(paid_tmp):
    prof = _fresh_profile()
    ps.derive_from_profile(prof)
    persona_v1 = (paid_tmp / "persona.md").read_text()
    settings_v1 = (paid_tmp / "settings.json").read_text()

    # Simulate a session reload
    prof2 = p.load_profile()
    assert prof2 is not None
    ps.derive_from_profile(prof2)

    persona_v2 = (paid_tmp / "persona.md").read_text()
    settings_v2 = (paid_tmp / "settings.json").read_text()
    assert persona_v1 == persona_v2
    assert settings_v1 == settings_v2
