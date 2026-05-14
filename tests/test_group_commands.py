"""Tests for v1.5 Phase 7 group self-service slash commands.

Verifies the owner-side commands intercepted in pre_gateway_dispatch:
  /paid-enable-group [mode]
  /paid-disable-group
  /paid-set-group-mode <mode>
  /paid-set-group-name <name>
  /paid-group-status
  /paid-list-groups

Each command must:
  - run only for owner
  - reply via send_dm to the calling chat
  - persist to GroupConfig storage
  - return a {action:skip, reason:paid_group_*} dispatch action
"""

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
        "paid_v1_group_cmd_test", _ROOT / "__init__.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_event(*, text, chat_id, chat_type, platform="feishu",
                user_id="owner_lark"):
    plat = SimpleNamespace(value=platform)
    src = SimpleNamespace(
        platform=plat, user_id=user_id, chat_id=chat_id, chat_type=chat_type,
    )
    return SimpleNamespace(source=src, text=text)


def _capture(monkeypatch, plugin):
    sent: list[tuple[str, str, str]] = []

    def fake_send(platform, user_id, message, **kw):
        sent.append((platform, user_id, message))
        return {"ok": True, "msg_id": "stub"}

    monkeypatch.setattr(plugin.hermes_io, "send_dm", fake_send)
    monkeypatch.setattr(
        plugin, "_ensure_telegram_callback_registered", lambda: None,
    )
    # Mock owner identity
    monkeypatch.setattr(
        plugin.identity, "is_owner",
        lambda p, s: p == "feishu" and s == "owner_lark",
    )
    monkeypatch.setattr(plugin.identity, "load_owner", lambda: None)
    monkeypatch.setattr(plugin.identity, "display_name", lambda o: "Jimmy")
    monkeypatch.setattr(
        plugin.safety, "detect_prompt_injection",
        lambda t: (False, []),
    )
    return sent


@pytest.fixture
def paid_tmp_iso(tmp_path, monkeypatch):
    from paid import storage
    monkeypatch.setattr(storage, "PAID_DIR", tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# /paid-enable-group
# ---------------------------------------------------------------------------


def test_enable_group_default_mode_review_only(paid_tmp_iso, monkeypatch):
    plugin = _fresh_plugin()
    sent = _capture(monkeypatch, plugin)

    e = _make_event(text="/paid-enable-group", chat_id="oc_grp",
                    chat_type="group")
    rv = plugin.on_pre_gateway_dispatch(event=e)
    assert rv == {"action": "skip", "reason": "paid_group_enabled"}

    cfg = plugin.group_routing.load_group_config("feishu_oc_grp")
    assert cfg is not None
    assert cfg.enabled is True
    assert cfg.mode == "review-only"
    assert cfg.owner_user_id == "owner_lark"

    # Reply went to the group chat_id, not owner DM
    assert sent
    assert sent[0][1] == "oc_grp"
    assert "enabled" in sent[0][2].lower()


def test_enable_group_with_explicit_mode(paid_tmp_iso, monkeypatch):
    plugin = _fresh_plugin()
    _capture(monkeypatch, plugin)

    e = _make_event(text="/paid-enable-group everyday",
                    chat_id="oc_x", chat_type="group")
    plugin.on_pre_gateway_dispatch(event=e)
    cfg = plugin.group_routing.load_group_config("feishu_oc_x")
    assert cfg.mode == "everyday"


def test_enable_group_rejects_unknown_mode(paid_tmp_iso, monkeypatch):
    plugin = _fresh_plugin()
    sent = _capture(monkeypatch, plugin)

    e = _make_event(text="/paid-enable-group bogus",
                    chat_id="oc_q", chat_type="group")
    rv = plugin.on_pre_gateway_dispatch(event=e)
    assert rv == {"action": "skip", "reason": "paid_group_bad_mode"}
    assert plugin.group_routing.load_group_config("feishu_oc_q") is None
    assert "unknown mode" in sent[0][2].lower()


def test_enable_group_in_dm_refuses(paid_tmp_iso, monkeypatch):
    plugin = _fresh_plugin()
    sent = _capture(monkeypatch, plugin)

    e = _make_event(text="/paid-enable-group", chat_id="oc_dm",
                    chat_type="p2p")
    rv = plugin.on_pre_gateway_dispatch(event=e)
    assert rv == {"action": "skip", "reason": "paid_group_cmd_not_in_group"}
    assert plugin.group_routing.load_group_config("feishu_oc_dm") is None
    assert "must be run inside a group" in sent[0][2]


def test_enable_group_idempotent_preserves_created_at(paid_tmp_iso, monkeypatch):
    plugin = _fresh_plugin()
    _capture(monkeypatch, plugin)

    e = _make_event(text="/paid-enable-group", chat_id="oc_y", chat_type="group")
    plugin.on_pre_gateway_dispatch(event=e)
    first = plugin.group_routing.load_group_config("feishu_oc_y")

    # Re-enable
    e2 = _make_event(text="/paid-enable-group everyday",
                     chat_id="oc_y", chat_type="group")
    plugin.on_pre_gateway_dispatch(event=e2)
    second = plugin.group_routing.load_group_config("feishu_oc_y")
    assert second.created_at == first.created_at  # preserved
    assert second.mode == "everyday"  # updated


# ---------------------------------------------------------------------------
# /paid-disable-group
# ---------------------------------------------------------------------------


def test_disable_group_round_trip(paid_tmp_iso, monkeypatch):
    plugin = _fresh_plugin()
    sent = _capture(monkeypatch, plugin)

    plugin.group_routing.save_group_config(plugin.group_routing.GroupConfig(
        group_key="feishu_oc_dis", platform="feishu", group_id="oc_dis",
        enabled=True, mode="review-only", owner_user_id="owner_lark",
    ))

    e = _make_event(text="/paid-disable-group", chat_id="oc_dis",
                    chat_type="group")
    rv = plugin.on_pre_gateway_dispatch(event=e)
    assert rv == {"action": "skip", "reason": "paid_group_disabled"}

    cfg = plugin.group_routing.load_group_config("feishu_oc_dis")
    assert cfg.enabled is False  # kept, just disabled
    assert "disabled" in sent[0][2].lower()


def test_disable_group_when_not_enabled(paid_tmp_iso, monkeypatch):
    plugin = _fresh_plugin()
    sent = _capture(monkeypatch, plugin)

    e = _make_event(text="/paid-disable-group", chat_id="oc_never",
                    chat_type="group")
    rv = plugin.on_pre_gateway_dispatch(event=e)
    assert rv == {"action": "skip", "reason": "paid_group_already_disabled"}
    assert "not currently enabled" in sent[0][2].lower()


# ---------------------------------------------------------------------------
# /paid-set-group-mode
# ---------------------------------------------------------------------------


def test_set_group_mode_round_trip(paid_tmp_iso, monkeypatch):
    plugin = _fresh_plugin()
    sent = _capture(monkeypatch, plugin)

    plugin.group_routing.save_group_config(plugin.group_routing.GroupConfig(
        group_key="feishu_oc_setm", platform="feishu", group_id="oc_setm",
        enabled=True, mode="review-only",
    ))

    e = _make_event(text="/paid-set-group-mode both", chat_id="oc_setm",
                    chat_type="group")
    rv = plugin.on_pre_gateway_dispatch(event=e)
    assert rv == {"action": "skip", "reason": "paid_group_mode_set"}
    cfg = plugin.group_routing.load_group_config("feishu_oc_setm")
    assert cfg.mode == "both"


def test_set_group_mode_no_arg(paid_tmp_iso, monkeypatch):
    plugin = _fresh_plugin()
    sent = _capture(monkeypatch, plugin)

    plugin.group_routing.save_group_config(plugin.group_routing.GroupConfig(
        group_key="feishu_oc_setm2", platform="feishu", group_id="oc_setm2",
        enabled=True,
    ))
    e = _make_event(text="/paid-set-group-mode", chat_id="oc_setm2",
                    chat_type="group")
    rv = plugin.on_pre_gateway_dispatch(event=e)
    assert rv == {"action": "skip", "reason": "paid_group_set_mode_no_arg"}
    assert "usage" in sent[0][2].lower()


def test_set_group_mode_not_configured(paid_tmp_iso, monkeypatch):
    plugin = _fresh_plugin()
    sent = _capture(monkeypatch, plugin)

    e = _make_event(text="/paid-set-group-mode everyday",
                    chat_id="oc_unkn", chat_type="group")
    rv = plugin.on_pre_gateway_dispatch(event=e)
    assert rv == {"action": "skip", "reason": "paid_group_not_configured"}


# ---------------------------------------------------------------------------
# /paid-set-group-name
# ---------------------------------------------------------------------------


def test_set_group_name_round_trip(paid_tmp_iso, monkeypatch):
    plugin = _fresh_plugin()
    sent = _capture(monkeypatch, plugin)

    plugin.group_routing.save_group_config(plugin.group_routing.GroupConfig(
        group_key="feishu_oc_nam", platform="feishu", group_id="oc_nam",
        enabled=True,
    ))

    e = _make_event(text="/paid-set-group-name JELabs Engineering",
                    chat_id="oc_nam", chat_type="group")
    rv = plugin.on_pre_gateway_dispatch(event=e)
    assert rv == {"action": "skip", "reason": "paid_group_name_set"}
    cfg = plugin.group_routing.load_group_config("feishu_oc_nam")
    assert cfg.display_name == "JELabs Engineering"


def test_set_group_name_no_arg(paid_tmp_iso, monkeypatch):
    plugin = _fresh_plugin()
    sent = _capture(monkeypatch, plugin)

    plugin.group_routing.save_group_config(plugin.group_routing.GroupConfig(
        group_key="feishu_oc_nam2", platform="feishu", group_id="oc_nam2",
    ))

    e = _make_event(text="/paid-set-group-name", chat_id="oc_nam2",
                    chat_type="group")
    rv = plugin.on_pre_gateway_dispatch(event=e)
    assert rv == {"action": "skip", "reason": "paid_group_set_name_no_arg"}


def test_set_group_name_truncated(paid_tmp_iso, monkeypatch):
    plugin = _fresh_plugin()
    _capture(monkeypatch, plugin)

    plugin.group_routing.save_group_config(plugin.group_routing.GroupConfig(
        group_key="feishu_oc_nam3", platform="feishu", group_id="oc_nam3",
        enabled=True,
    ))
    long_name = "X" * 300
    e = _make_event(text=f"/paid-set-group-name {long_name}",
                    chat_id="oc_nam3", chat_type="group")
    plugin.on_pre_gateway_dispatch(event=e)
    cfg = plugin.group_routing.load_group_config("feishu_oc_nam3")
    assert len(cfg.display_name) == 120


# ---------------------------------------------------------------------------
# /paid-group-status
# ---------------------------------------------------------------------------


def test_group_status_when_configured(paid_tmp_iso, monkeypatch):
    plugin = _fresh_plugin()
    sent = _capture(monkeypatch, plugin)

    plugin.group_routing.save_group_config(plugin.group_routing.GroupConfig(
        group_key="feishu_oc_stat", platform="feishu", group_id="oc_stat",
        enabled=True, mode="review-only", display_name="My Group",
    ))

    e = _make_event(text="/paid-group-status", chat_id="oc_stat",
                    chat_type="group")
    rv = plugin.on_pre_gateway_dispatch(event=e)
    assert rv == {"action": "skip", "reason": "paid_group_status_reported"}
    msg = sent[0][2]
    assert "feishu_oc_stat" in msg
    assert "enabled" in msg
    assert "review-only" in msg
    assert "My Group" in msg


def test_group_status_when_not_configured(paid_tmp_iso, monkeypatch):
    plugin = _fresh_plugin()
    sent = _capture(monkeypatch, plugin)

    e = _make_event(text="/paid-group-status", chat_id="oc_none",
                    chat_type="group")
    rv = plugin.on_pre_gateway_dispatch(event=e)
    assert rv == {"action": "skip", "reason": "paid_group_status_none"}
    assert "not configured" in sent[0][2].lower()


# ---------------------------------------------------------------------------
# /paid-list-groups
# ---------------------------------------------------------------------------


def test_list_groups_empty(paid_tmp_iso, monkeypatch):
    plugin = _fresh_plugin()
    sent = _capture(monkeypatch, plugin)

    e = _make_event(text="/paid-list-groups", chat_id="ou_owner_dm",
                    chat_type="p2p")
    rv = plugin.on_pre_gateway_dispatch(event=e)
    assert rv == {"action": "skip", "reason": "paid_group_list_empty"}
    assert "no groups" in sent[0][2].lower()


def test_list_groups_shows_each(paid_tmp_iso, monkeypatch):
    plugin = _fresh_plugin()
    sent = _capture(monkeypatch, plugin)

    for i in range(3):
        plugin.group_routing.save_group_config(plugin.group_routing.GroupConfig(
            group_key=f"feishu_oc_l{i}", platform="feishu", group_id=f"oc_l{i}",
            enabled=(i != 1), mode="review-only",
            display_name=f"Group {i}",
        ))

    e = _make_event(text="/paid-list-groups", chat_id="ou_owner_dm",
                    chat_type="p2p")
    rv = plugin.on_pre_gateway_dispatch(event=e)
    assert rv == {"action": "skip", "reason": "paid_group_list_reported"}
    msg = sent[0][2]
    assert "Group 0" in msg
    assert "Group 1" in msg
    assert "Group 2" in msg
    # Disabled badge for group 1
    assert "[off]" in msg
    # Enabled badge for group 0 / 2
    assert msg.count("[ON ]") == 2


def test_list_groups_works_in_group_too(paid_tmp_iso, monkeypatch):
    """List should work from any chat (not just DM)."""
    plugin = _fresh_plugin()
    sent = _capture(monkeypatch, plugin)

    plugin.group_routing.save_group_config(plugin.group_routing.GroupConfig(
        group_key="feishu_oc_inl", platform="feishu", group_id="oc_inl",
        enabled=True,
    ))

    e = _make_event(text="/paid-list-groups", chat_id="oc_inl",
                    chat_type="group")
    rv = plugin.on_pre_gateway_dispatch(event=e)
    assert rv == {"action": "skip", "reason": "paid_group_list_reported"}


# ---------------------------------------------------------------------------
# Non-owner is rejected before commands fire
# ---------------------------------------------------------------------------


def test_non_owner_paid_command_not_handled_by_group_handler(
    paid_tmp_iso, monkeypatch,
):
    """The group-command handler only fires after the owner-check passes.
    Non-owners hitting /paid-enable-group inside a (disabled) group should
    be dropped by the routing gate, never reach the command handler."""
    plugin = _fresh_plugin()
    _capture(monkeypatch, plugin)

    e = _make_event(text="/paid-enable-group", chat_id="oc_impo",
                    chat_type="group", user_id="ou_imposter")
    rv = plugin.on_pre_gateway_dispatch(event=e)
    assert rv == {"action": "skip", "reason": "paid_group_not_enabled"}
    # Group must NOT have been enabled by the imposter
    assert plugin.group_routing.load_group_config("feishu_oc_impo") is None


# ---------------------------------------------------------------------------
# Unknown /paid-* falls through to slash dispatcher
# ---------------------------------------------------------------------------


def test_unknown_paid_command_falls_through(paid_tmp_iso, monkeypatch):
    """Existing /paid-pending, /paid-status, etc — not in group handler's
    set — must return None so the slash dispatcher picks them up."""
    plugin = _fresh_plugin()
    _capture(monkeypatch, plugin)

    e = _make_event(text="/paid-pending", chat_id="ou_owner_dm",
                    chat_type="p2p")
    rv = plugin.on_pre_gateway_dispatch(event=e)
    assert rv is None
