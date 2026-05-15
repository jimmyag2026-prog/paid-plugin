"""v1.6.15 + v1.6.18 — jelabs pilot day-1 ingest hardening.

v1.6.15:
  - URL that no backend claims OR that a backend fails on must STAY in
    remaining_text (annotated), never silently dropped → review must not
    run on empty input.
  - WebScrapeBackend must detect anti-scrape / JS-wall placeholder pages
    and reject them instead of passing the shell through as content.

v1.6.18:
  - LarkDocBackend recognises /file/<token> Drive URLs, downloads via
    LarkClient.download_file, and re-routes through the file backends.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paid_review import ingest as ingest_mod
from paid_review.ingest_backends.base import BackendResult, IngestBackend
from paid_review.ingest_backends.text import TextBackend
from paid_review.ingest_backends.lark_doc import (
    LarkDocBackend,
    extract_lark_resource,
)
from paid_review.ingest_backends.web_scrape import WebScrapeBackend


# ---------------------------------------------------------------------------
# v1.6.15 — _route_urls_in_text fallback
# ---------------------------------------------------------------------------


class _NoClaimBackend(IngestBackend):
    name = "noclaim"

    def can_handle_url(self, url: str) -> bool:
        return False


class _FailingURLBackend(IngestBackend):
    name = "failing"

    def can_handle_url(self, url: str) -> bool:
        return True

    def ingest_url(self, url: str) -> BackendResult:
        return BackendResult(
            normalized="",
            backend=self.name,
            source=url,
            errors=["anti-scrape wall"],
        )


class _GoodURLBackend(IngestBackend):
    name = "good"

    def can_handle_url(self, url: str) -> bool:
        return True

    def ingest_url(self, url: str) -> BackendResult:
        return BackendResult(
            normalized="REAL CONTENT", backend=self.name, source=url
        )


def test_unclaimed_url_stays_annotated_not_dropped():
    text = "look at https://x.com/foo/status/123 please"
    out_text, results = ingest_mod._route_urls_in_text(
        text, [_NoClaimBackend()]
    )
    assert "https://x.com/foo/status/123" in out_text
    assert "未能读取此链接" in out_text
    assert results == []


def test_failed_fetch_keeps_url_and_reason():
    text = "review this https://x.com/foo/status/123"
    out_text, results = ingest_mod._route_urls_in_text(
        text, [_FailingURLBackend()]
    )
    # URL preserved + reason surfaced; not silently stripped to "".
    assert "https://x.com/foo/status/123" in out_text
    assert "anti-scrape wall" in out_text
    assert len(results) == 1
    assert results[0].errors


def test_successful_fetch_still_strips_url():
    text = "see https://example.com/article now"
    out_text, results = ingest_mod._route_urls_in_text(
        text, [_GoodURLBackend()]
    )
    assert "https://example.com/article" not in out_text
    assert results[0].normalized == "REAL CONTENT"


def test_ingest_with_failed_url_does_not_yield_empty_normalized(tmp_path):
    """End-to-end: a message that is ONLY a failing URL must still
    produce a normalized.md that contains the URL + reason, not ''."""
    sid_dir = tmp_path / "sid_x"
    # Production _default_backends always includes a TextBackend; the
    # annotated remaining_text is surfaced through it.
    out = ingest_mod.ingest(
        "https://x.com/foo/status/123",
        [],
        sid_dir,
        backends=[_FailingURLBackend(), TextBackend()],
    )
    assert "https://x.com/foo/status/123" in out.normalized_text
    assert "anti-scrape wall" in out.normalized_text


# ---------------------------------------------------------------------------
# v1.6.15 — anti-scrape placeholder detection
# ---------------------------------------------------------------------------


def _ws_with_extracted(monkeypatch, body_text: str) -> WebScrapeBackend:
    # The anti-scrape heuristic runs AFTER extraction, so it only
    # applies when an extractor is available. On a venv without
    # bs4/readability the backend short-circuits at the missing-dep
    # check (which is itself the v1.6.15 doctor.py concern) — skip
    # rather than assert the wrong branch.
    pytest.importorskip("bs4")
    b = WebScrapeBackend()
    # Force the extractor to yield the placeholder body.
    monkeypatch.setattr(
        b, "_extract_with_readability",
        lambda *_a, **_k: (body_text, ""),
    )
    monkeypatch.setattr(
        b, "_extract_with_bs4", lambda *_a, **_k: body_text
    )
    return b


def test_antiscrape_js_disabled_shell_rejected(monkeypatch):
    b = _ws_with_extracted(
        monkeypatch,
        "We've detected that JavaScript is disabled in this browser.",
    )

    class _Resp:
        status_code = 200
        headers = {"content-type": "text/html"}
        content = b"<html>x</html>"

    monkeypatch.setattr(
        "paid_review.ingest_backends.web_scrape._fetch_with_ssrf_aware_redirects",
        lambda url: (_Resp(), None),
    )
    res = b.ingest_url("https://x.com/foo/status/1")
    assert res.normalized == ""
    assert res.errors
    assert "anti-scrape" in res.errors[0].lower()


def test_antiscrape_does_not_nuke_real_article_mentioning_javascript(
    monkeypatch,
):
    long_body = (
        "This is a real technical article about JavaScript performance. "
        * 60
    )
    b = _ws_with_extracted(monkeypatch, long_body)

    class _Resp:
        status_code = 200
        headers = {"content-type": "text/html"}
        content = b"<html>x</html>"

    monkeypatch.setattr(
        "paid_review.ingest_backends.web_scrape._fetch_with_ssrf_aware_redirects",
        lambda url: (_Resp(), None),
    )
    res = b.ingest_url("https://example.com/js-perf")
    assert res.normalized
    assert "real technical article" in res.normalized


# ---------------------------------------------------------------------------
# v1.6.18 — Lark /file/ recognition + download routing
# ---------------------------------------------------------------------------


def test_extract_lark_resource_recognises_file_url():
    url = "https://jsg8iy06jkpz.sg.larksuite.com/file/RX0HbVfsko7LWJxCUd9lvLNAgLg?from=from_copylink"
    assert extract_lark_resource(url) == ("file", "RX0HbVfsko7LWJxCUd9lvLNAgLg")


def test_extract_lark_resource_still_does_doc_and_wiki():
    assert extract_lark_resource(
        "https://x.feishu.cn/docx/ABC123"
    ) == ("doc", "ABC123")
    assert extract_lark_resource(
        "https://x.larksuite.com/wiki/WK99"
    ) == ("wiki", "WK99")


class _FakeLarkClient:
    def __init__(self, content: bytes, fname: str, mime: str):
        self._c = content
        self._f = fname
        self._m = mime
        self.called_with = None

    def download_file(self, file_token: str):
        self.called_with = file_token
        return self._c, self._f, self._m


def test_lark_file_url_downloads_and_routes_to_text_backend():
    fake = _FakeLarkClient(b"plain text body here", "notes.txt", "text/plain")
    backend = LarkDocBackend(fake)
    url = "https://x.larksuite.com/file/TOK123"
    assert backend.can_handle_url(url)
    res = backend.ingest_url(url)
    assert fake.called_with == "TOK123"
    assert "plain text body here" in res.normalized
    assert res.source == url
    assert "Lark Drive" in res.note


def test_lark_file_download_failure_surfaces_error():
    class _BoomClient:
        def download_file(self, file_token):
            raise RuntimeError("403 no permission")

    backend = LarkDocBackend(_BoomClient())
    res = backend.ingest_url("https://x.larksuite.com/file/TOK")
    assert res.normalized == ""
    assert res.errors
    assert "403 no permission" in res.errors[0]


def test_lark_file_zero_bytes_surfaces_error():
    backend = LarkDocBackend(_FakeLarkClient(b"", "empty.bin", "application/octet-stream"))
    res = backend.ingest_url("https://x.larksuite.com/file/TOK")
    assert res.normalized == ""
    assert res.errors
