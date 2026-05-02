"""Tests for paid.settings — runtime config layer."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from paid import settings, storage  # noqa: E402


def test_load_returns_defaults_when_file_missing(paid_tmp):
    out = settings.load()
    assert out["confidence_threshold_direct"] == 0.75
    assert out["approval_timeout_minutes"] == 30
    assert out["llm_retry_backoffs_seconds"] == [0.5, 1.5, 4.0]


def test_load_layers_user_overrides_on_top_of_defaults(paid_tmp):
    storage.write_json(
        storage.PAID_DIR / "settings.json",
        {"confidence_threshold_direct": 0.9},
    )
    out = settings.load()
    assert out["confidence_threshold_direct"] == 0.9
    # Fields the user didn't set keep their defaults.
    assert out["approval_timeout_minutes"] == 30


def test_confidence_threshold_clamps_out_of_range(paid_tmp):
    storage.write_json(storage.PAID_DIR / "settings.json", {"confidence_threshold_direct": 5.0})
    assert settings.confidence_threshold_direct() == 1.0
    storage.write_json(storage.PAID_DIR / "settings.json", {"confidence_threshold_direct": -0.1})
    assert settings.confidence_threshold_direct() == 0.0


def test_confidence_threshold_falls_back_on_garbage(paid_tmp):
    storage.write_json(storage.PAID_DIR / "settings.json", {"confidence_threshold_direct": "not a number"})
    assert settings.confidence_threshold_direct() == 0.75


def test_approval_timeout_seconds_negative_disables(paid_tmp):
    storage.write_json(storage.PAID_DIR / "settings.json", {"approval_timeout_minutes": -10})
    assert settings.approval_timeout_seconds() == 0.0


def test_llm_retry_backoffs_drops_invalid_entries(paid_tmp):
    storage.write_json(
        storage.PAID_DIR / "settings.json",
        {"llm_retry_backoffs_seconds": [0.1, "x", -1.0, 2.0]},
    )
    out = settings.llm_retry_backoffs()
    assert out == (0.1, 2.0)


def test_llm_retry_backoffs_falls_back_on_non_list(paid_tmp):
    storage.write_json(storage.PAID_DIR / "settings.json", {"llm_retry_backoffs_seconds": "no"})
    out = settings.llm_retry_backoffs()
    assert out == (0.5, 1.5, 4.0)


def test_decision_uses_settings_threshold(paid_tmp):
    """End-to-end: bumping the threshold to 0.95 should flip a 0.9-confidence
    in-scope/low-stakes message from direct to request."""
    from paid.decision import decide_action, Action  # noqa
    from dataclasses import dataclass

    @dataclass
    class _C:
        topic = "logistics"
        stakes = "low"
        in_scope = True
        is_blacklisted = False
        confidence = 0.9
        draft_answer = "yes"
        reasoning = ""

    @dataclass
    class _CP:
        cp_id = "feishu_test"
        platform = "feishu"
        user_id = "test"
        display_name = "T"
        role = "junior"
        topics_allowed = ["logistics"]
        topics_always_escalate: list = None  # type: ignore
        web_search_allowed = True
        notes = ""

    # Default threshold 0.75 → 0.9 confidence direct
    a = decide_action(_C(), _CP())
    assert a.state == "direct"

    # Bump to 0.95 → 0.9 should now route to request (default branch)
    storage.write_json(
        storage.PAID_DIR / "settings.json",
        {"confidence_threshold_direct": 0.95},
    )
    a = decide_action(_C(), _CP())
    assert a.state == "request"
