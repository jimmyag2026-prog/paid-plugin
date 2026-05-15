#!/usr/bin/env python3
"""Migrate legacy single-file audit + pending logs to per-cp physical isolation (v1.6.4).

Reads:
  ~/.hermes/paid/audit_log.jsonl         → counterparties/<cp_id>/audit.jsonl
  ~/.hermes/paid/pending_approvals.jsonl → counterparties/<cp_id>/pending.jsonl

After migration, legacy files are renamed to *.migrated_v1.6.4 so the
grace-period read path finds nothing there and only reads per-cp files.

Usage:
  python -m bin.migrate_to_per_cp_audit [--dry-run] [--force]

Exit codes:
  0  success (or dry-run preview)
  1  error
  2  already migrated (both legacy files missing/empty)
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def migrate(
    paid_dir: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> int:
    from paid import storage

    if paid_dir is not None:
        storage.PAID_DIR = paid_dir
    base = storage.PAID_DIR

    audit_src = base / "audit_log.jsonl"
    pending_src = base / "pending_approvals.jsonl"

    # Check already migrated
    migrated_audit = base / "audit_log.jsonl.migrated_v1.6.4"
    migrated_pending = base / "pending_approvals.jsonl.migrated_v1.6.4"
    if migrated_audit.exists() and migrated_pending.exists() and not force:
        print("Already migrated (found *.migrated_v1.6.4 marker files). Use --force to re-run.")
        return 2

    total_audit = _migrate_file(
        src=audit_src,
        dest_fn=lambda entry: base / "counterparties" / _safe(entry.get("counterparty") or "unknown") / "audit.jsonl",
        label="audit_log.jsonl",
        dry_run=dry_run,
    )
    total_pending = _migrate_file(
        src=pending_src,
        dest_fn=lambda entry: base / "counterparties" / _safe(entry.get("counterparty_id") or "unknown") / "pending.jsonl",
        label="pending_approvals.jsonl",
        dry_run=dry_run,
    )

    if not dry_run:
        # Rename legacy files so grace-period reads find nothing
        for src, dst in [(audit_src, migrated_audit), (pending_src, migrated_pending)]:
            if src.exists():
                src.rename(dst)
                print(f"  renamed {src.name} → {dst.name}")

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Migration complete.")
    print(f"  audit entries migrated:   {total_audit}")
    print(f"  pending entries migrated: {total_pending}")
    return 0


def _safe(cp_id: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9_\-@.]", "_", str(cp_id)) or "unknown"


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    rows.append(obj)
            except json.JSONDecodeError:
                pass
    return rows


def _migrate_file(
    src: Path,
    dest_fn,  # callable[[dict], Path]
    label: str,
    dry_run: bool,
) -> int:
    rows = _read_jsonl(src)
    if not rows:
        print(f"  {label}: empty or missing, skip.")
        return 0

    grouped: dict[Path, list[dict]] = defaultdict(list)
    for row in rows:
        dest = dest_fn(row)
        grouped[dest].append(row)

    for dest_path, entries in grouped.items():
        cp_dir = dest_path.parent
        print(f"  → {dest_path.relative_to(dest_path.parent.parent.parent)} ({len(entries)} entries)")
        if not dry_run:
            cp_dir.mkdir(parents=True, exist_ok=True)
            with dest_path.open("a", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    return len(rows)


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    force = "--force" in sys.argv
    if dry_run:
        print("[DRY RUN] No files will be written.\n")
    return migrate(dry_run=dry_run, force=force)


if __name__ == "__main__":
    sys.exit(main())
