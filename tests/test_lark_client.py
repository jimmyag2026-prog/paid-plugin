"""Tests for paid/lark_client.py (v1.5 Phase 1).

Uses httpx.MockTransport to mock HTTP responses — exercises real
LarkClient logic against deterministic fake server. Faster + more
realistic than mocking out _http.request directly.

Coverage:
  - token TTL caching (subsequent calls don't re-hit auth endpoint)
  - token expiry → refresh
  - 401 → refresh + retry once; second 401 raises
  - 429 → backoff + retry; respects Retry-After
  - 5xx → exponential backoff retry
  - Lark code 99991400 (rate-limit) → retry; eventual surface
  - Lark code != 0 (non-rate-limit) → immediate LarkAPIError
  - get_doc_raw happy path
  - get_wiki_node happy path
  - domain → base_url resolution
  - LarkConfigError when missing creds
  - singleton via get_lark_client
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from paid import lark_client
from paid.lark_client import (
    LarkAPIError,
    LarkClient,
    LarkConfigError,
    _resolve_base_url,
    get_lark_client,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_client(handler, *, app_id: str = "cli_test", app_secret: str = "secret_test",
                  domain: str = "feishu") -> LarkClient:
    """Build a LarkClient with httpx.MockTransport routing to *handler*."""
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, timeout=5.0)
    return LarkClient(app_id, app_secret, domain=domain, http=http)


def _token_response(token: str = "t_xxx", expire: int = 7200) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "code": 0,
            "msg": "ok",
            "tenant_access_token": token,
            "expire": expire,
        },
    )


def _ok_doc_response(content: str = "hello world") -> httpx.Response:
    return httpx.Response(
        200,
        json={"code": 0, "msg": "ok", "data": {"content": content}},
    )


# ---------------------------------------------------------------------------
# _resolve_base_url
# ---------------------------------------------------------------------------


def test_resolve_base_url_lark():
    assert _resolve_base_url("lark") == "https://open.larksuite.com"
    assert _resolve_base_url("larksuite") == "https://open.larksuite.com"
    assert _resolve_base_url("LARK") == "https://open.larksuite.com"


def test_resolve_base_url_feishu():
    assert _resolve_base_url("feishu") == "https://open.feishu.cn"


def test_resolve_base_url_default_falls_back_to_feishu():
    """Mirrors hermes feishu adapter default — preserves consistency."""
    assert _resolve_base_url(None) == "https://open.feishu.cn"
    assert _resolve_base_url("") == "https://open.feishu.cn"
    assert _resolve_base_url("unknown") == "https://open.feishu.cn"


# ---------------------------------------------------------------------------
# Constructor / config errors
# ---------------------------------------------------------------------------


def test_constructor_missing_creds_raises():
    with pytest.raises(LarkConfigError):
        LarkClient("", "secret")
    with pytest.raises(LarkConfigError):
        LarkClient("cli_x", "")
    with pytest.raises(LarkConfigError):
        LarkClient("", "")


# ---------------------------------------------------------------------------
# Token caching
# ---------------------------------------------------------------------------


def test_token_fetched_lazily_on_first_call():
    auth_calls = 0
    doc_calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal auth_calls, doc_calls
        if "tenant_access_token" in str(req.url):
            auth_calls += 1
            return _token_response()
        if "raw_content" in str(req.url):
            doc_calls += 1
            return _ok_doc_response()
        return httpx.Response(404)

    client = _build_client(handler)
    assert auth_calls == 0  # not fetched yet

    text = client.get_doc_raw("doc_abc")
    assert text == "hello world"
    assert auth_calls == 1
    assert doc_calls == 1


def test_token_cached_within_ttl():
    auth_calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal auth_calls
        if "tenant_access_token" in str(req.url):
            auth_calls += 1
            return _token_response(expire=7200)
        if "raw_content" in str(req.url):
            return _ok_doc_response("x")
        return httpx.Response(404)

    client = _build_client(handler)
    for _ in range(5):
        client.get_doc_raw("doc_z")
    assert auth_calls == 1  # 1 auth call total despite 5 doc requests


def test_token_refresh_when_near_expiry():
    """Token within `_TOKEN_REFRESH_SAFETY_SEC` of expiry → refresh."""
    auth_calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal auth_calls
        if "tenant_access_token" in str(req.url):
            auth_calls += 1
            # First call: token already near-expired (expire=100s).
            return _token_response(token=f"t_{auth_calls}", expire=100)
        return _ok_doc_response()

    client = _build_client(handler)
    client.get_doc_raw("d1")
    client.get_doc_raw("d2")
    # Both calls trigger refresh because the first token's expires_at is
    # only 100s out which is within the 600s safety window.
    assert auth_calls == 2


def test_token_fetch_failure_raises_with_code():
    def handler(req: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in str(req.url):
            return httpx.Response(
                200,
                json={"code": 10003, "msg": "invalid app_secret"},
            )
        return httpx.Response(500)

    client = _build_client(handler)
    with pytest.raises(LarkAPIError) as exc_info:
        client.get_doc_raw("d")
    assert exc_info.value.code == 10003


# ---------------------------------------------------------------------------
# Retry policy — HTTP layer
# ---------------------------------------------------------------------------


def test_http_401_triggers_token_refresh_and_retry(monkeypatch):
    monkeypatch.setattr(lark_client, "_backoff", lambda *a, **kw: None)
    auth_calls = 0
    doc_calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal auth_calls, doc_calls
        if "tenant_access_token" in str(req.url):
            auth_calls += 1
            return _token_response(token=f"t_{auth_calls}")
        if "raw_content" in str(req.url):
            doc_calls += 1
            if doc_calls == 1:
                return httpx.Response(401, text="token invalid")
            return _ok_doc_response()
        return httpx.Response(404)

    client = _build_client(handler)
    text = client.get_doc_raw("d")
    assert text == "hello world"
    assert auth_calls == 2  # initial + forced refresh after 401
    assert doc_calls == 2   # retry once after refresh


def test_http_401_twice_in_one_call_raises(monkeypatch):
    monkeypatch.setattr(lark_client, "_backoff", lambda *a, **kw: None)

    def handler(req: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in str(req.url):
            return _token_response()
        if "raw_content" in str(req.url):
            return httpx.Response(401, text="still bad")
        return httpx.Response(404)

    client = _build_client(handler)
    with pytest.raises(LarkAPIError) as exc:
        client.get_doc_raw("d")
    assert exc.value.status == 401


def test_http_429_retries_with_backoff(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(lark_client.time, "sleep", lambda s: sleeps.append(s))
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        if "tenant_access_token" in str(req.url):
            return _token_response()
        if "raw_content" in str(req.url):
            calls += 1
            if calls < 3:
                return httpx.Response(429, headers={"Retry-After": "2"}, text="slow down")
            return _ok_doc_response()
        return httpx.Response(404)

    client = _build_client(handler)
    text = client.get_doc_raw("d")
    assert text == "hello world"
    assert calls == 3
    # Two backoffs of 2s each (from Retry-After) before success.
    assert sleeps[:2] == [2.0, 2.0]


def test_http_429_max_retries_exhausted_raises(monkeypatch):
    monkeypatch.setattr(lark_client.time, "sleep", lambda s: None)

    def handler(req: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in str(req.url):
            return _token_response()
        return httpx.Response(429, text="never recovers")

    client = _build_client(handler)
    with pytest.raises(LarkAPIError) as exc:
        client.get_doc_raw("d")
    assert exc.value.status == 429


def test_http_5xx_exponential_backoff(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(lark_client.time, "sleep", lambda s: sleeps.append(s))
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        if "tenant_access_token" in str(req.url):
            return _token_response()
        calls += 1
        if calls < 3:
            return httpx.Response(503, text="bad gateway")
        return _ok_doc_response()

    client = _build_client(handler)
    client.get_doc_raw("d")
    # No Retry-After header → fall back to 1s, 2s, 4s... pattern.
    # First 2 retries → sleep 1s, 2s.
    assert sleeps[:2] == [1.0, 2.0]


def test_http_404_no_retry(monkeypatch):
    """Deterministic 4xx (not 401/429) — surfaces immediately."""
    monkeypatch.setattr(lark_client.time, "sleep", lambda s: None)
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        if "tenant_access_token" in str(req.url):
            return _token_response()
        calls += 1
        return httpx.Response(404, text="doc not found")

    client = _build_client(handler)
    with pytest.raises(LarkAPIError) as exc:
        client.get_doc_raw("d")
    assert exc.value.status == 404
    assert calls == 1  # no retry


# ---------------------------------------------------------------------------
# Retry — Lark business code layer
# ---------------------------------------------------------------------------


def test_lark_code_rate_limit_retries(monkeypatch):
    monkeypatch.setattr(lark_client.time, "sleep", lambda s: None)
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        if "tenant_access_token" in str(req.url):
            return _token_response()
        calls += 1
        if calls < 3:
            return httpx.Response(
                200, json={"code": 99991400, "msg": "frequency limit"},
            )
        return _ok_doc_response("after retry")

    client = _build_client(handler)
    text = client.get_doc_raw("d")
    assert text == "after retry"
    assert calls == 3


def test_lark_code_nonzero_non_rate_limit_raises(monkeypatch):
    monkeypatch.setattr(lark_client.time, "sleep", lambda s: None)

    def handler(req: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in str(req.url):
            return _token_response()
        return httpx.Response(
            200,
            json={"code": 1254000, "msg": "doc not found"},
        )

    client = _build_client(handler)
    with pytest.raises(LarkAPIError) as exc:
        client.get_doc_raw("d_404")
    assert exc.value.code == 1254000


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def test_get_doc_raw_happy_path():
    def handler(req: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in str(req.url):
            return _token_response()
        assert "raw_content" in str(req.url)
        assert req.url.params.get("lang") == "0"
        return _ok_doc_response("Q3 budget proposal\n240k...")

    client = _build_client(handler)
    assert client.get_doc_raw("doc_abc") == "Q3 budget proposal\n240k..."


def test_get_doc_raw_lang_param_passed():
    captured_params = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in str(req.url):
            return _token_response()
        captured_params["lang"] = req.url.params.get("lang")
        return _ok_doc_response("en text")

    client = _build_client(handler)
    client.get_doc_raw("doc_xyz", lang=1)
    assert captured_params["lang"] == "1"


def test_get_wiki_node_happy_path():
    def handler(req: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in str(req.url):
            return _token_response()
        assert "wiki/v2/spaces/get_node" in str(req.url)
        assert req.url.params.get("token") == "wiki_tk_123"
        return httpx.Response(
            200,
            json={
                "code": 0, "msg": "ok",
                "data": {"node": {
                    "obj_token": "docx_abc",
                    "obj_type": "docx",
                    "title": "Q3 Planning",
                }},
            },
        )

    client = _build_client(handler)
    node = client.get_wiki_node("wiki_tk_123")
    assert node["obj_token"] == "docx_abc"
    assert node["obj_type"] == "docx"
    assert node["title"] == "Q3 Planning"


def test_get_wiki_node_missing_data_returns_empty_dict():
    def handler(req: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in str(req.url):
            return _token_response()
        return httpx.Response(200, json={"code": 0, "data": {}})

    client = _build_client(handler)
    assert client.get_wiki_node("missing") == {}


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


def test_singleton_returns_same_instance(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "cli_singleton")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret_singleton")
    monkeypatch.setenv("FEISHU_DOMAIN", "lark")
    lark_client._reset_singleton_for_tests()
    try:
        c1 = get_lark_client()
        c2 = get_lark_client()
        assert c1 is c2
        assert c1.app_id == "cli_singleton"
        assert c1.base_url == "https://open.larksuite.com"
    finally:
        lark_client._reset_singleton_for_tests()


def test_singleton_legacy_lark_env_var_aliases(monkeypatch):
    """LARK_APP_ID / LARK_APP_SECRET work as fallbacks when FEISHU_ * unset."""
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    monkeypatch.setenv("LARK_APP_ID", "cli_legacy")
    monkeypatch.setenv("LARK_APP_SECRET", "secret_legacy")
    lark_client._reset_singleton_for_tests()
    try:
        c = get_lark_client()
        assert c.app_id == "cli_legacy"
    finally:
        lark_client._reset_singleton_for_tests()


def test_singleton_missing_creds_raises(monkeypatch):
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    monkeypatch.delenv("LARK_APP_ID", raising=False)
    monkeypatch.delenv("LARK_APP_SECRET", raising=False)
    lark_client._reset_singleton_for_tests()
    try:
        with pytest.raises(LarkConfigError):
            get_lark_client()
    finally:
        lark_client._reset_singleton_for_tests()


# ---------------------------------------------------------------------------
# Thread safety smoke
# ---------------------------------------------------------------------------


def test_concurrent_token_fetch_single_auth_call():
    """Multiple threads hitting the client simultaneously should refresh
    the token only ONCE. Lock-protected double-check."""
    auth_calls = 0
    auth_lock = threading.Lock()

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal auth_calls
        if "tenant_access_token" in str(req.url):
            with auth_lock:
                auth_calls += 1
            # Simulate slow auth call so threads race.
            time.sleep(0.05)
            return _token_response()
        return _ok_doc_response()

    client = _build_client(handler)
    threads = [threading.Thread(target=lambda: client.get_doc_raw(f"d{i}"))
               for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert auth_calls == 1
