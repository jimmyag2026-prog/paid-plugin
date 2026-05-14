"""Tests for paid.setup_wizard — /paid-setup interactive flow (v1.6.0 sprint 3)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


from paid import setup_wizard as w
from paid import profile as p
from paid import storage


@pytest.fixture(autouse=True)
def fresh_state(tmp_path, monkeypatch):
    """Each test starts with empty wizard state + isolated PAID_DIR."""
    monkeypatch.setattr(storage, "PAID_DIR", tmp_path)
    w._clear_for_tests()
    yield
    w._clear_for_tests()


# ---------------------------------------------------------------------------
# is_active / start (first-time)
# ---------------------------------------------------------------------------


def test_is_active_false_when_nothing_started():
    assert w.is_active("feishu", "ou_x") is False


def test_start_first_time_returns_q1():
    out = w.start("feishu", "ou_x")
    assert "1/5" in out
    assert "名字" in out


def test_start_marks_owner_active():
    w.start("feishu", "ou_x")
    assert w.is_active("feishu", "ou_x") is True


def test_start_other_owner_does_not_activate_first():
    w.start("feishu", "ou_x")
    assert w.is_active("feishu", "ou_y") is False
    assert w.is_active("telegram", "ou_x") is False  # diff platform


# ---------------------------------------------------------------------------
# Full first-time 5Q happy path
# ---------------------------------------------------------------------------


def test_first_time_complete_flow():
    w.start("feishu", "ou_jimmy")

    # Q1: name
    reply, done = w.consume("feishu", "ou_jimmy", "Jimmy Yin")
    assert not done
    assert "2/5" in reply

    # Q2: voice_preset (numeric pick "1" = founder)
    reply, done = w.consume("feishu", "ou_jimmy", "1")
    assert not done
    assert "3/5" in reply

    # Q3: always_escalate — custom list
    reply, done = w.consume("feishu", "ou_jimmy", "薪资, 招聘, 客户")
    assert not done
    assert "4/5" in reply

    # Q4: preferred_language
    reply, done = w.consume("feishu", "ou_jimmy", "zh")
    assert not done
    assert "5/5" in reply

    # Q5: daily_cost_cap_usd
    reply, done = w.consume("feishu", "ou_jimmy", "10")
    assert done
    assert "PAID setup 完成" in reply
    assert "Jimmy Yin" in reply

    # Profile persisted
    prof = p.load_profile()
    assert prof.name == "Jimmy Yin"
    assert prof.voice.tone == "direct-friendly"  # founder preset
    assert prof.topics.always_escalate == ["薪资", "招聘", "客户"]
    assert prof.preferred_language == "zh"
    assert prof.preferences.daily_cost_cap_usd == 10.0

    # Wizard cleared
    assert not w.is_active("feishu", "ou_jimmy")


def test_first_time_default_escalate():
    """`default` keyword keeps v1.4.x baseline 5 topics."""
    w.start("feishu", "ou_x")
    w.consume("feishu", "ou_x", "X")
    w.consume("feishu", "ou_x", "professional")
    w.consume("feishu", "ou_x", "default")
    w.consume("feishu", "ou_x", "auto")
    w.consume("feishu", "ou_x", "5")

    prof = p.load_profile()
    assert "salary" in prof.topics.always_escalate
    assert "equity" in prof.topics.always_escalate


def test_first_time_inf_cost_cap():
    """`inf` sets effectively-unlimited cap."""
    w.start("feishu", "ou_x")
    for ans in ["X", "minimal", "default", "auto", "inf"]:
        w.consume("feishu", "ou_x", ans)

    prof = p.load_profile()
    assert prof.preferences.daily_cost_cap_usd > 1000


# ---------------------------------------------------------------------------
# Cancel paths
# ---------------------------------------------------------------------------


def test_cancel_mid_wizard():
    w.start("feishu", "ou_x")
    w.consume("feishu", "ou_x", "Jimmy")

    reply, done = w.consume("feishu", "ou_x", "/paid-setup cancel")
    assert done
    assert "取消" in reply
    assert not w.is_active("feishu", "ou_x")
    # No profile written
    assert p.load_profile() is None


def test_cancel_keyword_variants():
    """'cancel' / '退出' / '/cancel' all should work."""
    for variant in ["cancel", "退出", "/cancel"]:
        w._clear_for_tests()
        w.start("feishu", "ou_x")
        _, done = w.consume("feishu", "ou_x", variant)
        assert done, f"variant {variant!r} should cancel"


def test_cancel_when_no_wizard_active():
    reply, done = w.consume("feishu", "ou_no_wizard", "Jimmy")
    assert done
    assert "no active wizard" in reply.lower() or "no active" in reply.lower()


# ---------------------------------------------------------------------------
# Validation / re-prompt
# ---------------------------------------------------------------------------


def test_invalid_voice_preset_reprompts():
    w.start("feishu", "ou_x")
    w.consume("feishu", "ou_x", "Jimmy")  # Q1 ok
    reply, done = w.consume("feishu", "ou_x", "weird-tone")
    assert not done
    assert "无法识别" in reply or "ERR" not in reply  # error text + re-prompt of Q2
    assert "2/5" in reply


def test_invalid_cost_reprompts():
    w.start("feishu", "ou_x")
    for ans in ["Jimmy", "1", "default", "zh"]:
        w.consume("feishu", "ou_x", ans)
    reply, done = w.consume("feishu", "ou_x", "five dollars")
    assert not done
    assert "5/5" in reply  # re-asked


def test_negative_cost_reprompts():
    w.start("feishu", "ou_x")
    for ans in ["Jimmy", "1", "default", "zh"]:
        w.consume("feishu", "ou_x", ans)
    reply, done = w.consume("feishu", "ou_x", "-3")
    assert not done
    assert "5/5" in reply


def test_empty_name_reprompts():
    w.start("feishu", "ou_x")
    reply, done = w.consume("feishu", "ou_x", "   ")
    assert not done
    assert "1/5" in reply


# ---------------------------------------------------------------------------
# Edit mode
# ---------------------------------------------------------------------------


def test_start_with_existing_profile_enters_edit_mode():
    # Seed an existing profile first
    prof = p.new_profile("jimmy", name="Existing")
    p.save_profile(prof)

    out = w.start("feishu", "ou_jimmy")
    # Should show edit menu, not Q1
    assert "1/5" not in out
    assert "改哪个" in out or "改哪" in out
    assert "Existing" in out


def test_edit_mode_change_name():
    prof = p.new_profile("jimmy", name="Old")
    p.save_profile(prof)

    w.start("feishu", "ou_jimmy")
    reply, done = w.consume("feishu", "ou_jimmy", "1")  # pick "name"
    assert not done
    assert "名字" in reply  # question about name

    reply, done = w.consume("feishu", "ou_jimmy", "New Name")
    assert not done
    assert "已更新" in reply

    updated = p.load_profile()
    assert updated.name == "New Name"


def test_edit_mode_change_voice():
    prof = p.new_profile("jimmy", name="X")
    p.save_profile(prof)

    w.start("feishu", "ou_jimmy")
    w.consume("feishu", "ou_jimmy", "2")  # voice_preset
    w.consume("feishu", "ou_jimmy", "minimal")

    updated = p.load_profile()
    assert updated.voice.tone == "minimal"


def test_edit_mode_change_cost():
    prof = p.new_profile("jimmy", name="X")
    prof.preferences.daily_cost_cap_usd = 5.0
    p.save_profile(prof)

    w.start("feishu", "ou_jimmy")
    w.consume("feishu", "ou_jimmy", "5")  # cost
    w.consume("feishu", "ou_jimmy", "20")

    updated = p.load_profile()
    assert updated.preferences.daily_cost_cap_usd == 20.0


def test_edit_mode_done_exits():
    prof = p.new_profile("jimmy", name="X")
    p.save_profile(prof)

    w.start("feishu", "ou_jimmy")
    reply, done = w.consume("feishu", "ou_jimmy", "6")  # done/save
    assert done
    assert "完成" in reply or "PAID setup" in reply


def test_edit_mode_invalid_choice_reprompts():
    prof = p.new_profile("jimmy", name="X")
    p.save_profile(prof)

    w.start("feishu", "ou_jimmy")
    reply, done = w.consume("feishu", "ou_jimmy", "9")
    assert not done
    assert "1-6" in reply


# ---------------------------------------------------------------------------
# Resync
# ---------------------------------------------------------------------------


def test_resync_with_no_profile():
    out = w.resync()
    assert "/paid-setup" in out  # suggests setup


def test_resync_regenerates_derived_files(tmp_path):
    """resync() should rewrite owner.json / persona.md / sop.md / settings.json
    from existing profile."""
    prof = p.new_profile("jimmy", name="Test")
    prof.identities = [{"platform": "feishu", "user_id": "ou_x", "enabled": True}]
    p.save_profile(prof)

    # Delete derived files to verify resync recreates them
    for f in ("owner.json", "persona.md", "sop.md", "settings.json"):
        path = storage.PAID_DIR / f
        if path.exists():
            path.unlink()

    out = w.resync()
    assert "重新生成" in out
    for f in ("owner.json", "persona.md", "sop.md", "settings.json"):
        assert (storage.PAID_DIR / f).exists()


# ---------------------------------------------------------------------------
# TTL prune
# ---------------------------------------------------------------------------


def test_ttl_expires_stale_wizard(monkeypatch):
    w.start("feishu", "ou_stale")
    # Force the entry to look ancient
    state = w._WIZARD_STATE[("feishu", "ou_stale")]
    state.since_ts = 0  # very old
    assert w.is_active("feishu", "ou_stale") is False  # prune-on-check kicks in


# ---------------------------------------------------------------------------
# Multi-owner isolation
# ---------------------------------------------------------------------------


def test_two_owners_isolated():
    w.start("feishu", "ou_a")
    w.start("feishu", "ou_b")
    w.consume("feishu", "ou_a", "Alice")
    w.consume("feishu", "ou_b", "Bob")

    # Both still active, on Q2
    assert w.is_active("feishu", "ou_a")
    assert w.is_active("feishu", "ou_b")
    state_a = w._WIZARD_STATE[("feishu", "ou_a")]
    state_b = w._WIZARD_STATE[("feishu", "ou_b")]
    assert state_a.answers["name"] == "Alice"
    assert state_b.answers["name"] == "Bob"
