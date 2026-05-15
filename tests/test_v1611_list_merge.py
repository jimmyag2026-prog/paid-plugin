"""v1.6.11 — _parse_proposals must merge list-typed profile fields,
not replace them.

Live regression from VPS testing right after v1.6.10 made the LLM
extraction work for the first time: owner accepted a conv_capture
proposal that read

    field=topics.always_decline  proposed=['交付时间']

and the apply step overwrote the entire always_decline list — wiping
the three earlier entries ("ongoing negotiations", "personal contact
info", "compensation/equity/hiring") down to just `['交付时间']`.

The LLM emits *delta* values for list fields (items to add), so the
parser is responsible for merging with the current value before any
setattr happens.

Same risk exists for `voice.do_not_say`, `topics.always_direct`, and
`topics.always_escalate`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from paid import doc_ingest, profile, storage


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "PAID_DIR", tmp_path)


def _seeded_profile_with_existing_decline() -> profile.OwnerProfile:
    prof = profile.new_profile(owner_id="o1")
    prof.topics.always_decline = [
        "ongoing negotiations",
        "personal contact info",
        "compensation/equity/hiring",
    ]
    prof.voice.do_not_say = ["按规定", "依据条款"]
    prof.topics.always_escalate = ["equity", "hiring"]
    prof.topics.always_direct = ["logistics"]
    return prof


# ---------------------------------------------------------------------------
# always_decline — the exact live scenario
# ---------------------------------------------------------------------------


def test_always_decline_merges_does_not_replace():
    """The literal VPS regression: ['交付时间'] proposed against an existing
    3-item always_decline must produce a 4-item merged list, NOT replace."""
    prof = _seeded_profile_with_existing_decline()
    raw = json.dumps([{
        "field": "topics.always_decline",
        "proposed": ["交付时间"],
        "rationale": "owner said '以后客户问 交付时间 直接拒绝'",
    }])
    proposals = doc_ingest._parse_proposals(raw, prof)
    assert len(proposals) == 1
    assert proposals[0].proposed == [
        "ongoing negotiations",
        "personal contact info",
        "compensation/equity/hiring",
        "交付时间",
    ]


def test_apply_writes_merged_value_to_profile():
    """End-to-end: parse → apply → load round-trip preserves both pre-existing
    and newly-proposed items."""
    prof = _seeded_profile_with_existing_decline()
    profile.save_profile(prof)

    raw = json.dumps([{
        "field": "topics.always_decline",
        "proposed": ["pricing"],
        "rationale": "owner said reject pricing inquiries",
    }])
    proposals = doc_ingest._parse_proposals(raw, prof)
    proposals[0].accepted = True
    doc_ingest.apply_proposals(prof, proposals)

    reloaded = profile.load_profile()
    assert reloaded.topics.always_decline == [
        "ongoing negotiations",
        "personal contact info",
        "compensation/equity/hiring",
        "pricing",
    ]


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


def test_merge_dedupes_when_llm_re_proposes_existing_item():
    """LLM might re-propose an item already in the list (e.g. owner re-says
    the same SOP). Dedup so we don't get duplicates."""
    prof = _seeded_profile_with_existing_decline()
    raw = json.dumps([{
        "field": "topics.always_decline",
        "proposed": ["ongoing negotiations", "pricing"],  # first is dup
        "rationale": "...",
    }])
    proposals = doc_ingest._parse_proposals(raw, prof)
    assert proposals[0].proposed == [
        "ongoing negotiations",
        "personal contact info",
        "compensation/equity/hiring",
        "pricing",
    ]


def test_merge_against_empty_current_just_uses_proposed():
    """Fresh profile (empty list field) → proposed becomes the new value."""
    prof = profile.new_profile(owner_id="o1")
    # always_decline default is []
    raw = json.dumps([{
        "field": "topics.always_decline",
        "proposed": ["pricing", "competitor info"],
        "rationale": "...",
    }])
    proposals = doc_ingest._parse_proposals(raw, prof)
    assert proposals[0].proposed == ["pricing", "competitor info"]


# ---------------------------------------------------------------------------
# Coverage across all list fields
# ---------------------------------------------------------------------------


def test_voice_do_not_say_merges():
    prof = _seeded_profile_with_existing_decline()
    raw = json.dumps([{
        "field": "voice.do_not_say",
        "proposed": ["请理解"],
        "rationale": "...",
    }])
    proposals = doc_ingest._parse_proposals(raw, prof)
    assert proposals[0].proposed == ["按规定", "依据条款", "请理解"]


def test_topics_always_escalate_merges():
    prof = _seeded_profile_with_existing_decline()
    raw = json.dumps([{
        "field": "topics.always_escalate",
        "proposed": ["salary"],
        "rationale": "...",
    }])
    proposals = doc_ingest._parse_proposals(raw, prof)
    assert proposals[0].proposed == ["equity", "hiring", "salary"]


def test_topics_always_direct_merges():
    prof = _seeded_profile_with_existing_decline()
    raw = json.dumps([{
        "field": "topics.always_direct",
        "proposed": ["scheduling"],
        "rationale": "...",
    }])
    proposals = doc_ingest._parse_proposals(raw, prof)
    assert proposals[0].proposed == ["logistics", "scheduling"]


# ---------------------------------------------------------------------------
# Scalar fields keep replace-on-apply semantics
# ---------------------------------------------------------------------------


def test_voice_tone_still_replaces():
    """Scalar fields must NOT be merged — owner says "tone professional"
    means replace, not append."""
    prof = profile.new_profile(owner_id="o1")  # default tone "direct-friendly"
    raw = json.dumps([{
        "field": "voice.tone",
        "proposed": "professional",
        "rationale": "...",
    }])
    proposals = doc_ingest._parse_proposals(raw, prof)
    # proposed stays as-is, not wrapped in a list or merged with anything
    assert proposals[0].proposed == "professional"


def test_daily_cost_cap_usd_still_replaces():
    prof = profile.new_profile(owner_id="o1")  # default 5.0
    raw = json.dumps([{
        "field": "preferences.daily_cost_cap_usd",
        "proposed": 25.0,
        "rationale": "...",
    }])
    proposals = doc_ingest._parse_proposals(raw, prof)
    assert proposals[0].proposed == 25.0


def test_observed_decision_window_still_replaces():
    prof = profile.new_profile(owner_id="o1")
    raw = json.dumps([{
        "field": "observed.preferred_decision_window_hrs",
        "proposed": 4.5,
        "rationale": "owner mentioned 4-5h response",
    }])
    proposals = doc_ingest._parse_proposals(raw, prof)
    assert proposals[0].proposed == 4.5


# ---------------------------------------------------------------------------
# Frozenset contract
# ---------------------------------------------------------------------------


def test_list_profile_fields_subset_of_allowed():
    """Every list field must be in the master allow-list — otherwise the
    parser would drop the proposal before merge logic could run."""
    assert profile.LIST_PROFILE_FIELDS <= profile.ALLOWED_PROFILE_FIELDS


def test_list_profile_fields_has_all_owner_list_fields():
    """Defense against future schema drift: any list-typed field on the
    OwnerProfile dataclass that's also in ALLOWED_PROFILE_FIELDS should
    appear in LIST_PROFILE_FIELDS, or someone will write the same bug
    again."""
    expected = {
        "voice.do_not_say",
        "topics.always_direct",
        "topics.always_escalate",
        "topics.always_decline",
    }
    assert profile.LIST_PROFILE_FIELDS == expected
