#!/usr/bin/env python3
"""Idempotent patch for hermes-agent's feishu adapter — Lark rich-text fix.

# Problem

`hermes-agent/gateway/platforms/feishu.py::_build_outbound_payload` bails to
plain `text` msg_type whenever it sees a markdown table. That kills bold,
headings, lists, AND leaves the table itself with proportional-font
columns (misaligned).

# Fix

Wrap markdown table blocks in ``` fences before deciding msg_type. Tables
inside fences render as monospace inside Lark's post `md` renderer
(aligned columns), and the rest of the message stays in post mode so
**bold** / # headings / lists / links keep formatting.

# Usage

    python3 scripts/patch_hermes_feishu_rich_text.py [path/to/feishu.py]

If no path given, tries the conventional VPS location under
``~/.hermes/hermes-agent/gateway/platforms/feishu.py``. Re-running is
safe — a marker comment (PAID_RICH_TEXT_PATCH_VERSION) lets the script
detect prior application and exit cleanly. Re-apply after a hermes
upgrade overwrites the adapter.

# Status

Local patch until the equivalent change is upstreamed to hermes-agent.
Track upstream PR status in [[reference_hermes_feishu_allowlist]].
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PATCH_VERSION = "paid-v1.6.19"
MARKER = f"# PAID_RICH_TEXT_PATCH_VERSION = {PATCH_VERSION!r}"

DEFAULT_TARGET = Path.home() / ".hermes/hermes-agent/gateway/platforms/feishu.py"

# hermes-agent v0.13.0 shape — bails to plain text on markdown table.
OLD_BUILD_V013 = '''    def _build_outbound_payload(self, content: str) -> tuple[str, str]:
        # Feishu post-type 'md' elements do not render markdown tables; sending
        # table content as post causes the message to appear blank on the client.
        # Force plain text for anything that looks like a markdown table.
        if _MARKDOWN_TABLE_RE.search(content):
            text_payload = {"text": content}
            return "text", json.dumps(text_payload, ensure_ascii=False)
        if _MARKDOWN_HINT_RE.search(content):
            return "post", _build_markdown_post_payload(content)
        text_payload = {"text": content}
        return "text", json.dumps(text_payload, ensure_ascii=False)'''

# hermes-agent v0.12.0 shape — no table bail, but raw `|...|` rendered as
# post 'md' produces a blank message per the v0.13 comment. Same fix
# applies: wrap tables in fences so they render as monospace.
OLD_BUILD_V012 = '''    def _build_outbound_payload(self, content: str) -> tuple[str, str]:
        if _MARKDOWN_HINT_RE.search(content):
            return "post", _build_markdown_post_payload(content)
        text_payload = {"text": content}
        return "text", json.dumps(text_payload, ensure_ascii=False)'''

NEW_BUILD = f'''    def _build_outbound_payload(self, content: str) -> tuple[str, str]:
        {MARKER}
        # PAID v1.6.19 fix: wrap markdown table blocks in ``` fences so the
        # Lark post renderer treats them as monospace code (aligned columns)
        # instead of refusing to render or producing a blank message. Bold,
        # headings, lists outside the table keep their post formatting.
        if _MARKDOWN_TABLE_BLOCK_RE.search(content):
            content = _wrap_tables_in_code_fences(content)
        if _MARKDOWN_HINT_RE.search(content):
            return "post", _build_markdown_post_payload(content)
        text_payload = {{"text": content}}
        return "text", json.dumps(text_payload, ensure_ascii=False)'''

# Anchors for the helper insertion — one per known hermes shape.
HELPER_ANCHOR_V013 = (
    '_MARKDOWN_TABLE_RE = re.compile(r"^\\|.*\\|\\n\\|[-|: ]+\\|", re.MULTILINE)'
)
HELPER_ANCHOR_V012 = (
    '_MARKDOWN_LINK_RE = re.compile(r"\\[([^\\]]+)\\]\\(([^)]+)\\)")'
)

HELPER_BLOCK = '''
# PAID v1.6.19: capture a full markdown table block (header + separator +
# zero-or-more body rows) and wrap it in fenced code blocks for Lark post
# rendering. The block regex is used both as a presence check and for the
# substitution itself — _wrap_tables_in_code_fences is a no-op when no
# table is present, so the call site is safe regardless of content.
_MARKDOWN_TABLE_BLOCK_RE = re.compile(
    r"(^\\|.*\\|\\n\\|[-|: ]+\\|(?:\\n\\|.*\\|)*)",
    re.MULTILINE,
)


def _wrap_tables_in_code_fences(content: str) -> str:
    """Wrap markdown table blocks in fenced code blocks for Lark post rendering."""
    return _MARKDOWN_TABLE_BLOCK_RE.sub(
        lambda m: "\\n```\\n" + m.group(1) + "\\n```\\n",
        content,
    )
'''


def _detect_shape(src: str) -> tuple[str | None, str | None]:
    """Return (matched_build_block, matched_anchor) for the first shape
    that fits, or (None, None) if neither matches."""
    if OLD_BUILD_V013 in src and HELPER_ANCHOR_V013 in src:
        return OLD_BUILD_V013, HELPER_ANCHOR_V013
    if OLD_BUILD_V012 in src and HELPER_ANCHOR_V012 in src:
        return OLD_BUILD_V012, HELPER_ANCHOR_V012
    return None, None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "target",
        nargs="?",
        default=str(DEFAULT_TARGET),
        help=f"Path to feishu.py (default: {DEFAULT_TARGET})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing.",
    )
    args = parser.parse_args()

    target = Path(args.target).expanduser().resolve()
    if not target.is_file():
        print(f"ERROR: target not found: {target}", file=sys.stderr)
        return 2

    src = target.read_text(encoding="utf-8")

    if MARKER in src:
        print(f"[skip] {target} already patched ({PATCH_VERSION})")
        return 0

    old_build, helper_anchor = _detect_shape(src)
    if old_build is None or helper_anchor is None:
        print(
            f"ERROR: no known hermes-agent shape detected in {target}.\n"
            f"       Tried v0.13.0 build block + _MARKDOWN_TABLE_RE anchor,\n"
            f"       then v0.12.0 build block + _MARKDOWN_LINK_RE anchor.\n"
            f"       hermes-agent may have changed again — re-derive OLD_BUILD_*.",
            file=sys.stderr,
        )
        return 3

    new_src = src.replace(old_build, NEW_BUILD, 1)
    new_src = new_src.replace(
        helper_anchor,
        helper_anchor + HELPER_BLOCK,
        1,
    )

    if args.dry_run:
        digest = hashlib.sha256(new_src.encode("utf-8")).hexdigest()[:16]
        print(f"[dry-run] would write {target} ({len(new_src)} bytes, sha256:{digest}...)")
        return 0

    backup = target.with_suffix(
        target.suffix + f".bak.pre-{PATCH_VERSION}.{int(time.time())}"
    )
    shutil.copy2(target, backup)
    target.write_text(new_src, encoding="utf-8")

    # Syntax check: import as a module file to confirm we didn't break parse.
    chk = subprocess.run(
        [sys.executable, "-c", f"import ast; ast.parse(open({str(target)!r}).read())"],
        capture_output=True,
        text=True,
    )
    if chk.returncode != 0:
        # Rollback.
        shutil.copy2(backup, target)
        print(
            f"ERROR: post-patch syntax check failed; rolled back from {backup}\n"
            f"       stderr: {chk.stderr}",
            file=sys.stderr,
        )
        return 5

    print(f"[ok] patched {target}")
    print(f"     backup at {backup}")
    print(f"     marker: {PATCH_VERSION}")
    print(f"     restart hermes-gateway to pick up the change.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
