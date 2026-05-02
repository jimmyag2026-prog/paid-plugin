# Lark / Feishu Setup Runbook

This is the gotcha-by-gotcha guide for getting PAID running on Lark Suite
(international) or Feishu (China). It captures every wrong turn we hit
during the v0.9 live dogfood so the next operator doesn't re-derive them.

The runbook assumes you already have the bot connected for plain text
messages. If text messages from a counterparty are reaching PAID and PAID
is replying, you've cleared 80% of the setup. The remaining 20% is the
interactive-card path, which has its own traps.

If you're reading this top-to-bottom: budget 15-25 minutes including the
Lark Open Platform clicks.

---

## TL;DR — what must be true for everything to work

1. **Your Lark app is on long-connection (WebSocket) subscription mode.**
   _Events & Callbacks → Subscription mode → "Receive events through
   persistent connection"._

2. **`Message received` event (`im.message.receive_v1`) is subscribed.**
   _Events & Callbacks → Events added._

3. **The bot's "Interactive Card" capability is enabled in Bot config.**
   _App Features → Bot → Interactive Card toggle ON._

4. **`card.action.trigger` event is subscribed.**
   _Events & Callbacks → Add Events → search "card"._
   This event only appears in the picker after step 3.

5. **The owner's `open_id` (the `ou_…` form) is in
   `~/.hermes/pairing/feishu-approved.json` AND in PAID's
   `~/.hermes/paid/owner.json`.**
   See "Identity bifurcation" below.

6. **The bot is added as a friend in DM** with both the owner and any
   junior counterparty you want PAID to handle.

7. **The owner has run `/sethome` once in their DM with the bot.**
   This populates `FEISHU_HOME_CHANNEL` in `~/.hermes/.env` so PAID's
   approval card has a chat_id to address.

8. **A new app version was published** after each Lark-Open-Platform
   change. Saving alone doesn't take effect; you must publish.

---

## The 3-step Interactive Card setup (the most common pitfall)

If button clicks on PAID approval cards return **error 200340** ("Card
callback URL unreachable"), one of these three steps is missing:

### Step 1 — Enable Interactive Card capability

`open.larksuite.com` → your app → **App Features → Bot** →
toggle **"Interactive Card"** to ON → Save.

Without this, step 2's event picker won't show `card.action.trigger`.

### Step 2 — Subscribe to `card.action.trigger`

`Events & Callbacks → Events added → Add Events` (top right blue button)
→ search `card` → tick `card.action.trigger` (or "Card Callback" in some
locales) → Add.

Without this, Lark has no idea where to deliver button clicks. Some
locales also list a separate `Replacement card approval` event — that's
unrelated, leave it.

### Step 3 — Verify long-connection mode

Same page → top section → make sure subscription mode says
**"Receive events through persistent connection"**, not webhook /
encrypted callback. Hermes' Lark adapter listens on the long connection;
if your app is in webhook mode, every event tries to POST to a URL the
gateway doesn't expose, and you get 200340 / 230002 errors.

### Then publish

Top of the app page — there's a banner like
**"You have unpublished changes — Create version and release"** or
similar. Walk through the publish dialog. Self-built apps within your
own tenant are usually approved instantly; cross-tenant apps need admin
approval first.

After publish, no hermes restart is needed — long-connection clients
hot-reload the event subscription.

---

## Identity bifurcation: open_id vs user_id

This caught us during the live test. Lark sends two different identity
forms depending on what type of event it is:

| Event type | Identity form Lark provides |
|---|---|
| `im.message.receive_v1` (text message) | tenant `user_id` (~8 hex chars, e.g. `8ea86e3b`) |
| `card.action.trigger` (button click) | `open_id` (~32 chars, prefixed `ou_`, e.g. `ou_8580f481e0c7fac2b36f3dd5f88144a1`) |

When the operator runs `hermes pairing approve feishu <code>`, only the
form Lark sent in the original message is added to
`feishu-approved.json`. Card click events arrive from a **different
identity form** that the gateway then rejects as "Unauthorized user".

### Fix: add the open_id form to BOTH approved-users files

```bash
# 1. Find your open_id from a card-click rejection in journalctl:
journalctl --user -u hermes-gateway | grep "Unauthorized user.*on feishu"
#   → ou_8580f481e0c7fac2b36f3dd5f88144a1 (Jimmy Yin)

# 2. Add it to hermes' approved list:
python3 - <<PY
import json, time
from pathlib import Path
p = Path.home() / ".hermes" / "pairing" / "feishu-approved.json"
data = json.loads(p.read_text())
data["ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"] = {  # ← paste your open_id
    "user_name": "OWNER (open_id form)",
    "approved_at": time.time(),
}
p.write_text(json.dumps(data, indent=2, ensure_ascii=False))
PY

# 3. Add it to PAID's owner.json identities so is_owner() recognises it:
python3 - <<PY
import json
from pathlib import Path
p = Path.home() / ".hermes" / "paid" / "owner.json"
data = json.loads(p.read_text())
data["identities"].append({
    "platform": "feishu",
    "user_id": "ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",  # ← same open_id
})
p.write_text(json.dumps(data, indent=2, ensure_ascii=False))
PY
```

No restart needed — both files are read on each auth check.

### Why we don't normalise this in code

Lark's API doesn't always give us the alt-id form on every event payload,
and translating user_id → open_id requires an extra contact-API call.
The lazy "add both forms when you find them" pattern is cheap and
correct; we'd rather wait for the upstream hermes fix that consolidates
these identities at the gateway level.

---

## /sethome and FEISHU_HOME_CHANNEL

PAID's approval card is sent to the owner's chat_id, not their user_id.
Lark's IM API technically accepts `receive_id_type=user_id` for
plain-text messages — but for the **chat_id-style sends used by hermes'
adapter**, you need an actual `oc_…` chat_id.

`/sethome` is the canonical way to capture this. Run it once in your
owner DM with the bot:

1. Owner DMs the bot any message (so the chat exists)
2. Owner replies `/sethome`
3. Hermes saves `FEISHU_HOME_CHANNEL=oc_…` into `~/.hermes/.env`

PAID's approval-card delivery code (`_resolve_owner_send_target`) reads
that env var and uses it as the receive_id when platform is feishu/lark.

If you see PAID approval cards landing in your **outbound queue**
(`~/.hermes/paid/outbound_queue.jsonl`) with `[230001] invalid
receive_id`, you forgot `/sethome`.

---

## What hermes' synthetic-command path looks like

When the operator clicks a PAID interactive-card button:

```
Lark client click
  → Lark backend
  → long-connection event card.action.trigger
  → hermes feishu adapter (gateway/platforms/feishu.py:_on_card_action_trigger)
  → not a hermes-owned card (no `hermes_action` key) →
    _handle_card_action_event builds synthetic MessageEvent
    text="/card button {json action_value}", type=COMMAND
  → routed through full inbound pipeline (auth/pairing → command dispatch)
  → PAID's `card` slash command (registered as `/card` in __init__.py)
  → _cmd_card parses payload.paid_action + .request_id
  → dispatches to _cmd_approve / _cmd_reject
  → set_status + send_dm to junior
```

When debugging, the key journalctl line to grep for:

```bash
journalctl --user -u hermes-gateway | grep "Routing card action"
```

If you see this line, the click reached hermes. If you don't, the click
didn't make it past Lark — re-check the 3-step setup above.

---

## Sweep / cron and the standalone Lark client

PAID v0.9.2+ supports calling outbound Lark from **outside the gateway
process** (cron-driven `bin/sweep_pending.py`, ad-hoc CLI usage). The
flow is:

```
hermes_io.send_dm(...)
  → try gateway adapter (cheapest; shares token cache)
  → on failure (no live runner) → try standalone client
       (built from FEISHU_APP_ID / FEISHU_APP_SECRET in ~/.hermes/.env)
  → on failure → enqueue to outbound_queue.jsonl
```

For sweep / cron / scripts to deliver successfully, the .env file must
already contain credentials. They land there automatically when the
operator runs `hermes gateway setup` and picks Lark, so there's nothing
extra to do.

If the standalone client fails to build (typically: missing creds, or
older lark_oapi without `Client.builder().domain()` support), the queue
still captures intent for later retry / hand-delivery.

---

## Common error codes you'll hit

| Code | Meaning | Likely fix |
|---|---|---|
| **200340** | Card callback URL unreachable | 3-step Interactive Card setup not complete; mostly the missing "Interactive Card" toggle |
| **230001** | Invalid receive_id | Wrong receive_id_type for the ID format you passed (check `_detect_lark_receive_id_type`); or never ran `/sethome` |
| **230002** | Bot not in chat | Owner / junior hasn't added the bot as a friend / to the group |
| **99991663** | App permission insufficient | Re-check Permissions & Scopes — `im:message:send_as_bot`, `im:message`, etc. |
| **WS conn 1000040351 "Incorrect domain name"** | Wrong FEISHU_DOMAIN | If your bot is on Lark Suite (international), set `FEISHU_DOMAIN=lark`; for Feishu CN keep it `feishu` (default) |

---

## Verifying everything end-to-end

After a fresh setup, walk this sequence:

```bash
# 1. Bot is alive on Lark
hermes gateway status        # active running
journalctl --user -u hermes-gateway | grep "Lark.*connected"

# 2. Plugin is loaded
tail ~/.hermes/paid/plugin_runtime.log
#   expect: PAID v1 plugin registering / registered: pre_gateway_dispatch
#           registered: /card / hooks: pre_llm_call, post_llm_call,
#           pre_gateway_dispatch / commands: /paid-pending /paid-approve …

# 3. Owner identity in both files
cat ~/.hermes/pairing/feishu-approved.json
cat ~/.hermes/paid/owner.json

# 4. /sethome captured
grep FEISHU_HOME_CHANNEL ~/.hermes/.env       # should be a non-empty oc_…

# 5. Junior message → request → owner card
#    (send "Jimmy 工资多少" from a non-owner Lark account)
tail -f ~/.hermes/paid/plugin_runtime.log
#   expect: [pre_llm] cp=… state=request topic=salary
#           [approval] created #…
#           [approval] notify owner #… via feishu:oc_… (interactive card)
#           → {ok: True, msg_id: om_…}

# 6. Click ❌ Reject on the card
#   expect: pending_approvals.jsonl has a status=rejected event
#           junior receives the rejection text
```

If step 6 doesn't fire, you're hitting one of the 200340 / identity-
bifurcation issues above.
