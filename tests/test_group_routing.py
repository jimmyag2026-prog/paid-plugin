"""Tests for paid.group_routing (v1.5 Phase 6)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from paid import group_routing, storage


@pytest.fixture(autouse=True)
def isolate_paid_dir(tmp_path, monkeypatch):
    """Each test gets its own ~/.hermes/paid dir."""
    monkeypatch.setattr(storage, "PAID_DIR", tmp_path / "paid")
    storage.PAID_DIR.mkdir(parents=True, exist_ok=True)
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(*, platform="feishu", chat_id="oc_xxx", chat_type=None,
                is_group=None, user_id="ou_user", text="", message_id=""):
    plat = SimpleNamespace(value=platform)
    source = SimpleNamespace(
        platform=plat,
        user_id=user_id,
        chat_id=chat_id,
    )
    if chat_type is not None:
        source.chat_type = chat_type
    if is_group is not None:
        source.is_group = is_group
    if message_id:
        source.message_id = message_id
    event = SimpleNamespace(source=source, text=text)
    if message_id:
        event.message_id = message_id
    return event


# ---------------------------------------------------------------------------
# classify_chat
# ---------------------------------------------------------------------------


def test_classify_chat_explicit_p2p():
    e = _make_event(chat_type="p2p")
    assert group_routing.classify_chat(e) == "p2p"


def test_classify_chat_explicit_group():
    e = _make_event(chat_type="group")
    assert group_routing.classify_chat(e) == "group"


def test_classify_chat_telegram_supergroup():
    e = _make_event(platform="telegram", chat_id="-100123", chat_type="supergroup")
    assert group_routing.classify_chat(e) == "group"


def test_classify_chat_telegram_private():
    e = _make_event(platform="telegram", chat_id="123", chat_type="private")
    assert group_routing.classify_chat(e) == "p2p"


def test_classify_chat_is_group_bool_fallback():
    e = _make_event(is_group=True)
    assert group_routing.classify_chat(e) == "group"
    e2 = _make_event(is_group=False)
    assert group_routing.classify_chat(e2) == "p2p"


def test_classify_chat_negative_id_heuristic():
    """When chat_type missing, TG negative chat_id = group."""
    e = _make_event(platform="telegram", chat_id="-100456")
    assert group_routing.classify_chat(e) == "group"


def test_classify_chat_none_event():
    assert group_routing.classify_chat(None) == "p2p"


def test_classify_chat_missing_source():
    assert group_routing.classify_chat(SimpleNamespace()) == "p2p"


def test_classify_chat_lark_default_p2p_when_chat_type_missing():
    """oc_xxx without chat_type — we conservatively assume p2p, matching
    existing v1.4 behavior."""
    e = _make_event(chat_id="oc_xxx")
    assert group_routing.classify_chat(e) == "p2p"


# ---------------------------------------------------------------------------
# get_group_key
# ---------------------------------------------------------------------------


def test_group_key_format():
    e = _make_event(platform="feishu", chat_id="oc_abc")
    assert group_routing.get_group_key(e) == "feishu_oc_abc"


def test_group_key_normalizes_platform_case():
    plat = SimpleNamespace(value="Feishu")
    source = SimpleNamespace(platform=plat, chat_id="oc_xyz", user_id="ou_u")
    e = SimpleNamespace(source=source, text="")
    assert group_routing.get_group_key(e) == "feishu_oc_xyz"


def test_group_key_empty_when_missing():
    assert group_routing.get_group_key(None) == ""
    assert group_routing.get_group_key(SimpleNamespace()) == ""
    e = _make_event(chat_id="")
    assert group_routing.get_group_key(e) == ""


# ---------------------------------------------------------------------------
# GroupConfig persistence
# ---------------------------------------------------------------------------


def test_save_and_load_group_config():
    cfg = group_routing.GroupConfig(
        group_key="feishu_oc_abc",
        platform="feishu",
        group_id="oc_abc",
        enabled=True,
        mode="review-only",
        owner_user_id="ou_owner",
        display_name="JELabs eng",
    )
    saved = group_routing.save_group_config(cfg)
    assert saved.created_at
    assert saved.updated_at

    loaded = group_routing.load_group_config("feishu_oc_abc")
    assert loaded is not None
    assert loaded.enabled is True
    assert loaded.mode == "review-only"
    assert loaded.owner_user_id == "ou_owner"
    assert loaded.display_name == "JELabs eng"


def test_load_unknown_group_returns_none():
    assert group_routing.load_group_config("not_a_real_group") is None


def test_load_empty_key_returns_none():
    assert group_routing.load_group_config("") is None


def test_save_rejects_empty_key():
    with pytest.raises(ValueError):
        group_routing.save_group_config(group_routing.GroupConfig(
            group_key="", platform="feishu", group_id="oc_abc",
        ))


def test_save_rejects_invalid_mode():
    with pytest.raises(ValueError):
        group_routing.save_group_config(group_routing.GroupConfig(
            group_key="feishu_oc_x", platform="feishu", group_id="oc_x",
            mode="nonsense",
        ))


def test_list_group_configs_returns_all():
    for i in range(3):
        group_routing.save_group_config(group_routing.GroupConfig(
            group_key=f"feishu_oc_{i}",
            platform="feishu",
            group_id=f"oc_{i}",
            enabled=(i % 2 == 0),
        ))
    listed = group_routing.list_group_configs()
    assert len(listed) == 3
    assert {c.group_key for c in listed} == {
        "feishu_oc_0", "feishu_oc_1", "feishu_oc_2",
    }


def test_list_skips_malformed_files(tmp_path):
    # Garbage JSON file in groups dir → skipped, not crashing
    (storage.PAID_DIR / "groups").mkdir(parents=True, exist_ok=True)
    bad = storage.PAID_DIR / "groups" / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    # And a non-json file
    (storage.PAID_DIR / "groups" / "notjson.txt").write_text("x", encoding="utf-8")

    group_routing.save_group_config(group_routing.GroupConfig(
        group_key="feishu_ok",
        platform="feishu",
        group_id="ok",
        enabled=True,
    ))
    listed = group_routing.list_group_configs()
    keys = [c.group_key for c in listed]
    assert "feishu_ok" in keys
    assert "bad" not in keys


def test_delete_group_config():
    group_routing.save_group_config(group_routing.GroupConfig(
        group_key="feishu_oc_del",
        platform="feishu",
        group_id="oc_del",
        enabled=True,
    ))
    assert group_routing.load_group_config("feishu_oc_del") is not None
    assert group_routing.delete_group_config("feishu_oc_del") is True
    assert group_routing.load_group_config("feishu_oc_del") is None
    # Second delete → False
    assert group_routing.delete_group_config("feishu_oc_del") is False


def test_delete_empty_key():
    assert group_routing.delete_group_config("") is False


# ---------------------------------------------------------------------------
# classify_routing
# ---------------------------------------------------------------------------


def test_routing_p2p_passthrough():
    e = _make_event(chat_type="p2p")
    assert group_routing.classify_routing(e) == "p2p"


def test_routing_group_unconfigured_drops():
    e = _make_event(chat_type="group", chat_id="oc_unknown")
    assert group_routing.classify_routing(e) == "group_disabled"


def test_routing_group_disabled_drops():
    group_routing.save_group_config(group_routing.GroupConfig(
        group_key="feishu_oc_off",
        platform="feishu",
        group_id="oc_off",
        enabled=False,  # explicit disable
    ))
    e = _make_event(chat_type="group", chat_id="oc_off")
    assert group_routing.classify_routing(e) == "group_disabled"


def test_routing_group_review_only_with_review_command():
    group_routing.save_group_config(group_routing.GroupConfig(
        group_key="feishu_oc_rev",
        platform="feishu",
        group_id="oc_rev",
        enabled=True,
        mode="review-only",
    ))
    e = _make_event(chat_type="group", chat_id="oc_rev")
    assert group_routing.classify_routing(e, text="/review draft 1") == "group_review"
    assert group_routing.classify_routing(e, text="/r quick check") == "group_review"


def test_routing_group_review_only_returns_strict_for_chatter():
    """v1.5.1 (audit Critical #5): in review-only mode, non-command messages
    return 'group_review_strict' so the caller (pre_gateway_dispatch) MUST
    verify the sender has an active review session before letting them
    through. /review and /paid- prefix → 'group_review' (caller routes
    directly)."""
    group_routing.save_group_config(group_routing.GroupConfig(
        group_key="feishu_oc_rev2",
        platform="feishu",
        group_id="oc_rev2",
        enabled=True,
        mode="review-only",
    ))
    e = _make_event(chat_type="group", chat_id="oc_rev2")
    assert group_routing.classify_routing(e, text="lunch?") == "group_review_strict"
    assert group_routing.classify_routing(e, text="/review draft") == "group_review"
    assert group_routing.classify_routing(e, text="/paid-status") == "group_review"


def test_routing_group_everyday_mode():
    group_routing.save_group_config(group_routing.GroupConfig(
        group_key="feishu_oc_ev",
        platform="feishu",
        group_id="oc_ev",
        enabled=True,
        mode="everyday",
    ))
    e = _make_event(chat_type="group", chat_id="oc_ev")
    assert group_routing.classify_routing(e) == "group_everyday"


def test_routing_group_both_mode():
    group_routing.save_group_config(group_routing.GroupConfig(
        group_key="feishu_oc_bo",
        platform="feishu",
        group_id="oc_bo",
        enabled=True,
        mode="both",
    ))
    e = _make_event(chat_type="group", chat_id="oc_bo")
    assert group_routing.classify_routing(e) == "group_both"


def test_routing_unknown_mode_conservatively_drops(tmp_path):
    """Forward-compat: if config carries a mode we don't recognize
    (e.g. v1.6 introduces 'broadcast'), drop to be safe."""
    # Save raw json with bogus mode bypassing save_group_config validation
    (storage.PAID_DIR / "groups").mkdir(parents=True, exist_ok=True)
    storage.write_json(
        storage.PAID_DIR / "groups" / "feishu_oc_future.json",
        {
            "schema_version": 1,
            "group_key": "feishu_oc_future",
            "platform": "feishu",
            "group_id": "oc_future",
            "enabled": True,
            "mode": "broadcast-future",
            "created_at": "2026-05-13T00:00:00+00:00",
            "updated_at": "2026-05-13T00:00:00+00:00",
        },
    )
    e = _make_event(chat_type="group", chat_id="oc_future")
    assert group_routing.classify_routing(e) == "group_disabled"


def test_routing_group_chat_missing_key_drops():
    """Group chat but we can't extract a group key (no chat_id) → drop."""
    e = _make_event(chat_type="group", chat_id="")
    assert group_routing.classify_routing(e) == "group_disabled"


# ---------------------------------------------------------------------------
# extract_message_id
# ---------------------------------------------------------------------------


def test_extract_message_id_event_level():
    e = _make_event(message_id="om_abc123")
    assert group_routing.extract_message_id(e) == "om_abc123"


def test_extract_message_id_source_level_fallback():
    """Some adapters put message_id on source, not event."""
    plat = SimpleNamespace(value="feishu")
    source = SimpleNamespace(
        platform=plat,
        user_id="ou_u",
        chat_id="oc_x",
        message_id="om_xyz",
    )
    e = SimpleNamespace(source=source, text="hello")
    # Event has no .message_id but source does
    assert group_routing.extract_message_id(e) == "om_xyz"


def test_extract_message_id_missing():
    e = _make_event()
    assert group_routing.extract_message_id(e) == ""
    assert group_routing.extract_message_id(None) == ""
