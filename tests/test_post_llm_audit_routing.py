"""v1.6.7 regression — on_post_llm_call must route audit rows to
counterparties/<cp_id>/audit.jsonl, not legacy audit_log.jsonl.

Before v1.6.7 the hook hardcoded counterparty=None when calling
audit.log_action, even though cp_id had already been resolved a few
lines earlier. Result: every assistant reply audit row landed in the
legacy file the v1.6.4 migration had just renamed/emptied.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _fresh_plugin_module():
    spec = importlib.util.spec_from_file_location(
        "paid_v167_post_llm_routing_test", _ROOT / "__init__.py"
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


def test_post_llm_audit_routes_to_per_cp(paid_tmp, monkeypatch):
    """Assistant reply for a cp should land in counterparties/<cp_id>/audit.jsonl
    with the cp_id populated — never in legacy audit_log.jsonl."""
    _make_owner(paid_tmp)
    plug = _fresh_plugin_module()
    plug.identity.ensure_counterparty("feishu", "ou_cp1")

    # Pre-populate the session→cp cache the way pre_llm_call would.
    plug._cache_session_meta("sess-1", "feishu", "ou_cp1", "feishu_ou_cp1")

    # Stub the L4 output check so the hook focuses on audit routing.
    from paid import safety
    monkeypatch.setattr(safety, "check_output",
                        lambda resp, cp_id: {"ok": True, "name_leakage": [], "pii": []})

    plug.on_post_llm_call(
        session_id="sess-1",
        assistant_response="hi there",
    )

    cp_audit = paid_tmp / "counterparties" / "feishu_ou_cp1" / "audit.jsonl"
    legacy = paid_tmp / "audit_log.jsonl"

    assert cp_audit.exists(), "expected per-cp audit file"
    rows = [json.loads(l) for l in cp_audit.read_text().splitlines() if l]
    assert any(
        r.get("counterparty") == "feishu_ou_cp1"
        and r.get("extra", {}).get("assistant_response_preview") == "hi there"
        for r in rows
    )
    # And the legacy file must NOT have grown from this hook.
    assert not legacy.exists(), \
        "legacy audit_log.jsonl must not be written for cp-bound assistant replies"


def test_post_llm_system_event_still_falls_back_to_legacy(paid_tmp, monkeypatch):
    """If cp_id can't be resolved (e.g. session cache missing, no platform
    or sender in kwargs), the audit row legitimately falls back to legacy.
    This documents the design — only cp-bound rows should be in per-cp."""
    _make_owner(paid_tmp)
    plug = _fresh_plugin_module()

    from paid import safety
    monkeypatch.setattr(safety, "check_output",
                        lambda resp, cp_id: {"ok": True, "name_leakage": [], "pii": []})

    # No session cache, no kwargs platform/sender → cp_id stays empty.
    plug.on_post_llm_call(
        session_id="orphan-session",
        assistant_response="system bubble",
    )

    legacy = paid_tmp / "audit_log.jsonl"
    assert legacy.exists()
    rows = [json.loads(l) for l in legacy.read_text().splitlines() if l]
    assert rows
    assert rows[-1]["counterparty"] is None
