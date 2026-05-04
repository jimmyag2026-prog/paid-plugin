"""INV-6 + R7: concurrent intake must be protected by fcntl.flock.

Uses multiprocessing (not threads) because flock is per-process on POSIX.
Exactly one intake() wins; the other raises ReviewSessionConflict or IntakeRefused.

Spec ref: §3.1 INV-6, §8 R7, spec Ⓜ19.
"""

from __future__ import annotations

import json
import multiprocessing
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# Worker function (runs in child process)
# ---------------------------------------------------------------------------

def _worker(paid_dir: str, cp_data: dict, sid_slot: int, result_queue) -> None:
    """Attempt intake() in a subprocess; put result in queue."""
    import sys
    from pathlib import Path as _Path
    _ROOT = _Path(__file__).resolve().parent.parent
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))

    from paid import storage as _storage, identity as _identity
    _storage.PAID_DIR = _Path(paid_dir)

    from paid_review.api import intake, IntakeRefused, ReviewSessionConflict

    # Reconstruct cp from dict
    cp = _identity.ensure_counterparty(
        cp_data["platform"], cp_data["user_id"], cp_data.get("display_name", "")
    )

    try:
        sid = intake(
            cp=cp,
            initial_message="concurrent test",
            attachments=[],
        )
        result_queue.put(("ok", sid))
    except ReviewSessionConflict as e:
        result_queue.put(("conflict", str(e)))
    except IntakeRefused as e:
        result_queue.put(("refused", str(e)))
    except Exception as e:
        result_queue.put(("error", repr(e)))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_inv6_concurrent_intake_exactly_one_wins(paid_tmp):
    """Two concurrent intake() calls on same cp: exactly one wins, other fails."""
    from paid import storage, identity
    storage.PAID_DIR = paid_tmp

    cp = identity.ensure_counterparty("telegram", "555", "Concurrent J")
    cp_data = {
        "platform": cp.platform,
        "user_id": cp.user_id,
        "display_name": cp.display_name,
    }

    ctx = multiprocessing.get_context("fork")
    result_queue = ctx.Queue()

    p1 = ctx.Process(target=_worker, args=(str(paid_tmp), cp_data, 0, result_queue))
    p2 = ctx.Process(target=_worker, args=(str(paid_tmp), cp_data, 1, result_queue))

    p1.start()
    p2.start()
    p1.join(timeout=10)
    p2.join(timeout=10)

    results = []
    while not result_queue.empty():
        results.append(result_queue.get_nowait())

    assert len(results) == 2, f"Expected 2 results, got: {results}"

    outcomes = [r[0] for r in results]
    ok_count = outcomes.count("ok")
    fail_count = outcomes.count("conflict") + outcomes.count("refused")

    assert ok_count == 1, f"Expected exactly 1 winner, got outcomes: {outcomes}"
    assert fail_count == 1, f"Expected exactly 1 loser, got outcomes: {outcomes}"

    # The cp profile must have exactly one active session
    reloaded_cp = identity.load_counterparty("telegram", "555")
    assert reloaded_cp is not None
    assert reloaded_cp.active_review_session != "", "Winner's sid must be set"

    # The sid in the profile must match the winner's return value
    winner_sid = next(r[1] for r in results if r[0] == "ok")
    assert reloaded_cp.active_review_session == winner_sid


def test_inv6_sequential_intake_conflict_blocked(paid_tmp):
    """Sequential second intake() raises IntakeRefused (not a race, but same guard)."""
    from paid import storage, identity
    storage.PAID_DIR = paid_tmp

    from paid_review.api import intake, IntakeRefused

    cp = identity.ensure_counterparty("telegram", "556", "Sequential J")

    sid1 = intake(cp=cp, initial_message="first", attachments=[])
    assert sid1

    # Reload cp so active_review_session is set on the object
    cp2 = identity.load_counterparty("telegram", "556")

    with pytest.raises(IntakeRefused):
        intake(cp=cp2, initial_message="second", attachments=[])


def test_inv6_no_silent_overwrite(paid_tmp):
    """After one intake wins, the winning sid is persisted and not overwritten."""
    from paid import storage, identity
    storage.PAID_DIR = paid_tmp

    from paid_review.api import intake, IntakeRefused

    cp = identity.ensure_counterparty("telegram", "557", "J")
    sid1 = intake(cp=cp, initial_message="first", attachments=[])

    cp_reloaded = identity.load_counterparty("telegram", "557")
    assert cp_reloaded.active_review_session == sid1

    # Attempt second intake (should be refused)
    with pytest.raises(IntakeRefused):
        intake(cp=cp_reloaded, initial_message="second", attachments=[])

    # Profile must still show the original sid
    cp_after = identity.load_counterparty("telegram", "557")
    assert cp_after.active_review_session == sid1
