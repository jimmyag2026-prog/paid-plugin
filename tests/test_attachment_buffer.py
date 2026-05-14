"""Tests for paid_review.attachment_buffer (v1.5.4).

Module-level buffer is shared across tests; each test starts by clearing
to a known state."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture(autouse=True)
def fresh_buffer():
    from paid_review import attachment_buffer as ab
    ab.clear()
    yield
    ab.clear()


def test_add_then_drain_returns_attachment():
    from paid_review import attachment_buffer as ab
    ab.add("feishu", "ou_evie", path="/tmp/a.png", mime="image/png", name="a.png")
    out = ab.drain("feishu", "ou_evie")
    assert len(out) == 1
    assert out[0]["path"] == "/tmp/a.png"
    assert out[0]["mimetype"] == "image/png"
    assert out[0]["name"] == "a.png"


def test_drain_clears_buffer():
    from paid_review import attachment_buffer as ab
    ab.add("feishu", "ou_x", path="/tmp/x.png", mime="image/png")
    ab.drain("feishu", "ou_x")
    assert ab.peek("feishu", "ou_x") == []


def test_add_multiple_attachments_preserves_order():
    from paid_review import attachment_buffer as ab
    for i in range(3):
        ab.add("feishu", "ou_e", path=f"/tmp/{i}.png", mime="image/png", name=f"{i}.png")
    out = ab.drain("feishu", "ou_e")
    assert [a["name"] for a in out] == ["0.png", "1.png", "2.png"]


def test_drain_returns_empty_for_unknown_cp():
    from paid_review import attachment_buffer as ab
    assert ab.drain("feishu", "ou_nope") == []


def test_separate_cps_are_isolated():
    from paid_review import attachment_buffer as ab
    ab.add("feishu", "ou_a", path="/tmp/a.png")
    ab.add("feishu", "ou_b", path="/tmp/b.png")
    out_a = ab.drain("feishu", "ou_a")
    out_b = ab.drain("feishu", "ou_b")
    assert [x["path"] for x in out_a] == ["/tmp/a.png"]
    assert [x["path"] for x in out_b] == ["/tmp/b.png"]


def test_separate_platforms_are_isolated():
    from paid_review import attachment_buffer as ab
    ab.add("feishu", "shared_id", path="/tmp/f.png")
    ab.add("telegram", "shared_id", path="/tmp/t.png")
    assert [x["path"] for x in ab.drain("feishu", "shared_id")] == ["/tmp/f.png"]
    assert [x["path"] for x in ab.drain("telegram", "shared_id")] == ["/tmp/t.png"]


def test_ttl_expiry_removes_stale_entries(monkeypatch):
    from paid_review import attachment_buffer as ab

    # Stub monotonic clock for deterministic TTL behavior
    fake_clock = [1000.0]
    monkeypatch.setattr(ab, "_now", lambda: fake_clock[0])

    ab.add("feishu", "ou_e", path="/tmp/old.png")
    fake_clock[0] += 100  # > TTL (90s)
    ab.add("feishu", "ou_e", path="/tmp/fresh.png")

    out = ab.drain("feishu", "ou_e")
    paths = [a["path"] for a in out]
    assert "/tmp/old.png" not in paths
    assert "/tmp/fresh.png" in paths


def test_ttl_within_window_keeps_entry(monkeypatch):
    from paid_review import attachment_buffer as ab

    fake_clock = [1000.0]
    monkeypatch.setattr(ab, "_now", lambda: fake_clock[0])

    ab.add("feishu", "ou_e", path="/tmp/recent.png")
    fake_clock[0] += 30  # < TTL
    out = ab.drain("feishu", "ou_e")
    assert len(out) == 1


def test_cap_drops_oldest_when_over_max():
    from paid_review import attachment_buffer as ab
    # _MAX_PER_CP is 8; add 10 and verify only 8 most-recent remain
    for i in range(10):
        ab.add("feishu", "ou_e", path=f"/tmp/{i}.png", name=f"{i}.png")
    out = ab.drain("feishu", "ou_e")
    assert len(out) == 8
    assert [a["name"] for a in out] == [f"{i}.png" for i in range(2, 10)]


def test_clear_all():
    from paid_review import attachment_buffer as ab
    ab.add("feishu", "ou_a", path="/tmp/a.png")
    ab.add("feishu", "ou_b", path="/tmp/b.png")
    removed = ab.clear()
    assert removed == 2
    assert ab.peek("feishu", "ou_a") == []
    assert ab.peek("feishu", "ou_b") == []


def test_clear_specific_cp():
    from paid_review import attachment_buffer as ab
    ab.add("feishu", "ou_a", path="/tmp/a.png")
    ab.add("feishu", "ou_b", path="/tmp/b.png")
    removed = ab.clear("feishu", "ou_a")
    assert removed == 1
    assert ab.peek("feishu", "ou_a") == []
    assert len(ab.peek("feishu", "ou_b")) == 1


def test_empty_platform_or_sender_no_ops():
    from paid_review import attachment_buffer as ab
    ab.add("", "ou_e", path="/tmp/x.png")
    ab.add("feishu", "", path="/tmp/x.png")
    ab.add("feishu", "ou_e", path="")
    assert ab.drain("feishu", "ou_e") == []
    assert ab.drain("", "ou_e") == []


def test_peek_does_not_clear():
    from paid_review import attachment_buffer as ab
    ab.add("feishu", "ou_e", path="/tmp/a.png")
    out1 = ab.peek("feishu", "ou_e")
    out2 = ab.peek("feishu", "ou_e")
    assert len(out1) == 1
    assert len(out2) == 1
