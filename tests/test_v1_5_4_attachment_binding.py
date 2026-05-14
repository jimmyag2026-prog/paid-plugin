"""Hook-level integration tests for v1.5.4 attachment binding.

Two paths verified:
  - Order A: /review arrives first → buffer is empty at intake → no buffered atts.
    Then image arrives → cp has active session → add_attachments_to_session called.
  - Order B: image arrives first → buffered. /review arrives within TTL →
    buffer drained at intake() call.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _fresh_plugin():
    spec = importlib.util.spec_from_file_location(
        "paid_v1_5_4_test", _ROOT / "__init__.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_event(*, text="", media_urls=None, media_types=None,
                chat_id="oc_dm", chat_type="p2p",
                platform="feishu", user_id="ou_evie"):
    plat = SimpleNamespace(value=platform)
    src = SimpleNamespace(
        platform=plat, user_id=user_id, chat_id=chat_id, chat_type=chat_type,
    )
    return SimpleNamespace(
        source=src, text=text,
        media_urls=media_urls or [],
        media_types=media_types or [],
    )


@pytest.fixture
def paid_tmp_iso(tmp_path, monkeypatch):
    from paid import storage
    monkeypatch.setattr(storage, "PAID_DIR", tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def clean_buffer():
    from paid_review import attachment_buffer as ab
    ab.clear()
    yield
    ab.clear()


def _silence(monkeypatch, plugin):
    monkeypatch.setattr(plugin.hermes_io, "send_dm",
                        lambda *a, **kw: {"ok": True, "msg_id": "stub"})
    monkeypatch.setattr(plugin, "_ensure_telegram_callback_registered",
                        lambda: None)
    monkeypatch.setattr(plugin.identity, "is_owner", lambda p, s: False)
    monkeypatch.setattr(plugin.identity, "load_owner", lambda: None)
    monkeypatch.setattr(plugin.identity, "display_name", lambda o: "Jimmy")
    monkeypatch.setattr(plugin.safety, "detect_prompt_injection",
                        lambda t: (False, []))


# ---------------------------------------------------------------------------
# Order B — image FIRST, /review LATER
# ---------------------------------------------------------------------------


def test_image_only_inbound_with_no_active_session_buffers(
    paid_tmp_iso, monkeypatch, tmp_path,
):
    """Cp drops an image without /review first. No active session →
    buffer the path. PAID does NOT skip the event (lets hermes continue
    its own vision-handling flow)."""
    plugin = _fresh_plugin()
    _silence(monkeypatch, plugin)

    # ensure cp has no active session
    monkeypatch.setattr(
        plugin.identity, "load_counterparty",
        lambda p, s: SimpleNamespace(
            cp_id=f"{p}_{s}", platform=p, user_id=s, active_review_session="",
        ),
    )

    f = tmp_path / "screenshot.png"
    f.write_bytes(b"\x89PNG fake")
    e = _make_event(
        text="",  # no text, just an image
        media_urls=[str(f)],
        media_types=["image/png"],
    )
    rv = plugin.on_pre_gateway_dispatch(event=e)
    # Did NOT skip → buffer happened passively
    assert rv is None

    # Verify buffered
    from paid_review import attachment_buffer as ab
    buffered = ab.peek("feishu", "ou_evie")
    assert len(buffered) == 1
    assert buffered[0]["path"] == str(f)


def test_review_after_buffered_image_includes_attachment(
    paid_tmp_iso, monkeypatch, tmp_path,
):
    """After image buffered, /review intake should drain + pass attachments."""
    plugin = _fresh_plugin()
    _silence(monkeypatch, plugin)

    # Pre-seed the buffer (simulate previously-arrived image)
    f = tmp_path / "shot.png"
    f.write_bytes(b"\x89PNG")
    from paid_review import attachment_buffer as ab
    ab.add("feishu", "ou_evie", path=str(f), mime="image/png", name="shot.png")

    # Capture what intake() receives
    captured = {}

    def fake_intake(*, cp, initial_message, attachments):
        captured["attachments"] = list(attachments)
        return "sid_synthesized"

    import paid_review.api as review_api
    monkeypatch.setattr(review_api, "intake", fake_intake)
    monkeypatch.setattr(
        plugin.identity, "set_active_review_session",
        lambda cp, sid: None,
    )
    monkeypatch.setattr(
        plugin.identity, "ensure_counterparty",
        lambda p, s: SimpleNamespace(
            cp_id=f"{p}_{s}", platform=p, user_id=s, active_review_session="",
        ),
    )
    # Make handle_inbound a no-op returning a non-closed reply so the
    # downstream dispatch logic doesn't try to close.
    monkeypatch.setattr(
        review_api, "handle_inbound",
        lambda sid, text, hk: SimpleNamespace(
            text="ok", closed=False, stage="SUBJECT", event_kind="subject_ask",
        ),
    )

    e = _make_event(text="/review 看一下", chat_type="p2p")
    rv = plugin.on_pre_gateway_dispatch(event=e)

    # Check intake was called with the buffered attachment
    assert "attachments" in captured, "intake() was not called"
    paths = [a.get("path") for a in captured["attachments"]]
    assert str(f) in paths

    # Buffer should be empty after drain
    assert ab.peek("feishu", "ou_evie") == []


# ---------------------------------------------------------------------------
# Order A — /review FIRST, image LATER
# ---------------------------------------------------------------------------


def test_image_only_inbound_with_active_session_binds_directly(
    paid_tmp_iso, monkeypatch, tmp_path,
):
    """Cp has an active session, sends an image-only message. PAID should
    call api.add_attachments_to_session() and return skip."""
    plugin = _fresh_plugin()
    _silence(monkeypatch, plugin)

    monkeypatch.setattr(
        plugin.identity, "load_counterparty",
        lambda p, s: SimpleNamespace(
            cp_id=f"{p}_{s}", platform=p, user_id=s,
            active_review_session="sid_active",
        ),
    )
    bind_calls = []

    import paid_review.api as review_api

    def fake_add(sid, attachments):
        bind_calls.append((sid, list(attachments)))
        return {"ok": True, "added_sources": len(attachments), "added_errors": 0, "appended_chars": 100}

    monkeypatch.setattr(review_api, "add_attachments_to_session", fake_add)

    f = tmp_path / "late_shot.png"
    f.write_bytes(b"\x89PNG")
    e = _make_event(
        text="",
        media_urls=[str(f)],
        media_types=["image/png"],
    )
    rv = plugin.on_pre_gateway_dispatch(event=e)
    assert rv == {"action": "skip", "reason": "paid_review_attachment_bound"}

    assert len(bind_calls) == 1
    sid_called, atts_called = bind_calls[0]
    assert sid_called == "sid_active"
    assert any(a["path"] == str(f) for a in atts_called)


def test_owner_image_does_not_bind_or_buffer(paid_tmp_iso, monkeypatch, tmp_path):
    """Owner-side image inbound should not trigger PAID's buffer/bind logic
    (owners use J0 vision flow, not review skill)."""
    plugin = _fresh_plugin()
    _silence(monkeypatch, plugin)

    # Owner identity returns True for this user_id
    monkeypatch.setattr(
        plugin.identity, "is_owner",
        lambda p, s: p == "feishu" and s == "owner_jimmy",
    )

    f = tmp_path / "owner_shot.png"
    f.write_bytes(b"\x89PNG")
    e = _make_event(
        text="",
        media_urls=[str(f)],
        media_types=["image/png"],
        user_id="owner_jimmy",
    )
    plugin.on_pre_gateway_dispatch(event=e)

    # Buffer must NOT have owner's image
    from paid_review import attachment_buffer as ab
    assert ab.peek("feishu", "owner_jimmy") == []


def test_text_with_review_prefix_does_not_buffer(
    paid_tmp_iso, monkeypatch, tmp_path,
):
    """Messages that bundle /review + media (Lark caption case) should
    bypass the media-only buffering branch and flow into normal review
    routing."""
    plugin = _fresh_plugin()
    _silence(monkeypatch, plugin)

    monkeypatch.setattr(
        plugin.identity, "load_counterparty",
        lambda p, s: SimpleNamespace(
            cp_id=f"{p}_{s}", platform=p, user_id=s, active_review_session="",
        ),
    )
    # Block downstream paths for clean test isolation
    monkeypatch.setattr(plugin.identity, "ensure_counterparty",
                        lambda p, s: SimpleNamespace(
                            cp_id=f"{p}_{s}", platform=p, user_id=s,
                            active_review_session="",
                        ))
    import paid_review.api as review_api
    monkeypatch.setattr(review_api, "intake", lambda **kw: "sid_x")
    monkeypatch.setattr(plugin.identity, "set_active_review_session",
                        lambda cp, sid: None)
    monkeypatch.setattr(review_api, "handle_inbound",
                        lambda sid, text, hk: SimpleNamespace(
                            text="ok", closed=False, stage="SUBJECT",
                            event_kind="subject_ask",
                        ))

    f = tmp_path / "with_caption.png"
    f.write_bytes(b"\x89PNG")
    e = _make_event(
        text="/review 看这张",  # text + media in one event
        media_urls=[str(f)],
        media_types=["image/png"],
    )
    plugin.on_pre_gateway_dispatch(event=e)

    # Buffer should be empty since we did NOT enter the media-only branch
    from paid_review import attachment_buffer as ab
    assert ab.peek("feishu", "ou_evie") == []
