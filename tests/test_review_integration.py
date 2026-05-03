"""Tests for review-skill integration deps (W2 batch 1).

Covers:
  R-B3 — classifier `needs_review` / `review_subject_hints`
  R-B4 — decision `review` 4th state + min-stakes threshold
  R-B5 — identity active_review_session helpers + history rotation +
         backward compat with v1 profile.json
  R-B2 — hermes_io render_options_block + send_dm options_block plumbing
"""

from __future__ import annotations

import json

import pytest

from paid import classifier, decision, hermes_io, identity, storage


# --------------------------------------------------------------------------
# Helpers / lightweight stand-ins so we don't hit live LLM
# --------------------------------------------------------------------------


class _StubCp:
    """Minimal counterparty for classifier/decision attr-only access."""

    display_name = "Junior J"
    role = "junior"
    topics_allowed = ["logistics"]
    topics_always_escalate = ["equity"]


# ==========================================================================
# R-B3 · Classifier needs_review + review_subject_hints
# ==========================================================================


def test_classification_default_has_review_fields():
    c = classifier.Classification()
    assert c.needs_review is False
    assert c.review_subject_hints == []


def test_classifier_parses_needs_review_true(monkeypatch):
    monkeypatch.setattr(
        hermes_io,
        "call_llm",
        lambda **kw: json.dumps(
            {
                "topic": "Q3 budget",
                "stakes": "high",
                "in_scope": True,
                "is_blacklisted": False,
                "confidence": 0.8,
                "needs_retrieval": False,
                "suggested_queries": [],
                "draft_answer": "",
                "reasoning": "structured ask with budget proposal",
                "needs_review": True,
                "review_subject_hints": [
                    "approve Q3 total $240k",
                    "reallocate channel mix 60/40",
                    "decide tier-2 city pilot",
                ],
            }
        ),
    )
    c = classifier.classify("here is my Q3 budget", _StubCp(), "Jimmy", "")
    assert c.needs_review is True
    assert len(c.review_subject_hints) == 3
    assert "approve Q3 total $240k" in c.review_subject_hints


def test_classifier_clamps_review_subject_hints_to_4(monkeypatch):
    monkeypatch.setattr(
        hermes_io,
        "call_llm",
        lambda **kw: json.dumps(
            {
                "topic": "x",
                "needs_review": True,
                "review_subject_hints": ["a", "b", "c", "d", "e", "f"],
            }
        ),
    )
    c = classifier.classify("foo", _StubCp(), "Jimmy", "")
    # Cap at 4 hints — anything beyond is noise.
    assert len(c.review_subject_hints) == 4
    assert c.review_subject_hints == ["a", "b", "c", "d"]


def test_classifier_review_defaults_when_omitted(monkeypatch):
    """An LLM that ignores the new fields must NOT crash the parser."""
    monkeypatch.setattr(
        hermes_io,
        "call_llm",
        lambda **kw: json.dumps(
            {"topic": "x", "stakes": "low", "in_scope": True, "confidence": 0.6}
        ),
    )
    c = classifier.classify("foo", _StubCp(), "Jimmy", "")
    assert c.needs_review is False
    assert c.review_subject_hints == []


# ==========================================================================
# R-B4 · Decision review state + threshold
# ==========================================================================


def _make_classification(**kw):
    base = dict(
        topic="Q3 budget",
        stakes="high",
        in_scope=True,
        is_blacklisted=False,
        confidence=0.8,
        needs_review=True,
    )
    base.update(kw)
    return classifier.Classification(**base)


def test_decide_action_review_state_on_needs_review(paid_tmp):
    cls = _make_classification()
    action = decision.decide_action(cls, _StubCp(), user_message="here is my draft")
    assert action.state == "review"
    assert "needs_review" in action.reason
    assert decision.is_review_state(action) is True


def test_decide_action_review_blocked_by_blacklist_keyword(paid_tmp):
    """Hard blacklist on user_message ALWAYS wins over review."""
    cls = _make_classification()
    action = decision.decide_action(
        cls, _StubCp(), user_message="here is my equity vesting draft"
    )
    assert action.state == "request"
    assert "hard blacklist" in action.reason


def test_decide_action_review_blocked_by_classifier_blacklist(paid_tmp):
    """is_blacklisted=True from classifier wins over review (rule 1)."""
    cls = _make_classification(is_blacklisted=True)
    action = decision.decide_action(cls, _StubCp(), user_message="please review this")
    assert action.state == "decline"


def test_decide_action_review_skipped_when_stakes_low_under_default(paid_tmp):
    """Default min_stakes=medium so a low-stakes review hint does NOT trigger."""
    cls = _make_classification(stakes="low", confidence=0.9)
    action = decision.decide_action(cls, _StubCp(), user_message="please review this")
    # Falls through; low+high-conf+in-scope → direct.
    assert action.state == "direct"


def test_decide_action_review_off_disables_handoff(paid_tmp, monkeypatch):
    """auto_trigger_min_stakes='off' → review never fires automatically."""
    settings_path = paid_tmp / "settings.json"
    settings_path.write_text(
        json.dumps({"review": {"auto_trigger_min_stakes": "off"}})
    )
    cls = _make_classification(stakes="high")
    action = decision.decide_action(cls, _StubCp(), user_message="please review this")
    # Falls through — high stakes + in_scope → request, NOT review.
    assert action.state == "request"


def test_decide_action_review_low_threshold_lets_low_stakes_through(
    paid_tmp, monkeypatch
):
    settings_path = paid_tmp / "settings.json"
    settings_path.write_text(
        json.dumps({"review": {"auto_trigger_min_stakes": "low"}})
    )
    cls = _make_classification(stakes="low", confidence=0.9)
    action = decision.decide_action(cls, _StubCp(), user_message="please review this")
    assert action.state == "review"


def test_action_states_constant_includes_review():
    assert "review" in decision.ACTION_STATES


# ==========================================================================
# R-B5 · Identity active_review_session helpers
# ==========================================================================


def test_counterparty_default_review_fields(paid_tmp):
    cp = identity.ensure_counterparty("telegram", "12345", "Junior J")
    assert cp.active_review_session == ""
    assert cp.review_history == []


def test_set_active_review_session_persists(paid_tmp):
    cp = identity.ensure_counterparty("telegram", "12345", "J")
    identity.set_active_review_session(cp, "sess-abc")
    reloaded = identity.load_counterparty("telegram", "12345")
    assert reloaded is not None
    assert reloaded.active_review_session == "sess-abc"


def test_set_active_review_session_conflict_blocked(paid_tmp):
    cp = identity.ensure_counterparty("telegram", "12345", "J")
    identity.set_active_review_session(cp, "sess-1")
    with pytest.raises(identity.ReviewSessionConflict):
        identity.set_active_review_session(cp, "sess-2")


def test_set_active_review_session_replace_overrides_conflict(paid_tmp):
    cp = identity.ensure_counterparty("telegram", "12345", "J")
    identity.set_active_review_session(cp, "sess-1")
    identity.set_active_review_session(cp, "sess-2", replace=True)
    reloaded = identity.load_counterparty("telegram", "12345")
    assert reloaded.active_review_session == "sess-2"


def test_clear_active_review_session_archives_and_stamps_closed_at(paid_tmp):
    cp = identity.ensure_counterparty("telegram", "12345", "J")
    identity.set_active_review_session(cp, "sess-1")
    identity.clear_active_review_session(
        cp, archive={"sid": "sess-1", "subject": "Q3 budget", "verdict": "READY"}
    )
    reloaded = identity.load_counterparty("telegram", "12345")
    assert reloaded.active_review_session == ""
    assert len(reloaded.review_history) == 1
    rec = reloaded.review_history[0]
    assert rec["sid"] == "sess-1"
    assert rec["verdict"] == "READY"
    # closed_at auto-stamped when caller didn't supply.
    assert "closed_at" in rec and rec["closed_at"]


def test_review_history_rotates_at_max(paid_tmp):
    cp = identity.ensure_counterparty("telegram", "12345", "J")
    for i in range(25):
        identity.set_active_review_session(cp, f"s{i}", replace=True)
        identity.clear_active_review_session(cp, archive={"sid": f"s{i}"})
    reloaded = identity.load_counterparty("telegram", "12345")
    # Cap at _REVIEW_HISTORY_MAX = 20; oldest should be rotated out.
    assert len(reloaded.review_history) == 20
    assert reloaded.review_history[0]["sid"] == "s5"
    assert reloaded.review_history[-1]["sid"] == "s24"


def test_load_counterparty_backward_compat_v1_profile(paid_tmp):
    """A v1 profile.json (no review fields) must load without error."""
    cp_dir = paid_tmp / "counterparties" / "telegram_99"
    cp_dir.mkdir(parents=True)
    (cp_dir / "profile.json").write_text(
        json.dumps(
            {
                "cp_id": "telegram_99",
                "platform": "telegram",
                "user_id": "99",
                "display_name": "Old Cp",
                "role": "junior",
                "topics_allowed": ["logistics"],
                "topics_always_escalate": ["equity"],
                "web_search_allowed": True,
                "notes": "",
            }
        )
    )
    cp = identity.load_counterparty("telegram", "99")
    assert cp is not None
    assert cp.active_review_session == ""
    assert cp.review_history == []


# ==========================================================================
# R-B2 · render_options_block + send_dm options_block plumbing
# ==========================================================================


def test_render_options_block_basic():
    out = hermes_io.render_options_block(
        [
            {"key": "a", "label": "accept"},
            {"key": "pass", "label": "skip"},
            {"key": "custom"},
        ]
    )
    assert out == "(a) accept\n(pass) skip\n(custom)"


def test_render_options_block_empty_returns_empty_string():
    assert hermes_io.render_options_block(None) == ""
    assert hermes_io.render_options_block([]) == ""
    assert hermes_io.render_options_block([{"label": "no key"}]) == ""


def test_render_options_block_skips_malformed_entries():
    out = hermes_io.render_options_block(
        [{"key": "a", "label": "ok"}, "not-a-dict", {"key": ""}, None]
    )
    assert out == "(a) ok"


def test_send_dm_appends_options_block_in_queue_fallback(paid_tmp):
    """When send fails and falls back to queue, the queued message MUST
    include the rendered options so an owner doing manual delivery sees
    the same body the recipient would have seen."""
    result = hermes_io.send_dm(
        platform="telegram",
        user_id="999",
        message="finding 1: tighten the ask",
        options_block=[
            {"key": "a", "label": "accept"},
            {"key": "pass", "label": "skip"},
        ],
    )
    # No live gateway in tests → falls back to queue.
    assert result["ok"] is False
    assert "queued" in result
    queue_path = paid_tmp / "outbound_queue.jsonl"
    line = queue_path.read_text().strip().splitlines()[-1]
    rec = json.loads(line)
    assert "(a) accept" in rec["message"]
    assert "(pass) skip" in rec["message"]
    # Original body still present.
    assert "finding 1: tighten the ask" in rec["message"]


def test_send_dm_no_options_block_unchanged_message(paid_tmp):
    result = hermes_io.send_dm(
        platform="telegram", user_id="999", message="plain reply"
    )
    assert result["ok"] is False
    queue_path = paid_tmp / "outbound_queue.jsonl"
    rec = json.loads(queue_path.read_text().strip().splitlines()[-1])
    assert rec["message"] == "plain reply"
