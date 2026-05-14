"""v1.5.6 review-fix #3 — verify on_pre_llm_call returns 'system unavailable'
wrap when daily hard cap is exhausted, instead of falling through to classifier
and creating J3 approval cards.

The pre-fix shipped behavior: cap-exhausted → call_llm raises in classifier →
classifier's broad except → fallback Classification → state=request → owner
gets flooded with approval cards.

The fix path: cap status checked BEFORE classifier runs. cap_exhausted →
return a wrap directive instructing the LLM to reply 'system unavailable'.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _fresh_plugin_module():
    spec = importlib.util.spec_from_file_location(
        "paid_v1_pre_llm_cap_test", _ROOT / "__init__.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_owner(paid_tmp):
    (paid_tmp / "owner.json").write_text(json.dumps({
        "schema_version": 2,
        "owner_id": "owner_test",
        "name": "Owner",
        "identities": [{"platform": "feishu", "user_id": "ou_owner"}],
    }), encoding="utf-8")


def _seed_cp_via_load(plug):
    """Ensure a counterparty profile exists for the test sender."""
    # ensure_counterparty in __init__.py creates the cp on first contact.
    plug.identity.ensure_counterparty("feishu", "ou_sender")


def _exceeded_cap_status():
    return {
        "today_usd": 25.0,
        "week_usd": 25.0,
        "daily_soft_cap": 5.0,
        "daily_hard_cap": 20.0,
        "weekly_soft_cap": 25.0,
        "daily_soft_exceeded": True,
        "daily_hard_exceeded": True,
        "weekly_soft_exceeded": True,
        "enabled": True,
    }


def _under_cap_status():
    return {
        "today_usd": 0.5,
        "week_usd": 0.5,
        "daily_soft_cap": 5.0,
        "daily_hard_cap": 20.0,
        "weekly_soft_cap": 25.0,
        "daily_soft_exceeded": False,
        "daily_hard_exceeded": False,
        "weekly_soft_exceeded": False,
        "enabled": True,
    }


def test_pre_llm_returns_unavailable_wrap_when_cap_exceeded(paid_tmp, monkeypatch):
    _make_owner(paid_tmp)
    plug = _fresh_plugin_module()
    _seed_cp_via_load(plug)

    # Patch cost.cap_status to report exceeded
    from paid import cost
    monkeypatch.setattr(cost, "cap_status", _exceeded_cap_status)

    # Belt-and-suspenders: also stub classifier.classify — we don't want it
    # called at all on the cap-exceeded path. If pre_llm_call still reaches
    # it, this assert blows up.
    def _classifier_called_unexpectedly(*a, **kw):
        raise AssertionError("classifier.classify should NOT be called when cap exceeded")

    monkeypatch.setattr(plug.classifier, "classify", _classifier_called_unexpectedly)

    result = plug.on_pre_llm_call(
        platform="feishu", sender_id="ou_sender",
        user_message="hi PAID, please answer my question",
        session_id="sess_test",
    )

    assert isinstance(result, dict)
    ctx = result.get("context", "")
    assert "系统暂时不可用" in ctx
    assert "System temporarily unavailable" in ctx
    # Must instruct LLM to IGNORE original message + reply EXACTLY
    assert "IGNORE the user message" in ctx
    assert "Reply EXACTLY" in ctx


def test_pre_llm_audit_row_written_with_blocked_by(paid_tmp, monkeypatch):
    _make_owner(paid_tmp)
    plug = _fresh_plugin_module()
    _seed_cp_via_load(plug)

    from paid import cost
    monkeypatch.setattr(cost, "cap_status", _exceeded_cap_status)
    monkeypatch.setattr(plug.classifier, "classify",
                        lambda *a, **kw: pytest.fail("should not reach classifier"))

    plug.on_pre_llm_call(
        platform="feishu", sender_id="ou_sender",
        user_message="test", session_id="sess_audit",
    )

    audit_path = paid_tmp / "audit_log.jsonl"
    assert audit_path.exists()
    rows = [json.loads(l) for l in audit_path.read_text().splitlines() if l.strip()]
    cap_rows = [r for r in rows if (r.get("extra") or {}).get("blocked_by") == "cost_cap_exceeded"]
    assert len(cap_rows) == 1
    extra = cap_rows[0]["extra"]
    assert extra["today_usd"] == 25.0
    assert extra["daily_hard_cap"] == 20.0


def test_pre_llm_normal_flow_when_under_cap(paid_tmp, monkeypatch):
    """Verify we don't block normal classify→decide flow when cap is under."""
    _make_owner(paid_tmp)
    plug = _fresh_plugin_module()
    _seed_cp_via_load(plug)

    from paid import cost
    monkeypatch.setattr(cost, "cap_status", _under_cap_status)

    classify_called = {"hit": False}

    def _spy_classify(*a, **kw):
        classify_called["hit"] = True
        return plug.classifier.Classification(
            topic="general", stakes="low", in_scope=True, is_blacklisted=False,
            confidence=0.9, needs_retrieval=False, suggested_queries=[],
            draft_answer="reply text", reasoning="ok",
        )

    monkeypatch.setattr(plug.classifier, "classify", _spy_classify)

    plug.on_pre_llm_call(
        platform="feishu", sender_id="ou_sender",
        user_message="under-cap msg", session_id="sess_normal",
    )

    assert classify_called["hit"] is True


def test_pre_llm_fail_open_when_cap_status_errors(paid_tmp, monkeypatch):
    """If cap_status itself raises, we should fail-open (run normal flow),
    matching the inline-enforce policy in hermes_io.call_llm."""
    _make_owner(paid_tmp)
    plug = _fresh_plugin_module()
    _seed_cp_via_load(plug)

    from paid import cost
    def _boom():
        raise RuntimeError("ledger corrupted")
    monkeypatch.setattr(cost, "cap_status", _boom)

    classify_called = {"hit": False}
    def _spy_classify(*a, **kw):
        classify_called["hit"] = True
        return plug.classifier.Classification(
            topic="general", stakes="low", in_scope=True, is_blacklisted=False,
            confidence=0.9, needs_retrieval=False, suggested_queries=[],
            draft_answer="reply text", reasoning="ok",
        )
    monkeypatch.setattr(plug.classifier, "classify", _spy_classify)

    # Must not raise; must reach classifier (fail-open)
    plug.on_pre_llm_call(
        platform="feishu", sender_id="ou_sender",
        user_message="x", session_id="sess_fail_open",
    )
    assert classify_called["hit"] is True


def test_pre_llm_disabled_cap_no_short_circuit(paid_tmp, monkeypatch):
    """settings.cost.enabled=False → cap check is no-op even with exceeded."""
    _make_owner(paid_tmp)
    plug = _fresh_plugin_module()
    _seed_cp_via_load(plug)

    from paid import cost
    status = _exceeded_cap_status()
    status["enabled"] = False
    monkeypatch.setattr(cost, "cap_status", lambda: status)

    classify_called = {"hit": False}
    def _spy_classify(*a, **kw):
        classify_called["hit"] = True
        return plug.classifier.Classification(
            topic="general", stakes="low", in_scope=True, is_blacklisted=False,
            confidence=0.9, needs_retrieval=False, suggested_queries=[],
            draft_answer="r", reasoning="ok",
        )
    monkeypatch.setattr(plug.classifier, "classify", _spy_classify)

    plug.on_pre_llm_call(
        platform="feishu", sender_id="ou_sender",
        user_message="x", session_id="sess_disabled",
    )
    assert classify_called["hit"] is True
