"""Tests for v1.6.8 blockers from VPS manual testing.

B1: setup_wizard._finalize_first_time used to call _profile.new_profile()
    even when an existing profile was loaded — clobbering fields the
    wizard didn't ask about (always_decline, style_notes from sop.md,
    counterparty_overrides, observed.*, etc.).

B2: safety.check_output set ok=False when L4d (unsourced_claims) fired,
    causing __init__.on_post_llm_call to send a "disregard previous
    reply" corrective DM to cp and a fatal_alerts row to disk on every
    benign summary of user-provided content.

B3 is covered in test_observer.py (rewritten for real schema).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from datetime import datetime, timezone

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from paid import profile as _profile
from paid import safety, setup_wizard, storage


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "PAID_DIR", tmp_path)


# ---------------------------------------------------------------------------
# B1 — wizard partial update
# ---------------------------------------------------------------------------


def _seed_existing_profile_with_rich_topics() -> _profile.OwnerProfile:
    """Mimic the state after v1.6.0 migration: profile has fields the
    5-question wizard does NOT ask about (always_decline, style_notes,
    counterparty_overrides, observed.preferred_decision_window_hrs)."""
    prof = _profile.new_profile(owner_id="owner_test", name="Original")
    prof.voice.style_notes = "Short Chinese-style sentences. Set by migration."
    prof.voice.do_not_say = ["按规定", "依据条款"]
    prof.topics.always_decline = [
        "ongoing negotiations",
        "personal contact info",
        "compensation/equity/hiring",
    ]
    prof.counterparty_overrides = [{"cp_id": "x", "voice_overrides": {}}]
    prof.observed.approval_rate = 0.85
    prof.observed.preferred_decision_window_hrs = 4.2
    _profile.save_profile(prof)
    return prof


def test_b1_wizard_first_time_preserves_existing_always_decline():
    """The exact bug from VPS: always_decline was emptied because wizard
    overwrote the profile when finalize ran."""
    _seed_existing_profile_with_rich_topics()

    state = setup_wizard.WizardState(
        platform="feishu", owner_id="owner_test", step=5, mode="first_time",
        answers={
            "name": "Jimmy",
            "voice_preset": "founder",
            "always_escalate": ["hiring", "salary"],
            "preferred_language": "auto",
            "daily_cost_cap_usd": 10.0,
        },
    )
    setup_wizard._finalize_first_time(state)

    after = _profile.load_profile()
    assert after.topics.always_decline == [
        "ongoing negotiations",
        "personal contact info",
        "compensation/equity/hiring",
    ], "wizard must NOT clear always_decline"


def test_b1_wizard_preserves_counterparty_overrides():
    _seed_existing_profile_with_rich_topics()
    state = setup_wizard.WizardState(
        platform="feishu", owner_id="owner_test", step=5, mode="first_time",
        answers={"name": "Renamed"},
    )
    setup_wizard._finalize_first_time(state)

    after = _profile.load_profile()
    assert len(after.counterparty_overrides) == 1
    assert after.counterparty_overrides[0]["cp_id"] == "x"


def test_b1_wizard_preserves_observed_fields():
    _seed_existing_profile_with_rich_topics()
    state = setup_wizard.WizardState(
        platform="feishu", owner_id="owner_test", step=5, mode="first_time",
        answers={"preferred_language": "zh"},
    )
    setup_wizard._finalize_first_time(state)

    after = _profile.load_profile()
    assert after.observed.approval_rate == 0.85
    assert after.observed.preferred_decision_window_hrs == 4.2


def test_b1_wizard_applies_the_5_answered_fields():
    _seed_existing_profile_with_rich_topics()
    state = setup_wizard.WizardState(
        platform="feishu", owner_id="owner_test", step=5, mode="first_time",
        answers={
            "name": "NewName",
            "preferred_language": "en",
            "daily_cost_cap_usd": 25.0,
        },
    )
    setup_wizard._finalize_first_time(state)

    after = _profile.load_profile()
    assert after.name == "NewName"
    assert after.preferred_language == "en"
    assert after.preferences.daily_cost_cap_usd == 25.0


def test_b1_wizard_truly_first_time_uses_defaults():
    """Without an existing profile, behavior must be unchanged: build fresh."""
    state = setup_wizard.WizardState(
        platform="feishu", owner_id="owner_first", step=5, mode="first_time",
        answers={
            "name": "FreshOwner",
            "voice_preset": "minimal",
            "preferred_language": "en",
        },
    )
    setup_wizard._finalize_first_time(state)

    prof = _profile.load_profile()
    assert prof is not None
    assert prof.owner_id == "owner_first"
    assert prof.name == "FreshOwner"
    assert prof.voice.tone == "minimal"


# ---------------------------------------------------------------------------
# B2 — L4d fail-open
# ---------------------------------------------------------------------------


def test_b2_unsourced_claim_alone_does_not_flip_ok(paid_tmp):
    """The exact bug seen on VPS: summary of cp-provided doc triggered
    L4d → corrective DM. Now L4d is observational only."""
    res = safety.check_output(
        "Report shows 12,500 active users last week.",
        "telegram_111",
    )
    assert res["ok"] is True
    # And the unsourced detector still recorded its finding
    assert "12,500" in " ".join(res.get("unsourced_claims", []))


def test_b2_pii_still_flips_ok(paid_tmp):
    """Sanity check: real PII (raw email) must still trigger ok=False."""
    res = safety.check_output(
        "Reach me directly at secret_email@example.com tomorrow.", "telegram_111",
    )
    assert res["ok"] is False
    assert res["pii"]  # non-empty


def test_b2_check_output_dict_includes_diagnostic_keys(paid_tmp):
    """v1.6.8: dict must surface all detector lists so audit/alert renderers
    can show *what* tripped instead of empty-list mystery alerts."""
    res = safety.check_output("DAU jumped to 9,999.", "telegram_111")
    # L4a/L4b lists always present
    assert "name_leakage" in res
    assert "pii" in res
    # L4d list present when run_l4d=True (default)
    assert "unsourced_claims" in res
