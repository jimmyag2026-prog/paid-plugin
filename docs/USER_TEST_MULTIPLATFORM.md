# Multi-platform v0.1 — Owner Test Walkthrough (Telegram + Slack)

This is the **owner-side runbook** to take PAID v1.2.0 from "Lark only" to
also delivering approval cards on **Telegram** and **Slack**.

**Prereqs**:
- PAID v1.2.0+ deployed on the hermes you use (`hermes plugins list` shows
  `paid-v1 │ enabled │ 1.2.0`).
- Lark already working end-to-end (you've successfully approved at least
  one card on Lark — that confirms the J3 path is wired).

**Estimate**: ~30 min for TG-only, ~60 min including Slack (Slack app
creation has more clicks).

This doc is structured as **independent sections** — you can do TG first,
ship it, come back for Slack later.

---

## Section A · Decide which platforms you want to enable

PAID v1.2.0 supports 3 owner platforms simultaneously. Pick the one(s)
relevant to your situation:

| Platform | When it's a good fit | Setup cost |
|---|---|---|
| **Lark / Feishu** | China-side teams; already done if you're reading this | (already done) |
| **Telegram** | Quick personal pilots; bot in 2 min via BotFather | low |
| **Slack** | Org / team pilots; richest Block Kit cards | medium-high |

**v0.1 scope reminder**: button **clicks** on TG/Slack **don't trigger
PAID actions** — they're visual. You operate via PAID's slash commands
(same as v1.0+ Lark fallback). The card body always tells you the exact
slash command to type. See `design/08_multiplatform_design.md §1` for
why (hermes upstream limitation).

---

## Section B · Telegram setup

### B.1 Create a Telegram bot (BotFather)

In your Telegram app:

1. DM **@BotFather** (verified blue checkmark)
2. Send `/newbot`
3. Pick a display name (e.g. `Jimmy's PAID`)
4. Pick a username ending in `bot` (e.g. `jimmy_paid_bot`)
5. BotFather replies with your **bot token** — looks like:
   ```
   1234567890:AAH-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
   ```
6. **Copy that token** — keep it secret, anyone with it can impersonate your bot

### B.2 Find your owner Telegram user_id

DM your new bot **anything** (e.g. `hi`). Your bot can't reply yet, but
your message reaches Telegram.

To get your `user_id`:

1. DM **@userinfobot** in Telegram → it replies with your numeric user ID
   (something like `123456789`)
2. Save that — it's both your `user_id` AND your `home_chat_id` for
   private chats with PAID

### B.3 Configure hermes

On the host running PAID's hermes (your laptop or VPS), edit
`~/.hermes/.env`:

```bash
# Add these two lines (use your actual values):
TELEGRAM_TOKEN=1234567890:AAH-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
# TELEGRAM_WEBHOOK_URL=    # leave UNSET — PAID/hermes uses polling by default

# (optional, only if you deploy hermes behind a reverse proxy and want
#  push delivery instead of polling — most owners can ignore)
```

> ⚠️ **Don't set `TELEGRAM_WEBHOOK_URL`** unless you really want webhook
> mode. With it set, hermes also requires `TELEGRAM_WEBHOOK_SECRET` (it
> refuses to start otherwise — this is hermes-side security
> [GHSA-3vpc-7q5r-276h](https://github.com/NousResearch/hermes-agent/security/advisories/GHSA-3vpc-7q5r-276h)).

### B.4 Add yourself to PAID's owner.json

Open `~/.hermes/paid/owner.json`. If you've been on Lark only, it
probably looks like:

```json
{
  "owner_id": "owner_jimmy",
  "name": "Jimmy",
  "identities": [
    {"platform": "feishu", "user_id": "ou_8580f481..."}
  ]
}
```

Add a TG identity. **Two options**:

**Option 1 — manual edit (recommended for one platform)**:

```json
{
  "schema_version": 2,
  "owner_id": "owner_jimmy",
  "name": "Jimmy",
  "preferred_platform": "telegram",
  "identities": [
    {"platform": "feishu", "user_id": "ou_8580f481...",
     "home_chat_id": "oc_f4de22018c4a9f9480450ef9f8c13231",
     "enabled": true},
    {"platform": "telegram", "user_id": "123456789",
     "home_chat_id": "123456789", "enabled": true}
  ]
}
```

> `preferred_platform` decides where PAID sends approval cards by
> **default** when a junior message comes in. Switch it any time without
> restarting hermes.

**Option 2 — run the migration script first**:

```bash
python3 ~/.hermes/plugins/paid-v1/bin/migrate_owner_v1_to_v2.py --dry-run
# review the proposed changes, then:
python3 ~/.hermes/plugins/paid-v1/bin/migrate_owner_v1_to_v2.py
# now ~/.hermes/paid/owner.json is v2 schema with backups at owner.json.v1.bak
# THEN add the telegram identity by hand
```

### B.5 Restart hermes

```bash
hermes gateway restart
sleep 5
hermes channels 2>/dev/null || hermes setup        # see Telegram listed
tail -30 ~/.hermes/paid/plugin_runtime.log         # look for clean PAID load
```

**Expected**:
- `tail` shows recent `PAID v1 plugin registering` line, no traceback
- Telegram polling connection established (look for `telegram` /
  `Polling started` in hermes journal/log)

### B.6 First test — owner ↔ bot smoke

DM your bot a fatal alert via the helper (verifies hermes_io.send_dm
reaches you on TG):

```bash
ssh -i ~/digitalocean root@159.65.75.97  # if your hermes is on the VPS
sudo -u paid -H bash -lc 'python3 << "EOF"
import sys, importlib.util
sys.path.insert(0, "/home/paid/.hermes/plugins/paid-v1")
spec = importlib.util.spec_from_file_location(
    "plug", "/home/paid/.hermes/plugins/paid-v1/__init__.py")
plug = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plug)
plug._alert_owner("tg_smoke_test", "Telegram pairing smoke check; safe to ignore.")
print("triggered.")
EOF'
```

**Success**: your Telegram bot DMs you a `⚠️ PAID fatal alert ...` message
within ~5 seconds.

**If you see nothing on Telegram**:

- Check `tail -20 ~/.hermes/paid/plugin_runtime.log` — should mention
  `notify owner` for telegram with `ok=True`
- Check `cat ~/.hermes/paid/outbound_queue.jsonl` — if last entry has
  `"platform":"telegram"` and `"ok":false`, the send_dm path failed and
  fell back to queue. Common cause: `TELEGRAM_TOKEN` typo. Fix .env, restart.

### B.7 First real approval card on TG

Set TG as `preferred_platform` (already done above), then have a
**junior account** (NOT your owner account) DM the **junior-side bot**
(your Lark / TG bot they normally talk to PAID through):

> Hi Jimmy, can you confirm we're still on for the meeting tomorrow at 3pm?

PAID's J3 path triggers (medium-stakes "request" classification). Within a
few seconds, **your owner Telegram should get an approval card**:

```
🟡 PAID approval needed [#abc123]

Junior: <name> (<their platform>)
Topic: scheduling · 🟠 medium · conf 0.65

Message:
> Hi Jimmy, can you confirm we're still on...

PAID's draft (junior will see this on approve):
> Confirmed for 3pm tomorrow — I'll send a calendar invite.

⏱️ Auto-defer in 30 min

📍 操作: 回 /paid-approve abc123 或 /paid-reject abc123
```

(With three buttons that **don't** click — see Section A reminder.)

**Verify the round-trip**: type `/paid-approve abc123` in your TG chat
with PAID. PAID's draft answer should reach the junior on their original
platform.

If you want to override the draft, use `/paid-approve abc123 <your custom text>`.

---

## Section C · Slack setup

> Skip this whole section if you're TG-only for now.

### C.1 Create a Slack app

1. Go to https://api.slack.com/apps → **Create New App** → **From scratch**
2. App Name: e.g. `PAID for Jimmy`. Pick the workspace you want to pilot
   in. Use a **sandbox workspace** if you don't want to pollute your main
   workspace's logs.
3. After creation, you land on the app's settings page. The next 5
   sub-steps are all on this page.

### C.2 Bot token scopes

Sidebar → **OAuth & Permissions** → scroll to **Scopes** → **Bot Token
Scopes** → **Add an OAuth Scope** → add each of these:

| Scope | Why |
|---|---|
| `chat:write` | Post messages as the bot |
| `chat:write.public` | Post in channels the bot isn't in (handy for `#general` notices) |
| `im:history` | Read DMs sent to the bot (junior messages) |
| `im:read` | List DM channels |
| `im:write` | Open DMs with users |
| `commands` | Register slash commands (`/paid-pending` etc.) |
| `app_mentions:read` | Catch `@PAID` in channels |
| `users:read` | Look up user display names |

### C.3 Enable Socket Mode

Sidebar → **Socket Mode** → toggle ON. Slack asks you to create an
**App-Level Token**:

- Token Name: `paid-socket`
- Scope: `connections:write`
- Click **Generate**
- Copy the token starting with `xapp-...` — this is your **APP TOKEN**

### C.4 Subscribe to events

Sidebar → **Event Subscriptions** → toggle **Enable Events** ON. Under
**Subscribe to bot events** → **Add Bot User Event** → add:

- `message.im` (DMs to bot)
- `app_mention` (@bot in channels)

> No "Request URL" needed because Socket Mode handles the inbound channel.

### C.5 Install to workspace

Sidebar → **Install App** → **Install to <workspace>**. Authorize. After
install, you're shown the **Bot User OAuth Token** — starts with
`xoxb-...`. **Copy this** — your **BOT TOKEN**.

### C.6 Configure hermes

Add to `~/.hermes/.env`:

```bash
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
```

### C.7 Find your owner Slack user_id + DM channel id

DM **@yourself** in Slack (Slack lets you DM yourself — that's your
"private notes"). Or DM your new PAID bot (which now exists in your
workspace).

To find your **user_id** (`U...`):
- Click your profile pic → **View profile** → **More** (`...`) → **Copy
  member ID**

To find your **DM channel_id** with the bot (`D...`):
- Open your DM with the bot
- In the URL bar, the channel id is the part after the last `/` (e.g.
  `https://app.slack.com/client/T01ABC/D01XYZ` → `D01XYZ`)
- OR run `python3 -c "from slack_sdk import WebClient; ..."` if you want
  it programmatically; Slack also has `conversations.list` API

### C.8 Add Slack identity to owner.json

```json
{
  "schema_version": 2,
  "owner_id": "owner_jimmy",
  "name": "Jimmy",
  "preferred_platform": "slack",     // or keep "feishu" / "telegram" — choose default
  "identities": [
    {"platform": "feishu", "user_id": "ou_...", "home_chat_id": "oc_...", "enabled": true},
    {"platform": "telegram", "user_id": "123456789", "home_chat_id": "123456789", "enabled": true},
    {"platform": "slack", "user_id": "U01ABCD",
     "home_chat_id": "D01XYZW",     // ← the D-prefix DM channel, NOT user_id
     "enabled": true}
  ]
}
```

**Critical**: `home_chat_id` for Slack is the DM **channel** id (`D...`),
not your user id (`U...`). If you put `U...` here, Slack will return
`channel_not_found` errors — see Section D troubleshooting.

### C.9 Restart + smoke test

```bash
hermes gateway restart
sleep 5

# Trigger a fatal alert on Slack:
sudo -u paid -H bash -lc 'python3 << "EOF"
import sys, importlib.util
sys.path.insert(0, "/home/paid/.hermes/plugins/paid-v1")
spec = importlib.util.spec_from_file_location(
    "plug", "/home/paid/.hermes/plugins/paid-v1/__init__.py")
plug = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plug)
plug._alert_owner("slack_smoke_test", "Slack pairing smoke check.")
EOF'
```

**Success**: PAID bot DMs you in Slack: `⚠️ PAID fatal alert ...`

### C.10 First real approval card on Slack

Same as B.7 but with `preferred_platform: "slack"` set. Junior sends a
medium-stakes message → you get a **Block Kit card** in Slack:

- Header block: `📨 PAID approval #abc123`
- Section block: From / Topic / Stakes / Confidence (4 fields)
- Section block: Q (junior asked)
- Section block: PAID's draft
- Actions block: 3 buttons (visual only)
- Context block: `⏱️ Auto-defer in 30 min · 📍 reply /paid-approve abc123`

Type `/paid-approve abc123` in your DM with PAID → junior gets the answer.

---

## Section D · Troubleshooting checklist

### D.1 Bot doesn't show up / hermes can't connect

```bash
# Restart and watch the gateway log
hermes gateway restart
journalctl --user -u hermes-gateway.service -f --since "1 minute ago"
```

Look for:
- `Telegram bot connected` / `Polling started` — TG OK
- `Slack: Socket mode handler started` — Slack OK
- Any `Failed to authenticate` / `invalid token` → token typo

### D.2 Token errors

| Symptom | Cause | Fix |
|---|---|---|
| `401 Unauthorized` (TG) | bad bot token | Copy fresh token from BotFather; no spaces |
| `invalid_auth` (Slack) | bad bot token | Reinstall app to workspace; copy `xoxb-` token |
| `not_allowed_token_type` (Slack Socket Mode) | passed `xoxb-` where `xapp-` needed | Set both `SLACK_BOT_TOKEN` AND `SLACK_APP_TOKEN` separately |

### D.3 PAID sends to wrong place / nothing happens

```bash
# What did PAID think it was sending where?
tail -50 ~/.hermes/paid/plugin_runtime.log | grep "notify owner"
# Should show:
#   [approval] notify owner #xxx via slack:D01XYZ (block kit) → {'ok': True, ...}
# If queued: see what's in the queue
tail -5 ~/.hermes/paid/outbound_queue.jsonl
```

### D.4 Slack `channel_not_found`

You used `U...` (user id) instead of `D...` (DM channel id) in
`home_chat_id`. Fix:

1. Open the DM with PAID bot in Slack
2. Copy the `D...` from the URL
3. Update `owner.json` `identities[*].home_chat_id`
4. No restart needed — `load_owner` re-reads on each notification

### D.5 Telegram `chat not found`

The TG `user_id` is your **personal numeric ID**, not your `@username`.
Re-confirm via `@userinfobot`.

### D.6 Card renders but I want to switch which platform I get cards on

Just edit `owner.json` `preferred_platform` to `"telegram"` / `"slack"` /
`"feishu"`. **No restart needed** — PAID re-reads `owner.json` on every
inbound message.

### D.7 Disable a platform temporarily without removing config

```json
"identities": [
  ...
  {"platform": "slack", "user_id": "U...", "home_chat_id": "D...",
   "enabled": false}    // ← set to false; re-enable later by flipping back
]
```

### D.8 The buttons on TG/Slack don't do anything when I click them

**That's the v0.1 design** (see Section A). Buttons render but click
callbacks aren't routed back to PAID. Use slash commands shown in the
card footer (`/paid-approve <id>` / `/paid-reject <id>`).

If pilots get confused, you can hide the buttons by setting
`PAID_HIDE_INACTIVE_BUTTONS=1` in `.env` (planned v1.x — not in v1.2).

### D.9 Too many alert/approval messages spamming the wrong platform

If `preferred_platform="slack"` but you didn't notice approval cards
piling up there because Slack notifications are off → fix:

- Slack Settings → **Notifications** → turn on for DMs from apps
- Or switch `preferred_platform` to a channel you watch

The fatal alert path (`_alert_owner`) is debounced (10 min same-reason),
but approval notifications are NOT — every J3 trigger sends one.

---

## Section E · End-to-end test matrix

Run all of these and confirm before declaring multi-platform live:

| # | Test | Expected | Pass? |
|---|---|---|---|
| 1 | TG smoke `_alert_owner` | TG DM `⚠️ PAID fatal alert` | ☐ |
| 2 | Slack smoke `_alert_owner` | Slack DM `⚠️ PAID fatal alert` | ☐ |
| 3 | Junior msg (medium stakes) → TG card | Inline keyboard card with body + buttons | ☐ |
| 4 | Junior msg (medium stakes) → Slack card | Block Kit card with header/section/actions | ☐ |
| 5 | `/paid-approve <id>` on TG | Junior gets PAID's draft answer | ☐ |
| 6 | `/paid-approve <id>` on Slack | Same | ☐ |
| 7 | `/paid-reject <id>` on TG | Junior gets "Jimmy will reply directly" | ☐ |
| 8 | Switch `preferred_platform` mid-session | Next J3 card lands on the new platform without restart | ☐ |
| 9 | Disable a platform with `enabled: false` | No cards land there; cards land on next enabled | ☐ |
| 10 | Lark still works alongside | Junior on Lark → owner on Slack still gets the card | ☐ |

When all 10 pass, multi-platform v0.1 is **live**.

---

## Section F · What about the junior side?

Multi-platform v0.1 is about **owner-side** platform spread (you receive
PAID approvals on TG/Slack/Lark). Junior side (where the people sending
PAID messages live) was already multi-platform via hermes — PAID has been
classifying inbound from any wired hermes platform since v0.5.

If you want a junior to message PAID via Telegram (for example), they
DM the same bot you set up in B.1 — PAID will:

1. See an unknown sender → discovery card (J4) lands on your owner platform
2. You add them via `python3 -m paid add-counterparty telegram <their_id> --name "..." --role junior --topic-allow ...`
3. Their next message goes through the J2 pipeline normally

---

## Section G · Rolling back

If multi-platform v0.1 misbehaves and you want to go back to v1.1.0
(Lark only):

```bash
cd /path/to/paid-plugin
git checkout v1.1.0     # or v1.0.0 if you want even further back
bash bin/install.sh
hermes gateway restart
```

`owner.json` v2 stays compatible with v1.1.0/v1.0.0 (those readers
ignore the v2-only fields), so you don't need to touch it. The file
`owner.json.v1.bak` (created by `migrate_owner_v1_to_v2.py`) is your
ultimate "go back to v1 schema" escape hatch.

---

## Section H · Known limitations (v0.1, not bugs)

- ❌ TG/Slack button clicks not routed back to PAID — use slash commands
- ❌ Slack thread reply model not used — PAID DMs you, doesn't open thread
- ❌ TG group chats not supported as owner channel — DM only
- ❌ Slack channel-as-owner not tested — DM with bot is the supported path
- ❌ Card content not localized per platform — same Markdown body
- ❌ PAID doesn't try to detect which platform the owner is "most active"
   on — `preferred_platform` is manually set

These are tracked in `paid-may` repo `design/05_backlog.md` M3.x and
`design/08_multiplatform_design.md §9`. Roadmap is v1.x post-pilot
feedback.
