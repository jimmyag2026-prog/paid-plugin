"""Tests for v1.6.5 non-review media enrichment (paid/media_enrichment.py)."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from paid import media_enrichment as me


@pytest.fixture(autouse=True)
def _clear_cache():
    me.clear_cache_for_tests()
    yield
    me.clear_cache_for_tests()


# ---------------------------------------------------------------------------
# pop_enriched_text / has_pending / cache basics
# ---------------------------------------------------------------------------


def test_pop_returns_none_when_empty():
    assert me.pop_enriched_text("feishu", "ou_x") is None


def test_has_pending_false_when_empty():
    assert me.has_pending("feishu", "ou_x") is False


def test_enrich_then_pop():
    """If extraction succeeds, pop returns the text and clears the cache."""
    with patch.object(me, "_run_tesseract", return_value="hello world"):
        me.enrich_media_for_cp("feishu", "ou_x", ["/tmp/img.png"], ["image/png"], "tesseract")

    text = me.pop_enriched_text("feishu", "ou_x")
    assert text == "hello world"
    # Cache cleared after pop
    assert me.pop_enriched_text("feishu", "ou_x") is None


def test_has_pending_true_after_enrich():
    with patch.object(me, "_run_tesseract", return_value="some text"):
        me.enrich_media_for_cp("feishu", "ou_a", ["/tmp/a.png"], ["image/png"], "tesseract")
    assert me.has_pending("feishu", "ou_a")


def test_pop_clears_has_pending():
    with patch.object(me, "_run_tesseract", return_value="x"):
        me.enrich_media_for_cp("feishu", "ou_b", ["/tmp/b.png"], [], "tesseract")
    me.pop_enriched_text("feishu", "ou_b")
    assert not me.has_pending("feishu", "ou_b")


def test_cache_keyed_by_platform_and_sender():
    with patch.object(me, "_run_tesseract", return_value="from_a"):
        me.enrich_media_for_cp("feishu", "ou_a", ["/tmp/a.png"], [], "tesseract")
    with patch.object(me, "_run_tesseract", return_value="from_b"):
        me.enrich_media_for_cp("feishu", "ou_b", ["/tmp/b.png"], [], "tesseract")

    assert me.pop_enriched_text("feishu", "ou_a") == "from_a"
    assert me.pop_enriched_text("feishu", "ou_b") == "from_b"


def test_expired_entry_returns_none(monkeypatch):
    """TTL enforcement: pop returns None if entry is older than _CACHE_TTL."""
    # Directly insert a stale entry
    me._CACHE["feishu:ou_old"] = ("stale text", time.monotonic() - me._CACHE_TTL - 1)
    assert me.pop_enriched_text("feishu", "ou_old") is None


def test_has_pending_false_when_expired(monkeypatch):
    me._CACHE["feishu:ou_exp"] = ("x", time.monotonic() - me._CACHE_TTL - 1)
    assert not me.has_pending("feishu", "ou_exp")


# ---------------------------------------------------------------------------
# enrich_media_for_cp — routing logic
# ---------------------------------------------------------------------------


def test_no_cache_if_extraction_returns_none():
    """If all extractions return None (e.g. tesseract not installed), no cache entry."""
    with patch.object(me, "_run_tesseract", return_value=None):
        me.enrich_media_for_cp("feishu", "ou_x", ["/tmp/img.png"], [], "tesseract")
    assert me.pop_enriched_text("feishu", "ou_x") is None


def test_mode_off_never_caches():
    """mode=off: no enrichment regardless of file type."""
    with patch.object(me, "_run_tesseract", side_effect=AssertionError("should not call")):
        me.enrich_media_for_cp("feishu", "ou_x", ["/tmp/img.png"], [], "off")
    assert me.pop_enriched_text("feishu", "ou_x") is None


def test_tesseract_mode_skips_pdf():
    """mode=tesseract should NOT call pdftotext for a .pdf file."""
    calls = []
    with patch.object(me, "_run_pdftotext", side_effect=lambda p: calls.append(p) or None):
        with patch.object(me, "_run_tesseract", return_value=None):
            me.enrich_media_for_cp("feishu", "ou_x", ["/tmp/doc.pdf"], ["application/pdf"], "tesseract")
    assert calls == []


def test_pdftotext_mode_skips_images():
    """mode=pdftotext should NOT call tesseract for an image file."""
    calls = []
    with patch.object(me, "_run_tesseract", side_effect=lambda p: calls.append(p) or None):
        with patch.object(me, "_run_pdftotext", return_value=None):
            me.enrich_media_for_cp("feishu", "ou_x", ["/tmp/img.png"], ["image/png"], "pdftotext")
    assert calls == []


def test_both_mode_tesseract_for_image():
    with patch.object(me, "_run_tesseract", return_value="img text") as mock_t:
        with patch.object(me, "_run_pdftotext", return_value=None):
            me.enrich_media_for_cp("feishu", "ou_x", ["/tmp/img.png"], ["image/png"], "both")
    mock_t.assert_called_once()
    assert me.pop_enriched_text("feishu", "ou_x") == "img text"


def test_both_mode_pdftotext_for_pdf():
    with patch.object(me, "_run_pdftotext", return_value="pdf text") as mock_p:
        with patch.object(me, "_run_tesseract", return_value=None):
            me.enrich_media_for_cp("feishu", "ou_x", ["/tmp/doc.pdf"], ["application/pdf"], "both")
    mock_p.assert_called_once()
    assert me.pop_enriched_text("feishu", "ou_x") == "pdf text"


def test_multiple_files_combined():
    """Multiple files → texts joined with double newline."""
    with patch.object(me, "_run_tesseract", side_effect=["page 1", "page 2"]):
        me.enrich_media_for_cp(
            "feishu", "ou_x",
            ["/tmp/a.png", "/tmp/b.png"], ["image/png", "image/png"],
            "tesseract",
        )
    text = me.pop_enriched_text("feishu", "ou_x")
    assert "page 1" in text
    assert "page 2" in text


def test_empty_media_url_skipped():
    """Empty path entries are skipped without error."""
    with patch.object(me, "_run_tesseract", return_value="ok") as mock_t:
        me.enrich_media_for_cp("feishu", "ou_x", ["", "/tmp/img.png"], [], "tesseract")
    assert mock_t.call_count == 1  # only called for the non-empty path


def test_max_chars_truncated():
    """Combined text capped at _MAX_OCR_CHARS."""
    long_text = "x" * (me._MAX_OCR_CHARS + 500)
    with patch.object(me, "_run_tesseract", return_value=long_text):
        me.enrich_media_for_cp("feishu", "ou_x", ["/tmp/img.png"], [], "tesseract")
    text = me.pop_enriched_text("feishu", "ou_x")
    assert len(text) == me._MAX_OCR_CHARS


# ---------------------------------------------------------------------------
# _run_tesseract / _run_pdftotext — graceful degradation
# ---------------------------------------------------------------------------


def test_tesseract_not_installed_returns_none():
    """FileNotFoundError (tesseract not installed) → returns None, no crash."""
    import subprocess
    with patch("subprocess.run", side_effect=FileNotFoundError):
        result = me._run_tesseract("/tmp/fake.png")
    assert result is None


def test_pdftotext_not_installed_returns_none():
    import subprocess
    with patch("subprocess.run", side_effect=FileNotFoundError):
        result = me._run_pdftotext("/tmp/fake.pdf")
    assert result is None


def test_tesseract_empty_output_returns_none():
    import subprocess
    mock_result = type("R", (), {"stdout": "   \n  ", "returncode": 0})()
    with patch("subprocess.run", return_value=mock_result):
        result = me._run_tesseract("/tmp/blank.png")
    assert result is None


# ---------------------------------------------------------------------------
# settings.media_enrichment_mode
# ---------------------------------------------------------------------------


def test_settings_default_is_off(tmp_path, monkeypatch):
    from paid import storage, settings
    monkeypatch.setattr(storage, "PAID_DIR", tmp_path)
    assert settings.media_enrichment_mode() == "off"


def test_settings_reads_tesseract(tmp_path, monkeypatch):
    import json
    from paid import storage, settings
    monkeypatch.setattr(storage, "PAID_DIR", tmp_path)
    (tmp_path / "settings.json").write_text(
        json.dumps({"media_enrichment_mode": "tesseract"})
    )
    assert settings.media_enrichment_mode() == "tesseract"


def test_settings_unknown_value_returns_off(tmp_path, monkeypatch):
    import json
    from paid import storage, settings
    monkeypatch.setattr(storage, "PAID_DIR", tmp_path)
    (tmp_path / "settings.json").write_text(
        json.dumps({"media_enrichment_mode": "vision"})  # not yet supported
    )
    assert settings.media_enrichment_mode() == "off"
