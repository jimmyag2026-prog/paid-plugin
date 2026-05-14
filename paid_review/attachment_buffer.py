"""Per-cp in-memory attachment buffer for v1.5 multimedia review (v1.5.4).

Lark (and some other IM platforms) delivers ``/review`` text and media
attachments as separate inbound events when both are sent in the same
chat action by the user. The ``/review`` event arrives first with no
attachments; the media arrives 1-10 seconds later. Without buffering,
the media never reaches PAID's review ingest pipeline — it's processed
by hermes' main agent (Claude vision pre-analyze) and the file path is
lost from the review session's perspective.

This module is the buffer that bridges that gap:

  Order A — image first, then /review:
    media event → add(path, mime) to buffer
    /review event → drain() returns buffered paths
    intake() runs with both message text + buffered media

  Order B — /review first, then image:
    /review event → opens session normally (no buffer hit yet)
    media event → cp has active session → caller skips buffer and
                  calls api.add_attachments_to_session() directly

  TTL: entries older than ``_TTL_SECONDS`` (90s) are pruned at every
  add/drain so an orphan media (cp dropped a picture but never /review'd)
  doesn't linger.

Thread-safe (PAID hooks run on the gateway loop's executor, which is
multi-threaded for cron + adapter callbacks).

Module-level state — naturally scoped to one gateway process. If the
gateway restarts mid-test, the buffer is lost; this is acceptable
because the user will see the /review either succeed (with whatever
attachments arrive AFTER the restart) or get a clean intake without
attachments and can re-send.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Optional


_TTL_SECONDS = 90.0
_MAX_PER_CP = 8  # cap to prevent memory blow-up from a chatty cp

_BUFFER: dict[tuple[str, str], list[dict]] = {}
_LOCK = threading.Lock()


def _now() -> float:
    return time.monotonic()


def add(
    platform: str,
    sender_id: str,
    *,
    path: str,
    mime: str = "",
    name: str = "",
) -> None:
    """Record a media path for *(platform, sender_id)*.

    Pruning happens inline so the buffer cannot grow unbounded.
    """
    if not platform or not sender_id or not path:
        return
    key = (platform, sender_id)
    item = {
        "path": path,
        "mime": (mime or "").lower(),
        "name": name or os.path.basename(path),
        "ts": _now(),
    }
    with _LOCK:
        bucket = _BUFFER.setdefault(key, [])
        bucket.append(item)
        # Cap to most-recent N
        if len(bucket) > _MAX_PER_CP:
            del bucket[: len(bucket) - _MAX_PER_CP]
        _prune_locked()


def drain(platform: str, sender_id: str) -> list[dict]:
    """Return + clear all fresh-within-TTL entries for *(platform, sender_id)*.

    Entries past TTL are dropped silently. The return shape matches what
    ``paid_review.ingest.ingest`` expects in its ``attachments`` list:
    each dict has ``path``, ``mimetype``, ``name``.
    """
    if not platform or not sender_id:
        return []
    key = (platform, sender_id)
    cutoff = _now() - _TTL_SECONDS
    with _LOCK:
        _prune_locked()
        items = _BUFFER.pop(key, [])
    # Filter once more by TTL (prune_locked already did it but be defensive)
    fresh = [i for i in items if i["ts"] >= cutoff]
    # Re-shape to ingest-dispatcher schema
    return [
        {"path": i["path"], "mimetype": i["mime"], "name": i["name"]}
        for i in fresh
    ]


def peek(platform: str, sender_id: str) -> list[dict]:
    """Return current fresh entries without clearing — for tests + diagnostics."""
    if not platform or not sender_id:
        return []
    key = (platform, sender_id)
    cutoff = _now() - _TTL_SECONDS
    with _LOCK:
        items = list(_BUFFER.get(key, []))
    return [i for i in items if i["ts"] >= cutoff]


def clear(platform: Optional[str] = None, sender_id: Optional[str] = None) -> int:
    """Clear all (or one cp's) entries. Returns count of removed items.
    Mostly for tests + manual ops."""
    with _LOCK:
        if platform is None and sender_id is None:
            n = sum(len(v) for v in _BUFFER.values())
            _BUFFER.clear()
            return n
        key = (platform or "", sender_id or "")
        removed = _BUFFER.pop(key, [])
        return len(removed)


def _prune_locked() -> None:
    """Drop entries past TTL across all cp keys. Caller must hold ``_LOCK``."""
    cutoff = _now() - _TTL_SECONDS
    empty_keys = []
    for key, items in _BUFFER.items():
        fresh = [i for i in items if i["ts"] >= cutoff]
        if fresh:
            _BUFFER[key] = fresh
        else:
            empty_keys.append(key)
    for k in empty_keys:
        _BUFFER.pop(k, None)
