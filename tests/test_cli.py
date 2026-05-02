"""CLI smoke tests for paid setup / add-counterparty / status / pending."""

from __future__ import annotations

import json

import pytest

from paid import approval, cli, storage


def _run(argv, capsys):
    rc = cli.main(argv)
    out = capsys.readouterr().out
    return rc, out


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------


def test_setup_creates_all_template_files(paid_tmp, capsys):
    rc, out = _run(
        ["setup", "--owner-id", "owner_jimmy", "--name", "Jimmy",
         "--identity", "telegram:8540"], capsys
    )
    assert rc == 0
    assert (paid_tmp / "owner.json").exists()
    assert (paid_tmp / "persona.md").exists()
    assert (paid_tmp / "sop.md").exists()
    assert (paid_tmp / "settings.json").exists()
    owner = json.loads((paid_tmp / "owner.json").read_text())
    assert owner["name"] == "Jimmy"
    assert owner["identities"] == [{"platform": "telegram", "user_id": "8540"}]


def test_setup_is_idempotent_without_force(paid_tmp, capsys):
    _run(["setup", "--owner-id", "owner_jimmy", "--name", "Jimmy"], capsys)
    (paid_tmp / "persona.md").write_text("# my edits\n")
    rc, _ = _run(["setup", "--owner-id", "owner_jimmy", "--name", "Jimmy"], capsys)
    assert rc == 0
    assert (paid_tmp / "persona.md").read_text() == "# my edits\n"


def test_setup_force_overwrites(paid_tmp, capsys):
    _run(["setup", "--owner-id", "owner_jimmy", "--name", "Jimmy"], capsys)
    (paid_tmp / "persona.md").write_text("# stale\n")
    rc, _ = _run(["setup", "--owner-id", "owner_jimmy", "--name", "Jimmy", "--force"], capsys)
    assert rc == 0
    assert "Jimmy" in (paid_tmp / "persona.md").read_text()


def test_setup_rejects_malformed_identity(paid_tmp, capsys):
    rc, _ = _run(["setup", "--name", "Jimmy", "--identity", "no-colon-here"], capsys)
    assert rc == 2


# ---------------------------------------------------------------------------
# add-counterparty
# ---------------------------------------------------------------------------


def test_add_counterparty_writes_profile(paid_tmp, capsys):
    rc, out = _run(
        ["add-counterparty", "telegram", "12345",
         "--name", "Alice",
         "--role", "junior",
         "--topic-allow", "scheduling",
         "--topic-allow", "logistics"],
        capsys,
    )
    assert rc == 0
    prof_path = paid_tmp / "counterparties" / "telegram_12345" / "profile.json"
    assert prof_path.exists()
    prof = json.loads(prof_path.read_text())
    assert prof["display_name"] == "Alice"
    assert prof["role"] == "junior"
    assert "scheduling" in prof["topics_allowed"]


def test_add_counterparty_idempotent_keeps_existing_role(paid_tmp, capsys):
    _run(["add-counterparty", "telegram", "12345", "--name", "Alice", "--role", "junior"], capsys)
    rc, _ = _run(
        ["add-counterparty", "telegram", "12345", "--role", "external", "--notes", "VIP"],
        capsys,
    )
    assert rc == 0
    prof = json.loads(
        (paid_tmp / "counterparties" / "telegram_12345" / "profile.json").read_text()
    )
    assert prof["role"] == "external"
    assert prof["notes"] == "VIP"
    # display_name preserved from first call (since we didn't pass --name on second)
    assert prof["display_name"] == "Alice"


# ---------------------------------------------------------------------------
# status / pending
# ---------------------------------------------------------------------------


def test_status_no_owner(paid_tmp, capsys):
    rc, out = _run(["status"], capsys)
    assert rc == 0
    assert "NOT CONFIGURED" in out


def test_status_after_setup_and_pending(paid_tmp, capsys):
    _run(["setup", "--name", "Jimmy", "--identity", "telegram:8540"], capsys)
    _run(["add-counterparty", "telegram", "111", "--name", "Alice"], capsys)
    approval.create(
        counterparty_id="telegram_111",
        counterparty_platform="telegram",
        counterparty_user_id="111",
        counterparty_display="Alice",
        junior_session_id="s",
        junior_question="?",
        draft_answer="",
        topic="x",
        stakes="low",
        confidence=0.5,
    )
    rc, out = _run(["status"], capsys)
    assert rc == 0
    assert "Jimmy" in out
    assert "counterparties: 1" in out
    assert "pending approvals: 1" in out


def test_pending_lists_open_requests(paid_tmp, capsys):
    approval.create(
        counterparty_id="telegram_111",
        counterparty_platform="telegram",
        counterparty_user_id="111",
        counterparty_display="Alice",
        junior_session_id="s",
        junior_question="What's friday like?",
        draft_answer="busy",
        topic="scheduling",
        stakes="low",
        confidence=0.5,
    )
    rc, out = _run(["pending"], capsys)
    assert rc == 0
    assert "Alice" in out
    assert "scheduling" in out
