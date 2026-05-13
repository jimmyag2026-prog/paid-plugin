"""Tests for bin/sweep_review_sessions.py (Sprint E TTL cron)."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Load the bin script as a module
_SWEEP_PATH = Path(__file__).resolve().parent.parent / "bin" / "sweep_review_sessions.py"
_spec = importlib.util.spec_from_file_location("paid_review_sweep", _SWEEP_PATH)
sweep_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sweep_mod)


def _make_cp(paid_tmp, monkeypatch, sid: str = ""):
    from paid import identity, storage
    monkeypatch.setattr(storage, "PAID_DIR", paid_tmp)
    cp = identity.Counterparty(
        cp_id=f"feishu_jr_{sid or 'x'}",
        platform="feishu",
        user_id=f"jr_{sid or 'x'}",
        display_name="Junior",
        role="junior",
        topics_allowed=[],
        topics_always_escalate=[],
        web_search_allowed=False,
        notes="",
        active_review_session=sid,
    )
    identity.save_counterparty(cp)
    return cp


def _seed_session(paid_tmp: Path, sid: str, *, stage: str = "QA",
                  last_inbound_iso: str = "", cp_id: str = "feishu_jr_x"):
    from paid_review.core import state as state_mod
    sid_dir = paid_tmp / "review" / "sessions" / sid
    sid_dir.mkdir(parents=True, exist_ok=True)
    st = state_mod.SessionState(
        sid=sid,
        cp_id=cp_id,
        owner_id="owner",
        platform="feishu",
        stage=stage,  # type: ignore[arg-type]
        verdict="PENDING",
        max_rounds=3,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        last_inbound_at=last_inbound_iso,
    )
    state_mod.save_state(st)
    return sid_dir


# --------------------------------------------------------------------------
# TTL parsing
# --------------------------------------------------------------------------


def test_ttl_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("PAID_REVIEW_SESSION_TTL_HOURS", raising=False)
    assert sweep_mod._ttl_hours_from_env() == 24


def test_ttl_clamped_to_ceiling(monkeypatch):
    monkeypatch.setenv("PAID_REVIEW_SESSION_TTL_HOURS", "999")
    assert sweep_mod._ttl_hours_from_env() == 72


def test_ttl_clamped_to_floor(monkeypatch):
    monkeypatch.setenv("PAID_REVIEW_SESSION_TTL_HOURS", "0")
    assert sweep_mod._ttl_hours_from_env() == 1


def test_ttl_garbage_falls_back(monkeypatch):
    monkeypatch.setenv("PAID_REVIEW_SESSION_TTL_HOURS", "abc")
    assert sweep_mod._ttl_hours_from_env() == 24


# --------------------------------------------------------------------------
# parse_iso
# --------------------------------------------------------------------------


def test_parse_iso_handles_z_suffix():
    dt = sweep_mod._parse_iso("2026-05-01T12:00:00Z")
    assert dt is not None and dt.tzinfo is not None


def test_parse_iso_garbage_returns_none():
    assert sweep_mod._parse_iso("") is None
    assert sweep_mod._parse_iso("not iso") is None


# --------------------------------------------------------------------------
# sweep — happy paths
# --------------------------------------------------------------------------


def test_sweep_no_sessions_dir_is_noop(paid_tmp, monkeypatch):
    from paid import storage
    monkeypatch.setattr(storage, "PAID_DIR", paid_tmp)
    summary = sweep_mod.sweep(now=datetime.now(timezone.utc), ttl_hours=24)
    assert summary["scanned"] == 0
    assert summary["expired"] == 0


def test_sweep_skips_recent_session(paid_tmp, monkeypatch):
    cp = _make_cp(paid_tmp, monkeypatch, sid="sid_recent")
    now = datetime.now(timezone.utc)
    _seed_session(paid_tmp, "sid_recent",
                  last_inbound_iso=(now - timedelta(hours=1)).isoformat(),
                  cp_id=cp.cp_id)
    summary = sweep_mod.sweep(now=now, ttl_hours=24)
    assert summary["scanned"] == 1
    assert summary["expired"] == 0
    # cp.active_review_session still set
    from paid import identity
    fresh = identity.load_counterparty("feishu", "jr_sid_recent")
    assert fresh.active_review_session == "sid_recent"


def test_sweep_force_closes_expired(paid_tmp, monkeypatch):
    cp = _make_cp(paid_tmp, monkeypatch, sid="sid_old")
    now = datetime.now(timezone.utc)
    _seed_session(paid_tmp, "sid_old",
                  last_inbound_iso=(now - timedelta(hours=48)).isoformat(),
                  cp_id=cp.cp_id)
    summary = sweep_mod.sweep(now=now, ttl_hours=24)
    assert summary["scanned"] == 1
    assert summary["expired"] == 1
    assert "sid_old" in summary["closed_sids"]
    # Session moved to CLOSED
    from paid_review.core import state as state_mod
    st = state_mod.load_state("sid_old")
    assert st is not None
    assert st.stage == "CLOSED"
    # cp.active_review_session cleared
    from paid import identity
    fresh = identity.load_counterparty("feishu", "jr_sid_old")
    assert fresh.active_review_session == ""


def test_sweep_skips_already_closed(paid_tmp, monkeypatch):
    _make_cp(paid_tmp, monkeypatch, sid="sid_done")
    now = datetime.now(timezone.utc)
    _seed_session(paid_tmp, "sid_done", stage="CLOSED",
                  last_inbound_iso=(now - timedelta(hours=72)).isoformat())
    summary = sweep_mod.sweep(now=now, ttl_hours=24)
    # Counts: scanned still increments (it inspects), but stage=CLOSED → no force_close
    assert summary["expired"] == 0


def test_sweep_skips_underscore_dirs(paid_tmp, monkeypatch):
    """sessions/_closed/* must not be scanned for TTL."""
    from paid import storage
    monkeypatch.setattr(storage, "PAID_DIR", paid_tmp)
    closed_dir = paid_tmp / "review" / "sessions" / "_closed" / "2025-12" / "old_sid"
    closed_dir.mkdir(parents=True)
    (closed_dir / "meta.json").write_text("{}", encoding="utf-8")
    summary = sweep_mod.sweep(now=datetime.now(timezone.utc), ttl_hours=24)
    assert summary["scanned"] == 0


def test_sweep_handles_missing_meta(paid_tmp, monkeypatch):
    """An sid dir with no meta.json — skip silently."""
    from paid import storage
    monkeypatch.setattr(storage, "PAID_DIR", paid_tmp)
    (paid_tmp / "review" / "sessions" / "naked_sid").mkdir(parents=True)
    summary = sweep_mod.sweep(now=datetime.now(timezone.utc), ttl_hours=24)
    assert summary["scanned"] == 0


def test_sweep_uses_updated_at_when_no_last_inbound(paid_tmp, monkeypatch):
    """SessionState created at intake but no inbound yet — fall back to
    updated_at so brand-new sessions still age out eventually."""
    cp = _make_cp(paid_tmp, monkeypatch, sid="sid_fresh")
    sid_dir = _seed_session(paid_tmp, "sid_fresh", stage="INTAKE",
                            last_inbound_iso="", cp_id=cp.cp_id)
    # Manually rewrite updated_at to be old
    from paid_review.core import state as state_mod
    st = state_mod.load_state("sid_fresh")
    st.updated_at = (datetime.now(timezone.utc)
                     - timedelta(hours=48)).isoformat()
    state_mod.save_state(st)
    summary = sweep_mod.sweep(now=datetime.now(timezone.utc), ttl_hours=24)
    assert summary["expired"] == 1


def test_sweep_force_close_failure_does_not_abort(paid_tmp, monkeypatch):
    """If api.force_close raises on one sid, others still get processed."""
    _make_cp(paid_tmp, monkeypatch, sid="sid_boom")
    _make_cp(paid_tmp, monkeypatch, sid="sid_ok")
    now = datetime.now(timezone.utc)
    _seed_session(paid_tmp, "sid_boom",
                  last_inbound_iso=(now - timedelta(hours=48)).isoformat(),
                  cp_id="feishu_jr_sid_boom")
    _seed_session(paid_tmp, "sid_ok",
                  last_inbound_iso=(now - timedelta(hours=48)).isoformat(),
                  cp_id="feishu_jr_sid_ok")

    from paid_review import api as review_api
    real_force = review_api.force_close

    def flaky(sid, *, reason):
        if sid == "sid_boom":
            raise RuntimeError("LLM 500")
        return real_force(sid, reason=reason)

    monkeypatch.setattr(review_api, "force_close", flaky)
    summary = sweep_mod.sweep(now=now, ttl_hours=24)
    assert summary["scanned"] == 2
    assert summary["expired"] == 1
    assert summary["errors"] == 1
    assert "sid_ok" in summary["closed_sids"]
    assert "sid_boom" not in summary["closed_sids"]


def test_sweep_main_returns_zero_even_on_crash(paid_tmp, monkeypatch):
    """main() must always exit 0 — cron mustn't get spam."""
    from paid import storage
    monkeypatch.setattr(storage, "PAID_DIR", paid_tmp)

    def crashing_sweep(*a, **kw):
        raise RuntimeError("everything is on fire")
    monkeypatch.setattr(sweep_mod, "sweep", crashing_sweep)

    monkeypatch.setattr(sys, "argv", ["sweep_review_sessions.py", "--quiet"])
    rc = sweep_mod.main()
    assert rc == 0
