"""Tests for paid.card_formatters — Lark / Telegram / Slack / plain.

Each formatter consumes the same ApprovalCardSpec and emits its own
platform-native payload. Tests cover:
  - Output shape (right top-level keys / list types)
  - Required content present (request id, junior name, draft text)
  - Conditional rendering (no-draft → no Approve button, sensitive notice)
  - Length / safety properties (TG MarkdownV1 escape, Slack notification text)
"""

from __future__ import annotations

import json

from paid import card_formatters, card_spec


def _make_spec(**overrides):
    base = dict(
        request_id="abc123",
        junior_name="Evie Wang",
        junior_platform="feishu",
        junior_msg="Jimmy 我整理了 Q3 营销预算草稿,看下能不能批",
        topic="onboarding",
        confidence=0.65,
        stakes="medium",
        draft="让我先转给 Jimmy 看一下数字, 应该今天给你回复.",
        has_draft=True,
        timeout_min=30,
        instructions="操作: 回 /paid-approve abc123 或 /paid-reject abc123",
    )
    base.update(overrides)
    return card_spec.ApprovalCardSpec(**base)


# ============================================================================
# Lark formatter
# ============================================================================


def test_lark_card_shape():
    card = card_formatters.format_lark(_make_spec())
    assert "config" in card
    assert "header" in card
    assert "elements" in card
    assert isinstance(card["elements"], list)
    # Card must serialize cleanly (no datetime / non-JSON types leak)
    json.dumps(card, ensure_ascii=False)


def test_lark_card_with_draft_shows_approve_button():
    card = card_formatters.format_lark(_make_spec(has_draft=True))
    actions = [el for el in card["elements"] if el.get("tag") == "action"]
    assert len(actions) == 1
    button_texts = [b["text"]["content"] for b in actions[0]["actions"]]
    assert any("Approve" in t for t in button_texts)
    assert any("Reject" in t for t in button_texts)


def test_lark_card_no_draft_renders_all_three_buttons_v1_2_2():
    """All three buttons render on no-draft cards (consistent visual model
    across cards). v1.4.0 update: the note now points the operator at the
    inline-prompt flow — click ✅ Approve and PAID will ask you to type
    your reply (rather than telling them to use /paid-approve slash)."""
    card = card_formatters.format_lark(_make_spec(has_draft=False, draft=""))
    actions = [el for el in card["elements"] if el.get("tag") == "action"]
    button_texts = [b["text"]["content"] for b in actions[0]["actions"]]
    assert any("Approve" in t for t in button_texts)
    assert any("Reply" in t for t in button_texts)
    assert any("Reject" in t for t in button_texts)
    # Note explains the revised v1.4.0-r2 flow: ✅ sends default agreement,
    # ✏️ Reply for custom answer, ❌ Reject for deflection.
    note = next(el for el in card["elements"] if el.get("tag") == "note")
    note_text = note["elements"][0]["content"]
    assert "draft a reply" in note_text.lower() or "couldn't draft" in note_text.lower()
    assert "default agreement" in note_text or "可以的" in note_text
    assert "Reply" in note_text  # ✏️ Reply path mentioned


def test_lark_card_no_draft_uses_red_header():
    card = card_formatters.format_lark(_make_spec(has_draft=False, draft=""))
    assert card["header"]["template"] == "red"


def test_lark_card_uses_paid_action_key_not_hermes_action():
    """Critical: hermes intercepts buttons with `hermes_action` value as
    its own approval flow. PAID must use `paid_action` to opt out."""
    card = card_formatters.format_lark(_make_spec())
    actions = [el for el in card["elements"] if el.get("tag") == "action"]
    for btn in actions[0]["actions"]:
        assert "paid_action" in btn["value"]
        assert "hermes_action" not in btn["value"]


def test_lark_card_includes_request_id_in_header():
    card = card_formatters.format_lark(_make_spec(request_id="xyz789"))
    assert "xyz789" in card["header"]["title"]["content"]


# ============================================================================
# Telegram formatter
# ============================================================================


def test_telegram_payload_shape():
    payload = card_formatters.format_telegram(_make_spec())
    assert "text" in payload
    assert "reply_markup" in payload
    assert "parse_mode" in payload
    assert payload["parse_mode"] in ("Markdown", "MarkdownV2", "HTML")
    assert "inline_keyboard" in payload["reply_markup"]
    assert isinstance(payload["reply_markup"]["inline_keyboard"], list)


def test_telegram_text_under_4096():
    """TG hard limit is 4096 chars; spec already truncates body to ~600+600
    so even with chrome we're well under."""
    payload = card_formatters.format_telegram(_make_spec())
    assert len(payload["text"]) < 4096


def test_telegram_text_includes_request_id_and_junior():
    payload = card_formatters.format_telegram(_make_spec())
    assert "abc123" in payload["text"]
    assert "Evie" in payload["text"]


def test_telegram_text_includes_instructions_footer():
    payload = card_formatters.format_telegram(
        _make_spec(instructions="please reply /paid-approve abc123")
    )
    assert "/paid-approve abc123" in payload["text"]


def test_telegram_keyboard_with_draft_has_three_buttons():
    payload = card_formatters.format_telegram(_make_spec(has_draft=True))
    rows = payload["reply_markup"]["inline_keyboard"]
    flat = [btn for row in rows for btn in row]
    texts = [b["text"] for b in flat]
    assert any("Approve" in t for t in texts)
    assert any("Reply" in t for t in texts)
    assert any("Reject" in t for t in texts)


def test_telegram_keyboard_no_draft_still_renders_all_three_buttons_v1_2_2():
    """v1.2.2 UX change: keep all 3 buttons even without a draft for visual
    consistency. Card text explicitly tells operator ✅ won't fire."""
    payload = card_formatters.format_telegram(_make_spec(has_draft=False, draft=""))
    rows = payload["reply_markup"]["inline_keyboard"]
    flat = [btn for row in rows for btn in row]
    texts = [b["text"] for b in flat]
    assert any("Approve" in t for t in texts)
    assert any("Reply" in t for t in texts)
    assert any("Reject" in t for t in texts)
    # Body text warns that ✅ won't send.
    assert "won't send" in payload["text"] or "won't" in payload["text"]
    assert "/paid-approve abc123" in payload["text"]


def test_telegram_callback_data_uses_paid_prefix():
    """Future v1.x callback wiring will key off paid_* prefix."""
    payload = card_formatters.format_telegram(_make_spec(request_id="rid42"))
    rows = payload["reply_markup"]["inline_keyboard"]
    for row in rows:
        for btn in row:
            cb = btn.get("callback_data", "")
            assert cb.startswith("paid_")
            assert ":rid42" in cb


def test_telegram_escapes_backticks_in_owner_content():
    """Backticks in junior_name / topic would unbalance MarkdownV1 inline code."""
    payload = card_formatters.format_telegram(
        _make_spec(junior_name="weird `backtick` name")
    )
    # Must not contain unescaped pair of backticks that would render as code
    assert "weird `backtick`" not in payload["text"]
    assert "weird \\`backtick\\`" in payload["text"]


# ============================================================================
# Slack formatter
# ============================================================================


def test_slack_payload_shape():
    payload = card_formatters.format_slack(_make_spec())
    assert "blocks" in payload
    assert "text" in payload
    assert isinstance(payload["blocks"], list)
    # Slack notification fallback text REQUIRED whenever blocks are sent.
    assert payload["text"].strip() != ""
    # Whole payload must JSON-serialize cleanly.
    json.dumps(payload, ensure_ascii=False)


def test_slack_blocks_under_50():
    """Slack hard limit is 50 blocks per message; we use ~7."""
    payload = card_formatters.format_slack(_make_spec())
    assert len(payload["blocks"]) < 50
    assert len(payload["blocks"]) >= 5  # at least header+section+actions+context


def test_slack_blocks_have_header():
    payload = card_formatters.format_slack(_make_spec(request_id="r9"))
    headers = [b for b in payload["blocks"] if b["type"] == "header"]
    assert len(headers) == 1
    assert "r9" in headers[0]["text"]["text"]


def test_slack_blocks_have_actions_with_three_buttons_when_draft():
    payload = card_formatters.format_slack(_make_spec(has_draft=True))
    actions = [b for b in payload["blocks"] if b["type"] == "actions"]
    assert len(actions) == 1
    elements = actions[0]["elements"]
    assert len(elements) == 3
    action_ids = [e["action_id"] for e in elements]
    assert "paid_approve" in action_ids
    assert "paid_reply" in action_ids
    assert "paid_reject" in action_ids


def test_slack_blocks_no_draft_still_renders_all_three_buttons_v1_2_2():
    """v1.2.2 UX change: 3 buttons even without a draft for consistency.
    The 'no draft' section block explicitly warns about ✅ being a no-op."""
    payload = card_formatters.format_slack(_make_spec(has_draft=False, draft=""))
    actions = [b for b in payload["blocks"] if b["type"] == "actions"]
    elements = actions[0]["elements"]
    assert len(elements) == 3
    action_ids = [e["action_id"] for e in elements]
    assert "paid_approve" in action_ids
    assert "paid_reply" in action_ids
    assert "paid_reject" in action_ids
    # The no-draft section warns about ✅ being inert.
    sections = [b for b in payload["blocks"] if b["type"] == "section"]
    no_draft_text = " ".join(s["text"]["text"] for s in sections if s.get("text"))
    assert "/paid-approve" in no_draft_text
    assert "won't" in no_draft_text or "no_draft" in no_draft_text.lower() or "none" in no_draft_text.lower()


def test_slack_action_value_is_request_id():
    """Slack actions carry the request_id in `value` so v1.x callback
    wiring can dispatch directly without parsing block context."""
    payload = card_formatters.format_slack(_make_spec(request_id="rid42"))
    actions = [b for b in payload["blocks"] if b["type"] == "actions"]
    for el in actions[0]["elements"]:
        assert el["value"] == "rid42"


def test_slack_context_includes_instructions():
    payload = card_formatters.format_slack(
        _make_spec(instructions="reply /paid-approve abc to confirm")
    )
    contexts = [b for b in payload["blocks"] if b["type"] == "context"]
    assert len(contexts) == 1
    text = contexts[0]["elements"][0]["text"]
    assert "/paid-approve abc" in text


def test_slack_notification_fallback_text_is_compact():
    """Notification text shows in mobile push and accessibility; must be
    one-line summary, not the whole card."""
    payload = card_formatters.format_slack(
        _make_spec(junior_msg="x" * 600, draft="y" * 600)
    )
    assert "\n" not in payload["text"]
    assert len(payload["text"]) < 200


# ============================================================================
# Plain text formatter
# ============================================================================


def test_plain_text_includes_essentials():
    out = card_formatters.format_plain(_make_spec())
    assert "abc123" in out
    assert "Evie Wang" in out
    assert "operating" not in out  # no untranslated dev string
    # Numbered shortcuts
    assert "1️⃣" in out and "2️⃣" in out and "3️⃣" in out
    # Slash command syntax
    assert "/paid-approve abc123" in out
    assert "/paid-reject abc123" in out


def test_plain_text_no_draft_marks_clearly():
    out = card_formatters.format_plain(_make_spec(has_draft=False, draft=""))
    assert "(no draft)" in out


def test_plain_text_includes_timeout():
    out = card_formatters.format_plain(_make_spec(timeout_min=15))
    assert "15 min" in out
