#!/usr/bin/env python3
"""Manual smoke test for the W2/W3 unblock batch.

Simulates a hermes pre_llm_call / post_llm_call / _alert_owner round-trip
without touching a real IM or LLM. Each PHASE prints OK / FAIL with a
reason; final exit status is 0 only if all phases pass.

Run:
    python3 bin/manual_smoke.py
    python3 bin/manual_smoke.py --paid-dir /tmp/paid-smoke   # explicit isolation

Exits 0 on full pass, 1 on any failure. Safe to run repeatedly — uses a
fresh tmpdir per run so PAID state stays clean.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Make `paid` package importable.
sys.path.insert(0, str(REPO_ROOT))

# --------------------------------------------------------------------------
# Tiny test harness — no pytest dep
# --------------------------------------------------------------------------

_RESULTS: list[tuple[str, bool, str]] = []  # (label, passed, detail)


def _check(label: str, condition: bool, detail: str = "") -> None:
    _RESULTS.append((label, bool(condition), detail))
    mark = "✓" if condition else "✗"
    print(f"  {mark} {label}" + (f" — {detail}" if detail else ""))


def _phase(name: str) -> None:
    print(f"\n=== {name} ===")


# --------------------------------------------------------------------------
# Stubs — let us run without a real counterparty / live LLM
# --------------------------------------------------------------------------


class _StubCp:
    display_name = "Smoke Junior"
    role = "junior"
    topics_allowed = ["logistics", "scheduling"]
    topics_always_escalate = ["equity", "salary"]


# --------------------------------------------------------------------------
# PHASE A — review-skill integration deps (R-B1~B5)
# --------------------------------------------------------------------------


def phase_a_review_integration(paid_dir: Path) -> None:
    _phase("PHASE A · review-skill integration deps (R-B1~B5)")
    from paid import classifier, decision, hermes_io, identity, storage
    storage.PAID_DIR = paid_dir

    # R-B3 — Classification fields
    cls = classifier.Classification()
    _check("R-B3 Classification.needs_review default False",
           cls.needs_review is False)
    _check("R-B3 Classification.review_subject_hints default []",
           cls.review_subject_hints == [])
    _check("R-B3 prompt schema mentions needs_review",
           "needs_review" in classifier._JSON_SCHEMA_DESCRIPTION)
    _check("R-B3 prompt schema explains Review-trigger rules",
           "Review-trigger rules" in classifier._JSON_SCHEMA_DESCRIPTION)

    # R-B3 — parser handles new fields + clamps to 4 hints
    raw = json.dumps({
        "topic": "Q3 budget", "stakes": "high", "in_scope": True,
        "is_blacklisted": False, "confidence": 0.8,
        "needs_review": True,
        "review_subject_hints": ["a", "b", "c", "d", "e", "f"],
    })
    parsed = classifier._parse_classification(raw)
    _check("R-B3 parser reads needs_review", parsed.needs_review is True)
    _check("R-B3 parser clamps hints at 4", len(parsed.review_subject_hints) == 4,
           f"got {len(parsed.review_subject_hints)}")

    # R-B3 — old LLM that omits new fields: must NOT crash
    parsed = classifier._parse_classification(json.dumps({"topic": "x"}))
    _check("R-B3 missing-field tolerance",
           parsed.needs_review is False and parsed.review_subject_hints == [])

    # R-B4 — decision states
    _check("R-B4 ACTION_STATES contains 'review'",
           "review" in decision.ACTION_STATES,
           f"states={decision.ACTION_STATES}")

    # 1) needs_review + high → review
    cls = classifier.Classification(
        needs_review=True, stakes="high", in_scope=True, confidence=0.8,
    )
    a = decision.decide_action(cls, _StubCp(), "review my Q3 plan")
    _check("R-B4 high-stakes review-needing → review",
           a.state == "review", f"reason={a.reason}")

    # 2) needs_review + low + default min=medium → does NOT trigger review
    cls = classifier.Classification(
        needs_review=True, stakes="low", in_scope=True, confidence=0.9,
    )
    a = decision.decide_action(cls, _StubCp(), "small thing")
    _check("R-B4 low-stakes review-needing → falls through (default min=medium)",
           a.state == "direct", f"got state={a.state}")

    # 3) blacklist keyword always wins
    cls = classifier.Classification(needs_review=True, stakes="high", in_scope=True)
    a = decision.decide_action(cls, _StubCp(), "review my equity vesting draft")
    _check("R-B4 blacklist keyword wins over review",
           a.state == "request" and "hard blacklist" in a.reason)

    # 4) settings off disables
    settings_path = paid_dir / "settings.json"
    settings_path.write_text(json.dumps(
        {"review": {"auto_trigger_min_stakes": "off"}}
    ))
    cls = classifier.Classification(needs_review=True, stakes="high", in_scope=True)
    a = decision.decide_action(cls, _StubCp(), "review my plan")
    # With review off, the high-stakes branch (rule 3) takes over → request.
    # The KEY check is that 'needs_review' did NOT win — i.e. state != 'review'.
    _check("R-B4 settings 'off' disables auto-trigger",
           a.state != "review",
           f"got state={a.state} reason={a.reason}")
    settings_path.unlink()

    # 5) settings 'low' lets low-stakes through
    settings_path.write_text(json.dumps(
        {"review": {"auto_trigger_min_stakes": "low"}}
    ))
    cls = classifier.Classification(
        needs_review=True, stakes="low", in_scope=True, confidence=0.9,
    )
    a = decision.decide_action(cls, _StubCp(), "small")
    _check("R-B4 settings 'low' allows low-stakes review",
           a.state == "review")
    settings_path.unlink()

    _check("R-B4 is_review_state() helper",
           decision.is_review_state(decision.Action(state="review", reason=""))
           and not decision.is_review_state(decision.Action(state="direct", reason="")))

    # R-B5 — identity helpers
    cp = identity.ensure_counterparty("telegram", "smoke", "Smoke J")
    _check("R-B5 Counterparty default active_review_session empty",
           cp.active_review_session == "")
    _check("R-B5 Counterparty default review_history empty",
           cp.review_history == [])

    identity.set_active_review_session(cp, "sess-1")
    reloaded = identity.load_counterparty("telegram", "smoke")
    _check("R-B5 set_active_review_session persists",
           reloaded.active_review_session == "sess-1")

    try:
        identity.set_active_review_session(cp, "sess-2")
        _check("R-B5 conflict raises ReviewSessionConflict", False, "did not raise")
    except identity.ReviewSessionConflict:
        _check("R-B5 conflict raises ReviewSessionConflict", True)

    identity.set_active_review_session(cp, "sess-2", replace=True)
    _check("R-B5 replace=True overrides conflict",
           identity.load_counterparty("telegram", "smoke").active_review_session
           == "sess-2")

    identity.clear_active_review_session(
        cp, archive={"sid": "sess-2", "subject": "Q3", "verdict": "READY"}
    )
    final = identity.load_counterparty("telegram", "smoke")
    _check("R-B5 clear empties active",
           final.active_review_session == "")
    _check("R-B5 clear appends history with closed_at",
           len(final.review_history) == 1
           and "closed_at" in final.review_history[0])

    # R-B5 — backward compat with v1 profile.json (missing new fields)
    cp_dir = paid_dir / "counterparties" / "telegram_v1cp"
    cp_dir.mkdir(parents=True)
    (cp_dir / "profile.json").write_text(json.dumps({
        "cp_id": "telegram_v1cp", "platform": "telegram",
        "user_id": "v1cp", "display_name": "Old Cp", "role": "junior",
        "topics_allowed": [], "topics_always_escalate": [],
        "web_search_allowed": True, "notes": "",
    }))
    v1cp = identity.load_counterparty("telegram", "v1cp")
    _check("R-B5 v1 profile loads with default new fields",
           v1cp is not None
           and v1cp.active_review_session == ""
           and v1cp.review_history == [])

    # R-B2 — render_options_block + send_dm options_block
    out = hermes_io.render_options_block([
        {"key": "a", "label": "accept"},
        {"key": "pass", "label": "skip"},
        {"key": "custom"},
    ])
    _check("R-B2 render_options_block format",
           out == "(a) accept\n(pass) skip\n(custom)", f"got: {out!r}")
    _check("R-B2 render_options_block None → empty",
           hermes_io.render_options_block(None) == "")

    result = hermes_io.send_dm(
        "telegram", "999", "finding 1: 改 ask",
        options_block=[
            {"key": "a", "label": "accept"},
            {"key": "pass", "label": "skip"},
        ],
    )
    _check("R-B2 send_dm queued (no live gateway in smoke)",
           result.get("ok") is False and "queued" in result)
    queue = paid_dir / "outbound_queue.jsonl"
    last = json.loads(queue.read_text().splitlines()[-1])
    _check("R-B2 queued message contains options block",
           "(a) accept" in last["message"] and "(pass) skip" in last["message"])
    _check("R-B2 queued message preserves original body",
           "finding 1" in last["message"])


# --------------------------------------------------------------------------
# PHASE B — Safety L4c / L4d
# --------------------------------------------------------------------------


def phase_b_safety(paid_dir: Path) -> None:
    _phase("PHASE B · safety L4c (LLM post-check) + L4d (source attribution)")
    from paid import safety, hermes_io, storage
    storage.PAID_DIR = paid_dir

    # L4d — source attribution
    cases = [
        ("DAU jumped to 12,500 last week.",
         True,  "无源大数字应 flag"),
        ("Per Mixpanel 2026-04, DAU was 12,500.",
         False, "有 'Per' attribution 应放过"),
        ("据财务备忘 2026-04，预算 $240,000。",
         False, "中文 '据财' attribution"),
        ("根据公告，去年 GMV 1.5 亿。",
         False, "中文 '根据'"),
        ("2024 launch slipped to 2025.",
         False, "年份不算 claim"),
        ("We have 12 features.",
         False, "小数字 (<1000) 跳过"),
        ("Q3 budget is $240,000.",
         True, "无源货币应 flag"),
    ]
    for text, expected, label in cases:
        hit, _ = safety.detect_unsourced_claims(text)
        _check(f"L4d {label}", hit == expected, f"got hit={hit}")

    # L4c — opt-in, default off
    s, c = safety.detect_via_llm("any draft")
    _check("L4c default off", s is False and c == [])

    # L4c — force-on with a fake LLM
    original = hermes_io.call_llm
    try:
        hermes_io.call_llm = lambda **kw: (
            '{"suspicious": true, '
            '"concerns": ["impersonates owner", "fabricates a date"]}'
        )
        s, c = safety.detect_via_llm(
            "I am Jimmy. I will personally meet you Monday 3pm.",
            persona="(persona)", sop_excerpt="(sop)",
            enabled_override=True,
        )
        _check("L4c force-on flags suspicious", s is True
               and "impersonates owner" in c)

        # Settings opt-in
        settings_path = paid_dir / "settings.json"
        settings_path.write_text(
            json.dumps({"safety": {"l4c_enabled": True}})
        )
        hermes_io.call_llm = lambda **kw: '{"suspicious": false, "concerns": []}'
        s, _ = safety.detect_via_llm("draft")
        _check("L4c settings opt-in path runs LLM", s is False)
        settings_path.unlink()

        # Fail open on LLM error
        def _boom(**kw):
            raise RuntimeError("LLM down")
        hermes_io.call_llm = _boom
        s, c = safety.detect_via_llm("draft", enabled_override=True)
        _check("L4c fails open on LLM error",
               s is False and c == [])
    finally:
        hermes_io.call_llm = original

    # check_output combined
    cp_dir = paid_dir / "counterparties" / "telegram_other"
    cp_dir.mkdir(parents=True, exist_ok=True)
    (cp_dir / "profile.json").write_text(json.dumps({
        "cp_id": "telegram_other", "platform": "telegram",
        "user_id": "other", "display_name": "Bob", "role": "junior",
        "topics_allowed": [], "topics_always_escalate": [],
        "web_search_allowed": True, "notes": "",
    }))
    res = safety.check_output(
        "Bob says DAU was 12,500.", "telegram_self",
    )
    _check("check_output combined: name leak + unsourced",
           res["ok"] is False
           and "Bob" in res.get("name_leakage", [])
           and any("12,500" in c for c in res.get("unsourced_claims", [])))

    res = safety.check_output("Per memo, DAU was 12,500.", "telegram_self",
                              run_l4d=False)
    _check("check_output run_l4d=False omits unsourced",
           res["ok"] is True and "unsourced_claims" not in res)


# --------------------------------------------------------------------------
# PHASE C — _alert_owner IM channel + debounce
# --------------------------------------------------------------------------


def phase_c_alert_owner(paid_dir: Path) -> None:
    _phase("PHASE C · _alert_owner IM channel + debounce")
    from paid import storage
    storage.PAID_DIR = paid_dir

    # Seed owner
    (paid_dir / "owner.json").write_text(json.dumps({
        "owner_id": "owner_smoke",
        "identities": [{"platform": "telegram", "user_id": "11111"}],
        "name": "Smoke Owner",
    }))

    # Load plugin entry under a stable name
    spec = importlib.util.spec_from_file_location(
        "paid_smoke_plug", REPO_ROOT / "__init__.py"
    )
    plug = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plug)
    plug._ALERT_LAST_SENT.clear()

    sent: list[dict] = []
    plug.hermes_io.send_dm = lambda p, u, m, **kw: (
        sent.append({"plat": p, "uid": u, "msg": m})
        or {"ok": True, "msg_id": "stub"}
    )

    # 1) basic IM send
    plug._alert_owner("smoke_test_reason_1", "stack line 1\nstack line 2")
    _check("alert: 1 IM after first call", len(sent) == 1)
    _check("alert: IM body has reason",
           "smoke_test_reason_1" in sent[0]["msg"])
    _check("alert: IM body has fatal_alerts.jsonl pointer",
           "fatal_alerts.jsonl" in sent[0]["msg"])

    # 2) debounce — same reason 5 times → still 1 IM total
    for _ in range(5):
        plug._alert_owner("smoke_test_reason_1", "x")
    _check("alert: same-reason debounced (5 retries → 1 IM)",
           len(sent) == 1, f"sent={len(sent)}")

    # 3) different reason → new IM
    plug._alert_owner("smoke_test_reason_2", "different")
    _check("alert: different reason fires new IM", len(sent) == 2)

    # 4) fatal_alerts.jsonl captures every entry (no debounce)
    fatal_lines = (
        (paid_dir / "fatal_alerts.jsonl").read_text().splitlines()
    )
    _check("alert: fatal_alerts.jsonl captured all 7 entries",
           len(fatal_lines) == 7, f"got {len(fatal_lines)}")

    # 5a) Lark: routable ou_ identity wins over FEISHU_HOME_CHANNEL (v1.2.4 fix)
    import os as _os
    _os.environ["FEISHU_HOME_CHANNEL"] = "oc_wrong_chat_123"
    (paid_dir / "owner.json").write_text(json.dumps({
        "owner_id": "owner_smoke",
        "identities": [{"platform": "feishu", "user_id": "ou_x"}],
        "name": "Smoke Owner",
    }))
    sent.clear()
    plug._ALERT_LAST_SENT.clear()
    plug._alert_owner("lark_smoke_ou", "x")
    _check("alert: Lark routable ou_ wins over FEISHU_HOME_CHANNEL",
           len(sent) == 1 and sent[0]["uid"] == "ou_x",
           f"got uid={sent[0]['uid'] if sent else 'none'}")

    # 5b) Lark: bare-hex identity falls back to FEISHU_HOME_CHANNEL (legacy /sethome)
    (paid_dir / "owner.json").write_text(json.dumps({
        "owner_id": "owner_smoke",
        "identities": [{"platform": "feishu", "user_id": "8ea86e3b"}],
        "name": "Smoke Owner",
    }))
    sent.clear()
    plug._ALERT_LAST_SENT.clear()
    plug._alert_owner("lark_smoke_legacy", "x")
    _check("alert: Lark bare-hex falls back to FEISHU_HOME_CHANNEL",
           len(sent) == 1 and sent[0]["uid"] == "oc_wrong_chat_123",
           f"got uid={sent[0]['uid'] if sent else 'none'}")
    _os.environ.pop("FEISHU_HOME_CHANNEL", None)

    # 6) send_dm exception swallowed
    def _explode(*a, **kw):
        raise RuntimeError("simulated gateway death")
    plug.hermes_io.send_dm = _explode
    plug._ALERT_LAST_SENT.clear()
    try:
        plug._alert_owner("explosive_smoke", "x")
        _check("alert: exception in send_dm is swallowed", True)
    except Exception as e:
        _check("alert: exception in send_dm is swallowed", False, str(e))


# --------------------------------------------------------------------------
# PHASE D — Multi-platform v0.1 (Slack + Telegram dispatch)
# --------------------------------------------------------------------------


def phase_d_multiplatform(paid_dir: Path) -> None:
    _phase("PHASE D · multi-platform v0.1 (TG/Slack card dispatch)")
    import importlib
    import json as _json
    from paid import (
        approval, card_formatters, card_spec, identity, storage,
    )
    # Phase C monkey-patched hermes_io.send_dm to raise; reload to get a clean
    # module-level state for Phase D (other tests may have other patches too).
    from paid import hermes_io as _hermes_io_mod
    importlib.reload(_hermes_io_mod)
    hermes_io = _hermes_io_mod
    storage.PAID_DIR = paid_dir

    # D1 — ApprovalCardSpec → format_telegram payload structurally OK
    spec = card_spec.ApprovalCardSpec(
        request_id="d1-rid",
        junior_name="Smoke J",
        junior_platform="lark",
        junior_msg="please approve my Q3 plan draft",
        topic="planning",
        confidence=0.65,
        stakes="medium",
        draft="On it — replying within an hour.",
        has_draft=True,
        timeout_min=30,
        instructions="reply /paid-approve d1-rid",
    )
    tg_payload = card_formatters.format_telegram(spec)
    _check("D1 format_telegram returns text+reply_markup",
           "text" in tg_payload and "reply_markup" in tg_payload)
    _check("D1 TG keyboard has 3 buttons (with draft)",
           len([b for row in tg_payload["reply_markup"]["inline_keyboard"] for b in row]) == 3)
    _check("D1 TG text contains request_id",
           "d1-rid" in tg_payload["text"])

    # D2 — ApprovalCardSpec → format_slack returns valid Block Kit
    sl_payload = card_formatters.format_slack(spec)
    _check("D2 format_slack returns blocks+text",
           isinstance(sl_payload.get("blocks"), list)
           and len(sl_payload.get("text", "")) > 0)
    _check("D2 Slack actions block has 3 buttons",
           any(b["type"] == "actions" and len(b["elements"]) == 3
               for b in sl_payload["blocks"]))

    # D3 — send_dm(platform=telegram, options_block) routes to send_telegram_card
    captured = {}
    original_tg = hermes_io.send_telegram_card
    def fake_tg(*args, **kwargs):
        captured["tg"] = (args, kwargs)
        return {"ok": True, "msg_id": "tg-d3", "platform": "telegram"}
    hermes_io.send_telegram_card = fake_tg
    try:
        result = hermes_io.send_dm(
            "telegram", "12345", "smoke msg",
            options_block=[
                {"key": "a", "label": "accept"},
                {"key": "pass", "label": "skip"},
            ],
        )
        _check("D3 send_dm(tg, options) → send_telegram_card called",
               "tg" in captured and result["ok"] is True)
        if "tg" in captured:
            kb = captured["tg"][1].get("keyboard")
            _check("D3 keyboard has 2 rows (one per option)",
                   isinstance(kb, list) and len(kb) == 2)
    finally:
        hermes_io.send_telegram_card = original_tg

    # D4 — send_dm(platform=slack, options_block) routes to send_slack_block
    captured.clear()
    original_sl = hermes_io.send_slack_block
    def fake_sl(*args, **kwargs):
        captured["sl"] = (args, kwargs)
        return {"ok": True, "msg_id": "sl-d4", "platform": "slack"}
    hermes_io.send_slack_block = fake_sl
    try:
        result = hermes_io.send_dm(
            "slack", "D12345", "smoke msg",
            options_block=[{"key": "a", "label": "accept"}],
        )
        _check("D4 send_dm(slack, options) → send_slack_block called",
               "sl" in captured and result["ok"] is True)
        if "sl" in captured:
            blocks = captured["sl"][0][1]
            _check("D4 blocks include section + actions",
                   any(b["type"] == "section" for b in blocks)
                   and any(b["type"] == "actions" for b in blocks))
    finally:
        hermes_io.send_slack_block = original_sl

    # D5 — owner v2 preferred_platform=slack → _notify_owner_about_request
    # uses slack path. Need to load plugin entry for this assertion.
    (paid_dir / "owner.json").write_text(_json.dumps({
        "schema_version": 2, "owner_id": "smoke", "name": "Smoke",
        "preferred_platform": "slack",
        "identities": [
            {"platform": "telegram", "user_id": "1"},
            {"platform": "slack", "user_id": "U1", "home_chat_id": "D1"},
        ],
    }))
    spec_obj = importlib.util.spec_from_file_location(
        "paid_smoke_plug_d", REPO_ROOT / "__init__.py"
    )
    plug = importlib.util.module_from_spec(spec_obj)
    spec_obj.loader.exec_module(plug)
    sl_calls: list = []
    plug.hermes_io.send_slack_block = lambda *a, **k: (
        sl_calls.append((a, k)) or {"ok": True, "msg_id": "x"}
    )
    plug.hermes_io.send_lark_card = lambda *a, **k: (_ for _ in ()).throw(AssertionError("D5: lark used"))
    plug.hermes_io.send_telegram_card = lambda *a, **k: (_ for _ in ()).throw(AssertionError("D5: tg used"))
    plug.hermes_io.send_dm = lambda *a, **k: (_ for _ in ()).throw(AssertionError("D5: plain used"))
    req = approval.PendingApproval(
        request_id="d5-rid", ts_created=1.0,
        counterparty_id="cp", counterparty_platform="lark",
        counterparty_user_id="ou", counterparty_display="J",
        junior_session_id="s", junior_question="?",
        draft_answer="ok", topic="x", stakes="medium", confidence=0.7,
    )
    plug._notify_owner_about_request(req)
    _check("D5 preferred_platform=slack → send_slack_block invoked",
           len(sl_calls) == 1)
    if sl_calls:
        _check("D5 Slack target uses home_chat_id=D1 (not user_id=U1)",
               sl_calls[0][0][0] == "D1")

    # D6 — migration script upgrade_payload pure function smoke
    from importlib import util as _ilu
    mig_spec = _ilu.spec_from_file_location(
        "paid_mig_d6", REPO_ROOT / "bin" / "migrate_owner_v1_to_v2.py"
    )
    mig = _ilu.module_from_spec(mig_spec)
    mig_spec.loader.exec_module(mig)
    new, changes = mig.upgrade_payload({
        "owner_id": "x",
        "identities": [{"platform": "telegram", "user_id": "1"}],
    })
    _check("D6 migration: schema_version stamped", new["schema_version"] == 2)
    _check("D6 migration: home_chat_id backfilled = user_id",
           new["identities"][0]["home_chat_id"] == "1")
    _check("D6 migration: preferred_platform = first identity",
           new["preferred_platform"] == "telegram")
    _check("D6 migration: change log non-empty", len(changes) >= 3)


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paid-dir", help="explicit PAID_DIR (default: tmpdir)")
    args = parser.parse_args()

    paid_dir = (
        Path(args.paid_dir) if args.paid_dir else Path(tempfile.mkdtemp(prefix="paid-smoke-"))
    )
    paid_dir.mkdir(parents=True, exist_ok=True)
    print(f"PAID_DIR for this smoke run: {paid_dir}")
    print(f"REPO_ROOT:                   {REPO_ROOT}")

    try:
        phase_a_review_integration(paid_dir)
    except Exception as e:
        _check(f"PHASE A unexpected exception: {type(e).__name__}", False, str(e))

    # Each phase uses a fresh sub-dir to avoid state bleed.
    phase_b_dir = paid_dir / "phase_b"
    phase_b_dir.mkdir(exist_ok=True)
    try:
        phase_b_safety(phase_b_dir)
    except Exception as e:
        _check(f"PHASE B unexpected exception: {type(e).__name__}", False, str(e))

    phase_c_dir = paid_dir / "phase_c"
    phase_c_dir.mkdir(exist_ok=True)
    try:
        phase_c_alert_owner(phase_c_dir)
    except Exception as e:
        _check(f"PHASE C unexpected exception: {type(e).__name__}", False, str(e))

    phase_d_dir = paid_dir / "phase_d"
    phase_d_dir.mkdir(exist_ok=True)
    try:
        phase_d_multiplatform(phase_d_dir)
    except Exception as e:
        _check(f"PHASE D unexpected exception: {type(e).__name__}", False, str(e))

    # Summary
    total = len(_RESULTS)
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    failed = total - passed
    print()
    print("=" * 60)
    print(f"SMOKE RESULT: {passed}/{total} passed, {failed} failed")
    print("=" * 60)
    if failed:
        print("\nFailed checks:")
        for label, ok, detail in _RESULTS:
            if not ok:
                print(f"  ✗ {label}" + (f" — {detail}" if detail else ""))
        return 1
    print("ALL GREEN ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
