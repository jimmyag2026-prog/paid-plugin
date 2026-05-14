"""paid_review.ingest_backends — multimedia ingest backend plugins.

Each backend implements ``IngestBackend`` and handles one source type
(text passthrough, Lark Doc URL, PDF file, OCR image, …). The dispatcher
in ``paid_review.ingest`` routes initial_message URLs + attachment files
to the appropriate backend and aggregates results.

Phase 1 (v1.5.0) backends:
  - text    — plain string + textlike files (.txt/.md/...)
  - lark_doc — Lark Doc / Wiki URL → text via Lark Open API

Phase 2+ planned:
  - pdf       — pdftotext / pdfminer
  - image     — tesseract OCR
  - web_scrape — beautifulsoup4 + readability-lxml + SSRF defense
  - audio     — Whisper (local or API)
"""

from .base import BackendResult, IngestBackend
from .text import TextBackend
from .lark_doc import LarkDocBackend
from .pdf import PdfBackend
from .image import ImageBackend

__all__ = [
    "BackendResult",
    "IngestBackend",
    "TextBackend",
    "LarkDocBackend",
    "PdfBackend",
    "ImageBackend",
]
