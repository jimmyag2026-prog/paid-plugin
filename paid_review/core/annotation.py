"""Annotation dataclass + append-only jsonl IO with atomic status updates."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

VALID_STATUSES: frozenset[str] = frozenset(
    {"open", "accepted", "rejected", "modified", "unresolvable"}
)

CLOSED_STATUSES: frozenset[str] = frozenset(
    {"accepted", "rejected", "modified", "unresolvable"}
)


@dataclass
class Annotation:
    """One review finding with its current disposition."""

    id: str
    pillar: str        # intent | data | logic | feasibility | stakeholder | risk | roi
    text: str
    status: str = "open"
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = _now_iso()
        if not self.updated_at:
            self.updated_at = self.created_at
        if self.status not in VALID_STATUSES:
            raise ValueError(f"Invalid annotation status: {self.status!r}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _annotations_path(sid_dir: Path) -> Path:
    return sid_dir / "annotations.jsonl"


def load_annotations(sid_dir: Path) -> list[Annotation]:
    """Read all annotations from annotations.jsonl. Returns [] if missing."""
    path = _annotations_path(sid_dir)
    if not path.exists():
        return []
    result: list[Annotation] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            result.append(
                Annotation(
                    id=d["id"],
                    pillar=d.get("pillar", ""),
                    text=d.get("text", ""),
                    status=d.get("status", "open"),
                    created_at=d.get("created_at", ""),
                    updated_at=d.get("updated_at", ""),
                )
            )
        except (KeyError, ValueError):
            continue
    return result


def append_annotation(sid_dir: Path, ann: Annotation) -> None:
    """Append one annotation line (append-only; fsynced)."""
    from paid import storage
    sid_dir.mkdir(parents=True, exist_ok=True)
    storage.append_jsonl(_annotations_path(sid_dir), asdict(ann))


def update_status(sid_dir: Path, ann_id: str, new_status: str) -> None:
    """Atomically update the status of annotation *ann_id* via tmp-rename rewrite.

    Raises ValueError if new_status is not a valid status or ann_id not found.
    """
    if new_status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {new_status!r}")

    annotations = load_annotations(sid_dir)
    found = False
    for ann in annotations:
        if ann.id == ann_id:
            ann.status = new_status
            ann.updated_at = _now_iso()
            found = True
            break

    if not found:
        raise ValueError(f"Annotation {ann_id!r} not found in {sid_dir}")

    path = _annotations_path(sid_dir)
    tmp = path.with_suffix(".tmp")
    lines = [json.dumps(asdict(a), ensure_ascii=False) for a in annotations]
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with tmp.open("r", encoding="utf-8") as fh:
        os.fsync(fh.fileno())
    tmp.replace(path)


def open_count(sid_dir: Path) -> int:
    """Count annotations with status='open'."""
    return sum(1 for a in load_annotations(sid_dir) if a.status == "open")


def all_resolved(sid_dir: Path) -> bool:
    """True when no annotation has status='open'."""
    return open_count(sid_dir) == 0
