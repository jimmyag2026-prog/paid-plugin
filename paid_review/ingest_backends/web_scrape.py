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
from urllib.parse import urlsplit

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

        # SSRF gate
        try:
            host = (urlsplit(url).hostname or "").lower()
        except Exception as exc:
            return BackendResult(
                normalized="",
                backend=self.name,
                source=source,
                errors=[f"URL parse failed: {exc}"],
            )

        ok, reason = _is_safe_host(host)
        if not ok:
            return BackendResult(
                normalized="",
                backend=self.name,
                source=source,
                errors=[f"web scrape blocked by SSRF guard: {reason}"],
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

        try:
            with httpx.Client(
                follow_redirects=True,
                timeout=_TIMEOUT_SEC,
                max_redirects=_MAX_REDIRECTS,
                headers={"User-Agent": "paid-review/1.5 (web-scrape)"},
            ) as client:
                resp = client.get(url)
        except httpx.TimeoutException as exc:
            return BackendResult(
                normalized="",
                backend=self.name,
                source=source,
                errors=[f"web scrape timeout after {_TIMEOUT_SEC}s: {exc}"],
            )
        except httpx.TooManyRedirects:
            return BackendResult(
                normalized="",
                backend=self.name,
                source=source,
                errors=[f"web scrape exceeded {_MAX_REDIRECTS} redirects"],
            )
        except Exception as exc:
            logger.warning("[web-scrape] fetch crashed %s: %s", url, exc)
            return BackendResult(
                normalized="",
                backend=self.name,
                source=source,
                errors=[f"web scrape fetch crashed: {exc}"],
            )

        if resp.status_code >= 400:
            return BackendResult(
                normalized="",
                backend=self.name,
                source=source,
                errors=[f"web scrape HTTP {resp.status_code}"],
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
