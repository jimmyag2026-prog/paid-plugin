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

    profile_path = storage.PAID_DIR / "counterparties" / cp.cp_id / "profile.json"
    storage.write_json(profile_path, asdict(cp))
    print(f"counterparty written: {profile_path}")
    print(json.dumps(asdict(cp), indent=2, ensure_ascii=False))
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
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
    a.set_defaults(func=_cmd_add_counterparty)

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

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    ns = parser.parse_args(argv)
    return int(ns.func(ns) or 0)


if __name__ == "__main__":  # pragma: no cover — module entry uses __main__.py
    raise SystemExit(main())
