#!/usr/bin/env python3
"""Migrate counterparty profile.json files from v1 → v2 schema in place.

v1 schema (pre-2026-05-02) had no:
  - ignore_reason / ignore_set_at  (added when J4 mark_ignored shipped)
  - discovery_notified_at          (added when J4 discovery card shipped)
  - active_review_session / review_history  (added for paid-review skill)
  - schema_version                 (added for explicitness)

PAID's counterparty reader (identity.load_counterparty) already tolerates
v1 records by filling defaults at read time — so this migration is NOT
required for correctness. What it gives you:

  - dashboards / one-off jq queries see a uniform schema
  - 'jq .schema_version' actually works on every file
  - debugging is easier (no "is this v1 or v2?" branching mentally)

Idempotent. Re-running on a v2 file is a no-op.

Side effects per upgraded file:
  - writes profile.json (new content)
  - writes profile.json.v1.bak (single backup; preserves prior bak if any)

Exit codes:
  0  success or no-op
  1  unexpected exception while reading any profile (continues other files)
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

# All fields the v2 reader populates with defaults — make them explicit
# on disk so 'jq' / dashboard see a uniform shape.
_V2_DEFAULTS: dict[str, object] = {
    "ignore_reason": "",
    "ignore_set_at": "",
    "discovery_notified_at": "",
    "active_review_session": "",
    "review_history": [],
    # v1.4.5 (backlog v1.4.7): added field for per-cp blacklist routing.
    # Default "decline" preserves pre-v1.4.5 behavior on existing profiles.
    "blacklist_action": "decline",
}


def _backup_path(profile_path: Path) -> Path:
    bak = profile_path.with_suffix(".json.v1.bak")
    if bak.exists():
        bak = profile_path.with_suffix(f".json.v1.bak.{int(time.time())}")
    return bak


def upgrade_cp_payload(data: dict) -> tuple[dict, list[str]]:
    """Pure function: returns (new_payload, change_log).

    Tests can call this directly without disk I/O.
    """
    if not isinstance(data, dict):
        raise ValueError(f"profile.json root must be dict, got {type(data).__name__}")

    changes: list[str] = []
    schema = int(data.get("schema_version", 1) or 1)
    new_data = dict(data)  # shallow copy

    if schema < _SCHEMA_VERSION:
        new_data["schema_version"] = _SCHEMA_VERSION
        changes.append(f"schema_version: {schema} → {_SCHEMA_VERSION}")

    for key, default in _V2_DEFAULTS.items():
        if key not in new_data:
            new_data[key] = default if not isinstance(default, list) else list(default)
            changes.append(f"{key}: ← {default!r}")

    return new_data, changes


def migrate_one(profile_path: Path, *, dry_run: bool = False) -> tuple[bool, list[str]]:
    """Upgrade a single profile.json. Returns (changed, changes_log).

    `changed=False` means already v2 / no-op.
    """
    raw_text = profile_path.read_text()
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise ValueError(f"{profile_path}: not valid JSON ({e})") from e

    new_data, changes = upgrade_cp_payload(data)
    if not changes:
        return False, []

    if dry_run:
        return True, changes

    bak = _backup_path(profile_path)
    bak.write_text(raw_text)
    profile_path.write_text(json.dumps(new_data, ensure_ascii=False, indent=2))
    return True, changes


def migrate_all(cp_root: Path, *, dry_run: bool = False) -> int:
    """Walk counterparties/*/profile.json and upgrade each. Returns number
    of profiles changed."""
    if not cp_root.exists():
        print(f"no counterparties/ dir at {cp_root}")
        return 0

    upgraded = 0
    skipped = 0
    failed = 0
    for child in sorted(cp_root.iterdir()):
        if not child.is_dir():
            continue
        profile = child / "profile.json"
        if not profile.exists():
            continue
        try:
            changed, log = migrate_one(profile, dry_run=dry_run)
        except Exception as exc:
            print(f"  [{child.name}] ERROR: {exc}")
            failed += 1
            continue
        if changed:
            upgraded += 1
            print(f"  [{child.name}] upgrading:")
            for line in log:
                print(f"    - {line}")
            if dry_run:
                print(f"    --dry-run: skipped write")
            else:
                print(f"    wrote backup: {child.name}/profile.json.v1.bak")
        else:
            skipped += 1
            print(f"  [{child.name}] already v{_SCHEMA_VERSION} — skipped")

    print()
    print(f"summary: {upgraded} upgraded, {skipped} skipped, {failed} failed")
    return upgraded if failed == 0 else -1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paid-dir", default=None,
                        help="override PAID_DIR (default: ~/.hermes/paid)")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would change without writing")
    args = parser.parse_args()

    if args.paid_dir:
        storage.PAID_DIR = Path(args.paid_dir)
    cp_root = storage.PAID_DIR / "counterparties"
    print(f"counterparties root: {cp_root}")
    rc = migrate_all(cp_root, dry_run=args.dry_run)
    return 0 if rc >= 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
