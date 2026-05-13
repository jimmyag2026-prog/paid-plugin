# PAID Pilot — Onboarding Guide

> Hi 👋 — thanks for agreeing to pilot PAID this week.
>
> **What this is**: PAID = **P**ersonal **A**I **D**elegate. You authorise an
> AI to handle a class of replies on your behalf — like how you'd brief a
> new assistant. Three states: it auto-answers / asks you to approve /
> hands back to you. You stay accountable; the AI does the busywork.
>
> **The deal this week**: 30 min setup, ~5 min/day, you give us honest
> feedback at the end. Reward: dinner per bug report, three reports = three
> dinners.

---

## 0. The roles

| Who | Does what |
|---|---|
| **You** (pilot) | Lark app creation in your workspace · writing your `persona.md` + `sop.md` · daily usage · end-of-week debrief |
| **Jimmy** (acting as your "CTO this week") | All server / VPS / hermes / PAID setup · debugging anything that breaks · on call via DM |

You will **not** install anything on your laptop. You will not touch a
terminal. Your PAID instance runs on Jimmy's VPS under a dedicated user
account isolated from his.

---

## 1. Before our call (≈15 min, do this solo)

You need to create a **Lark custom app** in your own Lark workspace.
Jimmy can't do this for you — Lark requires the workspace owner to
authorise the app. The three values you produce here are what Jimmy
plugs into the server.

### 1.1 Pick the right Lark domain

- **Lark (international)** — `https://open.larksuite.com/app/new`
- **Feishu (China)** — `https://open.feishu.cn/app/new`

If you're not sure which you're on: open Lark / Feishu in your browser,
look at the URL. `larksuite.com` → international. `feishu.cn` → China.
Tell Jimmy when you send him the three values below.

### 1.2 Create the app

1. Click **Create Custom App** (创建企业自建应用)
2. Name it whatever you want — e.g. `My PAID Delegate`
3. Add an icon (any image, even default works)
4. Submit

### 1.3 Add bot capability

In the app detail page:

1. Left sidebar → **Add Features** (添加应用能力)
2. Pick **Bot** (机器人)
3. Save

### 1.4 Add the four permission scopes

Left sidebar → **Permissions & Scopes** (权限管理). Add exactly these
four — copy paste:

- `im:message` — receive messages
- `im:message:send_as_bot` — send messages
- `im:resource` — handle images / files
- `im:chat:readonly` — read chat metadata

After adding, click **Create Version** / **Publish** (创建版本并发布).
If your Lark workspace requires admin approval, ping your admin — most
self-built apps clear within minutes. If it's your personal workspace,
you self-approve.

### 1.5 Grab the three values Jimmy needs

Once the app version is **published** (released):

**Value 1: App ID** — left sidebar → **Credentials & Basic Info** →
copy `App ID`. Looks like `cli_a1b2c3d4e5f6g7h8`.

**Value 2: App Secret** — same page → copy `App Secret`. 64-character
random string. **Treat this like a password** — anyone with it can
impersonate your bot. Send to Jimmy via DM, not group chat.

**Value 3: Your Open ID** — this is *your* Lark user identity, not
the bot's.

Two ways to get it:

- **Easy**: in Lark, DM your newly created bot any message (e.g. "hi").
  The bot can't reply yet — that's fine. Tell Jimmy "I just DM'd the bot",
  he'll pull your Open ID from the server log.
- **Manual**: in Lark admin → Members → click yourself → Open ID is in
  the detail panel. Looks like `ou_a1b2c3d4e5f6g7h8i9j0`.

### 1.6 Send Jimmy three values

DM Jimmy:

```
Lark domain: lark | feishu     (pick one)
App ID:      cli_xxxxxxxxxxxxxxxx
App Secret:  xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
Open ID:     ou_xxxxxxxxxxxxxxxxxxxx   (or: "I DM'd the bot")
```

Done with prep. Jimmy spends ~10 min wiring the server, then you book
the screen-share.

### 1.7 Also have ready for our call

- **1–2 friends** who've agreed to send you a few real questions this
  week. Don't loop them in yet — Jimmy will help you do the cp
  (counterparty) setup live on the call so you see how it works.
- **5 minutes of your real recent IM history** open in another tab —
  we'll use real examples for your `sop.md` rather than abstract bullets.

---

## 2. On the screen-share (≈60 min, with Jimmy)

Jimmy will share his screen (he's ssh'd into the VPS as your isolated
user). You watch and contribute when prompted.

### Block A · Confirm the bot is alive (5 min)

You DM your bot any message. Jimmy reads the server log and confirms
inbound is reaching PAID. Doesn't reply — owner messages bypass PAID
by design.

### Block B · Write your `persona.md` (10 min)

This is how PAID **sounds** when speaking on your behalf.

Don't write personality traits in the abstract. Write **specifics that
make your voice your voice**:

> ❌ "I'm friendly and professional."
>
> ✅ "I prefer short replies. I say 'sounds good' a lot. I write in
> Chinese unless the question is in English. I never use exclamation
> marks. When uncertain, I say 'let me check' rather than guessing."

5-10 lines. Jimmy will push back if it sounds like a chatbot bio.

### Block C · Write your `sop.md` (20-30 min — the leverage step)

This is **what PAID knows**. It's how PAID answers without making things
up. If you spend 5 minutes here you'll get robotic generic replies; if
you spend 25 minutes here PAID will sound like you.

Format: topic-tagged sections with concrete facts.

```markdown
## logistics — office / hours
- Office: 10:00-18:30 Mon-Fri, address: <real address>
- Default time zone: Asia/Hong_Kong
- Public holidays follow CN calendar

## schedule — meetings
- Weekly team standup: Tue 14:00 HKT
- Default meeting length: 25 min
- Book 1:1 via calendly.com/<your handle>

## projects — what I'm working on
- Project A: <one paragraph elevator pitch>
- Project B: <one paragraph elevator pitch>

## Things to NEVER say  (forces hand-back to you)
- Anything about ongoing negotiations
- Anyone's personal contact info that wasn't shared publicly
- Compensation / equity / hiring details
- Pricing / discounts
```

**Pro tip**: Jimmy will ask you "what's the most common question your
friends ping you about?" — start there. Real examples > made-up
scenarios.

### Block D · Add your first counterparty friend (5 min)

You name 1-2 friends who've agreed to send you test messages this week.
For each, you need their Lark Open ID. Two ways:

- Have them DM your bot a quick "hi" — Jimmy reads it from server log
- They DM you their Open ID directly from their own Lark profile

Jimmy runs `add-counterparty` on the server with their Open ID +
display name + which topics they're allowed to ask about (e.g.
`logistics`, `schedule`). Anything outside those topics auto-escalates
to you.

### Block E · Real test message (5-10 min)

Ask one of those friends to DM you a real-life question (something
they'd actually ask). You watch PAID's response in your Lark, and
Jimmy walks you through the audit log so you understand what PAID
classified, what state it picked, and why.

You're now live.

---

## 3. The week (≈5 min/day)

Use Lark like you normally do. PAID quietly classifies inbound messages
from your counterparties. Three outcomes you'll see:

### 3.1 `direct` — PAID auto-answered

You see the reply in your Lark history (you're in the same chat —
nothing hidden). Skim once a day. If PAID got something wrong, **just
follow up with a correction** in the chat — your correction overrides
PAID's reply, and you tell Jimmy at end of week.

### 3.2 `request` — PAID needs you to approve

You get an **interactive card in your own Lark** with:
- The original question
- A draft answer PAID suggests
- Three buttons: Approve / Edit / Reject (and a slash command in case
  buttons act weird)

You have 30 minutes. If you don't respond, PAID DMs the friend "Jimmy
is unavailable; he'll reach back out directly" so no one's left hanging.

### 3.3 `decline` — PAID handed back

You see in your Lark history that PAID said "this isn't something I'm
authorised to answer — please @ Jimmy directly." That's your cue to
reply manually.

### 3.4 Daily 30-second self-check

Look at your `~/.hermes/paid/audit_log.jsonl` once a day. You won't
have a terminal, but Jimmy can show you in DM. We'll set up a daily
status snapshot you read on Lark.

### 3.5 What to flag to Jimmy

DM Jimmy any time you see:

- **A wrong direct answer** — what was the question, what PAID said,
  what you'd have said
- **An over-escalation** — PAID asked you to approve something you'd
  trust it to handle
- **An under-escalation** — PAID answered something you'd want
  control over
- **Anything that surprised a counterparty** — they reply "what?" or
  "are you a bot?"

These four classes are exactly what Jimmy needs to tune the system.
**Don't filter — even small annoyances are signal.**

---

## 4. End of the week (≈30 min, scheduled call)

Friday or Sunday, we hop on a call. 30 min. The goal is your honest
read:

- 3 things that worked
- 3 things that didn't
- Would you keep using it? If yes, what'd you want changed first?
- Would you recommend it to one other person you trust?

This call is **the most valuable thing you give us** — far more than
the daily usage. Honest negative feedback > polite positive feedback.

---

## 5. Privacy / control

- **All your data stays on Jimmy's VPS under your isolated user account**
  (separate Linux user, separate `~/.hermes/paid/`). Jimmy has root and
  could technically read everything, but in practice we only look at
  audit logs when you flag a bug.
- **Your Lark workspace data**: PAID only sees messages your counterparties
  send to you via the bot. PAID has zero access to your other Lark
  chats, files, calendar.
- **At end of pilot**: you can keep PAID running, hand off the Lark app
  to your own server, or have Jimmy delete everything (you tell us).
- **Source code is MIT-licensed and public**:
  https://github.com/jimmyag2026-prog/paid-plugin

---

## 6. Quick reference — who fixes what

| You see | Who handles |
|---|---|
| PAID gave a wrong answer | DM Jimmy, he tweaks your SOP or persona |
| Bot stopped responding | DM Jimmy, server-side issue |
| You want to add another counterparty mid-week | DM Jimmy with their Open ID + name + allowed topics |
| You want to change persona / SOP | DM Jimmy the change, he edits the file (or for v2 we'll give you a Lark slash command) |
| You want to **stop** the pilot mid-week | DM Jimmy, instance off in 30 seconds, no questions |

---

## 7. If anything is unclear before our call

DM Jimmy. Don't try to figure out Lark app permissions alone — the
admin scope step is the part pilots most often get stuck on. Send a
screenshot of where you're stuck and Jimmy will unblock you in <5 min.

Looking forward to seeing you on the call.

— Jimmy
