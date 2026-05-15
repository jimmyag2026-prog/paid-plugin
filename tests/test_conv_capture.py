"""Tests for paid.conv_capture — conversation-level profile update detection (v1.6.2)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from paid import conv_capture as cc
from paid import profile as p
from paid import storage, doc_ingest as di


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "PAID_DIR", tmp_path)
    cc.clear_pending_for_tests()
    cc._clear_rate_limit_for_tests()
    yield
    cc.clear_pending_for_tests()
    cc._clear_rate_limit_for_tests()


# ---------------------------------------------------------------------------
# should_scan
# ---------------------------------------------------------------------------


def test_should_scan_blank():
    assert not cc.should_scan("")
    assert not cc.should_scan("   ")


def test_should_scan_short():
    assert not cc.should_scan("hi")


def test_should_scan_phrase_ban_zh():
    assert cc.should_scan("以后别说按规定这个词")


def test_should_scan_phrase_ban_en():
    assert cc.should_scan("never say 'as per regulations'")


def test_should_scan_time_window():
    assert cc.should_scan("客户问题 2 小时内必须回复")


def test_should_scan_remember():
    assert cc.should_scan("记住以后客户消息优先处理")


def test_should_scan_no_trigger():
    assert not cc.should_scan("今天天气不错")
    assert not cc.should_scan("你好，帮我查一下昨天的会议记录")


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_not_rate_limited_initially():
    assert not cc.is_rate_limited("feishu", "ou_x")


def test_rate_limited_after_extract(monkeypatch):
    import time
    # Manually mark extracted
    cc._mark_extracted("feishu", "ou_x")
    assert cc.is_rate_limited("feishu", "ou_x")


def test_rate_limit_clears(monkeypatch):
    import time
    cc._mark_extracted("feishu", "ou_x")
    # Fake time to be past cooldown
    monkeypatch.setattr(
        time, "time",
        lambda: cc._LAST_EXTRACT_TS.get("feishu:ou_x", 0) + cc._EXTRACT_COOLDOWN_SEC + 1,
    )
    assert not cc.is_rate_limited("feishu", "ou_x")


# ---------------------------------------------------------------------------
# extract_from_message
# ---------------------------------------------------------------------------


def test_extract_skips_no_trigger():
    prof = p.new_profile("jimmy", name="Jimmy")
    result = cc.extract_from_message("今天心情不错", prof, "feishu", "ou_x")
    assert result == []


def test_extract_returns_empty_on_llm_error(monkeypatch):
    from paid import hermes_io
    monkeypatch.setattr(hermes_io, "call_llm", lambda **kw: (_ for _ in ()).throw(RuntimeError("fail")))
    prof = p.new_profile("jimmy", name="Jimmy")
    result = cc.extract_from_message("以后别说按规定", prof, "feishu", "ou_x")
    assert result == []


def test_extract_parses_proposals(monkeypatch):
    import json
    from paid import hermes_io
    resp = json.dumps([
        {"field": "voice.do_not_say", "proposed": ["按规定"], "rationale": "owner said so"}
    ])
    monkeypatch.setattr(hermes_io, "call_llm", lambda **kw: resp)
    prof = p.new_profile("jimmy", name="Jimmy")
    result = cc.extract_from_message("以后别说按规定", prof, "feishu", "ou_x")
    assert len(result) == 1
    assert result[0].field == "voice.do_not_say"


def test_extract_respects_rate_limit(monkeypatch):
    import json
    from paid import hermes_io
    resp = json.dumps([
        {"field": "name", "proposed": "Bob", "rationale": "test"}
    ])
    call_count = [0]
    def counting_call(**kw):
        call_count[0] += 1
        return resp
    monkeypatch.setattr(hermes_io, "call_llm", counting_call)

    prof = p.new_profile("jimmy", name="Jimmy")
    # First call should run LLM
    cc.extract_from_message("以后别说按规定", prof, "feishu", "ou_x")
    # Second call should be rate limited → no LLM
    cc.extract_from_message("以后别说按规定", prof, "feishu", "ou_x")
    assert call_count[0] == 1  # only called once


# ---------------------------------------------------------------------------
# Pending state
# ---------------------------------------------------------------------------


def test_has_pending_false_by_default():
    assert not cc.has_pending("feishu", "ou_x")


def test_store_and_has_pending():
    proposals = [di.UpdateProposal("name", "Jimmy", "Bob", "test")]
    cc.store_pending("feishu", "ou_x", proposals)
    assert cc.has_pending("feishu", "ou_x")


def test_pop_pending_clears():
    proposals = [di.UpdateProposal("name", "Jimmy", "Bob", "test")]
    cc.store_pending("feishu", "ou_x", proposals)
    popped = cc.pop_pending("feishu", "ou_x")
    assert popped == proposals
    assert not cc.has_pending("feishu", "ou_x")


# ---------------------------------------------------------------------------
# format_confirm
# ---------------------------------------------------------------------------


def test_format_confirm_empty():
    assert cc.format_confirm([]) == ""


def test_format_confirm_has_header():
    proposals = [di.UpdateProposal("voice.do_not_say", [], ["按规定"], "owner said")]
    msg = cc.format_confirm(proposals)
    assert "💡" in msg
    assert "1." in msg


# ---------------------------------------------------------------------------
# apply_confirmed
# ---------------------------------------------------------------------------


def test_apply_confirmed_no_pending():
    msg = cc.apply_confirmed("feishu", "ou_x", "all")
    assert "没有待确认" in msg


def test_apply_confirmed_accept(monkeypatch):
    from paid import profile_sync
    monkeypatch.setattr(profile_sync, "derive_from_profile", lambda pr: {"wrote": []})

    prof = p.new_profile("jimmy", name="Jimmy")
    p.save_profile(prof)

    proposals = [di.UpdateProposal("name", "Jimmy", "Jimmy Yin", "test")]
    cc.store_pending("feishu", "ou_x", proposals)

    msg = cc.apply_confirmed("feishu", "ou_x", "all")
    assert "1/1" in msg
    assert not cc.has_pending("feishu", "ou_x")
    updated = p.load_profile()
    assert updated.name == "Jimmy Yin"


def test_apply_confirmed_reject(monkeypatch):
    from paid import profile_sync
    monkeypatch.setattr(profile_sync, "derive_from_profile", lambda pr: {"wrote": []})

    prof = p.new_profile("jimmy", name="Jimmy")
    p.save_profile(prof)

    proposals = [di.UpdateProposal("name", "Jimmy", "Bob", "test")]
    cc.store_pending("feishu", "ou_x", proposals)

    msg = cc.apply_confirmed("feishu", "ou_x", "none")
    assert "0/1" in msg
    updated = p.load_profile()
    assert updated.name == "Jimmy"  # unchanged
