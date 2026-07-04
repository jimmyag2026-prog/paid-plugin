"""Patch hermes-agent so an @all in a Feishu group is NOT treated as a
mention of the bot.

Upstream `gateway/platforms/feishu.py::_mentions_self` returns True on
``"@_all" in raw_content`` before even looking at the mentions[] list.
That makes every "@所有人" broadcast pass the require_mention gate and
get routed into PAID — exactly what owners do NOT want from a group bot.

This script removes the early-return on @_all. Real @bot mentions still
match through the mentions[] check below (keyed by open_id), so
"@bot" and "@all + @bot" continue to work; "@all alone" now falls
through to admission rejection.

Discipline (per repo convention for upstream patches):

- Idempotent: detects a sentinel marker in the patched body and no-ops.
- Backed up: writes ``<target>.bak.paid-atall-<ts>`` before mutating.
- Safe: parses the patched file with ``ast`` and rolls back the backup
  if parsing fails.
- Dry-run capable: ``--dry-run`` prints the planned diff and exits.

Usage:

    python scripts/patches/feishu_atall_not_bot_mention.py \
        [--hermes-dir ~/.hermes/hermes-agent] [--dry-run] [--revert]

Exit codes:
    0  patched, already-patched (no-op), or dry-run preview OK
    1  target file missing / unrecognized layout
    2  AST validation failed (backup restored)
"""

from __future__ import annotations

import argparse
import ast
import difflib
import shutil
import sys
import time
from pathlib import Path

MARKER = "PAID-PATCH atall-not-bot-mention v1"

ORIGINAL_BLOCK = """    def _mentions_self(self, message: Any) -> bool:
        # @_all is Feishu's @everyone placeholder.
        raw_content = getattr(message, "content", "") or ""
        if "@_all" in raw_content:
            return True
        mentions = getattr(message, "mentions", None) or []
        if mentions and self._message_mentions_bot(mentions):
            return True
        normalized = normalize_feishu_message(
            message_type=getattr(message, "message_type", "") or "",
            raw_content=raw_content,
            mentions=getattr(message, "mentions", None),
            bot=self._bot_identity(),
        )
        return self._post_mentions_bot(normalized.mentions)
"""

PATCHED_BLOCK = """    def _mentions_self(self, message: Any) -> bool:
        # {marker}: @_all is Feishu's @everyone placeholder and must NOT
        # count as a mention of the bot. Otherwise any "@所有人" broadcast
        # passes require_mention and triggers every bot in the group.
        # Real @bot mentions still match through the mentions[] check
        # below, so "@bot" and "@all + @bot" continue to admit.
        raw_content = getattr(message, "content", "") or ""
        mentions = getattr(message, "mentions", None) or []
        if mentions and self._message_mentions_bot(mentions):
            return True
        normalized = normalize_feishu_message(
            message_type=getattr(message, "message_type", "") or "",
            raw_content=raw_content,
            mentions=getattr(message, "mentions", None),
            bot=self._bot_identity(),
        )
        return self._post_mentions_bot(normalized.mentions)
""".format(marker=MARKER)


def _target(hermes_dir: Path) -> Path:
    return hermes_dir / "gateway" / "platforms" / "feishu.py"


def _print_diff(before: str, after: str, target: Path) -> None:
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{target}",
        tofile=f"b/{target}",
    )
    sys.stdout.writelines(diff)


def apply_patch(hermes_dir: Path, *, dry_run: bool, revert: bool) -> int:
    target = _target(hermes_dir)
    if not target.is_file():
        print(f"ERROR: target file not found: {target}", file=sys.stderr)
        return 1

    src = target.read_text(encoding="utf-8")

    if revert:
        if MARKER not in src:
            print(f"no-op: {target} is not patched (marker absent)")
            return 0
        if PATCHED_BLOCK not in src:
            print(
                "ERROR: marker present but patched block shape changed; "
                "manual revert required",
                file=sys.stderr,
            )
            return 1
        new_src = src.replace(PATCHED_BLOCK, ORIGINAL_BLOCK, 1)
        action = "REVERT"
    else:
        if MARKER in src:
            print(f"no-op: {target} already patched")
            return 0
        if ORIGINAL_BLOCK not in src:
            print(
                f"ERROR: expected upstream block not found in {target}. "
                "Hermes version may have changed; refusing to guess.",
                file=sys.stderr,
            )
            return 1
        new_src = src.replace(ORIGINAL_BLOCK, PATCHED_BLOCK, 1)
        action = "APPLY"

    if dry_run:
        print(f"[dry-run] would {action.lower()} patch on {target}:")
        _print_diff(src, new_src, target)
        return 0

    ts = time.strftime("%Y%m%dT%H%M%S")
    backup = target.with_suffix(target.suffix + f".bak.paid-atall-{ts}")
    shutil.copy2(target, backup)
    target.write_text(new_src, encoding="utf-8")

    try:
        ast.parse(new_src, filename=str(target))
    except SyntaxError as exc:
        shutil.copy2(backup, target)
        print(
            f"ERROR: patched file failed ast.parse ({exc}); "
            f"restored from {backup}",
            file=sys.stderr,
        )
        return 2

    print(f"{action}: patched {target}")
    print(f"  backup: {backup}")
    print(f"  marker: {MARKER}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--hermes-dir",
        default=str(Path.home() / ".hermes" / "hermes-agent"),
        help="Hermes agent source dir (default: ~/.hermes/hermes-agent)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--revert",
        action="store_true",
        help="Restore upstream block (only if marker is present).",
    )
    args = parser.parse_args()
    return apply_patch(
        Path(args.hermes_dir).expanduser(),
        dry_run=args.dry_run,
        revert=args.revert,
    )


if __name__ == "__main__":
    sys.exit(main())
