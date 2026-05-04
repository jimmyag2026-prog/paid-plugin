"""Module CF — platform-specific approval card formatters.

Each ``format_*(spec)`` takes the same ``ApprovalCardSpec`` and returns a
platform-native payload. Centralising them here keeps __init__.py focused
on hook glue and dispatch logic.

Conventions:
  - Buttons render on every platform. CLICK CALLBACK is NOT routed back
    to PAID on TG / Slack (hermes does not expose a plugin callback API
    — see design/08 §1). Buttons are visual; owner operates via slash
    commands. Lark's button click DOES round-trip via hermes's internal
    ``_handle_card_action_event`` → ``/card`` slash command.
  - Every card includes a footer with the owner-facing instructions
    (slash command syntax) — pilots discover the operation path even
    when buttons don't respond.
"""

from __future__ import annotations

from typing import Any

from .card_spec import ApprovalCardSpec


# ============================================================================
# Lark / Feishu — interactive card JSON
# ============================================================================


def format_lark(spec: ApprovalCardSpec) -> dict:
    """Build Lark interactive-card JSON.

    Buttons embed ``paid_action="approve" / "reject"`` inside ``value`` so
    we can detect "this is PAID's card, not hermes's tool-approval card"
    downstream. Hermes's adapter keys off the literal string
    ``hermes_action`` to mean "this is hermes's own approval", so we use a
    different key (``paid_action``) to avoid that branch and instead get
    routed to ``_handle_card_action_event`` → synthetic
    ``/card button {json}`` slash command our handler reads.
    """
    draft_for_display = (
        spec.draft if spec.has_draft
        else "(no draft — sensitive topic. Approve via /paid-approve "
             f"{spec.request_id} &lt;your reply text&gt;)"
    )

    # Only show ✅ Approve when there's a draft to send. Hard-blacklist
    # topics (salary / equity / etc) come through with empty drafts on
    # purpose — clicking ✅ would just bounce ("no draft" error).
    buttons: list[dict] = []
    if spec.has_draft:
        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "✅ Approve & send draft"},
            "type": "primary",
            "value": {"paid_action": "approve", "request_id": spec.request_id},
        })
    buttons.append({
        "tag": "button",
        "text": {"tag": "plain_text", "content": "❌ Reject"},
        "type": "danger",
        "value": {"paid_action": "reject", "request_id": spec.request_id},
    })

    note_content = (
        "Tip: to override the draft instead of sending it as-is, reply "
        f"/paid-approve {spec.request_id} &lt;your text&gt;"
        if spec.has_draft else
        f"This is a sensitive-topic request — no draft was generated. "
        f"To answer: /paid-approve {spec.request_id} &lt;your reply&gt;. "
        f"To deflect: tap ❌ Reject."
    )

    return {
        "config": {"wide_screen_mode": True, "enable_forward": False},
        "header": {
            "title": {"tag": "plain_text", "content": spec.header_title()},
            # Sensitive (no-draft) requests get a red header.
            "template": "blue" if spec.has_draft else "red",
        },
        "elements": [
            {
                "tag": "div",
                "fields": [
                    {"is_short": True, "text": {"tag": "lark_md",
                     "content": f"**From**\n{spec.junior_name}\n_{spec.junior_platform}_"}},
                    {"is_short": True, "text": {"tag": "lark_md",
                     "content": f"**Topic**\n{spec.topic}"}},
                    {"is_short": True, "text": {"tag": "lark_md",
                     "content": f"**Stakes**\n{spec.stakes_pill()}"}},
                    {"is_short": True, "text": {"tag": "lark_md",
                     "content": f"**Confidence**\n{spec.confidence_label()}"}},
                ],
            },
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md",
             "content": f"**Q (junior asked)**\n{spec.junior_msg}"}},
            {"tag": "div", "text": {"tag": "lark_md",
             "content": (
                 f"**Draft (junior will see this on approve)**\n{draft_for_display}"
                 if spec.has_draft else
                 f"**Draft**\n{draft_for_display}"
             )}},
            {"tag": "hr"},
            {"tag": "action", "actions": buttons},
            {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": note_content}],
            },
        ],
    }


# ============================================================================
# Telegram — text + InlineKeyboardMarkup
# ============================================================================


def _telegram_escape_markdown(text: str) -> str:
    """Light-touch MarkdownV1 sanitization — only escape backticks/asterisks
    that would unbalance the document. We deliberately don't go full
    MarkdownV2 (which requires escaping a dozen chars) — V1 is enough for
    a card body and survives most owner content."""
    if not text:
        return ""
    # Escape backticks (they wrap inline code) and underscores (italic).
    return text.replace("`", "\\`").replace("_", "\\_")


def format_telegram(spec: ApprovalCardSpec) -> dict:
    """Build a TG message payload.

    Returns ``{"text": str, "reply_markup": dict, "parse_mode": str}``. The
    sender (``send_telegram_card`` in hermes_io) takes that and feeds the
    bot's ``send_message``. Inline keyboard buttons render but their
    callbacks are NOT routed back to PAID (see module docstring) — the
    footer line tells the owner to use slash commands instead.

    TG message limit is 4096 chars; ApprovalCardSpec already truncates
    junior_msg + draft to 600 each so we stay well under.
    """
    msg_quoted = "\n".join(f"> {ln}" for ln in spec.junior_msg.splitlines())
    if not msg_quoted:
        msg_quoted = "> (empty)"
    if spec.has_draft:
        draft_quoted = "\n".join(f"> {ln}" for ln in spec.draft.splitlines())
        draft_section = (
            f"\n*PAID's draft (junior will see this on approve):*\n{draft_quoted}\n"
        )
    else:
        draft_section = (
            "\n*Draft:* (none — sensitive topic; approve with custom text "
            f"`/paid-approve {spec.request_id} <your reply>`)\n"
        )

    text = (
        f"{spec.confidence_pill()} *PAID approval needed* "
        f"\\[#{spec.request_id}\\]\n"
        f"\n"
        f"*Junior:* {_telegram_escape_markdown(spec.junior_name)} "
        f"({spec.junior_platform})\n"
        f"*Topic:* {_telegram_escape_markdown(spec.topic)} "
        f"· {spec.stakes_pill()} · conf {spec.confidence:.2f}\n"
        f"\n"
        f"*Message:*\n{_telegram_escape_markdown(msg_quoted)}\n"
        f"{draft_section}"
        f"\n"
        f"⏱️ Auto-defer in {spec.timeout_min} min\n"
        f"\n"
        f"📍 {spec.instructions}"
    )

    # Inline keyboard — button click NOT routed back to PAID; visual only.
    # callback_data is included anyway so the future v1.x can reuse the
    # same button payloads if/when we wire callback handling upstream.
    buttons_row: list[dict] = []
    if spec.has_draft:
        buttons_row.append({
            "text": "✅ Approve",
            "callback_data": f"paid_approve:{spec.request_id}",
        })
        buttons_row.append({
            "text": "✏️ Edit",
            "callback_data": f"paid_edit:{spec.request_id}",
        })
    buttons_row.append({
        "text": "❌ Reject",
        "callback_data": f"paid_reject:{spec.request_id}",
    })

    return {
        "text": text,
        "reply_markup": {"inline_keyboard": [buttons_row]},
        "parse_mode": "Markdown",
    }


# ============================================================================
# Slack — Block Kit
# ============================================================================


def format_slack(spec: ApprovalCardSpec) -> dict:
    """Build a Slack message payload.

    Returns ``{"blocks": [...], "text": str}``. ``text`` is the
    notification fallback shown in mobile push / accessibility readers /
    legacy clients — Slack API REQUIRES it whenever blocks are sent.
    Action buttons render but their click events are NOT routed back to
    PAID (see module docstring); the context block at the bottom
    instructs the owner to use slash commands.

    Slack limit: 50 blocks per message. We use ~7 blocks well under.
    """
    junior_msg_quoted = "\n".join(f"> {ln}" for ln in spec.junior_msg.splitlines())
    if not junior_msg_quoted:
        junior_msg_quoted = "> (empty)"

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": spec.header_title(), "emoji": True},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*From:*\n{spec.junior_name}\n_{spec.junior_platform}_"},
                {"type": "mrkdwn", "text": f"*Topic:*\n{spec.topic}"},
                {"type": "mrkdwn", "text": f"*Stakes:*\n{spec.stakes_pill()}"},
                {"type": "mrkdwn", "text": f"*Confidence:*\n{spec.confidence_label()}"},
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Q (junior asked):*\n{junior_msg_quoted}"},
        },
    ]

    if spec.has_draft:
        draft_quoted = "\n".join(f"> {ln}" for ln in spec.draft.splitlines())
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*PAID's draft (junior will see this on approve):*\n{draft_quoted}",
            },
        })
    else:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Draft:* _none — sensitive topic_\n"
                    f"Approve with custom text: `/paid-approve {spec.request_id} <reply>`"
                ),
            },
        })

    # Action buttons — visual only, click is NOT routed to PAID. action_id
    # uses the same paid_* prefix as TG callback_data for symmetry/v1.x.
    button_elements: list[dict] = []
    if spec.has_draft:
        button_elements.append({
            "type": "button",
            "style": "primary",
            "text": {"type": "plain_text", "text": "✅ Approve", "emoji": True},
            "value": spec.request_id,
            "action_id": "paid_approve",
        })
        button_elements.append({
            "type": "button",
            "text": {"type": "plain_text", "text": "✏️ Edit", "emoji": True},
            "value": spec.request_id,
            "action_id": "paid_edit",
        })
    button_elements.append({
        "type": "button",
        "style": "danger",
        "text": {"type": "plain_text", "text": "❌ Reject", "emoji": True},
        "value": spec.request_id,
        "action_id": "paid_reject",
    })

    blocks.extend([
        {"type": "actions", "elements": button_elements},
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn",
                 "text": f"⏱️ Auto-defer in {spec.timeout_min} min  ·  📍 {spec.instructions}"},
            ],
        },
    ])

    # Notification fallback — short single-line summary.
    text_fallback = (
        f"PAID approval #{spec.request_id} from {spec.junior_name} "
        f"on {spec.junior_platform} (topic={spec.topic}, "
        f"conf={spec.confidence:.2f})"
    )

    return {"blocks": blocks, "text": text_fallback}


# ============================================================================
# Plain text — last-resort fallback (any platform)
# ============================================================================


def format_plain(spec: ApprovalCardSpec) -> str:
    """Pure plain-text card. Used by:
      - Platforms with no card support
      - Any platform where the rich-card send fell through to fallback
      - outbound_queue.jsonl payloads when gateway is down

    Numbered shortcuts so the owner can reply by typing a digit; verbose
    slash-command form still works. Confidence + stakes get visual badges
    so a skim is enough.
    """
    draft = spec.draft or ""
    draft_preview = draft if draft else "(no draft)"
    return (
        f"📨 PAID approval #{spec.request_id}\n"
        f"From: {spec.junior_name} ({spec.junior_platform})\n"
        f"Topic: {spec.topic}  ·  Stakes: {spec.stakes_pill()}  "
        f"·  Conf: {spec.confidence_label()}\n"
        f"\n"
        f"Q (junior asked):\n{spec.junior_msg}\n"
        f"\n"
        f"Draft (junior will see this if you approve):\n{draft_preview}\n"
        f"\n"
        f"Reply:\n"
        f"  1️⃣ APPROVE — send draft as-is\n"
        f"     /paid-approve {spec.request_id}\n"
        f"  2️⃣ EDIT    — replace with your text\n"
        f"     /paid-approve {spec.request_id} <your reply>\n"
        f"  3️⃣ REJECT  — junior is told you'll reply directly\n"
        f"     /paid-reject {spec.request_id}\n"
        f"\n"
        f"⏱️ Auto-defer in {spec.timeout_min} min"
    )
