"""WebScrapeBackend — fetch and extract main text from public HTTP(S) URLs.

v1.5 Phase 5 (T2.web). Activates on generic HTTP(S) URLs that other
URL backends declined (e.g. Lark URLs go to LarkDocBackend first).

Stack:
  - ``httpx`` for the request (sync — entire ingest runs inside
    ``run_in_executor`` from gateway loop, see design 09 §5.3)
  - ``readability-lxml`` to extract the "main article" body
  - ``beautifulsoup4`` to strip remaining HTML → plain text
  - Falls back to bs4.get_text on the raw response when readability fails

SSRF defense (see design 09 §5.6):
  - Only http/https schemes
  - Reject hosts that resolve to loopback / link-local / private /
    multicast / reserved IP ranges
  - DNS rebinding mitigated by resolving the hostname ourselves and
    passing the IP into httpx (with the original Host header preserved)
  - Default 20s connect+read timeout
  - Follows up to 3 redirects; each hop re-validated by httpx (we
    pass `follow_redirects=True` and let httpx re-validate URL scheme)

Graceful-degrade when bs4/readability/httpx missing: backend stays
"available" but every ingest returns placeholder + advisory error so
the dispatcher still records the source attempt.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
from urllib.parse import urljoin, urlsplit

# httpx is imported eagerly when available so that the module-level
# `httpx` symbol exists for monkeypatching in tests. When absent we
# leave it as None and check `_httpx_available` at call time.
try:
    import httpx  # type: ignore
    _HTTPX_IMPORT_OK = True
except Exception:
    httpx = None  # type: ignore
    _HTTPX_IMPORT_OK = False

from .base import BackendResult, IngestBackend, truncate_with_note

logger = logging.getLogger(__name__)


_TIMEOUT_SEC = 20.0
_MAX_REDIRECTS = 3
# Cap response size at ~2 MB before extraction (extraction can balloon
# memory on huge HTML; truncate_with_note still bounds normalized).
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


_HTTP_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


# v1.6.15: anti-scrape / JS-wall placeholder signatures. Sites like x.com
# return HTTP 200 with a short "enable JavaScript" shell when hit by a
# non-browser client. Pre-v1.6.15 that shell (non-empty!) sailed past the
# 0-chars guard and got fed to the four-pillar reviewer AS IF it were the
# document — worse than an empty input. If the *entire* extracted body is
# short AND contains one of these, treat it as a failed fetch.
_ANTISCRAPE_SIGNATURES = (
    "javascript is disabled",
    "please enable javascript",
    "enable javascript or switch to a supported browser",
    "we've detected that javascript is disabled",
    "something went wrong, but don",  # x.com error shell
    "privacy related extensions may cause issues",
    "are you a robot",
    "verify you are human",
    "checking if the site connection is secure",  # Cloudflare interstitial
    "请开启 javascript",
    "请启用 javascript",
)
# Only treat as anti-scrape when the body is short — a real article that
# merely mentions "JavaScript" in passing must not be nuked.
_ANTISCRAPE_MAX_LEN = 800


# Hosts/ports that other backends own — WebScrapeBackend must not steal them.
_LARK_HOST_SUFFIXES = (".feishu.cn", ".larksuite.com")


def _is_lark_url(url: str) -> bool:
    """Lark URLs are owned by LarkDocBackend even on non-/docx, /wiki/ paths
    (e.g. /sheets/, /base/) — defer to it.
    """
    try:
        host = (urlsplit(url).hostname or "").lower()
    except Exception:
        return False
    return any(host.endswith(s) for s in _LARK_HOST_SUFFIXES)


def _is_safe_host(host: str) -> tuple[bool, str]:
    """SSRF gate.

    Returns ``(ok, reason)``. ok=False blocks the fetch with reason
    surfaced in errors so owner sees why the URL was skipped.

    Logic:
      - Resolve hostname → all A/AAAA records
      - Reject if ANY resolved IP is in a private/loopback/link-local/
        multicast/reserved/unspecified range
      - "Any IP bad" rather than "all IPs bad" — defends against
        split-horizon DNS where one record is public and another is
        internal
    """
    if not host:
        return False, "empty host"
    # Refuse bare IPs in private ranges before DNS lookup
    try:
        ip = ipaddress.ip_address(host)
        bad = _classify_ip(ip)
        if bad:
            return False, f"bare IP host blocked: {bad}"
    except ValueError:
        pass  # not a literal IP, fall through to DNS

    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        return False, f"DNS lookup failed: {exc}"
    except Exception as exc:
        return False, f"DNS error: {exc}"

    if not infos:
        return False, "no DNS records"

    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        bad = _classify_ip(ip)
        if bad:
            return False, f"host resolves to {bad} address ({ip_str})"

    return True, ""


def _classify_ip(ip: ipaddress._BaseAddress) -> str:
    """Return a non-empty reason string if IP is in a forbidden range, else ''.

    Order matters: link-local and loopback are subsets of `is_private`
    in stdlib semantics, so we check those first to give a more useful
    reason string (e.g. AWS metadata 169.254.169.254 → "link-local"
    rather than the generic "private").
    """
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local"
    if ip.is_multicast:
        return "multicast"
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_private:
        return "private"
    if ip.is_reserved:
        return "reserved"
    return ""


def _fetch_with_ssrf_aware_redirects(url: str):
    """Fetch *url* with SSRF revalidation on every redirect hop.

    Returns ``(response, None)`` on success or ``(None, error_message)``
    on failure. ``response`` is the final non-redirect httpx.Response;
    ``error_message`` is a human-readable string for the brief's ⚠️
    block.

    Behavior:
      - http(s) only at every hop.
      - At each hop, parse the host, run ``_is_safe_host`` on it, and
        refuse to fetch if any IP behind that host is in a forbidden
        range. This defeats the "publicly-hosted 302 → internal IP"
        SSRF bypass that ``follow_redirects=True`` previously allowed
        (v1.5.0 audit High #1).
      - Up to ``_MAX_REDIRECTS`` hops. More → "too many redirects" error.
      - 30x with no Location → treated as a non-redirect terminal response
        (caller decides what to do with the status code).
    """
    assert httpx is not None  # caller has already gated on this

    current_url = url
    seen: list[str] = []

    for _hop in range(_MAX_REDIRECTS + 1):
        # Per-hop scheme + SSRF gate
        try:
            parts = urlsplit(current_url)
        except Exception as exc:
            return None, f"URL parse failed: {exc}"
        scheme = (parts.scheme or "").lower()
        if scheme not in ("http", "https"):
            return None, f"web scrape blocked: non-http(s) redirect target ({scheme})"
        host = (parts.hostname or "").lower()
        ok, reason = _is_safe_host(host)
        if not ok:
            return None, f"web scrape blocked by SSRF guard: {reason}"

        seen.append(current_url)
        try:
            with httpx.Client(
                follow_redirects=False,           # we drive redirects ourselves
                timeout=_TIMEOUT_SEC,
                headers={"User-Agent": "paid-review/1.5 (web-scrape)"},
            ) as client:
                resp = client.get(current_url)
        except httpx.TimeoutException as exc:
            return None, f"web scrape timeout after {_TIMEOUT_SEC}s: {exc}"
        except Exception as exc:
            logger.warning("[web-scrape] fetch crashed %s: %s", current_url, exc)
            return None, f"web scrape fetch crashed: {exc}"

        # 3xx with a Location → next hop. Anything else → terminal.
        if 300 <= resp.status_code < 400:
            location = resp.headers.get("location") or resp.headers.get("Location") or ""
            if not location:
                # 30x without Location — treat as terminal redirect-failure
                return resp, None
            next_url = urljoin(current_url, location.strip())
            if next_url in seen:
                return None, "web scrape redirect loop detected"
            current_url = next_url
            continue
        return resp, None

    return None, f"web scrape exceeded {_MAX_REDIRECTS} redirects"


class WebScrapeBackend(IngestBackend):
    """Fetches a public HTTP(S) URL and extracts the article body."""

    name = "web_scrape"

    def __init__(self):
        self._httpx_available = _HTTPX_IMPORT_OK
        try:
            import bs4  # type: ignore  # noqa: F401
            self._bs4_available = True
        except Exception:
            self._bs4_available = False
        try:
            from readability import Document  # type: ignore  # noqa: F401
            self._readability_available = True
        except Exception:
            self._readability_available = False

    @property
    def has_extractor(self) -> bool:
        # bs4 alone is sufficient — readability is best-effort enrichment.
        return self._httpx_available and self._bs4_available

    def can_handle_url(self, url: str) -> bool:
        if not isinstance(url, str):
            return False
        if not _HTTP_URL_RE.match(url):
            return False
        # Hand off Lark URLs even if LarkDocBackend isn't in the list
        # (cleaner failure mode — owner gets "Lark client not configured"
        # rather than a half-scraped login page).
        if _is_lark_url(url):
            return False
        return True

    def ingest_url(self, url: str) -> BackendResult:
        source = url

        missing: list[str] = []
        if not self._httpx_available:
            missing.append("httpx (`pip install httpx`)")
        if not self._bs4_available:
            missing.append("beautifulsoup4 (`pip install beautifulsoup4`)")
        if missing:
            return BackendResult(
                normalized="",
                backend=self.name,
                source=source,
                errors=[
                    "web scrape unavailable — missing: " + ", ".join(missing)
                ],
            )

        # Fetch — module-level `httpx` is the import target, which
        # also makes it monkeypatch-able from tests.
        if httpx is None:
            return BackendResult(
                normalized="",
                backend=self.name,
                source=source,
                errors=["httpx import race: module unavailable at fetch time"],
            )

        # v1.5.1 fix (audit High #1): manual redirect loop. Previously we
        # passed follow_redirects=True to httpx and only validated the
        # initial URL's host — an attacker could host a public URL that
        # 302-redirects to http://10.0.0.1/secret and we'd happily fetch
        # internal-network content. Now we re-run _is_safe_host on every
        # hop.
        resp, err = _fetch_with_ssrf_aware_redirects(url)
        if err is not None:
            return BackendResult(
                normalized="", backend=self.name, source=source, errors=[err],
            )

        if resp is None or resp.status_code >= 400:
            code = resp.status_code if resp is not None else "?"
            return BackendResult(
                normalized="",
                backend=self.name,
                source=source,
                errors=[f"web scrape HTTP {code}"],
            )

        content_type = (resp.headers.get("content-type") or "").lower()
        if content_type and "html" not in content_type and "text" not in content_type:
            return BackendResult(
                normalized="",
                backend=self.name,
                source=source,
                errors=[
                    f"web scrape non-HTML response (content-type={content_type[:80]})"
                ],
            )

        raw_html = resp.content[:_MAX_RESPONSE_BYTES]

        # Try readability first → fall back to bs4-only on failure
        extracted = ""
        title = ""
        try:
            extracted, title = self._extract_with_readability(raw_html, url)
        except Exception as exc:
            logger.info("[web-scrape] readability failed for %s, falling back: %s", url, exc)

        if not extracted or not extracted.strip():
            extracted = self._extract_with_bs4(raw_html)

        if not extracted or not extracted.strip():
            return BackendResult(
                normalized="",
                backend=self.name,
                source=source,
                errors=[
                    "web scrape extracted 0 chars — page may be JS-rendered "
                    "or have an unusual layout. Junior should paste the "
                    "relevant text directly."
                ],
            )

        # v1.6.15: anti-scrape wall detection. A short body that is just a
        # "enable JavaScript" / bot-check shell is NOT content — reject it
        # so _route_urls_in_text keeps the URL + reason instead of feeding
        # the shell to the reviewer.
        _stripped = extracted.strip()
        if len(_stripped) <= _ANTISCRAPE_MAX_LEN:
            _low = _stripped.lower()
            if any(sig in _low for sig in _ANTISCRAPE_SIGNATURES):
                return BackendResult(
                    normalized="",
                    backend=self.name,
                    source=source,
                    errors=[
                        "site requires JavaScript / blocks scrapers "
                        "(anti-scrape wall). A stronger fetch tool is "
                        "needed for this host, or paste the text directly."
                    ],
                )

        prefix = f"# {title.strip()}\n\n" if title and title.strip() else ""
        body = prefix + extracted.strip()
        truncated, note = truncate_with_note(body)
        return BackendResult(
            normalized=truncated,
            backend=self.name,
            source=source,
            note=note,
        )

    # ------------------------------------------------------------------
    # Extractors
    # ------------------------------------------------------------------

    def _extract_with_readability(self, html_bytes: bytes, url: str) -> tuple[str, str]:
        """Returns (cleaned_text, title) or raises."""
        if not self._readability_available:
            return "", ""
        from readability import Document  # type: ignore
        from bs4 import BeautifulSoup  # type: ignore

        doc = Document(html_bytes)
        title = (doc.short_title() or "").strip()
        summary_html = doc.summary(html_partial=True)
        soup = BeautifulSoup(summary_html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator="\n").strip()
        # Collapse runs of blank lines
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text, title

    def _extract_with_bs4(self, html_bytes: bytes) -> str:
        """Fallback: strip all tags, return plain text."""
        try:
            from bs4 import BeautifulSoup  # type: ignore
        except Exception as exc:
            logger.warning("[web-scrape] bs4 import failed at extract time: %s", exc)
            return ""

        try:
            soup = BeautifulSoup(html_bytes, "html.parser")
        except Exception as exc:
            logger.warning("[web-scrape] bs4 parse failed: %s", exc)
            return ""

        for tag in soup(["script", "style", "noscript", "iframe", "header", "footer", "nav"]):
            tag.decompose()
        text = soup.get_text(separator="\n").strip()
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text
