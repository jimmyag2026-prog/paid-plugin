"""Tests for paid_review.ingest_backends.pdf (v1.5 Phase 3).

Strategy:
  - Use a tiny real PDF fixture (created in-test via minimal raw bytes)
    when possible — full real pdftotext invocation.
  - Mock subprocess.run for predictable extractor-failure paths
    (timeout, non-zero exit, decode error).
  - Skip the "real pdftotext on disk" tests if the binary isn't on the
    test machine ($PATH).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from paid_review.ingest_backends import PdfBackend
from paid_review.ingest_backends.pdf import _PDFTOTEXT_TIMEOUT_SEC


_HAS_PDFTOTEXT = shutil.which("pdftotext") is not None


# ---------------------------------------------------------------------------
# Capability gate
# ---------------------------------------------------------------------------


def test_pdf_backend_can_handle_pdf_ext():
    b = PdfBackend()
    assert b.can_handle_file(mime="", ext=".pdf")
    assert b.can_handle_file(mime="", ext=".PDF")


def test_pdf_backend_can_handle_pdf_mime():
    b = PdfBackend()
    assert b.can_handle_file(mime="application/pdf", ext="")


def test_pdf_backend_rejects_non_pdf():
    b = PdfBackend()
    assert not b.can_handle_file(mime="text/plain", ext=".txt")
    assert not b.can_handle_file(mime="image/png", ext=".png")


# ---------------------------------------------------------------------------
# No-extractor path (graceful degrade)
# ---------------------------------------------------------------------------


def test_pdf_backend_no_extractor_returns_placeholder(tmp_path):
    """When neither pdftotext nor pdfminer are available, backend
    returns a breadcrumb placeholder + advisory error so dispatcher
    archives the file but owner brief shows ⚠️ Ingest errors row."""
    b = PdfBackend()
    # Force both extractors unavailable
    b._pdftotext_path = None
    b._pdfminer_available = False

    p = tmp_path / "test.pdf"
    p.write_bytes(b"%PDF-1.4\nfake")
    r = b.ingest_file(p)

    # Breadcrumb retains (so dispatcher keeps the file in audit) — ok=True
    # is fine because the placeholder IS meaningful content for the brief.
    assert "[attachment: test.pdf]" in r.normalized
    assert any("no PDF extractor available" in e for e in r.errors)
    # has_extractor property reflects state
    assert not b.has_extractor


# ---------------------------------------------------------------------------
# Real pdftotext path (only when binary available)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_PDFTOTEXT, reason="pdftotext not on PATH")
def test_pdf_backend_extracts_text_from_real_pdf(tmp_path):
    """Build a minimal PDF on disk using a tiny known-good byte sequence,
    then verify the backend extracts its content via real pdftotext."""
    # Minimal PDF with "Hello PAID" text — known-good per PDF 1.4 spec.
    pdf_bytes = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
        b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
        b"4 0 obj<</Length 44>>stream\n"
        b"BT /F1 12 Tf 50 700 Td (Hello PAID) Tj ET\n"
        b"endstream endobj\n"
        b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
        b"xref\n0 6\n"
        b"0000000000 65535 f\n"
        b"0000000009 00000 n\n"
        b"0000000055 00000 n\n"
        b"0000000101 00000 n\n"
        b"0000000206 00000 n\n"
        b"0000000292 00000 n\n"
        b"trailer<</Size 6/Root 1 0 R>>\n"
        b"startxref\n352\n%%EOF\n"
    )
    p = tmp_path / "hello.pdf"
    p.write_bytes(pdf_bytes)

    b = PdfBackend()
    r = b.ingest_file(p)
    assert r.ok, f"extraction failed: {r.errors}"
    assert "Hello PAID" in r.normalized


# ---------------------------------------------------------------------------
# Mocked subprocess paths — timeouts / failures
# ---------------------------------------------------------------------------


def test_pdf_backend_pdftotext_timeout(tmp_path, monkeypatch):
    b = PdfBackend()
    if not b._pdftotext_path:
        pytest.skip("test requires pdftotext on PATH to set up the right code path")

    p = tmp_path / "stuck.pdf"
    p.write_bytes(b"%PDF-1.4 stuck")

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=_PDFTOTEXT_TIMEOUT_SEC)

    monkeypatch.setattr("paid_review.ingest_backends.pdf.subprocess.run", fake_run)
    r = b.ingest_file(p)
    # Has placeholder + timeout error
    assert "[attachment: stuck.pdf]" in r.normalized
    assert any("timeout" in e.lower() for e in r.errors)


def test_pdf_backend_pdftotext_nonzero_exit(tmp_path, monkeypatch):
    b = PdfBackend()
    if not b._pdftotext_path:
        pytest.skip("test requires pdftotext on PATH")

    p = tmp_path / "corrupt.pdf"
    p.write_bytes(b"not really a pdf")

    fake_result = MagicMock()
    fake_result.returncode = 1
    fake_result.stdout = b""
    fake_result.stderr = b"Couldn't open file"
    monkeypatch.setattr(
        "paid_review.ingest_backends.pdf.subprocess.run",
        lambda *a, **kw: fake_result,
    )

    r = b.ingest_file(p)
    assert "[attachment: corrupt.pdf]" in r.normalized
    assert any("pdftotext exit" in e for e in r.errors)
    assert any("Couldn't open" in e for e in r.errors)


def test_pdf_backend_pdftotext_empty_output_advisory(tmp_path, monkeypatch):
    """0-byte stdout → likely scanned PDF (no text layer). Surface as
    advisory error so owner brief shows ⚠️ Ingest errors row."""
    b = PdfBackend()
    if not b._pdftotext_path:
        pytest.skip("test requires pdftotext on PATH")

    p = tmp_path / "scanned.pdf"
    p.write_bytes(b"%PDF-1.4 scanned")

    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = b"   \n  \n"  # whitespace-only
    fake_result.stderr = b""
    monkeypatch.setattr(
        "paid_review.ingest_backends.pdf.subprocess.run",
        lambda *a, **kw: fake_result,
    )

    r = b.ingest_file(p)
    assert "[attachment: scanned.pdf]" in r.normalized
    assert any("image-based" in e.lower() or "0 chars" in e for e in r.errors)


def test_pdf_backend_large_output_truncated(tmp_path, monkeypatch):
    b = PdfBackend()
    if not b._pdftotext_path:
        pytest.skip("test requires pdftotext on PATH")

    p = tmp_path / "big.pdf"
    p.write_bytes(b"%PDF-1.4 big")

    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = b"x" * (300 * 1024)  # 300KB > 256KB cap
    fake_result.stderr = b""
    monkeypatch.setattr(
        "paid_review.ingest_backends.pdf.subprocess.run",
        lambda *a, **kw: fake_result,
    )

    r = b.ingest_file(p)
    assert r.ok
    assert "truncated to" in r.note
    assert "300.0KB" in r.note


# ---------------------------------------------------------------------------
# pdfminer fallback path
# ---------------------------------------------------------------------------


def test_pdf_backend_pdfminer_fallback_when_cli_missing(tmp_path, monkeypatch):
    """When pdftotext isn't on PATH but pdfminer is installed, fall back."""
    fake_pdfminer = MagicMock()
    fake_pdfminer.extract_text.return_value = "pdfminer extracted text"

    # Construct a PdfBackend that thinks CLI is missing but pdfminer works
    b = PdfBackend()
    b._pdftotext_path = None
    b._pdfminer_available = True

    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF-1.4")

    with patch.dict(
        sys.modules,
        {"pdfminer": fake_pdfminer, "pdfminer.high_level": fake_pdfminer},
    ):
        r = b.ingest_file(p)

    assert r.ok
    assert "pdfminer extracted text" in r.normalized


def test_pdf_backend_pdfminer_crash_handled(tmp_path):
    b = PdfBackend()
    b._pdftotext_path = None
    b._pdfminer_available = True

    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF-1.4")

    fake_pdfminer = MagicMock()
    fake_pdfminer.extract_text.side_effect = RuntimeError("malformed PDF")

    with patch.dict(
        sys.modules,
        {"pdfminer": fake_pdfminer, "pdfminer.high_level": fake_pdfminer},
    ):
        r = b.ingest_file(p)

    assert "[attachment: x.pdf]" in r.normalized
    assert any("pdfminer crashed" in e for e in r.errors)


# ---------------------------------------------------------------------------
# Dispatcher integration
# ---------------------------------------------------------------------------


def test_dispatcher_routes_pdf_to_pdf_backend(tmp_path):
    """Confirm ingest dispatcher picks PdfBackend over placeholder for .pdf."""
    from paid_review import ingest as ingest_mod

    if not _HAS_PDFTOTEXT:
        pytest.skip("pdftotext required for this integration test")

    pdf_bytes = (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
        b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
        b"4 0 obj<</Length 46>>stream\n"
        b"BT /F1 12 Tf 50 700 Td (Dispatch Test) Tj ET\n"
        b"endstream endobj\n"
        b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
        b"xref\n0 6\n0000000000 65535 f\n0000000009 00000 n\n"
        b"0000000055 00000 n\n0000000101 00000 n\n0000000206 00000 n\n"
        b"0000000294 00000 n\ntrailer<</Size 6/Root 1 0 R>>\n"
        b"startxref\n354\n%%EOF\n"
    )
    src = tmp_path / "doc.pdf"
    src.write_bytes(pdf_bytes)
    sid_dir = tmp_path / "sid_pdf"
    out = ingest_mod.ingest(
        "review this",
        [{"path": str(src), "name": "doc.pdf", "mimetype": "application/pdf"}],
        sid_dir,
    )
    assert "Dispatch Test" in out.normalized_text
    # Source audit shows pdf backend used
    assert any(s["backend"] == "pdf" for s in out.sources)
