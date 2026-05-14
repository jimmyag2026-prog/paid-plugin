"""Tests for paid_review.ingest_backends.web_scrape (v1.5 Phase 5).

Strategy:
  - httpx.MockTransport for all network mocking — no real sockets touched.
  - SSRF guard tested via direct host-classifier + via monkeypatched
    socket.getaddrinfo so we never resolve real DNS.
  - All HTTP responses are constructed inline so tests stay deterministic.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from paid_review.ingest_backends import WebScrapeBackend
from paid_review.ingest_backends.web_scrape import (
    _classify_ip,
    _is_lark_url,
    _is_safe_host,
)


# ---------------------------------------------------------------------------
# SSRF guard — IP classifier
# ---------------------------------------------------------------------------


def test_classify_ip_loopback():
    import ipaddress

    assert _classify_ip(ipaddress.ip_address("127.0.0.1")) == "loopback"
    assert _classify_ip(ipaddress.ip_address("::1")) == "loopback"


def test_classify_ip_private():
    import ipaddress

    assert _classify_ip(ipaddress.ip_address("10.0.0.1")) == "private"
    assert _classify_ip(ipaddress.ip_address("192.168.1.1")) == "private"
    assert _classify_ip(ipaddress.ip_address("172.16.0.1")) == "private"
    assert _classify_ip(ipaddress.ip_address("fc00::1")) == "private"


def test_classify_ip_link_local():
    import ipaddress

    assert _classify_ip(ipaddress.ip_address("169.254.169.254")) == "link-local"
    assert _classify_ip(ipaddress.ip_address("fe80::1")) == "link-local"


def test_classify_ip_multicast():
    import ipaddress

    assert _classify_ip(ipaddress.ip_address("224.0.0.1")) == "multicast"


def test_classify_ip_public_ok():
    import ipaddress

    # Cloudflare 1.1.1.1 is the canonical "definitely public" IP
    assert _classify_ip(ipaddress.ip_address("1.1.1.1")) == ""
    assert _classify_ip(ipaddress.ip_address("8.8.8.8")) == ""


# ---------------------------------------------------------------------------
# SSRF guard — host resolver
# ---------------------------------------------------------------------------


def _mock_dns(monkeypatch, *ips: str):
    """Replace socket.getaddrinfo to return the given IPs."""
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0))
            for ip in ips
        ]
    monkeypatch.setattr(
        "paid_review.ingest_backends.web_scrape.socket.getaddrinfo",
        fake_getaddrinfo,
    )


def test_is_safe_host_empty_rejected():
    ok, reason = _is_safe_host("")
    assert not ok
    assert "empty" in reason


def test_is_safe_host_bare_loopback_ip_rejected():
    ok, reason = _is_safe_host("127.0.0.1")
    assert not ok
    assert "loopback" in reason


def test_is_safe_host_bare_private_ip_rejected():
    ok, reason = _is_safe_host("192.168.1.1")
    assert not ok
    assert "private" in reason


def test_is_safe_host_aws_metadata_rejected():
    """EC2 169.254.169.254 metadata endpoint — classic SSRF target."""
    ok, reason = _is_safe_host("169.254.169.254")
    assert not ok
    assert "link-local" in reason


def test_is_safe_host_dns_resolves_to_private_rejected(monkeypatch):
    _mock_dns(monkeypatch, "10.0.0.5")
    ok, reason = _is_safe_host("internal.example.com")
    assert not ok
    assert "private" in reason


def test_is_safe_host_split_horizon_any_bad_rejects(monkeypatch):
    """Defense against split-horizon DNS: if ANY resolved IP is bad, reject."""
    _mock_dns(monkeypatch, "8.8.8.8", "10.0.0.5")
    ok, reason = _is_safe_host("split.example.com")
    assert not ok
    assert "private" in reason


def test_is_safe_host_public_ip_ok(monkeypatch):
    _mock_dns(monkeypatch, "1.1.1.1", "8.8.8.8")
    ok, reason = _is_safe_host("example.com")
    assert ok, reason


def test_is_safe_host_dns_failure_rejected(monkeypatch):
    def boom(*args, **kwargs):
        raise socket.gaierror("nodename nor servname provided")
    monkeypatch.setattr(
        "paid_review.ingest_backends.web_scrape.socket.getaddrinfo",
        boom,
    )
    ok, reason = _is_safe_host("nope.invalid.tld")
    assert not ok
    assert "DNS" in reason


# ---------------------------------------------------------------------------
# Lark URL recognition
# ---------------------------------------------------------------------------


def test_is_lark_url_true():
    assert _is_lark_url("https://jimmyresearch.feishu.cn/docx/abc")
    assert _is_lark_url("https://anything.larksuite.com/wiki/xyz")


def test_is_lark_url_false():
    assert not _is_lark_url("https://www.example.com/x")
    assert not _is_lark_url("https://github.com/foo/bar")
    assert not _is_lark_url("https://not-feishu.cn.evil.com/")


# ---------------------------------------------------------------------------
# Capability gate
# ---------------------------------------------------------------------------


def test_backend_handles_http_urls():
    b = WebScrapeBackend()
    assert b.can_handle_url("http://example.com")
    assert b.can_handle_url("https://example.com/path?q=1")


def test_backend_rejects_non_http():
    b = WebScrapeBackend()
    assert not b.can_handle_url("file:///etc/passwd")
    assert not b.can_handle_url("ftp://example.com")
    assert not b.can_handle_url("javascript:alert(1)")
    assert not b.can_handle_url("")
    assert not b.can_handle_url("not a url")


def test_backend_defers_lark_urls():
    """WebScrapeBackend must not handle Lark URLs even when LarkDocBackend
    isn't registered — owner gets a cleaner failure that way."""
    b = WebScrapeBackend()
    assert not b.can_handle_url("https://x.feishu.cn/docx/abc")
    assert not b.can_handle_url("https://x.larksuite.com/wiki/zzz")


# ---------------------------------------------------------------------------
# Missing-dep paths (graceful degrade)
# ---------------------------------------------------------------------------


def test_backend_missing_httpx():
    b = WebScrapeBackend()
    b._httpx_available = False
    b._bs4_available = True
    r = b.ingest_url("https://example.com")
    assert not r.ok
    assert any("httpx" in e for e in r.errors)


def test_backend_missing_bs4():
    b = WebScrapeBackend()
    b._httpx_available = True
    b._bs4_available = False
    r = b.ingest_url("https://example.com")
    assert not r.ok
    assert any("beautifulsoup4" in e for e in r.errors)


def test_backend_missing_both_lists_each():
    b = WebScrapeBackend()
    b._httpx_available = False
    b._bs4_available = False
    r = b.ingest_url("https://example.com")
    text = " ".join(r.errors)
    assert "httpx" in text
    assert "beautifulsoup4" in text


# ---------------------------------------------------------------------------
# Fetch + extraction (httpx.MockTransport)
# ---------------------------------------------------------------------------


def _fully_armed(monkeypatch, *, dns_ips=("1.1.1.1",)) -> WebScrapeBackend:
    b = WebScrapeBackend()
    b._httpx_available = True
    b._bs4_available = True  # bs4 must really be importable for these tests
    _mock_dns(monkeypatch, *dns_ips)
    return b


def _install_mock_transport(monkeypatch, handler):
    """Replace httpx.Client used by the backend with one whose transport
    routes through `handler` (a callable taking httpx.Request, returning
    httpx.Response)."""
    import httpx

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs.pop("transport", None)
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(
        "paid_review.ingest_backends.web_scrape.httpx.Client",
        fake_client,
    )


def test_backend_fetches_and_extracts_html_with_bs4_fallback(monkeypatch):
    pytest.importorskip("bs4")
    b = _fully_armed(monkeypatch)
    # Force readability off so we exercise bs4-only path even if installed
    b._readability_available = False

    import httpx

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=(
                b"<html><head><title>T</title>"
                b"<script>alert('x')</script></head>"
                b"<body><nav>nav</nav>"
                b"<article><h1>Headline</h1>"
                b"<p>This is the body content junior wanted reviewed.</p>"
                b"</article><footer>(C) 2026</footer></body></html>"
            ),
        )

    _install_mock_transport(monkeypatch, handler)
    r = b.ingest_url("https://example.com/article")
    assert r.ok, r.errors
    assert "body content junior wanted reviewed" in r.normalized
    # Stripped tags
    assert "alert" not in r.normalized
    # nav/footer stripped in bs4 fallback
    assert "(C) 2026" not in r.normalized


def test_backend_uses_readability_when_available(monkeypatch):
    pytest.importorskip("bs4")
    readability = pytest.importorskip("readability")
    b = _fully_armed(monkeypatch)
    b._readability_available = True

    import httpx

    html = (
        b"<html><head><title>Real Title</title></head><body>"
        b"<nav><a>nav</a></nav>"
        b"<article><h1>Real Title</h1>"
        b"<p>Substantive article body here with enough words for readability "
        b"to score it as the main content over the navigation block above. "
        b"It needs a decent amount of text to score well, so here is more "
        b"text in the body paragraph to push the readability score up.</p>"
        b"</article></body></html>"
    )

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=html,
        )

    _install_mock_transport(monkeypatch, handler)
    r = b.ingest_url("https://example.com/article2")
    assert r.ok, r.errors
    assert "Substantive article body" in r.normalized
    # Title prefix rendered
    assert "Real Title" in r.normalized


def test_backend_blocks_ssrf_at_fetch_time(monkeypatch):
    """SSRF guard must reject before any HTTP request is made."""
    b = WebScrapeBackend()
    b._httpx_available = True
    b._bs4_available = True
    _mock_dns(monkeypatch, "10.0.0.5")

    # If a fetch is attempted, raise loudly
    def handler(request):
        raise AssertionError("network must not be touched on SSRF reject")

    _install_mock_transport(monkeypatch, handler)
    r = b.ingest_url("https://internal.example.com/secret")
    assert not r.ok
    assert any("SSRF" in e for e in r.errors)


def test_backend_handles_http_404(monkeypatch):
    pytest.importorskip("bs4")
    b = _fully_armed(monkeypatch)
    import httpx

    def handler(request):
        return httpx.Response(404, content=b"not found")

    _install_mock_transport(monkeypatch, handler)
    r = b.ingest_url("https://example.com/missing")
    assert not r.ok
    assert any("HTTP 404" in e for e in r.errors)


def test_backend_rejects_non_html_content_type(monkeypatch):
    pytest.importorskip("bs4")
    b = _fully_armed(monkeypatch)
    import httpx

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "application/octet-stream"},
            content=b"\x00\x01\x02",
        )

    _install_mock_transport(monkeypatch, handler)
    r = b.ingest_url("https://example.com/blob")
    assert not r.ok
    assert any("non-HTML" in e for e in r.errors)


def test_backend_handles_timeout(monkeypatch):
    pytest.importorskip("bs4")
    b = _fully_armed(monkeypatch)
    import httpx

    def handler(request):
        raise httpx.ConnectTimeout("simulated")

    _install_mock_transport(monkeypatch, handler)
    r = b.ingest_url("https://slow.example.com/")
    assert not r.ok
    assert any("timeout" in e.lower() for e in r.errors)


def test_backend_empty_extraction_advisory(monkeypatch):
    pytest.importorskip("bs4")
    b = _fully_armed(monkeypatch)
    b._readability_available = False
    import httpx

    def handler(request):
        # 200 OK but body has only scripts → bs4 will strip them → 0 chars
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html><body><script>1</script><style>x</style></body></html>",
        )

    _install_mock_transport(monkeypatch, handler)
    r = b.ingest_url("https://example.com/jsonly")
    assert not r.ok
    assert any("0 chars" in e for e in r.errors)


def test_backend_truncates_large_extracted_text(monkeypatch):
    pytest.importorskip("bs4")
    b = _fully_armed(monkeypatch)
    b._readability_available = False
    import httpx

    big_body = b"<p>" + b"x" * (300 * 1024) + b"</p>"

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html><body>" + big_body + b"</body></html>",
        )

    _install_mock_transport(monkeypatch, handler)
    r = b.ingest_url("https://example.com/big")
    assert r.ok
    assert "truncated to" in r.note


# ---------------------------------------------------------------------------
# Dispatcher integration
# ---------------------------------------------------------------------------


def test_dispatcher_routes_web_url_to_web_scrape_backend(monkeypatch, tmp_path):
    pytest.importorskip("bs4")
    from paid_review import ingest as ingest_mod

    _mock_dns(monkeypatch, "1.1.1.1")
    import httpx

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html><body><p>dispatched web content</p></body></html>",
        )

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs.pop("transport", None)
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(
        "paid_review.ingest_backends.web_scrape.httpx.Client",
        fake_client,
    )

    out = ingest_mod.ingest(
        "please review https://example.com/post for me",
        [],
        tmp_path / "sid_web",
    )
    assert "dispatched web content" in out.normalized_text
    assert any(s["backend"] == "web_scrape" for s in out.sources)


def test_dispatcher_lark_url_not_stolen_by_web_scrape(monkeypatch, tmp_path):
    """If lark_client is None the Lark URL stays as text — WebScrapeBackend
    must NOT pick it up."""
    from paid_review import ingest as ingest_mod

    # Network handler raises if touched
    import httpx

    def handler(request):
        raise AssertionError("Lark URL must not be web-scraped")

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs.pop("transport", None)
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(
        "paid_review.ingest_backends.web_scrape.httpx.Client",
        fake_client,
    )

    out = ingest_mod.ingest(
        "review https://x.feishu.cn/docx/aaa",
        [],
        tmp_path / "sid_lark_skip",
    )
    # URL stays in plain text since no backend handled it
    assert "feishu.cn" in out.normalized_text
