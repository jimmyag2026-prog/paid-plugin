"""ImageBackend — OCR text from image attachments via tesseract.

v1.5 Phase 4 (T2.image). Activates on image/* mime + common image
extensions (.png/.jpg/.jpeg/.webp/.gif). Requires tesseract-ocr CLI
+ pytesseract + pillow at runtime; graceful-degrades to placeholder
when missing.

Default OCR language: ``chi_sim+eng`` (Chinese simplified + English),
matching JELabs pilot's bilingual context. Override via env var
``PAID_OCR_LANGS``.

OCR is best-effort; low-confidence images return placeholder + advisory
error so brief shows ⚠️ ingest_errors row (per doc 09 §5.5).
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from .base import BackendResult, IngestBackend, truncate_with_note

logger = logging.getLogger(__name__)


_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".tif"}
_IMAGE_MIME_PREFIX = "image/"

_OCR_TIMEOUT_SEC = 20


def _default_langs() -> str:
    """Languages passed to pytesseract.image_to_string(lang=...).

    Default targets JELabs bilingual case. Tesseract language packs
    must be installed for each (`apt install tesseract-ocr-chi-sim`
    on the VPS — see deploy notes in design 11 §6 Phase 4).
    """
    return (os.environ.get("PAID_OCR_LANGS") or "chi_sim+eng").strip()


class ImageBackend(IngestBackend):
    """Reads images and OCRs them to text.

    Probes pytesseract + PIL + tesseract CLI at construction time.
    When any piece is missing, backend stays "available" but every
    ingest returns placeholder + advisory error so dispatcher still
    archives the file and brief shows what was missing.
    """

    name = "image"

    def __init__(self):
        # Locate tesseract CLI — pytesseract itself looks at PATH by
        # default; we also check explicitly so we can report which
        # piece is missing in errors.
        self._tesseract_path = shutil.which("tesseract")
        try:
            import pytesseract  # type: ignore  # noqa: F401
            self._pytesseract_available = True
        except Exception:
            self._pytesseract_available = False
        try:
            from PIL import Image  # type: ignore  # noqa: F401
            self._pillow_available = True
        except Exception:
            self._pillow_available = False

    @property
    def has_extractor(self) -> bool:
        return (
            self._tesseract_path is not None
            and self._pytesseract_available
            and self._pillow_available
        )

    def can_handle_file(self, *, mime: str = "", ext: str = "") -> bool:
        ext_l = (ext or "").lower()
        mime_l = (mime or "").lower()
        return ext_l in _IMAGE_EXTS or mime_l.startswith(_IMAGE_MIME_PREFIX)

    def ingest_file(self, path: Path, *, mime: str = "") -> BackendResult:
        source = str(path)

        # Quick missing-piece reporting — owner sees exactly what to install.
        missing: list[str] = []
        if self._tesseract_path is None:
            missing.append("tesseract CLI (`apt install tesseract-ocr tesseract-ocr-chi-sim`)")
        if not self._pytesseract_available:
            missing.append("pytesseract (`pip install pytesseract`)")
        if not self._pillow_available:
            missing.append("Pillow (`pip install pillow`)")
        if missing:
            return BackendResult(
                normalized=f"[attachment: {path.name}]",
                backend=self.name,
                source=source,
                errors=[
                    "image OCR unavailable — missing: "
                    + ", ".join(missing)
                ],
            )

        # All deps present — actually run OCR.
        try:
            import pytesseract  # type: ignore
            from PIL import Image  # type: ignore
        except Exception as exc:
            # Race: probe succeeded but import failed (e.g. virtualenv
            # mutation mid-process). Treat as missing-piece.
            return BackendResult(
                normalized=f"[attachment: {path.name}]",
                backend=self.name,
                source=source,
                errors=[f"image OCR import race: {exc}"],
            )

        try:
            with Image.open(path) as img:
                text = pytesseract.image_to_string(
                    img,
                    lang=_default_langs(),
                    timeout=_OCR_TIMEOUT_SEC,
                )
        except RuntimeError as exc:
            # pytesseract raises RuntimeError on timeout.
            logger.warning("[image-backend] OCR timeout %s: %s", path, exc)
            return BackendResult(
                normalized=f"[attachment: {path.name}]",
                backend=self.name,
                source=source,
                errors=[f"tesseract OCR timeout/runtime error: {exc}"],
            )
        except Exception as exc:
            logger.warning("[image-backend] OCR crashed %s: %s", path, exc)
            return BackendResult(
                normalized=f"[attachment: {path.name}]",
                backend=self.name,
                source=source,
                errors=[f"image OCR crashed: {exc}"],
            )

        if not text or not text.strip():
            return BackendResult(
                normalized=f"[attachment: {path.name}]",
                backend=self.name,
                source=source,
                errors=[
                    f"tesseract extracted 0 chars (langs={_default_langs()}) — "
                    "image may have no readable text, contrast too low, OR "
                    "the language pack is wrong. Override via env: "
                    "PAID_OCR_LANGS=eng (English only), jpn+eng, kor+eng, "
                    "fra+eng, etc. Install lang packs with "
                    "`apt install tesseract-ocr-<lang>`. "
                    "(Vision-LLM fallback planned for v1.6.)"
                ],
            )

        truncated, note = truncate_with_note(text.strip())
        return BackendResult(
            normalized=truncated,
            backend=self.name,
            source=source,
            note=note,
        )
