"""Tests for scripts/doctor.py (Sprint E health check)."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_DOCTOR_PATH = Path(__file__).resolve().parent.parent / "scripts" / "doctor.py"
_spec = importlib.util.spec_from_file_location("paid_review_doctor", _DOCTOR_PATH)
doctor = importlib.util.module_from_spec(_spec)
sys.modules["paid_review_doctor"] = doctor  # dataclass needs this in sys.modules
_spec.loader.exec_module(doctor)


# --------------------------------------------------------------------------
# check_paid_dir_layout
# --------------------------------------------------------------------------


def test_layout_paid_dir_missing(tmp_path, monkeypatch):
    from paid import storage
    nonexistent = tmp_path / "ghost"
    monkeypatch.setattr(storage, "PAID_DIR", nonexistent)
    out = doctor.check_paid_dir_layout()
    assert any(c.severity == "error" and "PAID_DIR" in c.label for c in out)


def test_layout_owner_missing_warns(paid_tmp, monkeypatch):
    out = doctor.check_paid_dir_layout()
    sevs = [c.severity for c in out]
    assert "ok" in sevs  # PAID_DIR exists
    assert any(c.severity == "warn" and "owner" in c.label for c in out)


def test_layout_review_dir_present(paid_tmp, monkeypatch):
    (paid_tmp / "owner.json").write_text("{}", encoding="utf-8")
    (paid_tmp / "review" / "sessions").mkdir(parents=True)
    out = doctor.check_paid_dir_layout()
    labels = " ".join(c.label for c in out)
    assert "review/" in labels
    # No errors expected
    assert not any(c.severity == "error" for c in out)


# --------------------------------------------------------------------------
# check_prompts
# --------------------------------------------------------------------------


def test_prompts_all_present():
    out = doctor.check_prompts()
    assert all(c.severity == "ok" for c in out), \
        [c for c in out if c.severity != "ok"]


# --------------------------------------------------------------------------
# check_active_sessions
# --------------------------------------------------------------------------


def test_active_sessions_no_sessions_dir(paid_tmp, monkeypatch):
    out = doctor.check_active_sessions()
    assert any("no active sessions" in c.label.lower() for c in out)


def test_active_sessions_legal(paid_tmp, monkeypatch):
    sid_dir = paid_tmp / "review" / "sessions" / "good_sid"
    sid_dir.mkdir(parents=True)
    (sid_dir / "meta.json").write_text(
        json.dumps({"sid": "good_sid", "stage": "QA"}), encoding="utf-8"
    )
    out = doctor.check_active_sessions()
    assert not any(c.severity == "error" for c in out)


def test_active_sessions_illegal_stage_errors(paid_tmp, monkeypatch):
    sid_dir = paid_tmp / "review" / "sessions" / "bad_sid"
    sid_dir.mkdir(parents=True)
    (sid_dir / "meta.json").write_text(
        json.dumps({"sid": "bad_sid", "stage": "WTF"}), encoding="utf-8"
    )
    out = doctor.check_active_sessions()
    assert any(c.severity == "error" and "WTF" in c.detail + c.label
               for c in out)


def test_active_sessions_orphan_pointer(paid_tmp, monkeypatch):
    """cp.profile.json says active_review_session=phantom_sid but no session exists."""
    cps = paid_tmp / "counterparties" / "feishu_jr"
    cps.mkdir(parents=True)
    (cps / "profile.json").write_text(
        json.dumps({"cp_id": "feishu_jr", "active_review_session": "phantom"}),
        encoding="utf-8",
    )
    (paid_tmp / "review" / "sessions").mkdir(parents=True)
    out = doctor.check_active_sessions()
    # Warning message places phantom sid in label (not detail)
    assert any(c.severity == "warn" and "phantom" in (c.label + c.detail)
               for c in out)


def test_active_sessions_unreadable_meta_errors(paid_tmp, monkeypatch):
    sid_dir = paid_tmp / "review" / "sessions" / "broken_sid"
    sid_dir.mkdir(parents=True)
    (sid_dir / "meta.json").write_text("{not json", encoding="utf-8")
    out = doctor.check_active_sessions()
    assert any(c.severity == "error" and "broken_sid" in c.label for c in out)


# --------------------------------------------------------------------------
# check_cron_entry
# --------------------------------------------------------------------------


def test_cron_missing_warns(tmp_path):
    out = doctor.check_cron_entry(tmp_path / "absent")
    assert out[0].severity == "warn"


def test_cron_present_with_sweep_ref_ok(tmp_path):
    f = tmp_path / "paid-review-sweep"
    f.write_text(
        "0 * * * * paid python3 /opt/paid-plugin/bin/sweep_review_sessions.py\n",
        encoding="utf-8",
    )
    out = doctor.check_cron_entry(f)
    assert out[0].severity == "ok"


def test_cron_present_without_sweep_ref_warns(tmp_path):
    f = tmp_path / "paid-review-sweep"
    f.write_text("0 * * * * paid /usr/bin/true\n", encoding="utf-8")
    out = doctor.check_cron_entry(f)
    assert out[0].severity == "warn"


# --------------------------------------------------------------------------
# check_sweep_log_freshness
# --------------------------------------------------------------------------


def test_sweep_log_missing_warns(tmp_path):
    out = doctor.check_sweep_log_freshness(tmp_path / "ghost.log")
    assert out[0].severity == "warn"


def test_sweep_log_fresh_ok(tmp_path):
    log = tmp_path / "sweep.log"
    log.write_text("recent\n", encoding="utf-8")
    out = doctor.check_sweep_log_freshness(log, ttl_hours=24)
    assert out[0].severity == "ok"


def test_sweep_log_stale_errors(tmp_path):
    import os
    log = tmp_path / "sweep.log"
    log.write_text("ancient\n", encoding="utf-8")
    # Backdate by 100h (> 2*24)
    old = (datetime.now(timezone.utc) - timedelta(hours=100)).timestamp()
    os.utime(log, (old, old))
    out = doctor.check_sweep_log_freshness(log, ttl_hours=24)
    assert out[0].severity == "error"


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def test_main_returns_zero_on_clean_install(paid_tmp, monkeypatch, capsys):
    """No errors, only OKs and WARNs → exit 0."""
    (paid_tmp / "owner.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sys, "argv",
                        ["doctor.py", "--cron-path", "/nonexistent",
                         "--sweep-log", "/nonexistent"])
    rc = doctor.main()
    assert rc == 0


def test_main_returns_one_when_errors(paid_tmp, monkeypatch, capsys):
    """Illegal stage → at least 1 error → exit 1."""
    sid_dir = paid_tmp / "review" / "sessions" / "bad"
    sid_dir.mkdir(parents=True)
    (sid_dir / "meta.json").write_text(
        json.dumps({"sid": "bad", "stage": "BAD_STAGE"}), encoding="utf-8"
    )
    monkeypatch.setattr(sys, "argv",
                        ["doctor.py", "--cron-path", "/nonexistent",
                         "--sweep-log", "/nonexistent"])
    rc = doctor.main()
    assert rc == 1


def test_main_json_output(paid_tmp, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv",
                        ["doctor.py", "--json",
                         "--cron-path", "/nonexistent",
                         "--sweep-log", "/nonexistent"])
    doctor.main()
    out = capsys.readouterr().out.strip()
    data = json.loads(out)
    assert isinstance(data, list)
    assert all("severity" in d for d in data)
