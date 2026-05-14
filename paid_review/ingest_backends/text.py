"""TextBackend — plain string + textlike file (.txt/.md/.csv/.json/…).

Moved from the pre-v1.5 monolithic ingest.py into a proper backend so
the dispatcher can treat all sources uniformly.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .base import BackendResult, IngestBackend, truncate_with_note

logger = logging.getLogger(__name__)


_TEXTLIKE_EXTS = {".txt", ".md", ".markdown", ".csv", ".json", ".log", ".rst"}
_TEXTLIKE_MIME_PREFIXES = ("text/",)


class TextBackend(IngestBackend):
    """Reads plain text files (utf-8 best-effort). Does not handle URLs;
    URL routing is owned by URL-specific backends (lark_doc, web_scrape)."""

    name = "text"

    def can_handle_file(self, *, mime: str = "", ext: str = "") -> bool:
        ext_l = (ext or "").lower()
        mime_l = (mime or "").lower()
        if ext_l in _TEXTLIKE_EXTS:
            return True
        return any(mime_l.startswith(p) for p in _TEXTLIKE_MIME_PREFIXES)

    def ingest_file(self, path: Path, *, mime: str = "") -> BackendResult:
        source = str(path)
        try:
            data = path.read_bytes()
        except Exception as exc:
            logger.warning("[text-backend] read failed %s: %s", path, exc)
            return BackendResult(
                normalized="",
                backend=self.name,
                source=source,
                errors=[f"read failed: {exc}"],
            )

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            # Decode-with-replace so we don't lose the whole file to a
            # single bad byte.
            text = data.decode("utf-8", errors="replace")
        except Exception as exc:
            return BackendResult(
                normalized="",
                backend=self.name,
                source=source,
                errors=[f"decode failed: {exc}"],
            )

        truncated, note = truncate_with_note(text)
        return BackendResult(
            normalized=truncated,
            backend=self.name,
            source=source,
            note=note,
        )

    def ingest_string(self, text: str, *, label: str = "inline") -> BackendResult:
        """Convenience: wrap a raw string as a BackendResult. Used by
        dispatcher to fold initial_message into the same pipeline."""
        if not text or not text.strip():
            return BackendResult(
                normalized="",
                backend=self.name,
                source=label,
            )
        truncated, note = truncate_with_note(text)
        return BackendResult(
            normalized=truncated.strip(),
            backend=self.name,
            source=label,
            note=note,
        )
