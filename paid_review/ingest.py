"""paid_review.ingest — multi-backend dispatcher (v1.5).

Receives (initial_message, attachments) from api.intake; routes each
piece to the appropriate backend in :mod:`paid_review.ingest_backends`
and aggregates the results into a normalized markdown + audit trail
of sources and errors.

Backwards compatible with v1.4.x: ``IngestResult`` retains
``.normalized_text``, ``.saved_inputs``, ``.errors``, and ``.ok``.
New field ``.sources`` records per-backend audit info for the brief
to render.

Failure model (doc 09 §5.5 — partial-failure UX):
  - Each backend failure → entry in IngestResult.errors + the failing
    source's BackendResult.errors. Does NOT raise.
  - If at least one source succeeds → review proceeds, brief shows
    ⚠️ Ingest errors block at top.
  - If ALL sources fail and initial_message is empty → IngestResult
    is empty (ok=False), api.intake holds at INTAKE asking junior
    to retype.

URL detection: any HTTP(S) URL in initial_message is offered to URL
backends. Lark Doc/Wiki URLs route to LarkDocBackend; other URLs
remain in the message text (web_scrape backend will pick them up
in Phase 5).
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ingest_backends import (
    BackendResult,
    ImageBackend,
    IngestBackend,
    LarkDocBackend,
    PdfBackend,
    TextBackend,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public result type — backward-compatible with v1.4.x callers
# ---------------------------------------------------------------------------


@dataclass
class IngestResult:
    normalized_text: str
    saved_inputs: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # v1.5: per-backend audit. Each entry: {backend, source, note}.
    # Brief rendering uses this to show "# Source: ..." headers.
    sources: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.normalized_text.strip())


# ---------------------------------------------------------------------------
# Backend registry
# ---------------------------------------------------------------------------


_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _default_backends(lark_client: Any | None = None) -> list[IngestBackend]:
    """Build the standard backend list. Order matters for URL routing:
    a URL is offered to backends in order; first ``can_handle_url``
    True wins.

    lark_client is optional — when None, LarkDocBackend isn't included
    (Lark URLs fall back to remaining-in-text). Tests can pass a fake.
    """
    backends: list[IngestBackend] = [TextBackend()]
    if lark_client is not None:
        backends.append(LarkDocBackend(lark_client))
    # PDF backend always added — graceful-degrades when pdftotext/pdfminer
    # not installed (returns placeholder + advisory error).
    backends.append(PdfBackend())
    # Image OCR backend — same graceful-degrade contract (tesseract
    # CLI + pytesseract + pillow must all be present at runtime, else
    # backend returns placeholder + missing-piece advisory).
    backends.append(ImageBackend())
    return backends


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def ingest(
    initial_message: str,
    attachments: list[dict[str, Any]] | None,
    sid_dir: Path,
    *,
    lark_client: Any | None = None,
    backends: list[IngestBackend] | None = None,
) -> IngestResult:
    """Build normalized.md content from initial_message + attachments.

    Always returns; never raises. Caller writes ``result.normalized_text``
    to disk.

    Parameters
    ----------
    initial_message : the junior's message text. Embedded URLs are
        detected + routed to URL backends.
    attachments : list of {"path", "name"|"filename", "mimetype"|"mime_type",
        "text"|"content"} dicts (legacy v1.4.x schema preserved).
    sid_dir : session directory. File attachments are copied to
        ``sid_dir/input/`` for archiving.
    lark_client : optional LarkClient (production passes
        :func:`paid.lark_client.get_lark_client` singleton; tests pass
        a stub). If None, Lark URLs stay as text.
    backends : optional explicit backend list (tests / future
        extension). When None, ``_default_backends(lark_client)`` is
        used.
    """
    backends = backends if backends is not None else _default_backends(lark_client)

    text_backend = _find_text_backend(backends)
    url_backends = [b for b in backends if any_can_url(b)]

    parts: list[BackendResult] = []
    errors: list[str] = []
    saved: list[Path] = []

    # 1) initial_message text — split into "URL-driven backend results" + "remaining text"
    if initial_message and initial_message.strip():
        remaining_text, url_results = _route_urls_in_text(
            initial_message, url_backends,
        )
        parts.extend(url_results)
        if text_backend is not None and remaining_text.strip():
            parts.append(text_backend.ingest_string(remaining_text, label="message"))

    # 2) attachments — file backends
    input_dir = sid_dir / "input"
    for idx, att in enumerate(attachments or []):
        try:
            result, copied = _route_attachment(att, idx, input_dir, backends)
        except Exception as exc:  # backend-level safety net
            err = f"[ingest] attachment #{idx} unhandled error: {exc}"
            logger.warning(err)
            errors.append(err)
            continue
        if copied is not None:
            saved.append(copied)
        if result is not None:
            parts.append(result)

    # 3) Aggregate parts → normalized markdown + audit
    return _build_result(parts, errors, saved)


def any_can_url(backend: IngestBackend) -> bool:
    """True if backend overrides can_handle_url (not just the ABC default)."""
    try:
        return type(backend).can_handle_url is not IngestBackend.can_handle_url
    except Exception:
        return False


def _find_text_backend(backends: list[IngestBackend]) -> TextBackend | None:
    for b in backends:
        if isinstance(b, TextBackend):
            return b
    return None


def _route_urls_in_text(
    text: str, url_backends: list[IngestBackend],
) -> tuple[str, list[BackendResult]]:
    """For each URL in text, offer to URL backends. URLs handled by a
    backend are removed from the returned text (the backend's
    normalized content replaces them); URLs no one handles stay in
    text as-is.
    """
    if not url_backends:
        return text, []
    results: list[BackendResult] = []
    out_text = text
    for m in _URL_RE.finditer(text):
        url = m.group(0).rstrip(".,;)]>")
        handler = next((b for b in url_backends if b.can_handle_url(url)), None)
        if handler is None:
            continue
        try:
            res = handler.ingest_url(url)
        except Exception as exc:
            res = BackendResult(
                normalized="",
                backend=handler.name,
                source=url,
                errors=[f"unhandled exception: {exc}"],
            )
        results.append(res)
        # Strip the URL from the text — the backend's content replaces it
        # in normalized.md. Keep a placeholder to preserve sentence shape.
        out_text = out_text.replace(url, "")
    return out_text, results


def _route_attachment(
    att: dict[str, Any],
    idx: int,
    input_dir: Path,
    backends: list[IngestBackend],
) -> tuple[BackendResult | None, Path | None]:
    """Returns (BackendResult or None, copied_path or None)."""
    name = (att.get("name") or att.get("filename") or "").strip()
    path_str = att.get("path") or att.get("file_path") or ""
    mimetype = (att.get("mimetype") or att.get("mime_type") or "").lower()
    src = Path(path_str) if path_str else None

    # Inline-text attachment (no file on disk) → text backend convenience
    if (not src or not src.exists()) and (att.get("text") or att.get("content")):
        inline = (att.get("text") or att.get("content") or "").strip()
        text_backend = _find_text_backend(backends)
        if text_backend is None:
            return None, None
        label = name or f"inline_{idx}"
        return text_backend.ingest_string(inline, label=label), None

    if not src or not src.exists() or not src.is_file():
        # No usable source — return a placeholder breadcrumb so the
        # downstream prompt notes something was attached.
        label = name or f"attachment_{idx}"
        return BackendResult(
            normalized=f"[attachment: {label}]",
            backend="placeholder",
            source=label,
            errors=[f"attachment #{idx} ({label}): no readable path / inline content"],
        ), None

    # Copy to input_dir for archive (don't fail ingest if copy fails)
    copied: Path | None = None
    try:
        input_dir.mkdir(parents=True, exist_ok=True)
        safe_name = name or src.name or f"attachment_{idx}"
        dest = input_dir / safe_name
        if dest.exists():
            stem, suffix = dest.stem, dest.suffix
            dest = input_dir / f"{stem}_{idx}{suffix}"
        shutil.copy2(src, dest)
        copied = dest
    except Exception as exc:
        logger.warning("[ingest] copy failed for %s: %s", src, exc)

    suffix = src.suffix.lower()
    handler = next(
        (b for b in backends if b.can_handle_file(mime=mimetype, ext=suffix)),
        None,
    )

    if handler is None:
        # No backend claims it — placeholder, but file is saved.
        label = name or src.name or f"attachment_{idx}"
        return BackendResult(
            normalized=f"[attachment: {label}]",
            backend="placeholder",
            source=str(src),
            note=f"no backend for mime={mimetype or '?'} ext={suffix or '?'}",
        ), copied

    try:
        result = handler.ingest_file(src, mime=mimetype)
    except Exception as exc:
        # Backend internal failure — record as error.
        result = BackendResult(
            normalized="",
            backend=handler.name,
            source=str(src),
            errors=[f"unhandled exception: {exc}"],
        )
    return result, copied


def _build_result(
    parts: list[BackendResult],
    extra_errors: list[str],
    saved: list[Path],
) -> IngestResult:
    """Assemble final IngestResult.

    Normalized markdown layout:
        # Source: <source>
        _(<note>)_                 ← only if backend set a note
        <content>

        ---

        # Source: <source>
        ...
    """
    rendered_parts: list[str] = []
    sources: list[dict] = []
    errors: list[str] = list(extra_errors)

    for p in parts:
        if p.errors:
            errors.extend(f"[{p.backend}:{p.source or '?'}] {e}" for e in p.errors)
        sources.append({
            "backend": p.backend,
            "source": p.source,
            "note": p.note,
        })
        if not p.ok:
            continue
        # text backend on "message" source = junior's plain inbound text —
        # render bare (no header), matches v1.4 layout. Every other
        # source-bearing part gets "# Source: <id>" header so owner can
        # see what content came from where.
        suppress_header = (p.backend == "text" and p.source == "message")
        header = (
            f"# Source: {p.source}\n" if (p.source and not suppress_header) else ""
        )
        note_line = f"_({p.note})_\n\n" if p.note else ""
        body = p.normalized
        rendered_parts.append(f"{header}{note_line}{body}".strip())

    normalized = "\n\n---\n\n".join(rendered_parts)
    return IngestResult(
        normalized_text=normalized,
        saved_inputs=saved,
        errors=errors,
        sources=sources,
    )
