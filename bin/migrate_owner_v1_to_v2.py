#!/usr/bin/env python3
"""Migrate ~/.hermes/paid/owner.json from v1 → v2 schema in place.

v1 owner.json:
    {"owner_id": "...", "name": "...", "identities": [
       {"platform": "telegram", "user_id": "..."}
    ]}

v2 owner.json:
    {"schema_version": 2, "owner_id": "...", "name": "...",
     "preferred_platform": "<first identity's platform>",
     "identities": [
       {"platform": "telegram", "user_id": "...",
        "home_chat_id": "<= user_id by default>",
        "enabled": true}
     ]}

Idempotent: re-running on a v2 file is a no-op.

Side effects:
  - writes owner.json (new content)
  - writes owner.json.v1.bak (single backup; preserves prior bak if any)

Exit codes:
  0  success or no-op
  1  unexpected exception
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from paid import storage  # noqa: E402


_SCHEMA_VERSION = 2


def _backup_path(owner_path: Path) -> Path:
    """Return a non-clobbering backup path next to owner.json."""
    bak = owner_path.with_suffix(".json.v1.bak")
    if bak.exists():
        # If a v1 bak already exists from a prior failed run, time-stamp
        # this one rather than overwrite so debug history is preserved.
        bak = owner_path.with_suffix(f".json.v1.bak.{int(time.time())}")
    return bak


def upgrade_payload(data: dict) -> tuple[dict, list[str]]:
    """Return (new_payload, change_log). change_log lists what changed.

    Pure function for unit testing — no I/O.
    """
    if not isinstance(data, dict):
        raise ValueError(f"owner.json root must be dict, got {type(data).__name__}")

    changes: list[str] = []
    schema = int(data.get("schema_version", 1) or 1)
    if schema >= _SCHEMA_VERSION:
        return data, []  # already migrated

    new_data = dict(data)  # shallow copy

    # 1. Stamp schema_version
    new_data["schema_version"] = _SCHEMA_VERSION
    changes.append(f"schema_version: {schema} → {_SCHEMA_VERSION}")

    # 2. Identities — backfill home_chat_id + enabled
    raw = new_data.get("identities", [])
    if not isinstance(raw, list):
        raw = []
    upgraded_ids: list[dict] = []
    for ident in raw:
        if not isinstance(ident, dict):
            continue
        new_ident = dict(ident)
        if "home_chat_id" not in new_ident or not new_ident.get("home_chat_id"):
            new_ident["home_chat_id"] = new_ident.get("user_id", "")
            changes.append(
                f"identities[{ident.get('platform','?')}]: home_chat_id "
                f"← user_id ({new_ident['home_chat_id']!r})"
            )
        if "enabled" not in new_ident:
            new_ident["enabled"] = True
            changes.append(f"identities[{ident.get('platform','?')}]: enabled ← True")
        upgraded_ids.append(new_ident)
    new_data["identities"] = upgraded_ids

    # 3. preferred_platform
    if not new_data.get("preferred_platform"):
        if upgraded_ids:
            first_plat = str(upgraded_ids[0].get("platform", "")) or ""
            new_data["preferred_platform"] = first_plat
            if first_plat:
                changes.append(f"preferred_platform: '' → {first_plat!r}")
        else:
            new_data["preferred_platform"] = ""

    return new_data, changes


def migrate(owner_path: Path, *, dry_run: bool = False) -> int:
    if not owner_path.exists():
        print(f"no owner.json at {owner_path}")
        return 0

    raw_text = owner_path.read_text()
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        print(f"ERROR: owner.json is not valid JSON: {e}", file=sys.stderr)
        return 1

    new_data, changes = upgrade_payload(data)
    if not changes:
        print(f"owner.json already at schema_version={_SCHEMA_VERSION} — no-op")
        return 0

    print("Changes to apply:")
    for c in changes:
        print(f"  - {c}")

    if dry_run:
        print("--dry-run: not writing.")
        return 0

    bak = _backup_path(owner_path)
    bak.write_text(raw_text)
    print(f"Wrote backup: {bak}")

    owner_path.write_text(json.dumps(new_data, ensure_ascii=False, indent=2))
    print(f"Migrated: {owner_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paid-dir", default=None,
                        help="override PAID_DIR (default: ~/.hermes/paid)")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would change without writing")
    args = parser.parse_args()

    if args.paid_dir:
        storage.PAID_DIR = Path(args.paid_dir)
    owner_path = storage.PAID_DIR / "owner.json"
    print(f"owner.json path: {owner_path}")
    return migrate(owner_path, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
