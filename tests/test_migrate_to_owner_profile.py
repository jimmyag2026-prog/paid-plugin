"""Tests for bin/migrate_to_owner_profile.py (v1.6.0 sprint 2)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Import the bin module by file path (bin/ isn't a normal package)
import importlib.util


def _load_migrate_module():
    spec = importlib.util.spec_from_file_location(
        "migrate_to_owner_profile_mod",
        _ROOT / "bin" / "migrate_to_owner_profile.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def paid_tmp(tmp_path, monkeypatch):
    from paid import storage
    monkeypatch.setattr(storage, "PAID_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def migrate_mod():
    return _load_migrate_module()


# ---------------------------------------------------------------------------
# Helpers — seed legacy files
# ---------------------------------------------------------------------------


def _write_legacy(paid_tmp: Path, *, owner=None, persona=None, sop=None, settings=None):
    if owner is not None:
        (paid_tmp / "owner.json").write_text(
            json.dumps(owner), encoding="utf-8"
        )
    if persona is not None:
        (paid_tmp / "persona.md").write_text(persona, encoding="utf-8")
    if sop is not None:
        (paid_tmp / "sop.md").write_text(sop, encoding="utf-8")
    if settings is not None:
        (paid_tmp / "settings.json").write_text(
            json.dumps(settings), encoding="utf-8"
        )


# ---------------------------------------------------------------------------
# --no-llm path (no external LLM dependency)
# ---------------------------------------------------------------------------


def test_no_llm_no_files_creates_default_profile(paid_tmp, migrate_mod):
    audit = migrate_mod.migrate(use_llm=False)
    assert audit["wrote"] is True

    from paid import profile as p
    prof = p.load_profile()
    assert prof.owner_id == "owner"  # default
    assert prof.name == ""
    assert prof.identities == []
    assert prof.voice.tone == "direct-friendly"
    assert "salary" in prof.topics.always_escalate  # default 5


def test_no_llm_with_owner_and_settings(paid_tmp, migrate_mod):
    _write_legacy(paid_tmp,
        owner={
            "owner_id": "jimmy",
            "name": "Jimmy Yin",
            "identities": [
                {"platform": "feishu", "user_id": "ou_x", "enabled": True}
            ],
        },
        settings={
            "model_override": "deepseek-v4-pro",
            "daily_cost_cap_usd": 10.0,
            "review_max_rounds": 4,
        },
    )
    audit = migrate_mod.migrate(use_llm=False)
    assert audit["wrote"]
    assert audit["sources_read"]["owner.json"] is True
    assert audit["sources_read"]["settings.json"] is True
    assert audit["sources_read"]["persona.md"] is False  # not written

    from paid import profile as p
    prof = p.load_profile()
    assert prof.owner_id == "jimmy"
    assert prof.name == "Jimmy Yin"
    assert prof.identities[0]["platform"] == "feishu"
    assert prof.preferences.model_primary == "deepseek-v4-pro"
    assert prof.preferences.daily_cost_cap_usd == 10.0
    assert prof.preferences.review_max_rounds == 4


def test_no_llm_skips_extract(paid_tmp, migrate_mod):
    _write_legacy(paid_tmp,
        persona="# Persona\n\nLong description that would normally be extracted",
        sop="# SOP\n\nLots of topic rules"
    )
    audit = migrate_mod.migrate(use_llm=False)
    assert audit["extract"]["voice_used_llm"] is False
    assert audit["extract"]["topics_used_llm"] is False

    from paid import profile as p
    prof = p.load_profile()
    # Defaults applied since --no-llm
    assert prof.voice.tone == "direct-friendly"
    assert prof.voice.style_notes == ""


# ---------------------------------------------------------------------------
# Force / exists semantics
# ---------------------------------------------------------------------------


def test_aborts_when_profile_exists_without_force(paid_tmp, migrate_mod):
    (paid_tmp / "owner_profile.json").write_text(
        json.dumps({"owner_id": "x"}), encoding="utf-8"
    )
    with pytest.raises(FileExistsError):
        migrate_mod.migrate(use_llm=False)


def test_force_overwrites_existing_profile(paid_tmp, migrate_mod):
    (paid_tmp / "owner_profile.json").write_text(
        json.dumps({"owner_id": "OLD"}), encoding="utf-8"
    )
    _write_legacy(paid_tmp, owner={"owner_id": "NEW", "name": "", "identities": []})
    audit = migrate_mod.migrate(use_llm=False, force=True)
    assert audit["wrote"]

    from paid import profile as p
    assert p.load_profile().owner_id == "NEW"


def test_dry_run_does_not_write(paid_tmp, migrate_mod):
    _write_legacy(paid_tmp, owner={"owner_id": "jimmy", "name": "", "identities": []})
    audit = migrate_mod.migrate(use_llm=False, dry_run=True)
    assert audit["wrote"] is False
    assert "dry_run_payload" in audit
    assert audit["dry_run_payload"]["owner_id"] == "jimmy"

    # No file written
    assert not (paid_tmp / "owner_profile.json").exists()


# ---------------------------------------------------------------------------
# LLM extraction path — stubbed
# ---------------------------------------------------------------------------


def test_llm_voice_extraction_uses_response(paid_tmp, migrate_mod, monkeypatch):
    _write_legacy(paid_tmp,
        persona="Owner is a founder. Style: direct, short. Never say '按规定'.",
    )

    fake_voice = {
        "tone": "direct-friendly",
        "style_notes": "Short, direct sentences",
        "self_description": "Founder",
        "do_not_say": ["按规定"],
    }
    fake_topics = {
        "always_direct": [],
        "always_escalate": ["equity", "salary", "hiring", "customer", "finance"],
        "always_decline": [],
        "default_blacklist_action": "decline",
    }

    call_log = []

    def fake_call_llm(prompt, system=""):
        call_log.append(prompt[:30])
        if "persona.md" in prompt:
            return json.dumps(fake_voice)
        if "sop.md" in prompt:
            return json.dumps(fake_topics)
        return "{}"

    from paid import hermes_io
    monkeypatch.setattr(hermes_io, "call_llm", fake_call_llm)

    audit = migrate_mod.migrate(use_llm=True)
    assert audit["wrote"]
    assert audit["extract"]["voice_used_llm"]

    from paid import profile as p
    prof = p.load_profile()
    assert prof.voice.self_description == "Founder"
    assert "按规定" in prof.voice.do_not_say


def test_llm_empty_persona_skips_call(paid_tmp, migrate_mod, monkeypatch):
    """Empty persona.md should use defaults, NOT call LLM."""
    # No persona.md, no sop.md → both should skip LLM
    call_log = []

    def fake_call_llm(**kw):
        call_log.append("called")
        return "{}"

    from paid import hermes_io
    monkeypatch.setattr(hermes_io, "call_llm", fake_call_llm)

    migrate_mod.migrate(use_llm=True)
    # Both voice + topics had empty input → no LLM call made
    assert call_log == []


def test_llm_handles_json_fence_wrapped_response(paid_tmp, migrate_mod, monkeypatch):
    _write_legacy(paid_tmp, persona="some content")

    def fake_call_llm(prompt, system=""):
        # LLM returns markdown-fence-wrapped JSON (common Claude/Gemini behavior)
        return '```json\n{"tone": "casual", "style_notes": "s", "self_description": "d", "do_not_say": []}\n```'

    from paid import hermes_io
    monkeypatch.setattr(hermes_io, "call_llm", fake_call_llm)

    audit = migrate_mod.migrate(use_llm=True)
    assert audit["wrote"]

    from paid import profile as p
    prof = p.load_profile()
    assert prof.voice.tone == "casual"


def test_llm_failure_raises_runtime_error(paid_tmp, migrate_mod, monkeypatch):
    _write_legacy(paid_tmp, persona="content")

    def fake_call_llm(prompt, system=""):
        raise RuntimeError("LLM api down")

    from paid import hermes_io
    monkeypatch.setattr(hermes_io, "call_llm", fake_call_llm)

    with pytest.raises(RuntimeError, match="voice extract failed"):
        migrate_mod.migrate(use_llm=True)


# ---------------------------------------------------------------------------
# JSON parse strict
# ---------------------------------------------------------------------------


def test_parse_json_strict_plain(migrate_mod):
    out = migrate_mod._parse_json_strict('{"a": 1}')
    assert out == {"a": 1}


def test_parse_json_strict_strips_fence(migrate_mod):
    out = migrate_mod._parse_json_strict('```json\n{"a": 1}\n```')
    assert out == {"a": 1}


def test_parse_json_strict_strips_generic_fence(migrate_mod):
    out = migrate_mod._parse_json_strict('```\n{"a": 1}\n```')
    assert out == {"a": 1}
