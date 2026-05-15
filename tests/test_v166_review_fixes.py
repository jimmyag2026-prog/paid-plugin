"""Tests for v1.6.6 hotfixes from the v1.6.1-v1.6.5 code review.

Coverage:
  B1 — _ALLOWED_FIELDS shared between conv_capture + doc_ingest;
       observed.preferred_decision_window_hrs now actually applies.
  B3 — doctor._check_recent_errors reads via audit.read_all_entries,
       so per-cp audit entries surface fatal events post-migration.
  S6 — P75 uses linear interpolation, not nearest-rank.
  S7 — audit log_action stamps entry_id; read_all_entries dedups by entry_id.
  N9 — conv_capture._PENDING prunes expired entries.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from paid import audit, conv_capture, doc_ingest, observer, profile, storage


# ---------------------------------------------------------------------------
# B1 — allowed fields shared
# ---------------------------------------------------------------------------


def test_allowed_fields_includes_observed_window():
    """The whole point of the fix: this field must be in the allowed set
    so conv_capture's LLM prompt isn't silently dropping its proposals."""
    assert "observed.preferred_decision_window_hrs" in profile.ALLOWED_PROFILE_FIELDS


def test_allowed_fields_covers_conv_capture_prompt():
    """Every field the conv_capture system prompt claims it can extract
    must round-trip through _parse_proposals."""
    advertised = {
        "voice.do_not_say",
        "voice.tone",
        "voice.style_notes",
        "topics.always_escalate",
        "topics.always_direct",
        "preferred_language",
        "preferences.daily_cost_cap_usd",
        "observed.preferred_decision_window_hrs",
    }
    missing = advertised - profile.ALLOWED_PROFILE_FIELDS
    assert not missing, f"conv_capture prompts these but parser drops them: {missing}"


def test_parse_proposals_accepts_observed_field():
    """Before v1.6.6 this LLM response would be silently dropped."""
    prof = profile.new_profile(owner_id="o1")
    raw = '[{"field": "observed.preferred_decision_window_hrs", '\
          '"proposed": 4.5, "rationale": "owner said they reply within 4-5h"}]'
    props = doc_ingest._parse_proposals(raw, prof)
    assert len(props) == 1
    assert props[0].field == "observed.preferred_decision_window_hrs"
    assert props[0].proposed == 4.5


def test_parse_proposals_drops_unknown_field():
    """Whitelist still rejects bogus / sensitive field paths."""
    prof = profile.new_profile(owner_id="o1")
    raw = '[{"field": "secrets.api_key", "proposed": "sk-...", "rationale": "x"}]'
    props = doc_ingest._parse_proposals(raw, prof)
    assert props == []


def test_apply_observed_window_actually_writes(tmp_path, monkeypatch):
    """End-to-end: proposal → apply → profile.observed.preferred_decision_window_hrs set."""
    monkeypatch.setattr(storage, "PAID_DIR", tmp_path)
    prof = profile.new_profile(owner_id="o1")
    profile.save_profile(prof)

    raw = '[{"field": "observed.preferred_decision_window_hrs", '\
          '"proposed": 6.0, "rationale": "from owner DM"}]'
    props = doc_ingest._parse_proposals(raw, prof)
    for p in props:
        p.accepted = True
    doc_ingest.apply_proposals(prof, props)

    reloaded = profile.load_profile()
    assert reloaded.observed.preferred_decision_window_hrs == 6.0


# ---------------------------------------------------------------------------
# B3 — doctor reads via read_all_entries (per-cp aware)
# ---------------------------------------------------------------------------


def test_doctor_finds_fatal_in_per_cp_file(tmp_path, monkeypatch):
    """v1.6.6 fix: doctor must see fatal events that landed in per-cp files
    (the v1.6.4 layout). Before this fix it only read legacy audit_log.jsonl
    and post-migration always reported 'fresh install'."""
    monkeypatch.setattr(storage, "PAID_DIR", tmp_path)
    from datetime import datetime, timezone

    # Write a recent fatal entry directly to a per-cp audit file.
    cp_dir = tmp_path / "counterparties" / "cp_x"
    cp_dir.mkdir(parents=True)
    import json
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": "s",
        "counterparty": "cp_x",
        "extra": {"fatal": True},
        "reason": "test fatal",
    }
    (cp_dir / "audit.jsonl").write_text(json.dumps(row) + "\n")

    # Doctor should now flag this.
    from paid import doctor
    ok, detail, _hint = doctor._check_recent_errors()
    assert ok is False
    assert "fatal=1" in detail


def test_doctor_passes_when_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "PAID_DIR", tmp_path)
    from paid import doctor
    ok, detail, _ = doctor._check_recent_errors()
    assert ok is True
    assert "fresh install" in detail


# ---------------------------------------------------------------------------
# S6 — P75 linear interpolation
# ---------------------------------------------------------------------------


def test_p75_linear_three_values():
    """[1,2,3] should give 2.5 (nearest-rank gave 2)."""
    assert observer._percentile_linear([1.0, 2.0, 3.0], 0.75) == 2.5


def test_p75_linear_single_value():
    assert observer._percentile_linear([7.0], 0.75) == 7.0


def test_p75_linear_two_values():
    """[1, 5] @ q=0.75 → 1 + 4*0.75 = 4.0"""
    assert observer._percentile_linear([1.0, 5.0], 0.75) == 4.0


def test_p75_linear_matches_numpy_when_available():
    """Sanity check vs numpy.percentile (linear is its default)."""
    pytest.importorskip("numpy")
    import numpy as np
    vals = [0.5, 1.2, 3.4, 4.0, 5.1, 7.7, 12.0]
    expected = float(np.percentile(vals, 75))
    assert abs(observer._percentile_linear(vals, 0.75) - expected) < 1e-9


def test_p75_empty_raises():
    with pytest.raises(ValueError):
        observer._percentile_linear([], 0.75)


# ---------------------------------------------------------------------------
# S7 — audit entry_id + entry_id-based dedup
# ---------------------------------------------------------------------------


def test_audit_log_action_stamps_entry_id(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "PAID_DIR", tmp_path)

    class FakeCp:
        cp_id = "feishu_x"
        platform = "feishu"

    audit.log_action("s1", FakeCp(), "hi", None, None)
    rows = audit.read_all_entries()
    assert len(rows) == 1
    assert rows[0].get("entry_id")
    assert len(rows[0]["entry_id"]) >= 8  # hex token


def test_audit_entry_ids_are_unique(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "PAID_DIR", tmp_path)

    class FakeCp:
        cp_id = "feishu_x"
        platform = "feishu"

    for _ in range(20):
        audit.log_action("s", FakeCp(), "m", None, None)
    rows = audit.read_all_entries()
    ids = [r["entry_id"] for r in rows]
    assert len(set(ids)) == len(ids)


def test_audit_dedup_uses_entry_id(tmp_path, monkeypatch):
    """Same entry written to both legacy and per-cp dirs is deduped via entry_id."""
    monkeypatch.setattr(storage, "PAID_DIR", tmp_path)
    import json

    entry = {
        "entry_id": "deadbeef0000",
        "ts": "2026-01-01T00:00:00+00:00",
        "session_id": "s",
        "counterparty": "cp_x",
        "junior_msg": "hi",
    }
    (tmp_path / "audit_log.jsonl").write_text(json.dumps(entry) + "\n")
    cp_dir = tmp_path / "counterparties" / "cp_x"
    cp_dir.mkdir(parents=True)
    (cp_dir / "audit.jsonl").write_text(json.dumps(entry) + "\n")

    rows = audit.read_all_entries()
    assert len(rows) == 1


def test_audit_does_not_dedup_distinct_system_events_with_same_ts(tmp_path, monkeypatch):
    """Pre-v1.6.6 bug: two distinct system events with same ts + empty session_id
    were dropped to one. With entry_id they survive."""
    monkeypatch.setattr(storage, "PAID_DIR", tmp_path)
    import json

    ts = "2026-01-01T00:00:00+00:00"
    e1 = {"entry_id": "aaa1", "ts": ts, "session_id": "", "junior_msg": "alpha"}
    e2 = {"entry_id": "bbb2", "ts": ts, "session_id": "", "junior_msg": "beta"}
    (tmp_path / "audit_log.jsonl").write_text(
        json.dumps(e1) + "\n" + json.dumps(e2) + "\n"
    )

    rows = audit.read_all_entries()
    assert len(rows) == 2


def test_audit_dedup_falls_back_for_legacy_rows_without_entry_id(tmp_path, monkeypatch):
    """Pre-v1.6.6 rows have no entry_id. The fallback composite key still
    catches obvious duplicates of the same row in both files."""
    monkeypatch.setattr(storage, "PAID_DIR", tmp_path)
    import json

    legacy_row = {
        "ts": "2026-01-01T00:00:00+00:00",
        "session_id": "s",
        "counterparty": "cp_x",
        "junior_msg": "same message",
    }
    (tmp_path / "audit_log.jsonl").write_text(json.dumps(legacy_row) + "\n")
    cp_dir = tmp_path / "counterparties" / "cp_x"
    cp_dir.mkdir(parents=True)
    (cp_dir / "audit.jsonl").write_text(json.dumps(legacy_row) + "\n")

    rows = audit.read_all_entries()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# N9 — conv_capture._PENDING TTL prune
# ---------------------------------------------------------------------------


def test_pending_expires_after_ttl(monkeypatch):
    conv_capture.clear_pending_for_tests()

    # Insert with stale timestamp (2 hours ago > 1 hour TTL)
    conv_capture._PENDING[("feishu", "ou_old")] = (
        [{"field": "voice.tone", "proposed": "x", "rationale": "r"}],
        time.time() - 2 * 3600,
    )
    # Insert a fresh one
    conv_capture.store_pending("feishu", "ou_new", [{"x": 1}])

    assert not conv_capture.has_pending("feishu", "ou_old")
    assert conv_capture.has_pending("feishu", "ou_new")


def test_pending_pop_returns_proposals_not_tuple():
    """API contract: pop_pending returns just the list of proposals."""
    conv_capture.clear_pending_for_tests()
    conv_capture.store_pending("feishu", "ou_x", [{"field": "voice.tone"}])
    result = conv_capture.pop_pending("feishu", "ou_x")
    assert isinstance(result, list)
    assert result == [{"field": "voice.tone"}]


def test_pending_empty_when_never_set():
    conv_capture.clear_pending_for_tests()
    assert conv_capture.pop_pending("feishu", "ou_unknown") == []
    assert not conv_capture.has_pending("feishu", "ou_unknown")
