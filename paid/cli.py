"""Module CLI — minimal command-line entry for tester onboarding.

Usage:
    python -m paid setup [--owner-id ID] [--name NAME] [--platform P --user-id U] ...
    python -m paid add-counterparty <platform> <user_id> [--name N] [--role R]
    python -m paid status
    python -m paid pending
    python -m paid approve <id> [text...]
    python -m paid reject  <id>

Idempotent: re-running ``setup`` does NOT overwrite existing files. Use
``--force`` to overwrite.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from . import approval, identity, storage


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

_PERSONA_TEMPLATE = """\
# {owner_name} — Persona

> Edit this file to describe how you want PAID to speak when responding on
> your behalf. PAID reads it on every classified-as-direct reply.

## Voice
- {owner_name} is direct, brief, and prefers concrete examples over jargon.
- Replies in the same language the question came in (中文 / English).

## Background
- (Fill in: role, company, expertise, what you usually help people with.)

## Scope you're happy answering on behalf
- (e.g. technical questions about your open-source projects)
- (e.g. logistics like meeting times, address)

## Topics PAID should always escalate to you (not auto-answer)
- equity / compensation
- hiring decisions
- anything financial > $50
- legal / contracts
- introductions to other people
"""

_SOP_TEMPLATE = """\
# {owner_name} — SOP (Standard Operating Procedures)

> PAID reads relevant excerpts from this file when answering. Keep paragraphs
> short and topic-tagged; the substring scorer in retrieval.py picks the best
> match per question.

## How to handle scheduling questions
- Default time zone: (your TZ)
- Office hours: (e.g. 10am–6pm PT, Mon–Fri)
- Default meeting length: 25 min, 5 min buffer between

## How to introduce projects
- (Project A — one-paragraph elevator pitch)
- (Project B — one-paragraph elevator pitch)

## Things to NEVER say
- Anything about ongoing negotiations.
- Anyone's personal contact info that wasn't shared publicly.
"""


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _cmd_setup(args: argparse.Namespace) -> int:
    storage.ensure_dirs()
    owner_path = storage.PAID_DIR / "owner.json"
    persona_path = storage.PAID_DIR / "persona.md"
    sop_path = storage.PAID_DIR / "sop.md"
    settings_path = storage.PAID_DIR / "settings.json"

    created: list[str] = []
    skipped: list[str] = []

    # owner.json
    if owner_path.exists() and not args.force:
        skipped.append(str(owner_path))
    else:
        identities: list[dict] = []
        for pair in args.identity or []:
            if ":" not in pair:
                print(f"setup: --identity expects platform:user_id, got {pair!r}", file=sys.stderr)
                return 2
            plat, uid = pair.split(":", 1)
            identities.append({"platform": plat.strip(), "user_id": uid.strip()})
        owner_doc = {
            "owner_id": args.owner_id,
            "name": args.name,
            "identities": identities,
        }
        storage.write_json(owner_path, owner_doc)
        created.append(str(owner_path))

    # persona.md
    if persona_path.exists() and not args.force:
        skipped.append(str(persona_path))
    else:
        persona_path.write_text(
            _PERSONA_TEMPLATE.format(owner_name=args.name or args.owner_id),
            encoding="utf-8",
        )
        created.append(str(persona_path))

    # sop.md
    if sop_path.exists() and not args.force:
        skipped.append(str(sop_path))
    else:
        sop_path.write_text(
            _SOP_TEMPLATE.format(owner_name=args.name or args.owner_id),
            encoding="utf-8",
        )
        created.append(str(sop_path))

    # settings.json (defaults)
    if settings_path.exists() and not args.force:
        skipped.append(str(settings_path))
    else:
        storage.write_json(
            settings_path,
            {
                "update_mode": "confirm-each",
                "model_override": "",
            },
        )
        created.append(str(settings_path))

    print("PAID setup:")
    for p in created:
        print(f"  + {p}")
    for p in skipped:
        print(f"  · {p}  (kept; use --force to overwrite)")
    print()
    print("Next: edit persona.md and sop.md, then enable the plugin:")
    print("  hermes plugins enable paid-v1")
    return 0


def _cmd_add_counterparty(args: argparse.Namespace) -> int:
    cp = identity.ensure_counterparty(args.platform, args.user_id, display_name=args.name or "")
    if args.role:
        cp.role = args.role
    if args.topic_allow:
        cp.topics_allowed = list(args.topic_allow)
    if args.topic_escalate:
        cp.topics_always_escalate = list(args.topic_escalate)
    if args.notes:
        cp.notes = args.notes
    # v1.4.5 (backlog v1.4.7): owner picks what happens when classifier
    # flags a topic as blacklisted for this cp — decline-to-cp or request
    # (approval card). Default "decline" preserves pre-v1.4.5 behavior.
    if getattr(args, "blacklist_action", None):
        if args.blacklist_action not in ("decline", "request"):
            print(
                f"ERROR: --blacklist-action must be 'decline' or 'request' "
                f"(got {args.blacklist_action!r})",
                file=sys.stderr,
            )
            return 2
        cp.blacklist_action = args.blacklist_action

    profile_path = storage.PAID_DIR / "counterparties" / cp.cp_id / "profile.json"
    storage.write_json(profile_path, asdict(cp))
    print(f"counterparty written: {profile_path}")
    print(json.dumps(asdict(cp), indent=2, ensure_ascii=False))
    return 0


def _cmd_ask_counterparty(args: argparse.Namespace) -> int:
    """Send a clarifying question to a counterparty without granting them
    junior status yet. Used from the discovery card flow when the owner
    isn't sure who's pinging them and wants to ask before classifying.

    The message is sent *as the owner's PAID assistant* via send_dm, so it
    works on whatever IM the counterparty contacted us on. Outbound queue
    fallback applies if the gateway isn't running.
    """
    from . import hermes_io, identity as _identity
    owner = _identity.load_owner()
    owner_name = _identity.display_name(owner)
    body = (
        f"嗨,我是 {owner_name} 的 AI 助理。在帮 {owner_name} 处理消息前,他想先和你确认一件事:\n\n"
        f"{args.question}\n\n"
        f"— {owner_name}'s PAID"
        if any("一" <= c <= "鿿" for c in args.question)
        else
        f"Hi — I'm {owner_name}'s AI assistant. Before relaying your message, "
        f"{owner_name} wants to confirm one thing:\n\n"
        f"{args.question}\n\n"
        f"— {owner_name}'s PAID"
    )
    result = hermes_io.send_dm(
        args.platform, args.user_id, body, fallback_to_queue=True
    )
    print(f"sent to {args.platform}:{args.user_id} → {result}")
    return 0


def _cmd_ignore_counterparty(args: argparse.Namespace) -> int:
    cp = identity.load_counterparty(args.platform, args.user_id)
    if cp is None:
        # Auto-create as pending so we can record the ignore decision; this
        # matches the discovery card flow where unknown senders may not yet
        # have a profile.
        cp = identity.ensure_counterparty(args.platform, args.user_id)
    identity.mark_ignored(cp, reason=args.reason)
    print(
        f"ignored {cp.cp_id}: reason={cp.ignore_reason!r}  "
        f"set_at={cp.ignore_set_at}"
    )
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    """Read-only health + activity dashboard for the operator.

    Goes beyond simple counts: surfaces today's direct-rate, L1/L4 hits,
    most active counterparties, and the latest few inbound questions —
    so the operator can sanity-check PAID behaviour at a glance instead
    of grepping audit_log.jsonl by hand.
    """
    import json as _json
    from collections import Counter, defaultdict
    from datetime import datetime, timezone
    from typing import Any as _Any

    owner = identity.load_owner()
    cp_root = storage.PAID_DIR / "counterparties"
    cp_count = (
        sum(1 for c in cp_root.iterdir() if c.is_dir())
        if cp_root.exists() else 0
    )
    pendings = approval.list_pending() if approval._pending_log().exists() else []
    queue_path = storage.PAID_DIR / "outbound_queue.jsonl"
    queue_lines = 0
    if queue_path.exists():
        with queue_path.open("r", encoding="utf-8") as f:
            queue_lines = sum(1 for _ in f)

    print(f"PAID dir: {storage.PAID_DIR}")
    if owner is None:
        print("  owner: NOT CONFIGURED  (run: python -m paid setup)")
    else:
        print(f"  owner: {owner.name or owner.owner_id}  identities={len(owner.identities)}")
    print(f"  counterparties: {cp_count}")
    print(f"  pending approvals: {len(pendings)}")
    print(f"  outbound queue (unsent): {queue_lines}")

    # ---- today's metrics (UTC day) ----
    audit_path = storage.PAID_DIR / "audit_log.jsonl"
    if not audit_path.exists():
        return 0

    today = datetime.now(timezone.utc).date().isoformat()
    state_counts: Counter[str] = Counter()
    l1_hits = 0
    l4_hits = 0
    fallback_hits = 0
    cp_last_seen: dict[str, str] = {}
    recent_qs: list[tuple[str, str, str, str]] = []  # (ts, cp, state, q)

    for line in audit_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = _json.loads(line)
        except Exception:
            continue
        ts = row.get("ts", "")
        is_today = ts.startswith(today)
        action = row.get("action") or {}
        state = action.get("state") if isinstance(action, dict) else None
        cp = row.get("counterparty")
        msg = row.get("junior_msg", "")
        extra = row.get("extra") or {}

        if cp and ts:
            prev = cp_last_seen.get(cp, "")
            if ts > prev:
                cp_last_seen[cp] = ts

        if is_today and state:
            state_counts[state] += 1
            if msg:
                recent_qs.append((ts, cp or "(none)", state, msg))
            if extra.get("blocked_by") == "layer_1_prompt_injection":
                l1_hits += 1
            if extra.get("l4_ok") is False:
                l4_hits += 1
            if extra.get("fallback") is True:
                fallback_hits += 1

    total = sum(state_counts.values())
    if total:
        direct_rate = state_counts.get("direct", 0) / total * 100
        print()
        print(f"  today (UTC {today}):")
        print(
            f"    decisions: direct {state_counts.get('direct',0)} · "
            f"request {state_counts.get('request',0)} · "
            f"decline {state_counts.get('decline',0)}"
        )
        print(
            f"    direct-rate: {direct_rate:.0f}%  "
            + ("(target ≥50%)" if direct_rate >= 50 else "(below 50% target)")
        )
        if l1_hits:
            print(f"    L1 prompt-injection hits: {l1_hits}")
        if l4_hits:
            print(f"    L4 output-leak hits: {l4_hits}")
        if fallback_hits:
            print(f"    classifier fallback (LLM error): {fallback_hits}")

    # ---- counterparties sorted by last seen (most-active first) ----
    if cp_last_seen:
        print()
        print("  counterparties by last seen:")
        ordered = sorted(cp_last_seen.items(), key=lambda kv: kv[1], reverse=True)
        for cp_id, ts in ordered[:10]:
            print(f"    {cp_id}  last: {ts[:19]}")
        if len(ordered) > 10:
            print(f"    ... +{len(ordered) - 10} more")

    # ---- last 5 inbound questions today (newest first) ----
    if recent_qs:
        print()
        print("  recent activity (today, newest 5):")
        recent_qs.sort(key=lambda t: t[0], reverse=True)
        for ts, cp, state, q in recent_qs[:5]:
            qsnip = q.replace("\n", " ")[:80]
            print(f"    {ts[11:19]}  [{state}]  {cp}  → {qsnip}")

    return 0


def _cmd_pending(args: argparse.Namespace) -> int:
    pendings = approval.list_pending()
    if not pendings:
        print("(no pending approvals)")
        return 0
    for r in pendings:
        print(
            f"#{r.request_id}  {r.counterparty_display or r.counterparty_user_id} "
            f"({r.counterparty_platform})  topic={r.topic}  conf={r.confidence:.2f}"
        )
        print(f"  Q: {r.junior_question[:140]}")
    return 0


def _cmd_approve(args: argparse.Namespace) -> int:
    """CLI approve — does NOT call gateway send_dm (no live gateway in CLI).

    Writes status=approved with the final text; gateway-side workers
    (the slash-command path) are responsible for actual delivery.
    """
    rid = args.request_id.lstrip("#")
    req = approval.get(rid)
    if req is None:
        print(f"unknown request id #{rid}", file=sys.stderr)
        return 2
    final_text = " ".join(args.text or []).strip() or req.draft_answer
    if not final_text:
        print("no draft and no text provided; supply trailing text", file=sys.stderr)
        return 2
    owner = identity.load_owner()
    owner_name = identity.display_name(owner)
    decorated = f"{owner_name} 看了你的问题：\n\n{final_text}"
    approval.set_status(rid, "approved", final_text=decorated)
    # Best-effort live send. Falls back to outbound_queue.jsonl when no gateway.
    from . import hermes_io
    res = hermes_io.send_dm(
        req.counterparty_platform, req.counterparty_user_id, decorated, fallback_to_queue=True
    )
    print(f"#{rid} approved.  delivery={res}")
    return 0


def _cmd_reject(args: argparse.Namespace) -> int:
    rid = args.request_id.lstrip("#")
    req = approval.get(rid)
    if req is None:
        print(f"unknown request id #{rid}", file=sys.stderr)
        return 2
    owner = identity.load_owner()
    owner_name = identity.display_name(owner)
    msg = f"{owner_name} 会直接回复你。"
    approval.set_status(rid, "rejected", final_text=msg)
    from . import hermes_io
    res = hermes_io.send_dm(
        req.counterparty_platform, req.counterparty_user_id, msg, fallback_to_queue=True
    )
    print(f"#{rid} rejected.  delivery={res}")
    return 0


# ---------------------------------------------------------------------------
# Argparse glue
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="paid", description="PAID v0.5 CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="Generate owner/persona/sop/settings templates.")
    s.add_argument("--owner-id", default="owner_user")
    s.add_argument("--name", default="")
    s.add_argument(
        "--identity",
        action="append",
        metavar="PLATFORM:USER_ID",
        help="Add an owner identity (repeatable).",
    )
    s.add_argument("--force", action="store_true", help="Overwrite existing files.")
    s.set_defaults(func=_cmd_setup)

    a = sub.add_parser("add-counterparty", help="Manually register a counterparty.")
    a.add_argument("platform")
    a.add_argument("user_id")
    a.add_argument("--name", default="")
    a.add_argument(
        "--role",
        choices=["junior", "external", "ignored", "blocked", "pending"],
        default="junior",
    )
    a.add_argument("--topic-allow", action="append", default=[])
    a.add_argument("--topic-escalate", action="append", default=[])
    a.add_argument("--notes", default="")
    a.add_argument(
        "--blacklist-action",
        choices=["decline", "request"],
        default=None,
        help=(
            "How to route classifier-flagged blacklist topics: 'decline' "
            "(default, tell cp to ping owner directly) or 'request' "
            "(escalate to owner via approval card)."
        ),
    )
    a.set_defaults(func=_cmd_add_counterparty)

    ig = sub.add_parser(
        "ignore-counterparty",
        help="Mark a counterparty as ignored (silent reply, no escalation).",
    )
    ig.add_argument("platform")
    ig.add_argument("user_id")
    ig.add_argument(
        "--reason",
        default="",
        help="Why you ignored them — surfaced on the dashboard / discovery flow.",
    )
    ig.set_defaults(func=_cmd_ignore_counterparty)

    ak = sub.add_parser(
        "ask-counterparty",
        help="Send a clarifying question to a counterparty (no role change).",
    )
    ak.add_argument("platform")
    ak.add_argument("user_id")
    ak.add_argument("question", help="Plain text — the question to send.")
    ak.set_defaults(func=_cmd_ask_counterparty)

    st = sub.add_parser("status", help="Show summary of PAID state.")
    st.set_defaults(func=_cmd_status)

    pn = sub.add_parser("pending", help="List pending approvals.")
    pn.set_defaults(func=_cmd_pending)

    ap = sub.add_parser("approve", help="Approve a pending request and send to junior.")
    ap.add_argument("request_id")
    ap.add_argument("text", nargs="*", help="Optional override text (default: draft).")
    ap.set_defaults(func=_cmd_approve)

    rj = sub.add_parser("reject", help="Reject a pending request.")
    rj.add_argument("request_id")
    rj.set_defaults(func=_cmd_reject)

    db = sub.add_parser(
        "dashboard",
        help="Run the local read-only web dashboard (loopback only by default).",
    )
    db.add_argument("--host", default="127.0.0.1",
                    help="Bind address (default 127.0.0.1; pass 0.0.0.0 to expose).")
    db.add_argument("--port", type=int, default=7777, help="Bind port (default 7777).")
    db.set_defaults(func=_cmd_dashboard)

    return p


def _cmd_dashboard(args: argparse.Namespace) -> int:
    from . import dashboard
    dashboard.serve(host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv)
    return int(ns.func(ns) or 0)


if __name__ == "__main__":  # pragma: no cover — module entry uses __main__.py
    raise SystemExit(main())
