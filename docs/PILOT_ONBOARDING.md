# PAID Pilot — Your Personal AI Delegate, Set Up for You

> Hi 👋 — thanks for letting us pilot PAID with you this week.
>
> **What you're getting**: a personal AI assistant that handles routine
> inbound IM on your behalf — like a delegated EA who answers the small
> stuff and pings you when something needs your call. Three outcomes:
> auto-answer, ask-you-first, or hand-back-to-you. You always stay in
> control.
>
> **What you do**: brief the assistant the way you'd brief a new hire —
> a 30-minute conversation about how you work and what you'd be happy
> delegating. That's it.
>
> **What we do**: everything else. Install, configure, run, monitor,
> debug, fix. Think of Jimmy as your interim CTO for this week.

---

## How this works for you

| You handle | We handle |
|---|---|
| One 15-min Lark setup (only you can do it — see §1) | Server, install, configuration, plumbing |
| One 30-min briefing conversation with Jimmy on Lark | Translating your briefing into PAID's "persona" + "knowledge base" |
| Using Lark normally for a week | Watching for issues, tuning, fixing anything that breaks |
| End-of-week 30-min chat about how it went | Everything technical |

**You will never touch a terminal, a config file, or an SSH key.**
Everything is on Jimmy's side. You talk to Jimmy on Lark; he makes it
happen.

---

## §1. The one thing only you can do (≈15 min)

We need to register a small "app" inside *your* Lark workspace —
think of it as authorising a vendor to integrate. Only the workspace
owner / admin can do this, so Jimmy can't do it for you.

Open Lark on your phone or laptop, then:

### 1a. Create the app

Go to the Lark developer console:

- **Lark (international, larksuite.com)** → https://open.larksuite.com/app/new
- **Feishu (China, feishu.cn)** → https://open.feishu.cn/app/new

(If unsure, just open Lark in your browser — the URL tells you which.
DM Jimmy which one when you finish.)

Click **Create Custom App** (创建企业自建应用). Name it anything —
e.g. `My AI Delegate`. Pick any icon. Submit.

### 1b. Turn on the bot ability

In the app's detail page, left sidebar:

1. **Add Features** → pick **Bot** (机器人) → Save

### 1c. Grant four permissions

Left sidebar → **Permissions & Scopes** (权限管理). Add these four
exactly:

```
im:message
im:message:send_as_bot
im:resource
im:chat:readonly
```

Then click **Create Version** (创建版本并发布). If you're on a
corporate Lark workspace, your admin may need to approve — usually
clears in minutes.

### 1d. Send Jimmy three values

Once the version is **published**, three values appear in the
developer console:

1. **App ID** (`Credentials & Basic Info` page) — looks like `cli_xxxxxxxxxxxx`
2. **App Secret** (same page) — 64-character random string ⚠️ **treat
   like a password**, DM only, not group chat
3. **Your own Lark Open ID** — easiest: just DM the bot you just
   created ("hi" works), then tell Jimmy "I DM'd the bot" — he'll
   pick your ID from the server log

DM Jimmy these three values + which Lark domain you're on (lark or
feishu). That's the whole setup contribution from your side.

---

## §2. The briefing call (≈30 min, Lark voice or video)

Once Jimmy has the three values, he'll have your PAID instance running
in ~10 minutes. Then we book a **30-min Lark call** — the only call
needed all week. Bring a coffee.

**The call is just a conversation.** Jimmy asks, you answer. Jimmy
takes notes and turns them into PAID's brain.

We'll cover:

### How you want PAID to sound

Things like: "I write in Chinese unless they write in English." "I'm
short and direct, no exclamation marks." "When unsure, I'd rather say
'let me check' than guess." Whatever's true for you.

### What kinds of things you'd be OK delegating

Walk through 3-5 questions your colleagues / friends commonly ping you
about. Example: "people ask my office hours / address — fine to auto-
answer." "People ask if I can meet next Tuesday — should check with
me first." "Anything about pricing or hiring — must escalate, never
auto-answer."

### What's absolutely off-limits

"Never discuss compensation." "Never confirm a commitment without
checking with me." "Never disclose introductions in flight." Etc.

### Your first 1-2 test friends

Name 1-2 colleagues / friends who've agreed to send you a few real
questions this week (you don't need to tell them yet — Jimmy walks
you through that on the call too). Each friend's Lark contact info,
Jimmy sets them up.

After the call, Jimmy writes everything into PAID. You don't see code,
config, or files — you'll just notice PAID start to respond on your
behalf in Lark.

---

## §3. During the week (≈5 min/day)

Use Lark like you normally do. PAID quietly handles inbound from the
friends Jimmy set up. You'll see three things happen in your Lark
chats:

**🟢 Auto-answered** — PAID replied for you. You can see it inline in
the chat history (nothing hidden — you're in the same chat). If
something looks off, just follow up with a correction in the chat
yourself; tell Jimmy at end of week.

**🟡 Needs your call** — PAID DMs you a card: "Friend asked X. Here's
my draft answer. Approve / Edit / Reject?" You tap one and it goes
out. If you don't answer in 30 min, PAID tells the friend "Jimmy will
get back to you directly" — so nobody's left hanging.

**🔴 Handed back to you** — PAID told the friend "I'm not authorised
to answer this, please ping Jimmy directly." Your cue to handle
manually.

### What we want you to flag

Anytime over the week, just DM Jimmy when you notice:

1. PAID gave a **wrong answer** (what was the question, what was PAID's
   reply, what would you have said)
2. PAID **escalated something you'd have trusted it to handle**
3. PAID **answered something you'd have wanted to handle yourself**
4. A friend reacted with surprise — "huh?" or "are you a bot?"

These four things are exactly what we need to tune. **Be brutal.
Negative is more useful than positive.**

### Things you don't need to do

- Look at logs ─ Jimmy watches them
- Edit anything ─ tell Jimmy what to change, he changes it
- Restart anything ─ Jimmy handles it
- Worry about data ─ everything is isolated to your private user on
  Jimmy's server (see §5)

---

## §4. The end-of-week debrief (≈30 min)

Friday or Sunday, we hop on a final 30-min call. Just three questions:

1. What 3 things worked well?
2. What 3 things didn't?
3. Would you keep using it? If yes, what would you want changed first?

This call is **the single most valuable thing you give us this week** —
more than the day-to-day usage. Honest negative feedback >> polite
positive. We want PAID to be useful for you specifically, and the
only way to get there is unfiltered reactions.

---

## §5. Privacy & control

- **Your data stays on a dedicated isolated account on Jimmy's server.**
  Separate from his and the other pilots' instances.
- **PAID only sees**: messages your test friends send to you via the
  Lark bot. PAID has zero access to your other Lark chats, your files,
  your calendar, your email.
- **At end of pilot**: three options, your call:
  - Keep PAID running (we hand over keys + a how-to)
  - Have us archive everything and shut down
  - Have us delete every trace — no questions asked
- **Source code is MIT-licensed and fully public**:
  https://github.com/jimmyag2026-prog/paid-plugin

---

## §6. If you want to stop

DM Jimmy any time and say "let's stop." We shut down your instance in
under a minute. No questions, no debrief required, no awkwardness.

---

## §7. If you're stuck before our briefing call

DM Jimmy with a screenshot. The most common stuck-point is §1c — Lark
app permissions — and Jimmy will unblock you in <5 min. Don't burn
time figuring it out alone.

---

That's the whole onboarding. Three asks of you, total:

1. ~15 min on the Lark app setup (§1)
2. ~30 min on the briefing call (§2)
3. ~5 min/day during the week + ~30 min final debrief (§3, §4)

Looking forward to it.

— Jimmy
