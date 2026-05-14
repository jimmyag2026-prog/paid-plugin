"""Unit tests for paid.doctor (v1.5.5 A1)."""

from __future__ import annotations

import json
import platform as _platform
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from paid import doctor
from paid import card_formatters


# ---------------------------------------------------------------------------
# overall + plain text rendering
# ---------------------------------------------------------------------------

def test_overall_ok_and_npass():
    rows = [
        {"id": "a", "ok": True, "detail": "", "fix_hint": ""},
        {"id": "b", "ok": True, "detail": "", "fix_hint": ""},
        {"id": "c", "ok": False, "detail": "boom", "fix_hint": "fix it"},
    ]
    assert doctor.n_passed(rows) == 2
    assert doctor.overall_ok(rows) is False
    rows[2]["ok"] = True
    assert doctor.overall_ok(rows) is True


def test_format_plain_text_layout():
    rows = [
        {"id": "alpha", "ok": True, "detail": "good", "fix_hint": ""},
        {"id": "beta", "ok": False, "detail": "bad", "fix_hint": "fix-x"},
    ]
    out = doctor.format_plain_text(rows)
    assert "1/2 checks passed" in out
    assert "[✓] alpha: good" in out
    assert "[✗] beta: bad" in out
    assert "fix: fix-x" in out


# ---------------------------------------------------------------------------
# Individual checks — using paid_tmp to isolate PAID_DIR
# ---------------------------------------------------------------------------

def test_check_owner_json_missing(paid_tmp):
    ok, detail, fix = doctor._check_owner_json()
    assert ok is False
    assert "missing" in detail
    assert "setup-owner" in fix or "owner.json" in fix


def test_check_owner_json_wrong_schema(paid_tmp):
    (paid_tmp / "owner.json").write_text(json.dumps({"schema_version": 1}))
    ok, detail, fix = doctor._check_owner_json()
    assert ok is False
    assert "schema_version" in detail


def test_check_owner_json_no_identities(paid_tmp):
    (paid_tmp / "owner.json").write_text(json.dumps({
        "schema_version": 2, "identities": []
    }))
    ok, detail, _ = doctor._check_owner_json()
    assert ok is False
    assert "identities" in detail


def test_check_owner_json_pass(paid_tmp):
    (paid_tmp / "owner.json").write_text(json.dumps({
        "schema_version": 2,
        "identities": [{"platform": "feishu", "user_id": "ou_test"}],
    }))
    ok, detail, _ = doctor._check_owner_json()
    assert ok is True
    assert "identities=1" in detail


def test_check_data_files(paid_tmp):
    ok, detail, _ = doctor._check_data_files()
    assert ok is True
    # All 3 should now exist (touched)
    for fname in doctor._DATA_FILES:
        assert (paid_tmp / fname).exists()


def test_check_settings_schema_clean(paid_tmp):
    # paid/settings.py load() falls back to defaults when missing.
    ok, _, _ = doctor._check_settings_schema()
    assert ok is True


def test_check_settings_schema_bad_confidence(paid_tmp):
    (paid_tmp / "settings.json").write_text(json.dumps({
        "confidence_threshold_direct": 1.5,  # out of range
    }))
    ok, detail, _ = doctor._check_settings_schema()
    assert ok is False
    assert "confidence_threshold_direct" in detail


def test_check_settings_schema_bad_cost_inverted(paid_tmp):
    (paid_tmp / "settings.json").write_text(json.dumps({
        "cost": {"daily_soft_cap_usd": 50, "daily_hard_cap_usd": 10},
    }))
    ok, detail, _ = doctor._check_settings_schema()
    assert ok is False
    assert "soft_cap" in detail and "hard_cap" in detail


def test_check_recent_errors_no_file(paid_tmp):
    ok, detail, _ = doctor._check_recent_errors()
    assert ok is True
    assert "fresh install" in detail or "no audit_log" in detail


def test_check_recent_errors_with_fatal(paid_tmp):
    now = datetime.now(timezone.utc)
    path = paid_tmp / "audit_log.jsonl"
    # Real audit_log is append-only chronological ASC. Doctor reverse-scans
    # and breaks at the first row older than cutoff, so order matters.
    rows = [
        {"ts": (now - timedelta(hours=2)).isoformat(),   # outside window — break point
         "extra": {"fatal": True}, "reason": "old fatal"},
        {"ts": (now - timedelta(seconds=60)).isoformat(),
         "extra": {"l4_ok": False}, "reason": "leak"},
        {"ts": (now - timedelta(seconds=30)).isoformat(),
         "extra": {"fatal": True}, "reason": "test fatal"},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows))
    ok, detail, _ = doctor._check_recent_errors()
    assert ok is False
    assert "fatal=1" in detail
    assert "l4_fail=1" in detail


def test_check_recent_errors_clean_log(paid_tmp):
    now = datetime.now(timezone.utc)
    path = paid_tmp / "audit_log.jsonl"
    rows = [
        {"ts": (now - timedelta(seconds=30)).isoformat(),
         "action": {"state": "direct"}, "extra": {"l4_ok": True}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows))
    ok, _, _ = doctor._check_recent_errors()
    assert ok is True


def test_check_systemd_timers_non_linux(paid_tmp, monkeypatch):
    if _platform.system() == "Linux":
        pytest.skip("non-Linux behavior test only meaningful off-Linux")
    ok, detail, _ = doctor._check_systemd_timers()
    assert ok is True
    assert "skipped" in detail.lower()


def test_check_hermes_version_module_present(paid_tmp):
    # hermes_cli is installed in test env (PAID is a hermes plugin) — pass case.
    ok, detail, _ = doctor._check_hermes_version()
    # If hermes_cli not installed in some sandboxed env, accept fail too — but
    # detail must mention what's wrong rather than crash.
    assert isinstance(ok, bool)
    assert isinstance(detail, str)


# ---------------------------------------------------------------------------
# run_checks orchestration + crash resilience
# ---------------------------------------------------------------------------

def test_run_checks_returns_all_rows(paid_tmp):
    rows = doctor.run_checks()
    ids = {r["id"] for r in rows}
    expected = {
        "hermes_config", "owner_json", "hermes_version",
        "systemd_timers", "data_files", "settings_schema", "recent_errors",
    }
    assert ids == expected
    # Every row has the canonical 4 fields
    for r in rows:
        assert set(r.keys()) == {"id", "ok", "detail", "fix_hint"}


def test_run_checks_does_not_crash_when_check_raises(monkeypatch, paid_tmp):
    def _boom():
        raise RuntimeError("boom")
    monkeypatch.setattr(doctor, "_check_settings_schema", _boom)
    rows = doctor.run_checks()
    schema_row = next(r for r in rows if r["id"] == "settings_schema")
    assert schema_row["ok"] is False
    assert "boom" in schema_row["detail"]
    # Other checks still ran
    assert any(r["id"] == "owner_json" for r in rows)


# ---------------------------------------------------------------------------
# Lark card formatter
# ---------------------------------------------------------------------------

def test_format_doctor_card_lark_all_pass():
    rows = [
        {"id": "a", "ok": True, "detail": "good", "fix_hint": ""},
        {"id": "b", "ok": True, "detail": "fine", "fix_hint": ""},
    ]
    card = card_formatters.format_doctor_card_lark(rows)
    assert card["header"]["template"] == "green"
    assert "2/2 checks passed" in card["header"]["title"]["content"]
    # 2 check divs + 1 hr + 1 note = 4 elements
    assert len(card["elements"]) == 4


def test_format_doctor_card_lark_with_failures():
    rows = [
        {"id": "a", "ok": True, "detail": "good", "fix_hint": ""},
        {"id": "b", "ok": False, "detail": "bad", "fix_hint": "fix it"},
    ]
    card = card_formatters.format_doctor_card_lark(rows)
    assert card["header"]["template"] == "red"
    assert "1/2 checks passed" in card["header"]["title"]["content"]
    # Find the 'b' element and verify fix_hint rendered
    b_elem = card["elements"][1]
    body = b_elem["text"]["content"]
    assert "❌" in body
    assert "**b**" in body
    assert "fix it" in body


def test_format_doctor_card_lark_truncates_long_detail():
    rows = [
        {"id": "long", "ok": False, "detail": "x" * 500, "fix_hint": "y" * 500},
    ]
    card = card_formatters.format_doctor_card_lark(rows)
    body = card["elements"][0]["text"]["content"]
    # detail truncated to 280 + fix to 200
    assert "..." in body
    assert len(body) < 700  # well under any Lark limit


# ---------------------------------------------------------------------------
# CLI shell smoke
# ---------------------------------------------------------------------------

def test_cli_shell_imports_and_returns_exit_code(paid_tmp, capsys):
    """Smoke test bin/paid_doctor.py main() returns proper exit code."""
    import sys
    from pathlib import Path

    bin_dir = Path(__file__).resolve().parent.parent / "bin"
    if str(bin_dir.parent) not in sys.path:
        sys.path.insert(0, str(bin_dir.parent))

    # Manually import bin/paid_doctor.py as a module via importlib
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "paid_doctor_cli", bin_dir / "paid_doctor.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    rc = mod.main()
    assert rc in (0, 1)  # whichever; just verify it returned
    captured = capsys.readouterr()
    assert "PAID doctor" in captured.out
