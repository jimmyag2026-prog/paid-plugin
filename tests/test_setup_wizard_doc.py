"""Tests for v1.6.1 doc ingest extension of setup_wizard."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from paid import setup_wizard as w
from paid import profile as p
from paid import doc_ingest as di
from paid import storage


@pytest.fixture(autouse=True)
def fresh_state(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "PAID_DIR", tmp_path)
    w._clear_for_tests()
    yield
    w._clear_for_tests()


# ---------------------------------------------------------------------------
# start_doc_ingest
# ---------------------------------------------------------------------------


def test_start_doc_ingest_no_profile():
    out = w.start_doc_ingest("feishu", "ou_x", "https://example.com/doc")
    assert "/paid-setup" in out


def test_start_doc_ingest_fetch_error(monkeypatch):
    prof = p.new_profile("jimmy", name="Jimmy")
    p.save_profile(prof)

    monkeypatch.setattr(di, "fetch_content", lambda url: (_ for _ in ()).throw(ValueError("404")))
    out = w.start_doc_ingest("feishu", "ou_x", "https://bad.com/doc")
    assert "无法获取" in out


def test_start_doc_ingest_no_proposals(monkeypatch):
    prof = p.new_profile("jimmy", name="Jimmy")
    p.save_profile(prof)

    monkeypatch.setattr(di, "fetch_content", lambda url: ("web", "some text"))
    monkeypatch.setattr(di, "extract_profile_updates", lambda content, prof: [])
    out = w.start_doc_ingest("feishu", "ou_x", "https://example.com/doc")
    assert "没有找到" in out
    # Reference should still be added to profile
    loaded = p.load_profile()
    assert any(r.get("url") == "https://example.com/doc" for r in loaded.references)


def test_start_doc_ingest_with_proposals(monkeypatch):
    prof = p.new_profile("jimmy", name="Jimmy")
    p.save_profile(prof)

    monkeypatch.setattr(di, "fetch_content", lambda url: ("web", "doc content"))
    proposals = [di.UpdateProposal("name", "Jimmy", "Jimmy Yin", "full name")]
    monkeypatch.setattr(di, "extract_profile_updates", lambda c, pr: proposals)

    out = w.start_doc_ingest("feishu", "ou_x", "https://example.com/doc")
    assert "1." in out  # confirm prompt
    assert w.is_doc_confirm_active("feishu", "ou_x")


# ---------------------------------------------------------------------------
# consume_doc_confirm
# ---------------------------------------------------------------------------


def test_consume_doc_confirm_no_state():
    msg, done = w.consume_doc_confirm("feishu", "ou_no_state", "all")
    assert done
    assert "没有待确认" in msg


def test_consume_doc_confirm_accept_all(monkeypatch):
    prof = p.new_profile("jimmy", name="Jimmy")
    p.save_profile(prof)

    from paid import profile_sync
    monkeypatch.setattr(profile_sync, "derive_from_profile", lambda pr: {"wrote": []})

    # Seed doc_confirm state manually
    monkeypatch.setattr(di, "fetch_content", lambda url: ("web", "content"))
    proposals = [di.UpdateProposal("name", "Jimmy", "Jimmy Yin", "test")]
    monkeypatch.setattr(di, "extract_profile_updates", lambda c, pr: proposals)
    w.start_doc_ingest("feishu", "ou_x", "https://example.com/doc")

    msg, done = w.consume_doc_confirm("feishu", "ou_x", "all")
    assert done
    assert "1/1" in msg
    # Wizard state cleared
    assert not w.is_doc_confirm_active("feishu", "ou_x")
    # Profile updated
    updated = p.load_profile()
    assert updated.name == "Jimmy Yin"


def test_consume_doc_confirm_reject_all(monkeypatch):
    prof = p.new_profile("jimmy", name="Jimmy")
    p.save_profile(prof)

    from paid import profile_sync
    monkeypatch.setattr(profile_sync, "derive_from_profile", lambda pr: {"wrote": []})

    monkeypatch.setattr(di, "fetch_content", lambda url: ("web", "content"))
    proposals = [di.UpdateProposal("name", "Jimmy", "Bob", "test")]
    monkeypatch.setattr(di, "extract_profile_updates", lambda c, pr: proposals)
    w.start_doc_ingest("feishu", "ou_x", "https://example.com/doc")

    msg, done = w.consume_doc_confirm("feishu", "ou_x", "none")
    assert done
    assert "0/1" in msg
    # Name unchanged
    updated = p.load_profile()
    assert updated.name == "Jimmy"


def test_is_doc_confirm_active_inactive_by_default():
    assert not w.is_doc_confirm_active("feishu", "ou_x")


def test_doc_confirm_does_not_interfere_with_wizard(monkeypatch):
    """A normal wizard session and a doc confirm session are independent."""
    prof = p.new_profile("jimmy", name="Jimmy")
    p.save_profile(prof)

    monkeypatch.setattr(di, "fetch_content", lambda url: ("web", "content"))
    proposals = [di.UpdateProposal("name", "Jimmy", "New", "test")]
    monkeypatch.setattr(di, "extract_profile_updates", lambda c, pr: proposals)
    # Start doc confirm for owner A
    w.start_doc_ingest("feishu", "ou_a", "https://example.com/doc")

    # Start wizard for owner B (unrelated)
    w.start("feishu", "ou_b")

    assert w.is_doc_confirm_active("feishu", "ou_a")
    assert w.is_active("feishu", "ou_b")
    assert not w.is_doc_confirm_active("feishu", "ou_b")
    # doc_confirm state also appears in is_active (same registry)
    assert w.is_active("feishu", "ou_a")
