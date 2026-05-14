"""Tests for paid_review.ingest_backends.image (v1.5 Phase 4).

Strategy:
  - All OCR tests mock pytesseract.image_to_string and PIL.Image.open.
  - No requirement to have tesseract CLI / pytesseract / Pillow actually
    installed in the test env — installation is a deploy concern,
    backend logic is tested via mocks.
  - Capability gate + graceful-degrade paths are exercised explicitly.
"""

from __future__ import annotations

import shutil
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from paid_review.ingest_backends import ImageBackend


# ---------------------------------------------------------------------------
# Capability gate
# ---------------------------------------------------------------------------


def test_image_backend_can_handle_common_image_exts():
    b = ImageBackend()
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".tif"):
        assert b.can_handle_file(mime="", ext=ext)
    # Case-insensitive
    assert b.can_handle_file(mime="", ext=".PNG")


def test_image_backend_can_handle_image_mime():
    b = ImageBackend()
    assert b.can_handle_file(mime="image/png", ext="")
    assert b.can_handle_file(mime="image/jpeg", ext="")
    assert b.can_handle_file(mime="image/webp", ext="")


def test_image_backend_rejects_non_image():
    b = ImageBackend()
    assert not b.can_handle_file(mime="application/pdf", ext=".pdf")
    assert not b.can_handle_file(mime="text/plain", ext=".txt")
    assert not b.can_handle_file(mime="audio/ogg", ext=".ogg")


# ---------------------------------------------------------------------------
# Missing dependency paths (deploy-time degrade)
# ---------------------------------------------------------------------------


def test_image_backend_missing_tesseract_returns_placeholder(tmp_path):
    b = ImageBackend()
    b._tesseract_path = None
    b._pytesseract_available = True
    b._pillow_available = True

    p = tmp_path / "x.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n fake")
    r = b.ingest_file(p)

    assert "[attachment: x.png]" in r.normalized
    assert any("tesseract CLI" in e for e in r.errors)
    assert not b.has_extractor


def test_image_backend_missing_pytesseract_returns_placeholder(tmp_path):
    b = ImageBackend()
    b._tesseract_path = "/usr/bin/tesseract"
    b._pytesseract_available = False
    b._pillow_available = True

    p = tmp_path / "x.png"
    p.write_bytes(b"\x89PNG")
    r = b.ingest_file(p)

    assert "[attachment: x.png]" in r.normalized
    assert any("pytesseract" in e for e in r.errors)


def test_image_backend_missing_pillow_returns_placeholder(tmp_path):
    b = ImageBackend()
    b._tesseract_path = "/usr/bin/tesseract"
    b._pytesseract_available = True
    b._pillow_available = False

    p = tmp_path / "x.png"
    p.write_bytes(b"\x89PNG")
    r = b.ingest_file(p)

    assert "[attachment: x.png]" in r.normalized
    assert any("Pillow" in e for e in r.errors)


def test_image_backend_missing_all_lists_each_dep(tmp_path):
    b = ImageBackend()
    b._tesseract_path = None
    b._pytesseract_available = False
    b._pillow_available = False

    p = tmp_path / "x.png"
    p.write_bytes(b"\x89PNG")
    r = b.ingest_file(p)

    text = " ".join(r.errors)
    assert "tesseract CLI" in text
    assert "pytesseract" in text
    assert "Pillow" in text


# ---------------------------------------------------------------------------
# OCR happy + edge paths (all-deps-present, mocked OCR)
# ---------------------------------------------------------------------------


def _fully_armed_backend() -> ImageBackend:
    b = ImageBackend()
    b._tesseract_path = "/usr/bin/tesseract"
    b._pytesseract_available = True
    b._pillow_available = True
    return b


def _mock_pil_image_ctx() -> MagicMock:
    """Return a MagicMock that supports `with Image.open(p) as img:`."""
    mock_img = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=mock_img)
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def _install_mocks(monkeypatch, *, ocr_return: str = "", ocr_side_effect=None):
    """Install fake pytesseract + PIL modules into sys.modules. Returns
    the mock pytesseract module so tests can assert on call args."""
    fake_pytesseract = MagicMock()
    if ocr_side_effect is not None:
        fake_pytesseract.image_to_string.side_effect = ocr_side_effect
    else:
        fake_pytesseract.image_to_string.return_value = ocr_return

    fake_pil = types.SimpleNamespace()
    fake_pil.Image = MagicMock()
    fake_pil.Image.open = MagicMock(return_value=_mock_pil_image_ctx())

    monkeypatch.setitem(sys.modules, "pytesseract", fake_pytesseract)
    monkeypatch.setitem(sys.modules, "PIL", fake_pil)
    return fake_pytesseract


def test_image_backend_ocr_happy_chinese_english(tmp_path, monkeypatch):
    fake_pyt = _install_mocks(monkeypatch, ocr_return="周五能早下班吗？\nHello world")
    b = _fully_armed_backend()
    p = tmp_path / "screenshot.png"
    p.write_bytes(b"\x89PNG")

    r = b.ingest_file(p)
    assert r.ok
    assert r.backend == "image"
    assert "周五能早下班吗" in r.normalized
    assert "Hello world" in r.normalized
    # Default langs passed to pytesseract
    call_kwargs = fake_pyt.image_to_string.call_args.kwargs
    assert call_kwargs.get("lang") == "chi_sim+eng"


def test_image_backend_respects_paid_ocr_langs_env(tmp_path, monkeypatch):
    fake_pyt = _install_mocks(monkeypatch, ocr_return="text")
    monkeypatch.setenv("PAID_OCR_LANGS", "jpn+eng")
    b = _fully_armed_backend()
    p = tmp_path / "x.png"
    p.write_bytes(b"\x89PNG")

    b.ingest_file(p)
    call_kwargs = fake_pyt.image_to_string.call_args.kwargs
    assert call_kwargs.get("lang") == "jpn+eng"


def test_image_backend_empty_ocr_output_advisory(tmp_path, monkeypatch):
    _install_mocks(monkeypatch, ocr_return="   \n  ")
    b = _fully_armed_backend()
    p = tmp_path / "blank.png"
    p.write_bytes(b"\x89PNG")

    r = b.ingest_file(p)
    assert "[attachment: blank.png]" in r.normalized
    assert any("0 chars" in e or "no readable" in e.lower() for e in r.errors)


def test_image_backend_ocr_timeout(tmp_path, monkeypatch):
    """pytesseract raises RuntimeError on timeout — must capture."""
    _install_mocks(monkeypatch, ocr_side_effect=RuntimeError("tesseract timeout"))
    b = _fully_armed_backend()
    p = tmp_path / "slow.png"
    p.write_bytes(b"\x89PNG")

    r = b.ingest_file(p)
    assert "[attachment: slow.png]" in r.normalized
    assert any("timeout" in e.lower() for e in r.errors)


def test_image_backend_pil_open_failure(tmp_path, monkeypatch):
    """Image.open raises on corrupt image → captured as crash error."""
    fake_pyt = MagicMock()
    fake_pil = types.SimpleNamespace()
    fake_pil.Image = MagicMock()
    fake_pil.Image.open = MagicMock(side_effect=OSError("not a png"))
    monkeypatch.setitem(sys.modules, "pytesseract", fake_pyt)
    monkeypatch.setitem(sys.modules, "PIL", fake_pil)

    b = _fully_armed_backend()
    p = tmp_path / "corrupt.png"
    p.write_bytes(b"garbage")
    r = b.ingest_file(p)
    assert "[attachment: corrupt.png]" in r.normalized
    assert any("crashed" in e.lower() for e in r.errors)


def test_image_backend_large_output_truncated(tmp_path, monkeypatch):
    """260KB+ extracted text → truncate with note."""
    big_text = "x" * (260 * 1024)
    _install_mocks(monkeypatch, ocr_return=big_text)
    b = _fully_armed_backend()
    p = tmp_path / "big.png"
    p.write_bytes(b"\x89PNG")

    r = b.ingest_file(p)
    assert r.ok
    assert "truncated to" in r.note
    assert "260.0KB" in r.note


# ---------------------------------------------------------------------------
# Dispatcher integration
# ---------------------------------------------------------------------------


def test_dispatcher_routes_image_to_image_backend(tmp_path, monkeypatch):
    """When all OCR deps mocked-present, dispatcher uses ImageBackend
    for .png attachments."""
    from paid_review import ingest as ingest_mod

    _install_mocks(monkeypatch, ocr_return="extracted from screenshot")

    # Force a fresh ImageBackend instance via the default list — we patch
    # its instance attributes so it thinks deps are present.
    real_init = ImageBackend.__init__

    def fake_init(self):
        real_init(self)
        self._tesseract_path = "/usr/bin/tesseract"
        self._pytesseract_available = True
        self._pillow_available = True

    monkeypatch.setattr(ImageBackend, "__init__", fake_init)

    img = tmp_path / "ss.png"
    img.write_bytes(b"\x89PNG fake")
    sid_dir = tmp_path / "sid_img"
    out = ingest_mod.ingest(
        "look at this",
        [{"path": str(img), "name": "ss.png", "mimetype": "image/png"}],
        sid_dir,
    )
    assert "extracted from screenshot" in out.normalized_text
    assert any(s["backend"] == "image" for s in out.sources)


def test_dispatcher_image_missing_deps_still_archives(tmp_path):
    """When OCR deps missing (real test env without tesseract), the
    image still gets copied to sid/input/ but normalized has the
    placeholder + advisory error."""
    from paid_review import ingest as ingest_mod

    img = tmp_path / "screenshot.png"
    img.write_bytes(b"\x89PNG fake")
    sid_dir = tmp_path / "sid_no_ocr"

    # Force ImageBackend to think deps are missing — pretend a fresh
    # install with no tesseract.
    import paid_review.ingest_backends.image as image_mod

    class _DegradedImageBackend(image_mod.ImageBackend):
        def __init__(self):
            super().__init__()
            self._tesseract_path = None
            self._pytesseract_available = False
            self._pillow_available = False

    # Build a custom backend list bypassing default_backends.
    from paid_review.ingest_backends import TextBackend

    out = ingest_mod.ingest(
        "image attached:",
        [{"path": str(img), "name": "screenshot.png", "mimetype": "image/png"}],
        sid_dir,
        backends=[TextBackend(), _DegradedImageBackend()],
    )
    assert "[attachment: screenshot.png]" in out.normalized_text
    assert (sid_dir / "input" / "screenshot.png").exists()
    assert any("OCR unavailable" in e for e in out.errors)
