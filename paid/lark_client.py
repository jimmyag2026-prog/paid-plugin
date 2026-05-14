"""Module LC — sync Lark/Feishu Open Platform client (v1.5 Phase 1).

Lightweight sync httpx wrapper around the Lark Open API endpoints that
paid_review v1.5 multimedia ingest needs (Tier 1 = Lark Doc + Wiki).

Design constraints (see paid-may/design/09_review_v1.5_multimedia_ingest §3):
  - sync httpx, NOT async — paid_review ingest runs inside
    ``run_in_executor`` from the gateway loop; sync API simplifies the
    bridge and avoids the deadlock class we hit in v1.4 TG callback
    (see memory feedback_im_bot_api_traps).
  - token TTL caching, lock-protected for multi-thread safety
  - retry matrix (401 refresh / 429 backoff / 5xx exponential)
  - module-level lazy singleton — caller does NOT manage lifetime
  - env vars: FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_DOMAIN
    (also accepts LARK_APP_ID / LARK_APP_SECRET legacy aliases)

Public surface (Phase 1):
  - get_doc_raw(document_id) -> str
  - get_wiki_node(wiki_token, obj_type="wiki") -> dict
  - LarkAPIError (raised on non-recoverable failures)
  - get_lark_client() — singleton accessor

Tier 2/3 additions (later phases): download_file, get_bitable_records,
append_doc_blocks, etc. — keep this module focused for v1.5.0 Tier 1.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LarkAPIError(RuntimeError):
    """Raised on non-recoverable Lark API failures (after retries exhausted).

    Caller (typically an ingest backend) catches this, records to
    IngestResult.errors, and the dispatcher continues with whatever
    succeeded (per doc 09 §5.5 partial-failure UX).
    """

    def __init__(self, message: str, *, code: int | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


class LarkConfigError(RuntimeError):
    """Raised at construction time when LARK/FEISHU env vars are missing."""


# ---------------------------------------------------------------------------
# Token cache (TTL + thread-safe)
# ---------------------------------------------------------------------------


@dataclass
class _TokenState:
    value: str = ""
    expires_at: float = 0.0  # epoch seconds


_TOKEN_REFRESH_SAFETY_SEC = 600  # refresh 10 min before actual expiry


# ---------------------------------------------------------------------------
# Domain → base URL
# ---------------------------------------------------------------------------


_DOMAIN_BASE_URLS = {
    "lark": "https://open.larksuite.com",
    "larksuite": "https://open.larksuite.com",
    "feishu": "https://open.feishu.cn",
}


def _resolve_base_url(domain: str | None) -> str:
    key = (domain or "").strip().lower()
    if key in _DOMAIN_BASE_URLS:
        return _DOMAIN_BASE_URLS[key]
    # Default: feishu (CN) — matches hermes feishu adapter's default
    # (gateway/platforms/feishu.py:1450 `os.getenv("FEISHU_DOMAIN", "feishu")`).
    return _DOMAIN_BASE_URLS["feishu"]


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


_DEFAULT_MAX_RETRIES = 3
_DEFAULT_CONNECT_TIMEOUT = 5.0
_DEFAULT_READ_TIMEOUT = 30.0

# Lark business-error codes we treat as rate-limited (retry with backoff).
# 99991400 = "frequency limited" per Lark Open Platform docs.
_LARK_RATE_LIMIT_CODES = {99991400}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class LarkClient:
    """Sync Lark Open API client with token caching + retry policy.

    Construct directly for tests; production code should use the
    module-level singleton via :func:`get_lark_client`.
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        base_url: str | None = None,
        domain: str | None = None,
        http: httpx.Client | None = None,
        connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = _DEFAULT_READ_TIMEOUT,
    ):
        if not app_id or not app_secret:
            raise LarkConfigError(
                "LarkClient requires app_id + app_secret; "
                "set FEISHU_APP_ID / FEISHU_APP_SECRET in ~/.hermes/.env"
            )
        self.app_id = app_id
        self.app_secret = app_secret
        self.base_url = (base_url or _resolve_base_url(domain)).rstrip("/")

        # Caller-supplied http client wins (lets tests inject MockTransport).
        # When we create our own, configure conservative timeouts.
        if http is not None:
            self._http = http
            self._owned_http = False
        else:
            self._http = httpx.Client(
                timeout=httpx.Timeout(read_timeout, connect=connect_timeout),
            )
            self._owned_http = True

        self._token = _TokenState()
        self._token_lock = threading.Lock()

    def close(self) -> None:
        if self._owned_http:
            try:
                self._http.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------
    # Token lifecycle
    # ------------------------------------------------------------------

    def _get_tenant_access_token(self, *, force_refresh: bool = False) -> str:
        """Return a cached token, refreshing when it's within
        ``_TOKEN_REFRESH_SAFETY_SEC`` of expiry (or on force_refresh).

        Lock-protected for thread safety: PAID's gateway may dispatch
        events on multiple threads, all hitting LarkClient concurrently.
        """
        # Fast path — no lock when cache is fresh.
        if not force_refresh:
            tok = self._token
            if tok.value and time.time() < tok.expires_at - _TOKEN_REFRESH_SAFETY_SEC:
                return tok.value

        with self._token_lock:
            # Re-check under lock — another thread may have refreshed.
            if not force_refresh:
                tok = self._token
                if tok.value and time.time() < tok.expires_at - _TOKEN_REFRESH_SAFETY_SEC:
                    return tok.value

            url = f"{self.base_url}/open-apis/auth/v3/tenant_access_token/internal"
            try:
                resp = self._http.post(
                    url,
                    json={"app_id": self.app_id, "app_secret": self.app_secret},
                    timeout=15.0,
                )
            except httpx.HTTPError as exc:
                raise LarkAPIError(
                    f"tenant_access_token network error: {exc}"
                ) from exc

            if resp.status_code != 200:
                raise LarkAPIError(
                    f"tenant_access_token HTTP {resp.status_code}: {resp.text[:300]}",
                    status=resp.status_code,
                )

            try:
                body = resp.json()
            except Exception as exc:
                raise LarkAPIError(
                    f"tenant_access_token non-JSON response: {resp.text[:300]}"
                ) from exc

            code = body.get("code")
            if code != 0:
                raise LarkAPIError(
                    f"tenant_access_token failed (code={code}, msg={body.get('msg')!r})",
                    code=code,
                )

            self._token = _TokenState(
                value=str(body.get("tenant_access_token", "")),
                expires_at=time.time() + int(body.get("expire", 7200)),
            )
            return self._token.value

    # ------------------------------------------------------------------
    # Request transport with retry
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> dict:
        """All Lark API calls go through here. Handles:

          - Bearer auth (from cached tenant_access_token)
          - 401 → refresh token + retry ONCE (most likely a stale cache)
          - 429 → backoff (Retry-After respected; otherwise 1s/2s/4s)
          - 5xx → exponential backoff
          - Lark code != 0 + recoverable rate-limit codes → backoff
          - Lark code != 0 + non-recoverable → raise LarkAPIError
          - Network errors (httpx.HTTPError) → backoff up to max_retries

        Returns the parsed JSON dict on success.
        """
        url = f"{self.base_url}{path}"
        attempts = 0
        last_exc: Exception | None = None
        refreshed_this_call = False

        while attempts <= max_retries:
            attempts += 1
            try:
                token = self._get_tenant_access_token()
            except LarkAPIError:
                # Token fetch is itself retried inside; if it raises here,
                # treat as terminal.
                raise

            headers = {"Authorization": f"Bearer {token}"}
            if method.upper() in {"POST", "PUT", "PATCH"} and json is not None:
                headers["Content-Type"] = "application/json"

            try:
                resp = self._http.request(
                    method.upper(),
                    url,
                    headers=headers,
                    params=params,
                    json=json,
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempts > max_retries:
                    raise LarkAPIError(
                        f"{method} {path} network error after {attempts} attempts: {exc}"
                    ) from exc
                _backoff(attempts)
                continue

            # 401 — token likely stale even though TTL says fine.
            # Refresh once and retry; if it happens twice in one call,
            # the credential is wrong and we surface the error.
            if resp.status_code == 401 and not refreshed_this_call:
                refreshed_this_call = True
                try:
                    self._get_tenant_access_token(force_refresh=True)
                except LarkAPIError:
                    raise
                continue

            # 429 — rate limited at HTTP layer.
            if resp.status_code == 429:
                if attempts > max_retries:
                    raise LarkAPIError(
                        f"{method} {path} HTTP 429 after {attempts} attempts",
                        status=429,
                    )
                _backoff(attempts, retry_after=resp.headers.get("Retry-After"))
                continue

            # 5xx — server-side transient.
            if 500 <= resp.status_code < 600:
                if attempts > max_retries:
                    raise LarkAPIError(
                        f"{method} {path} HTTP {resp.status_code} after {attempts} attempts: "
                        f"{resp.text[:300]}",
                        status=resp.status_code,
                    )
                _backoff(attempts)
                continue

            # 4xx other than 401/429 — deterministic, no retry.
            if resp.status_code >= 400:
                raise LarkAPIError(
                    f"{method} {path} HTTP {resp.status_code}: {resp.text[:300]}",
                    status=resp.status_code,
                )

            # 2xx path — inspect Lark business code.
            try:
                body = resp.json()
            except Exception as exc:
                raise LarkAPIError(
                    f"{method} {path} non-JSON response: {resp.text[:300]}"
                ) from exc

            code = body.get("code")
            if code == 0:
                return body

            # Recoverable Lark errors (rate-limit family) → backoff.
            if code in _LARK_RATE_LIMIT_CODES:
                if attempts > max_retries:
                    raise LarkAPIError(
                        f"{method} {path} Lark code={code} msg={body.get('msg')!r} "
                        f"after {attempts} attempts",
                        code=code,
                    )
                _backoff(attempts)
                continue

            # Non-recoverable Lark error.
            raise LarkAPIError(
                f"{method} {path} Lark code={code} msg={body.get('msg')!r}",
                code=code,
                status=resp.status_code,
            )

        # Loop fall-through (shouldn't reach in practice).
        raise LarkAPIError(
            f"{method} {path} exhausted retries; last={last_exc}",
        )

    # ------------------------------------------------------------------
    # Public API — Tier 1
    # ------------------------------------------------------------------

    def get_doc_raw(self, document_id: str, *, lang: int = 0) -> str:
        """Fetch raw text content of a Lark Doc.

        Endpoint: ``GET /open-apis/docx/v1/documents/{document_id}/raw_content``
        Required scope: ``docx:document:readonly``

        Returns the concatenated plain text. Lark caps content size on
        its side; very large docs may return truncated text.
        """
        body = self._request(
            "GET",
            f"/open-apis/docx/v1/documents/{document_id}/raw_content",
            params={"lang": lang},
        )
        content = body.get("data", {}).get("content", "")
        return content if isinstance(content, str) else str(content)

    def get_wiki_node(self, wiki_token: str, *, obj_type: str = "wiki") -> dict:
        """Resolve a Wiki node to its underlying doc/sheet/object.

        Endpoint: ``GET /open-apis/wiki/v2/spaces/get_node``
        Required scope: ``wiki:wiki:readonly``

        Returns the node dict with keys including ``obj_token``,
        ``obj_type`` (e.g. ``"docx"`` when the wiki node wraps a doc),
        ``title``, etc. The lark_doc ingest backend chains this with
        ``get_doc_raw(node['obj_token'])`` when ``obj_type == 'docx'``.
        """
        body = self._request(
            "GET",
            "/open-apis/wiki/v2/spaces/get_node",
            params={"token": wiki_token, "obj_type": obj_type},
        )
        return body.get("data", {}).get("node", {}) or {}


# ---------------------------------------------------------------------------
# Backoff helper
# ---------------------------------------------------------------------------


def _backoff(attempt: int, *, retry_after: str | None = None) -> None:
    """Sleep with exponential backoff, capped. Respects HTTP Retry-After
    when provided (seconds-form).

    attempt is 1-based (first failed call = attempt 1 → sleep 1s).
    """
    if retry_after:
        try:
            seconds = float(retry_after)
            if seconds > 0:
                time.sleep(min(seconds, 30.0))
                return
        except (TypeError, ValueError):
            pass
    # Exponential: 1s, 2s, 4s, 8s capped at 8.
    delay = min(2 ** (attempt - 1), 8.0)
    time.sleep(delay)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


_SINGLETON: LarkClient | None = None
_SINGLETON_LOCK = threading.Lock()


def get_lark_client() -> LarkClient:
    """Lazy singleton accessor. Reads creds from env (FEISHU_APP_ID /
    FEISHU_APP_SECRET, with LARK_APP_ID / LARK_APP_SECRET as legacy
    aliases). Domain from FEISHU_DOMAIN (default "feishu").

    Subsequent calls return the same instance. Raises LarkConfigError
    if creds are missing.

    Tests should construct LarkClient directly; this singleton is for
    plugin runtime where the call is made deep inside an ingest backend.
    """
    global _SINGLETON
    if _SINGLETON is not None:
        return _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is not None:
            return _SINGLETON
        app_id = (
            os.environ.get("FEISHU_APP_ID")
            or os.environ.get("LARK_APP_ID")
            or ""
        ).strip()
        app_secret = (
            os.environ.get("FEISHU_APP_SECRET")
            or os.environ.get("LARK_APP_SECRET")
            or ""
        ).strip()
        domain = os.environ.get("FEISHU_DOMAIN", "").strip() or None
        _SINGLETON = LarkClient(app_id, app_secret, domain=domain)
        return _SINGLETON


def _reset_singleton_for_tests() -> None:
    """Drop the cached singleton so tests can re-construct with mocks."""
    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is not None:
            try:
                _SINGLETON.close()
            except Exception:
                pass
        _SINGLETON = None
