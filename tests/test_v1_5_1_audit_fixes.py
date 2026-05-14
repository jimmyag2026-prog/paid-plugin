"""v1.5.1 audit-fix tests.

Three fixes from the 2026-05-14 audit report:
  - Critical #5: review-only mode now drops non-command chatter unless
    the sender has an active review session (group_review_strict).
  - High #1: SSRF redirect bypass — every redirect hop is re-validated.
  - Medium #6: OCR 0-char error now names PAID_OCR_LANGS.
"""

from __future__ import annotations

import importlib.util
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# Critical #5 — review-only mode hook integration
# ---------------------------------------------------------------------------


def _fresh_plugin():
    spec = importlib.util.spec_from_file_location(
        "paid_v1_5_1_test", _ROOT / "__init__.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_event(*, text, chat_id, chat_type, platform="feishu",
                user_id="ou_junior"):
    plat = SimpleNamespace(value=platform)
    src = SimpleNamespace(
        platform=plat, user_id=user_id, chat_id=chat_id, chat_type=chat_type,
    )
    return SimpleNamespace(source=src, text=text)


@pytest.fixture
def paid_tmp_iso(tmp_path, monkeypatch):
    from paid import storage
    monkeypatch.setattr(storage, "PAID_DIR", tmp_path)
    return tmp_path


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


def test_review_only_drops_chatter_from_cp_without_active_session(
    paid_tmp_iso, monkeypatch,
):
    """Junior posts 'lunch?' in a review-only group → drop. No active session
    → bot doesn't auto-reply."""
    plugin = _fresh_plugin()
    _silence(monkeypatch, plugin)

    plugin.group_routing.save_group_config(plugin.group_routing.GroupConfig(
        group_key="feishu_oc_rev",
        platform="feishu",
        group_id="oc_rev",
        enabled=True,
        mode="review-only",
        owner_user_id="owner_lark",
    ))

    e = _make_event(
        text="lunch?", chat_id="oc_rev", chat_type="group",
        user_id="ou_evie",
    )
    rv = plugin.on_pre_gateway_dispatch(event=e)
    assert rv == {
        "action": "skip",
        "reason": "paid_group_review_only_non_review_message",
    }


def test_review_only_allows_chatter_from_cp_with_active_session(
    paid_tmp_iso, monkeypatch,
):
    """Continuation of an active review QA from a junior must NOT be dropped
    even though the message doesn't start with /review."""
    plugin = _fresh_plugin()
    _silence(monkeypatch, plugin)

    plugin.group_routing.save_group_config(plugin.group_routing.GroupConfig(
        group_key="feishu_oc_rev",
        platform="feishu",
        group_id="oc_rev",
        enabled=True,
        mode="review-only",
        owner_user_id="owner_lark",
    ))

    # Create a cp with an active session — Phase 6 has_active_review path
    # downstream will pick it up and route through review skill.
    plugin.identity.ensure_counterparty("feishu", "ou_evie", "Evie")
    cp = plugin.identity.load_counterparty("feishu", "ou_evie")
    cp.active_review_session = "active_sid_xyz"
    plugin.identity.save_counterparty(cp)

    captured = {}

    def fake_handler(platform, sender_id, stripped):
        captured["called"] = (platform, sender_id, stripped)
        return {"action": "skip", "reason": "paid_review_handled"}

    monkeypatch.setattr(plugin, "_handle_review_in_pre_gateway", fake_handler)

    e = _make_event(
        text="my answer to QA round 1", chat_id="oc_rev", chat_type="group",
        user_id="ou_evie",
    )
    rv = plugin.on_pre_gateway_dispatch(event=e)
    # has_active_review path catches this and routes through review skill
    assert rv == {"action": "skip", "reason": "paid_review_handled"}
    assert captured["called"][1] == "ou_evie"


def test_review_only_lets_review_command_through(
    paid_tmp_iso, monkeypatch,
):
    """Phase 6 contract still holds: /review prefix always routes."""
    plugin = _fresh_plugin()
    _silence(monkeypatch, plugin)

    plugin.group_routing.save_group_config(plugin.group_routing.GroupConfig(
        group_key="feishu_oc_rev",
        platform="feishu",
        group_id="oc_rev",
        enabled=True,
        mode="review-only",
        owner_user_id="owner_lark",
    ))

    captured = {}

    def fake_handler(platform, sender_id, stripped):
        captured["called"] = (platform, sender_id, stripped)
        return {"action": "skip", "reason": "paid_review_handled"}

    monkeypatch.setattr(plugin, "_handle_review_in_pre_gateway", fake_handler)

    e = _make_event(
        text="/review draft v1", chat_id="oc_rev", chat_type="group",
        user_id="ou_evie",
    )
    rv = plugin.on_pre_gateway_dispatch(event=e)
    assert rv == {"action": "skip", "reason": "paid_review_handled"}


def test_review_only_owner_paid_commands_always_through(
    paid_tmp_iso, monkeypatch,
):
    """Owner /paid- commands bypass the strict gate even with no active session."""
    plugin = _fresh_plugin()
    _silence(monkeypatch, plugin)
    monkeypatch.setattr(
        plugin.identity, "is_owner",
        lambda p, s: p == "feishu" and s == "owner_lark",
    )

    plugin.group_routing.save_group_config(plugin.group_routing.GroupConfig(
        group_key="feishu_oc_rev",
        platform="feishu",
        group_id="oc_rev",
        enabled=True,
        mode="review-only",
        owner_user_id="owner_lark",
    ))

    e = _make_event(
        text="/paid-group-status", chat_id="oc_rev", chat_type="group",
        user_id="owner_lark",
    )
    rv = plugin.on_pre_gateway_dispatch(event=e)
    assert rv == {"action": "skip", "reason": "paid_group_status_reported"}


# ---------------------------------------------------------------------------
# High #1 — SSRF redirect bypass
# ---------------------------------------------------------------------------


def _mock_dns(monkeypatch, mapping):
    """mapping: dict of host → list-of-ips. Replaces socket.getaddrinfo."""
    def fake(host, port, *args, **kwargs):
        ips = mapping.get(host)
        if ips is None:
            raise socket.gaierror(f"unknown host {host}")
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0))
            for ip in ips
        ]
    monkeypatch.setattr(
        "paid_review.ingest_backends.web_scrape.socket.getaddrinfo", fake,
    )


def _mount_transport(monkeypatch, handler):
    """Replace WebScrapeBackend's httpx.Client with a MockTransport-backed one."""
    import httpx
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs.pop("transport", None)
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(
        "paid_review.ingest_backends.web_scrape.httpx.Client", fake_client,
    )


def _fully_armed():
    from paid_review.ingest_backends import WebScrapeBackend
    b = WebScrapeBackend()
    b._httpx_available = True
    b._bs4_available = True
    return b


def test_ssrf_followed_redirect_to_internal_ip_blocked(monkeypatch):
    """Public start, 302 → internal IP. Must be blocked by the per-hop SSRF gate.
    Pre-v1.5.1 this would have been followed because httpx.follow_redirects=True
    didn't re-validate; v1.5.1 manual redirect loop must catch it."""
    pytest.importorskip("bs4")
    _mock_dns(monkeypatch, {
        "evil.example.com": ["1.1.1.1"],
        "internal.corp": ["10.0.0.5"],
    })
    import httpx

    def handler(request):
        host = request.url.host
        if host == "evil.example.com":
            return httpx.Response(
                302, headers={"location": "http://internal.corp/secret"},
            )
        # If we ever reach the internal host, the fetch loop is broken
        raise AssertionError(
            f"SSRF gate failed: actually fetched {host}",
        )

    _mount_transport(monkeypatch, handler)
    b = _fully_armed()
    r = b.ingest_url("http://evil.example.com/start")
    assert not r.ok
    assert any("SSRF" in e for e in r.errors), r.errors


def test_ssrf_redirect_chain_of_public_hosts_followed(monkeypatch):
    """302 → 302 → 200 chain across public hosts. Each hop revalidated, all OK."""
    pytest.importorskip("bs4")
    _mock_dns(monkeypatch, {
        "a.example.com": ["1.1.1.1"],
        "b.example.com": ["8.8.8.8"],
        "c.example.com": ["1.0.0.1"],
    })
    import httpx

    def handler(request):
        host = request.url.host
        if host == "a.example.com":
            return httpx.Response(
                302, headers={"location": "http://b.example.com/next"},
            )
        if host == "b.example.com":
            return httpx.Response(
                302, headers={"location": "http://c.example.com/final"},
            )
        if host == "c.example.com":
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=b"<html><body><p>chain target reached</p></body></html>",
            )
        raise AssertionError(f"unexpected host {host}")

    _mount_transport(monkeypatch, handler)
    b = _fully_armed()
    b._readability_available = False
    r = b.ingest_url("http://a.example.com/start")
    assert r.ok, r.errors
    assert "chain target reached" in r.normalized


def test_ssrf_redirect_loop_detected(monkeypatch):
    """A → A redirect → loop, refuse."""
    pytest.importorskip("bs4")
    _mock_dns(monkeypatch, {"loop.example.com": ["1.1.1.1"]})
    import httpx

    def handler(request):
        return httpx.Response(
            302, headers={"location": "http://loop.example.com/x"},
        )

    _mount_transport(monkeypatch, handler)
    b = _fully_armed()
    r = b.ingest_url("http://loop.example.com/x")
    assert not r.ok
    # Either loop-detected OR too-many-redirects acceptable
    assert any(
        ("loop" in e.lower() or "exceeded" in e.lower())
        for e in r.errors
    ), r.errors


def test_ssrf_redirect_to_non_http_scheme_blocked(monkeypatch):
    """302 → file://... must be refused. Bot input is hostile by default."""
    pytest.importorskip("bs4")
    _mock_dns(monkeypatch, {"trick.example.com": ["1.1.1.1"]})
    import httpx

    def handler(request):
        return httpx.Response(
            302, headers={"location": "file:///etc/passwd"},
        )

    _mount_transport(monkeypatch, handler)
    b = _fully_armed()
    r = b.ingest_url("http://trick.example.com/")
    assert not r.ok
    assert any(
        ("non-http" in e.lower() or "ssrf" in e.lower())
        for e in r.errors
    ), r.errors


def test_ssrf_relative_redirect_resolves_against_origin(monkeypatch):
    """Location: /next → urljoin → same host, must work end-to-end."""
    pytest.importorskip("bs4")
    _mock_dns(monkeypatch, {"r.example.com": ["1.1.1.1"]})
    import httpx

    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final"})
        if request.url.path == "/final":
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=b"<html><body><p>relative redirect target</p></body></html>",
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    _mount_transport(monkeypatch, handler)
    b = _fully_armed()
    b._readability_available = False
    r = b.ingest_url("http://r.example.com/start")
    assert r.ok, r.errors
    assert "relative redirect target" in r.normalized


# ---------------------------------------------------------------------------
# Medium #6 — OCR error message names PAID_OCR_LANGS
# ---------------------------------------------------------------------------


def test_ocr_empty_result_error_mentions_paid_ocr_langs(tmp_path, monkeypatch):
    """When tesseract returns 0 chars, error must surface PAID_OCR_LANGS
    so the owner knows they can switch language packs without code changes."""
    import sys
    from unittest.mock import MagicMock
    import types

    fake_pyt = MagicMock()
    fake_pyt.image_to_string.return_value = ""
    fake_pil = types.SimpleNamespace()
    fake_pil.Image = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=MagicMock())
    cm.__exit__ = MagicMock(return_value=False)
    fake_pil.Image.open = MagicMock(return_value=cm)
    monkeypatch.setitem(sys.modules, "pytesseract", fake_pyt)
    monkeypatch.setitem(sys.modules, "PIL", fake_pil)

    from paid_review.ingest_backends import ImageBackend
    b = ImageBackend()
    b._tesseract_path = "/usr/bin/tesseract"
    b._pytesseract_available = True
    b._pillow_available = True

    p = tmp_path / "blank.png"
    p.write_bytes(b"\x89PNG")
    r = b.ingest_file(p)
    text = " ".join(r.errors)
    # Must mention the env var by name
    assert "PAID_OCR_LANGS" in text
    # Must mention current lang attempt
    assert "chi_sim" in text or "eng" in text
    # Must hint at apt install for additional lang packs
    assert "tesseract-ocr-" in text


def test_ocr_empty_result_error_mentions_current_langs(tmp_path, monkeypatch):
    """Error message must include whatever PAID_OCR_LANGS resolved to."""
    import sys
    from unittest.mock import MagicMock
    import types

    fake_pyt = MagicMock()
    fake_pyt.image_to_string.return_value = ""
    fake_pil = types.SimpleNamespace()
    fake_pil.Image = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=MagicMock())
    cm.__exit__ = MagicMock(return_value=False)
    fake_pil.Image.open = MagicMock(return_value=cm)
    monkeypatch.setitem(sys.modules, "pytesseract", fake_pyt)
    monkeypatch.setitem(sys.modules, "PIL", fake_pil)
    monkeypatch.setenv("PAID_OCR_LANGS", "jpn+eng")

    from paid_review.ingest_backends import ImageBackend
    b = ImageBackend()
    b._tesseract_path = "/usr/bin/tesseract"
    b._pytesseract_available = True
    b._pillow_available = True

    p = tmp_path / "x.png"
    p.write_bytes(b"\x89PNG")
    r = b.ingest_file(p)
    text = " ".join(r.errors)
    assert "jpn+eng" in text
