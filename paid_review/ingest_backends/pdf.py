"""PdfBackend — extract text from PDF attachments.

v1.5 Phase 3 (T2.PDF). Primary path uses the ``pdftotext`` CLI from
``poppler-utils`` — already installed on VPS paid user (Phase 0 inventory).
Optional pdfminer.six fallback if Python lib is installed.

Activates when the dispatcher sees an attachment with ``.pdf`` ext or
``application/pdf`` mime. Returns ``[attachment: name]`` placeholder
when no extractor is available — graceful degrade, never raises.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from .base import BackendResult, IngestBackend, truncate_with_note

logger = logging.getLogger(__name__)


_PDF_EXTS = {".pdf"}
_PDF_MIMES = {"application/pdf"}

_PDFTOTEXT_TIMEOUT_SEC = 30


class PdfBackend(IngestBackend):
    """Reads PDF files and inlines their plain-text content.

    Backend selection at construction time:
      1. ``pdftotext`` CLI on $PATH (preferred — fast, mature, handles
         layout / forms / multi-column)
      2. ``pdfminer.six`` Python lib if installed (fallback when CLI
         missing — slower but pure-python deploy)
      3. Neither → backend is "available" but returns placeholders +
         warning errors; dispatcher still archives the file
    """

    name = "pdf"

    def __init__(self):
        self._pdftotext_path = shutil.which("pdftotext")
        try:
            import pdfminer  # type: ignore  # noqa: F401
            self._pdfminer_available = True
        except Exception:
            self._pdfminer_available = False

    @property
    def has_extractor(self) -> bool:
        return bool(self._pdftotext_path) or self._pdfminer_available

    def can_handle_file(self, *, mime: str = "", ext: str = "") -> bool:
        ext_l = (ext or "").lower()
        mime_l = (mime or "").lower()
        return ext_l in _PDF_EXTS or mime_l in _PDF_MIMES

    def ingest_file(self, path: Path, *, mime: str = "") -> BackendResult:
        source = str(path)

        # CLI path
        if self._pdftotext_path:
            return self._ingest_via_pdftotext(path, source)

        # Fallback to pdfminer
        if self._pdfminer_available:
            return self._ingest_via_pdfminer(path, source)

        # No extractor — placeholder + advisory error so owner knows why
        return BackendResult(
            normalized=f"[attachment: {path.name}]",
            backend=self.name,
            source=source,
            errors=[
                "no PDF extractor available — install `poppler-utils` (apt) "
                "or `pdfminer.six` (pip) to enable PDF text extraction"
            ],
        )

    # ------------------------------------------------------------------
    # Extractors
    # ------------------------------------------------------------------

    def _ingest_via_pdftotext(self, path: Path, source: str) -> BackendResult:
        """Invoke `pdftotext -layout <path> -` and capture stdout.

        ``-layout`` preserves columns/tables better than the default
        reading order. ``-`` sends output to stdout instead of a file.
        """
        try:
            proc = subprocess.run(
                [self._pdftotext_path, "-layout", str(path), "-"],
                capture_output=True,
                timeout=_PDFTOTEXT_TIMEOUT_SEC,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return BackendResult(
                normalized=f"[attachment: {path.name}]",
                backend=self.name,
                source=source,
                errors=[f"pdftotext timeout after {_PDFTOTEXT_TIMEOUT_SEC}s"],
            )
        except FileNotFoundError as exc:
            # Race: pdftotext disappeared between __init__ and now.
            return BackendResult(
                normalized=f"[attachment: {path.name}]",
                backend=self.name,
                source=source,
                errors=[f"pdftotext invocation failed: {exc}"],
            )
        except Exception as exc:
            logger.warning("[pdf-backend] pdftotext crashed %s: %s", path, exc)
            return BackendResult(
                normalized=f"[attachment: {path.name}]",
                backend=self.name,
                source=source,
                errors=[f"pdftotext crashed: {exc}"],
            )

        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace")[:200]
            return BackendResult(
                normalized=f"[attachment: {path.name}]",
                backend=self.name,
                source=source,
                errors=[f"pdftotext exit {proc.returncode}: {stderr}"],
            )

        try:
            text = proc.stdout.decode("utf-8", errors="replace")
        except Exception as exc:
            return BackendResult(
                normalized=f"[attachment: {path.name}]",
                backend=self.name,
                source=source,
                errors=[f"pdftotext stdout decode failed: {exc}"],
            )

        if not text.strip():
            # Could be a scanned/image PDF — no text layer.
            # Phase 4 OCR backend would chain here in future; for v1.5.0
            # T2.PDF alone, we surface as advisory.
            return BackendResult(
                normalized=f"[attachment: {path.name}]",
                backend=self.name,
                source=source,
                errors=[
                    "pdftotext extracted 0 chars — PDF may be image-based "
                    "(scanned). OCR support is Phase 4 image backend."
                ],
            )

        truncated, note = truncate_with_note(text)
        return BackendResult(
            normalized=truncated,
            backend=self.name,
            source=source,
            note=note,
        )

    def _ingest_via_pdfminer(self, path: Path, source: str) -> BackendResult:
        """Fallback Python extraction. Only invoked when pdftotext missing."""
        try:
            from pdfminer.high_level import extract_text  # type: ignore
        except Exception as exc:
            return BackendResult(
                normalized=f"[attachment: {path.name}]",
                backend=self.name,
                source=source,
                errors=[f"pdfminer import failed: {exc}"],
            )

        try:
            text = extract_text(str(path))
        except Exception as exc:
            logger.warning("[pdf-backend] pdfminer crashed %s: %s", path, exc)
            return BackendResult(
                normalized=f"[attachment: {path.name}]",
                backend=self.name,
                source=source,
                errors=[f"pdfminer crashed: {exc}"],
            )

        if not text or not text.strip():
            return BackendResult(
                normalized=f"[attachment: {path.name}]",
                backend=self.name,
                source=source,
                errors=["pdfminer extracted 0 chars (likely image-based PDF)"],
            )

        truncated, note = truncate_with_note(text)
        return BackendResult(
            normalized=truncated,
            backend=self.name,
            source=source,
            note=note,
        )
