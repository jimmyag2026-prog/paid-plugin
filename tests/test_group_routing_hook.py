"""Hook-level integration tests for v1.5 Phase 6 group routing.

Validates that ``on_pre_gateway_dispatch`` consults group_routing and
drops group messages unless the group is explicitly enabled.
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
        "paid_v1_group_hook_test", _ROOT / "__init__.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_event(*, text, chat_id, chat_type, platform="feishu",
                user_id="ou_junior"):
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
    # load_owner used by L1 decline path — return None to skip
    monkeypatch.setattr(plugin.identity, "load_owner", lambda: None)
    monkeypatch.setattr(plugin.identity, "display_name", lambda o: "Jimmy")


def _silence_outbound(monkeypatch, plugin):
    monkeypatch.setattr(
        plugin.hermes_io, "send_dm",
        lambda *a, **kw: {"ok": True, "msg_id": "stub"},
    )
    # TG callback registration tries to attach to a live PTB app — stub
    monkeypatch.setattr(
        plugin, "_ensure_telegram_callback_registered", lambda: None,
    )


@pytest.fixture
def paid_tmp_iso(tmp_path, monkeypatch):
    from paid import storage
    monkeypatch.setattr(storage, "PAID_DIR", tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Drop-by-default
# ---------------------------------------------------------------------------


def test_unconfigured_group_message_is_dropped(paid_tmp_iso, monkeypatch):
    plugin = _fresh_plugin()
    _mock_owner(monkeypatch, plugin)
    _silence_outbound(monkeypatch, plugin)

    e = _make_event(
        text="hello group",
        chat_id="oc_unknown_group",
        chat_type="group",
        user_id="ou_random_user",
    )
    rv = plugin.on_pre_gateway_dispatch(event=e)
    assert rv == {"action": "skip", "reason": "paid_group_not_enabled"}


def test_p2p_still_proceeds_normally(paid_tmp_iso, monkeypatch):
    """P2P unchanged — non-owner, non-/review, non-injection text returns
    None (let hermes dispatch proceed)."""
    plugin = _fresh_plugin()
    _mock_owner(monkeypatch, plugin)
    _silence_outbound(monkeypatch, plugin)
    # No counterparty profile → load_counterparty returns None → no
    # active-session interception. Stub safety to return no-hit.
    monkeypatch.setattr(
        plugin.safety, "detect_prompt_injection",
        lambda t: (False, []),
    )

    e = _make_event(
        text="hello bot",
        chat_id="ou_evie_dm",
        chat_type="p2p",
        user_id="ou_evie",
    )
    rv = plugin.on_pre_gateway_dispatch(event=e)
    assert rv is None


# ---------------------------------------------------------------------------
# Owner /paid-* in disabled group bypasses drop
# ---------------------------------------------------------------------------


def test_owner_paid_command_in_disabled_group_is_let_through(
    paid_tmp_iso, monkeypatch,
):
    """Phase 7 self-service: owner must be able to run /paid-enable-group
    inside a group even when that group is not yet enabled.

    Phase 7 actually executes the command in pre_gateway_dispatch — so
    rather than returning None (older Phase 6 contract), the hook returns
    {action:skip, reason:paid_group_enabled} once the command runs. We
    verify the group was actually persisted to confirm the bypass worked
    end-to-end."""
    plugin = _fresh_plugin()
    _mock_owner(monkeypatch, plugin)
    _silence_outbound(monkeypatch, plugin)
    monkeypatch.setattr(
        plugin.safety, "detect_prompt_injection",
        lambda t: (False, []),
    )

    e = _make_event(
        text="/paid-enable-group",
        chat_id="oc_owner_added_group",
        chat_type="group",
        user_id="owner_lark",  # owner per _mock_owner above
    )
    rv = plugin.on_pre_gateway_dispatch(event=e)
    assert rv == {"action": "skip", "reason": "paid_group_enabled"}
    cfg = plugin.group_routing.load_group_config("feishu_oc_owner_added_group")
    assert cfg is not None
    assert cfg.enabled is True


def test_non_owner_paid_command_in_disabled_group_is_still_dropped(
    paid_tmp_iso, monkeypatch,
):
    """Non-owners can't bypass the group gate by typing /paid-*."""
    plugin = _fresh_plugin()
    _mock_owner(monkeypatch, plugin)
    _silence_outbound(monkeypatch, plugin)

    e = _make_event(
        text="/paid-enable-group",
        chat_id="oc_unknown_group",
        chat_type="group",
        user_id="ou_imposter",
    )
    rv = plugin.on_pre_gateway_dispatch(event=e)
    assert rv == {"action": "skip", "reason": "paid_group_not_enabled"}


# ---------------------------------------------------------------------------
# Enabled review-only group
# ---------------------------------------------------------------------------


def test_review_only_group_lets_review_command_through(paid_tmp_iso, monkeypatch):
    plugin = _fresh_plugin()
    _mock_owner(monkeypatch, plugin)
    _silence_outbound(monkeypatch, plugin)
    monkeypatch.setattr(
        plugin.safety, "detect_prompt_injection",
        lambda t: (False, []),
    )

    plugin.group_routing.save_group_config(plugin.group_routing.GroupConfig(
        group_key="feishu_oc_jelabs",
        platform="feishu",
        group_id="oc_jelabs",
        enabled=True,
        mode="review-only",
        owner_user_id="owner_lark",
    ))

    # /review command — fall-through path; the _handle_review_in_pre_gateway
    # branch will handle it. We just need to confirm the group gate didn't drop it.
    captured = {}

    def fake_handler(platform, sender_id, stripped, *, event=None):
        captured["called"] = (platform, sender_id, stripped)
        return {"action": "skip", "reason": "paid_review_handled"}

    monkeypatch.setattr(plugin, "_handle_review_in_pre_gateway", fake_handler)

    e = _make_event(
        text="/review draft v1",
        chat_id="oc_jelabs",
        chat_type="group",
        user_id="ou_evie",
    )
    rv = plugin.on_pre_gateway_dispatch(event=e)
    assert rv == {"action": "skip", "reason": "paid_review_handled"}
    assert captured["called"][2].startswith("/review")


def test_everyday_mode_drops_non_command_chatter(paid_tmp_iso, monkeypatch):
    plugin = _fresh_plugin()
    _mock_owner(monkeypatch, plugin)
    _silence_outbound(monkeypatch, plugin)

    plugin.group_routing.save_group_config(plugin.group_routing.GroupConfig(
        group_key="feishu_oc_ev",
        platform="feishu",
        group_id="oc_ev",
        enabled=True,
        mode="everyday",
        owner_user_id="owner_lark",
    ))

    e = _make_event(
        text="random message in the group",
        chat_id="oc_ev",
        chat_type="group",
        user_id="ou_evie",
    )
    rv = plugin.on_pre_gateway_dispatch(event=e)
    assert rv == {
        "action": "skip", "reason": "paid_group_mode_reserved_group_everyday",
    }


def test_everyday_mode_allows_paid_commands(paid_tmp_iso, monkeypatch):
    """Even in everyday-mode groups, owner /paid-* and /review keep working."""
    plugin = _fresh_plugin()
    _mock_owner(monkeypatch, plugin)
    _silence_outbound(monkeypatch, plugin)
    monkeypatch.setattr(
        plugin.safety, "detect_prompt_injection",
        lambda t: (False, []),
    )

    plugin.group_routing.save_group_config(plugin.group_routing.GroupConfig(
        group_key="feishu_oc_ev2",
        platform="feishu",
        group_id="oc_ev2",
        enabled=True,
        mode="everyday",
        owner_user_id="owner_lark",
    ))

    e = _make_event(
        text="/paid-status",
        chat_id="oc_ev2",
        chat_type="group",
        user_id="owner_lark",
    )
    rv = plugin.on_pre_gateway_dispatch(event=e)
    # Owner /paid-* → falls through; returns None so slash dispatcher handles it.
    assert rv is None


# ---------------------------------------------------------------------------
# v1.5.2 regression test: card synthetic commands bypass group routing
# ---------------------------------------------------------------------------


def test_card_synthetic_command_bypasses_group_routing(paid_tmp_iso, monkeypatch):
    """Hermes feishu adapter synthesizes button-click events as
    ``/card button {json}`` MessageEvent with chat_type='group' HARDCODED
    (see hermes/gateway/platforms/feishu.py::_handle_card_action_event).
    On a P2P bot↔owner DM this collides with Phase 6 group routing and
    drops the click as 'paid_group_not_enabled'.

    Regression introduced in v1.5.0; fixed in v1.5.2 by short-circuiting
    /card synthetic commands through the routing gate as if they were
    plain P2P traffic. The slash dispatcher will then route them to
    _cmd_card.

    Latent because:
      - paid uid 1002 + paid-jelabs uid 1004 VPS gateway logs show
        ``_handle_card_action_event`` firing on every Lark click;
      - the synthetic ``/card button ...`` then hit Phase 6 ``classify_routing``,
        which saw chat_type=='group' + unconfigured group_key →
        ``paid_group_not_enabled`` → drop.
    """
    plugin = _fresh_plugin()
    _mock_owner(monkeypatch, plugin)
    _silence_outbound(monkeypatch, plugin)

    # Simulate what hermes adapter actually delivers: synthetic /card command
    # with chat_type='group' even though the click came from owner's DM with bot.
    e = _make_event(
        text='/card button {"paid_action": "approve", "request_id": "abc12345"}',
        chat_id="oc_owner_dm_chat",
        chat_type="group",                       # ← hermes hardcodes this
        user_id="owner_lark",                    # owner clicked
    )

    captured = {}

    def fake_cmd_card(raw_args):
        captured["called"] = raw_args
        return "PAID: handled card click"

    monkeypatch.setattr(plugin, "_cmd_card", fake_cmd_card)

    rv = plugin.on_pre_gateway_dispatch(event=e)

    # Must NOT be dropped by Phase 6 group routing.
    assert rv != {"action": "skip", "reason": "paid_group_not_enabled"}, \
        "v1.5.2 regression: /card synthetic command got dropped by group routing"
    # Must fall through to hermes slash dispatcher (which routes to _cmd_card).
    # pre_gateway_dispatch returns None on fall-through.
    assert rv is None


def test_card_synthetic_command_bypasses_review_only_strict(paid_tmp_iso, monkeypatch):
    """Same fix applies to review-only group strict mode — a card click
    on an approval card from inside (or appearing to be inside) a
    review-only group must not be gated by the group_review_strict
    active-session check."""
    plugin = _fresh_plugin()
    _mock_owner(monkeypatch, plugin)
    _silence_outbound(monkeypatch, plugin)

    # Enable a group in review-only mode; the chat_id matches the synthetic
    # event's chat_id, so classify_routing would normally hit group_review_strict.
    plugin.group_routing.save_group_config(plugin.group_routing.GroupConfig(
        group_key="feishu_oc_some_review_group",
        platform="feishu",
        group_id="oc_some_review_group",
        enabled=True,
        mode="review-only",
        owner_user_id="owner_lark",
    ))

    e = _make_event(
        text='/card button {"paid_action": "approve", "request_id": "xyz789"}',
        chat_id="oc_some_review_group",
        chat_type="group",
        user_id="owner_lark",
    )

    monkeypatch.setattr(plugin, "_cmd_card", lambda *a, **kw: "ok")

    rv = plugin.on_pre_gateway_dispatch(event=e)
    assert rv != {
        "action": "skip", "reason": "paid_group_review_only_non_review_message",
    }, "/card click in review-only group must not be drop-gated"
    assert rv is None  # falls through to slash dispatcher
