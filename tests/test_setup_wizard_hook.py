"""Hook-level integration tests for /paid-setup wizard (v1.6.0 sprint 3).

Validates that pre_gateway_dispatch routes /paid-setup + subsequent owner
DM replies to the wizard state machine, NOT to the LLM."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _fresh_plugin():
    spec = importlib.util.spec_from_file_location(
        "paid_v1_6_setup_test", _ROOT / "__init__.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_event(*, text, chat_id="oc_owner_dm", chat_type="p2p",
                platform="feishu", user_id="owner_lark"):
    plat = SimpleNamespace(value=platform)
    src = SimpleNamespace(
        platform=plat, user_id=user_id, chat_id=chat_id, chat_type=chat_type,
    )
    return SimpleNamespace(source=src, text=text)


def _mock_owner(monkeypatch, plugin, *, platform="feishu", uid="owner_lark"):
    monkeypatch.setattr(
        plugin.identity, "is_owner",
        lambda p, s: p == platform and s == uid,
    )
    monkeypatch.setattr(plugin.identity, "load_owner", lambda: None)
    monkeypatch.setattr(plugin.identity, "display_name", lambda o: "Jimmy")


def _silence_safe(monkeypatch, plugin):
    monkeypatch.setattr(
        plugin, "_ensure_telegram_callback_registered", lambda: None,
    )
    monkeypatch.setattr(
        plugin.safety, "detect_prompt_injection", lambda t: (False, []),
    )


@pytest.fixture
def paid_tmp_iso(tmp_path, monkeypatch):
    from paid import storage
    monkeypatch.setattr(storage, "PAID_DIR", tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def fresh_wizard_state():
    from paid import setup_wizard
    setup_wizard._clear_for_tests()
    yield
    setup_wizard._clear_for_tests()


# ---------------------------------------------------------------------------
# /paid-setup intercepts
# ---------------------------------------------------------------------------


def test_paid_setup_starts_wizard_returns_skip(paid_tmp_iso, monkeypatch):
    plugin = _fresh_plugin()
    _mock_owner(monkeypatch, plugin)
    _silence_safe(monkeypatch, plugin)
    sent: list[tuple] = []
    monkeypatch.setattr(
        plugin.hermes_io, "send_dm",
        lambda p, t, m, **kw: (sent.append((p, t, m)), {"ok": True})[1],
    )

    e = _make_event(text="/paid-setup", user_id="owner_lark")
    rv = plugin.on_pre_gateway_dispatch(event=e)
    assert rv == {"action": "skip", "reason": "paid_setup_started"}
    assert sent  # at least one send_dm fired
    assert "1/5" in sent[-1][2]


def test_paid_setup_cancel_clears(paid_tmp_iso, monkeypatch):
    plugin = _fresh_plugin()
    _mock_owner(monkeypatch, plugin)
    _silence_safe(monkeypatch, plugin)
    monkeypatch.setattr(
        plugin.hermes_io, "send_dm", lambda *a, **kw: {"ok": True},
    )

    # Start wizard, then cancel
    plugin.on_pre_gateway_dispatch(event=_make_event(text="/paid-setup"))
    e_cancel = _make_event(text="/paid-setup cancel")
    rv = plugin.on_pre_gateway_dispatch(event=e_cancel)
    assert rv == {"action": "skip", "reason": "paid_setup_cancelled"}

    from paid import setup_wizard
    assert not setup_wizard.is_active("feishu", "owner_lark")


def test_paid_setup_answer_captured_during_wizard(paid_tmp_iso, monkeypatch):
    """While wizard active, owner's plain-text reply gets captured as answer
    (not routed to LLM)."""
    plugin = _fresh_plugin()
    _mock_owner(monkeypatch, plugin)
    _silence_safe(monkeypatch, plugin)
    sent: list[tuple] = []
    monkeypatch.setattr(
        plugin.hermes_io, "send_dm",
        lambda p, t, m, **kw: (sent.append((p, t, m)), {"ok": True})[1],
    )

    # Start
    plugin.on_pre_gateway_dispatch(event=_make_event(text="/paid-setup"))
    sent.clear()
    # Q1 answer
    rv = plugin.on_pre_gateway_dispatch(event=_make_event(text="Jimmy"))
    assert rv == {"action": "skip", "reason": "paid_setup_step"}
    # Reply went to owner DM (Q2 prompt)
    assert sent
    assert "2/5" in sent[-1][2]


def test_paid_setup_full_flow_completes(paid_tmp_iso, monkeypatch):
    plugin = _fresh_plugin()
    _mock_owner(monkeypatch, plugin)
    _silence_safe(monkeypatch, plugin)
    monkeypatch.setattr(
        plugin.hermes_io, "send_dm", lambda *a, **kw: {"ok": True},
    )

    answers = ["/paid-setup", "Jimmy", "1", "default", "auto", "10"]
    final_rv = None
    for ans in answers:
        final_rv = plugin.on_pre_gateway_dispatch(event=_make_event(text=ans))
    # Last submit (answer to Q5) ends wizard
    assert final_rv == {"action": "skip", "reason": "paid_setup_done"}

    # Profile actually persisted
    from paid import profile
    prof = profile.load_profile()
    assert prof.name == "Jimmy"
    assert prof.preferences.daily_cost_cap_usd == 10.0


def test_paid_setup_skips_slash_commands_during_wizard(paid_tmp_iso, monkeypatch):
    """Slash commands during wizard should NOT be eaten as wizard answer —
    they go through normal routing (so /paid-setup cancel works)."""
    plugin = _fresh_plugin()
    _mock_owner(monkeypatch, plugin)
    _silence_safe(monkeypatch, plugin)
    monkeypatch.setattr(
        plugin.hermes_io, "send_dm", lambda *a, **kw: {"ok": True},
    )

    plugin.on_pre_gateway_dispatch(event=_make_event(text="/paid-setup"))
    rv = plugin.on_pre_gateway_dispatch(event=_make_event(text="/paid-pending"))
    # Should NOT be a wizard step skip — /paid-pending falls through
    assert rv != {"action": "skip", "reason": "paid_setup_step"}


def test_paid_setup_inactive_owner_text_not_captured(paid_tmp_iso, monkeypatch):
    """Owner text WITHOUT active wizard → falls through (not captured)."""
    plugin = _fresh_plugin()
    _mock_owner(monkeypatch, plugin)
    _silence_safe(monkeypatch, plugin)
    monkeypatch.setattr(
        plugin.hermes_io, "send_dm", lambda *a, **kw: {"ok": True},
    )

    rv = plugin.on_pre_gateway_dispatch(event=_make_event(text="hello"))
    # No wizard active → returns None (proceeds normally)
    assert rv is None or rv.get("reason") != "paid_setup_step"


def test_paid_resync_with_no_profile(paid_tmp_iso, monkeypatch):
    plugin = _fresh_plugin()
    _mock_owner(monkeypatch, plugin)
    _silence_safe(monkeypatch, plugin)
    sent: list[tuple] = []
    monkeypatch.setattr(
        plugin.hermes_io, "send_dm",
        lambda p, t, m, **kw: (sent.append((p, t, m)), {"ok": True})[1],
    )

    rv = plugin.on_pre_gateway_dispatch(event=_make_event(text="/paid-resync"))
    assert rv == {"action": "skip", "reason": "paid_resync_done"}
    assert "/paid-setup" in sent[-1][2]  # suggests setup


def test_paid_resync_with_profile(paid_tmp_iso, monkeypatch):
    """/paid-resync regenerates derived files from existing profile."""
    plugin = _fresh_plugin()
    _mock_owner(monkeypatch, plugin)
    _silence_safe(monkeypatch, plugin)
    sent: list[tuple] = []
    monkeypatch.setattr(
        plugin.hermes_io, "send_dm",
        lambda p, t, m, **kw: (sent.append((p, t, m)), {"ok": True})[1],
    )

    from paid import profile
    prof = profile.new_profile("jimmy", name="Test")
    prof.identities = [
        {"platform": "feishu", "user_id": "owner_lark", "enabled": True}
    ]
    profile.save_profile(prof)

    rv = plugin.on_pre_gateway_dispatch(event=_make_event(text="/paid-resync"))
    assert rv == {"action": "skip", "reason": "paid_resync_done"}
    assert (paid_tmp_iso / "persona.md").exists()
    assert (paid_tmp_iso / "owner.json").exists()
