"""v1.6.5 — Non-review media enrichment (tesseract OCR + pdftotext).

CP-sent images/PDFs that are NOT part of a /review session are run through
local OCR so the extracted text can be injected into the LLM context window.

Feature flag via settings.json ``media_enrichment_mode``:
  "off"       — disabled (default; safe on systems without OCR tools)
  "tesseract" — OCR images via tesseract subprocess
  "pdftotext" — extract PDF text via pdftotext subprocess
  "both"      — tesseract images + pdftotext PDFs

Lifecycle:
  1. on_pre_gateway_dispatch → enrich_media_for_cp(...)  (stores in cache)
  2. on_pre_llm_call         → pop_enriched_text(...)    (retrieves + clears)

Both callers silently swallow exceptions so media enrichment never blocks
the main message flow.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

_CACHE: dict[str, tuple[str, float]] = {}
_CACHE_TTL = 30.0

_MAX_OCR_CHARS = 4_000
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".tif", ".webp"}
_PDF_EXTS = {".pdf"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def enrich_media_for_cp(
    platform: str,
    sender_id: str,
    media_urls: list[str],
    mimetypes: list[str],
    mode: str,
) -> None:
    """Run OCR/extraction on media files and cache the combined text for this CP.

    Silently skips files that don't match the mode or when the required
    binary (tesseract, pdftotext) is not installed.
    """
    texts: list[str] = []
    for i, path in enumerate(media_urls):
        if not path:
            continue
        mime = mimetypes[i] if i < len(mimetypes) else ""
        extracted = _extract_one(path, mime, mode)
        if extracted:
            texts.append(extracted.strip())

    combined = "\n\n".join(texts)[:_MAX_OCR_CHARS]
    if combined:
        key = f"{platform}:{sender_id}"
        _CACHE[key] = (combined, time.monotonic())


def pop_enriched_text(platform: str, sender_id: str) -> str | None:
    """Return and remove cached enrichment text for this CP.

    Returns None if no entry exists, or if the TTL has expired.
    """
    key = f"{platform}:{sender_id}"
    entry = _CACHE.pop(key, None)
    if entry is None:
        return None
    text, ts = entry
    if time.monotonic() - ts > _CACHE_TTL:
        return None
    return text or None


def has_pending(platform: str, sender_id: str) -> bool:
    """Return True if there is a non-expired enrichment in cache."""
    key = f"{platform}:{sender_id}"
    entry = _CACHE.get(key)
    if entry is None:
        return False
    _, ts = entry
    return time.monotonic() - ts <= _CACHE_TTL


def clear_cache_for_tests() -> None:
    """Test helper: wipe the in-memory cache."""
    _CACHE.clear()


# ---------------------------------------------------------------------------
# Internal extraction
# ---------------------------------------------------------------------------


def _extract_one(path: str, mime: str, mode: str) -> str | None:
    p = Path(path)
    ext = p.suffix.lower()
    is_image = ext in _IMAGE_EXTS or (mime or "").startswith("image/")
    is_pdf = ext in _PDF_EXTS or "pdf" in (mime or "")

    if is_pdf and mode in ("pdftotext", "both"):
        return _run_pdftotext(path)
    if is_image and mode in ("tesseract", "both"):
        return _run_tesseract(path)
    return None


def _run_tesseract(path: str) -> str | None:
    """Run tesseract OCR on an image. Returns None if not installed or empty."""
    try:
        result = subprocess.run(
            ["tesseract", path, "stdout", "-l", "chi_sim+eng"],
            capture_output=True, text=True, timeout=30,
        )
        text = (result.stdout or "").strip()
        return text if text else None
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _run_pdftotext(path: str) -> str | None:
    """Run pdftotext on a PDF. Returns None if not installed or empty."""
    try:
        result = subprocess.run(
            ["pdftotext", path, "-"],
            capture_output=True, text=True, timeout=30,
        )
        text = (result.stdout or "").strip()
        return text if text else None
    except FileNotFoundError:
        return None
    except Exception:
        return None
