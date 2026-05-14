"""v1.5.3 backlog fixes — collected during 2026-05-14 live manual test.

Covers:
- #5: cancel command synonyms (/review close/stop/abort/end/exit/quit)
- #6: /review regex tolerates CJK chars without space
- #7: /review reply routes back to group chat_id (not cp DM)
- i18n: detect_lang + reply templates in zh / en / ko
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
        "paid_v1_5_3_test", _ROOT / "__init__.py"
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


def _make_cp(active_session: str = "", platform: str = "feishu",
             user_id: str = "u1") -> SimpleNamespace:
    return SimpleNamespace(
        cp_id=f"{platform}_{user_id}",
        platform=platform,
        user_id=user_id,
        active_review_session=active_session,
    )


@pytest.fixture
def paid_tmp_iso(tmp_path, monkeypatch):
    from paid import storage
    monkeypatch.setattr(storage, "PAID_DIR", tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# i18n.detect_lang
# ---------------------------------------------------------------------------


def test_detect_lang_chinese():
    from paid_review.i18n import detect_lang
    assert detect_lang("帮我看一下这个文档") == "zh"
    assert detect_lang("review 这个 doc 一下") == "zh"  # mostly CJK
    assert detect_lang("你的产品定位策略是什么") == "zh"


def test_detect_lang_english():
    from paid_review.i18n import detect_lang
    assert detect_lang("review the Q3 roadmap please") == "en"
    assert detect_lang("look at this pitch deck") == "en"


def test_detect_lang_korean():
    from paid_review.i18n import detect_lang
    assert detect_lang("이 문서 좀 봐주세요") == "ko"
    assert detect_lang("리뷰 부탁드립니다") == "ko"


def test_detect_lang_empty_defaults_zh():
    from paid_review.i18n import detect_lang
    assert detect_lang("") == "zh"
    assert detect_lang("   ") == "zh"


def test_detect_lang_mixed_with_url():
    """URL Latin chars currently skew toward 'en' when CJK fraction is small.
    Known limitation in v1.5.3 detector — URL-content stripping can be added
    in v1.5.4 if it causes UX issues. For now: heavy URL + short Chinese
    legit becomes 'en'.
    """
    from paid_review.i18n import detect_lang
    # Mostly Chinese with one URL still detects zh
    assert detect_lang("帮我看一下这篇文章 https://x.com") == "zh"
    # Pure English with URL → en
    assert detect_lang("review https://example.com") == "en"


# ---------------------------------------------------------------------------
# i18n.t — template lookup
# ---------------------------------------------------------------------------


def test_t_returns_zh_template_by_default():
    from paid_review.i18n import t
    out = t("subject_no_candidates", "zh")
    assert "直接打给我" in out or "subject" not in out.lower()


def test_t_returns_en_template():
    from paid_review.i18n import t
    out = t("subject_no_candidates", "en")
    assert "type" in out.lower() or "subject" in out.lower()


def test_t_returns_ko_template():
    from paid_review.i18n import t
    out = t("subject_no_candidates", "ko")
    # Korean Hangul block presence
    assert any('가' <= c <= '힯' for c in out)


def test_t_falls_back_to_zh_on_unknown_lang():
    from paid_review.i18n import t
    zh = t("subject_no_candidates", "zh")
    unknown = t("subject_no_candidates", "ja")  # not in table
    assert unknown == zh


def test_t_substitutes_kwargs():
    from paid_review.i18n import t
    out = t("subject_ask", "zh", top="Q3 OKR", alt_hint="")
    assert "Q3 OKR" in out


# ---------------------------------------------------------------------------
# #5: cancel command synonyms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variant", [
    "/review cancel",
    "/review close",
    "/review stop",
    "/review abort",
    "/review end",
    "/review exit",
    "/review quit",
    "/r cancel",
    "/r close",
    "/r stop",
])
def test_cancel_synonym_force_closes_active(variant, paid_tmp_iso, monkeypatch):
    """All cancel synonyms close the active session, not open a new one."""
    plugin = _fresh_plugin()
    cp = _make_cp(active_session="sid_xyz")
    monkeypatch.setattr(
        plugin._review_api_module if hasattr(plugin, "_review_api_module")
        else __import__("paid_review.api", fromlist=["force_close"]),
        "force_close", lambda s, reason: "closed",
    )
    monkeypatch.setattr(
        plugin.identity, "clear_active_review_session",
        lambda cp, archive: None,
    )
    out = plugin._maybe_route_to_review_skill(cp, variant, {})
    assert out is not None
    ctx = out.get("context", "")
    # zh ("已关闭") OR en ("Closed") OR ko ("닫았습니다") variants accepted
    assert any(s in ctx for s in ("已关闭", "Closed", "닫았습니다"))


def test_cancel_synonym_no_active_returns_friendly(paid_tmp_iso):
    """No active session + cancel synonym → friendly reject, no new intake."""
    plugin = _fresh_plugin()
    cp = _make_cp(active_session="")
    out = plugin._maybe_route_to_review_skill(cp, "/review stop", {})
    assert out is not None
    ctx = out["context"]
    # zh OR en variants
    assert (
        "没有进行中" in ctx
        or "have an active review session" in ctx.lower()
        or "진행 중인 review session 이 없" in ctx
    )


# ---------------------------------------------------------------------------
# #6: /review regex CJK-friendly
# ---------------------------------------------------------------------------


def test_review_cmd_recognized_with_cjk_directly_after_prefix():
    """`/review看一下我的资料` (no space, CJK directly) must classify as
    review command, not chatter — v1.5.3 fix #6."""
    from paid.group_routing import _REVIEW_CMD_RE
    assert _REVIEW_CMD_RE.match("/review看一下")
    assert _REVIEW_CMD_RE.match("/r看一下")
    # punctuation after also recognized
    assert _REVIEW_CMD_RE.match("/review:这个")
    # but `/reviewing` (alpha continuation) must NOT be recognized
    assert not _REVIEW_CMD_RE.match("/reviewing")
    assert not _REVIEW_CMD_RE.match("/reviewers")


def test_review_cmd_still_recognized_with_space():
    """Backward-compat: original space-after pattern still works."""
    from paid.group_routing import _REVIEW_CMD_RE
    assert _REVIEW_CMD_RE.match("/review hello")
    assert _REVIEW_CMD_RE.match("/r hello")
    assert _REVIEW_CMD_RE.match("/review")
    assert _REVIEW_CMD_RE.match("/r")


# ---------------------------------------------------------------------------
# #7: /review in group → reply routes to group chat_id
# ---------------------------------------------------------------------------


def test_review_in_group_reply_routes_to_group(paid_tmp_iso, monkeypatch):
    """When cp sends /review in a Lark group, PAID's reply goes back to
    the group chat_id (not the cp's DM with the bot)."""
    plugin = _fresh_plugin()
    sent_targets: list[str] = []

    def fake_send(platform, target, message, **kw):
        sent_targets.append(target)
        return {"ok": True, "msg_id": "stub"}

    monkeypatch.setattr(plugin.hermes_io, "send_dm", fake_send)
    monkeypatch.setattr(
        plugin.identity, "ensure_counterparty",
        lambda p, sid: _make_cp(active_session="", platform=p, user_id=sid),
    )
    # Stub out the routing fn to return canned content
    monkeypatch.setattr(
        plugin, "_maybe_route_to_review_skill",
        lambda cp, text, hk: {"context": "stub reply"},
    )

    event = _make_event(
        text="/review 看一下", chat_id="oc_some_group",
        chat_type="group", user_id="ou_evie",
    )
    rv = plugin._handle_review_in_pre_gateway(
        "feishu", "ou_evie", "/review 看一下", event=event,
    )
    assert rv == {"action": "skip", "reason": "paid_review_routed"}
    # Reply sent to the group, NOT to ou_evie
    assert sent_targets == ["oc_some_group"]


def test_review_in_dm_reply_still_routes_to_cp_dm(paid_tmp_iso, monkeypatch):
    """Backward-compat: when /review is sent in P2P DM, reply still goes
    to the cp's user_id (DM target)."""
    plugin = _fresh_plugin()
    sent_targets: list[str] = []

    monkeypatch.setattr(
        plugin.hermes_io, "send_dm",
        lambda p, t, m, **kw: (sent_targets.append(t), {"ok": True})[1],
    )
    monkeypatch.setattr(
        plugin.identity, "ensure_counterparty",
        lambda p, sid: _make_cp(active_session="", platform=p, user_id=sid),
    )
    monkeypatch.setattr(
        plugin, "_maybe_route_to_review_skill",
        lambda cp, text, hk: {"context": "stub reply"},
    )

    event = _make_event(
        text="/review 看一下", chat_id="oc_evie_dm",
        chat_type="p2p", user_id="ou_evie",
    )
    plugin._handle_review_in_pre_gateway(
        "feishu", "ou_evie", "/review 看一下", event=event,
    )
    # Sent to cp user_id (DM), not group chat_id
    assert sent_targets == ["ou_evie"]


def test_review_no_event_still_works_dm(paid_tmp_iso, monkeypatch):
    """Backward-compat: callers that don't pass event= keep DM behavior."""
    plugin = _fresh_plugin()
    sent_targets: list[str] = []

    monkeypatch.setattr(
        plugin.hermes_io, "send_dm",
        lambda p, t, m, **kw: (sent_targets.append(t), {"ok": True})[1],
    )
    monkeypatch.setattr(
        plugin.identity, "ensure_counterparty",
        lambda p, sid: _make_cp(active_session="", platform=p, user_id=sid),
    )
    monkeypatch.setattr(
        plugin, "_maybe_route_to_review_skill",
        lambda cp, text, hk: {"context": "stub"},
    )
    plugin._handle_review_in_pre_gateway("feishu", "ou_evie", "/review 看")
    assert sent_targets == ["ou_evie"]


# ---------------------------------------------------------------------------
# Subject prompt — cancel hint included (fix #1)
# ---------------------------------------------------------------------------


def test_subject_ask_includes_cancel_hint():
    from paid_review.i18n import t
    out_zh = t("subject_ask", "zh", top="Q3 OKR", alt_hint="")
    out_en = t("subject_ask", "en", top="Q3 OKR", alt_hint="")
    out_ko = t("subject_ask", "ko", top="Q3 OKR", alt_hint="")
    for s in (out_zh, out_en, out_ko):
        assert "/review cancel" in s, f"missing cancel hint: {s!r}"
