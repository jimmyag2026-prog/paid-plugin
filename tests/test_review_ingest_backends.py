"""Tests for paid_review.ingest_backends (v1.5 Phase 2).

Covers:
  - BackendResult dataclass + ok property
  - IngestBackend ABC: can_handle_* defaults + raises on un-overridden ingest
  - TextBackend file + string ingest, truncation note
  - LarkDocBackend URL extraction (doc / wiki / unsupported)
  - LarkDocBackend doc happy path
  - LarkDocBackend wiki → docx chain
  - LarkDocBackend wiki → non-docx (sheet/bitable) note
  - LarkDocBackend LarkClient failure → captured in errors (not raised)
  - ingest dispatcher: URL routing, attachment routing, partial failure
  - Source rendering (# Source: header for non-message sources)
  - Truncation marker rendering in normalized_text
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from paid_review import ingest as ingest_mod
from paid_review.ingest_backends import (
    BackendResult,
    IngestBackend,
    LarkDocBackend,
    TextBackend,
)
from paid_review.ingest_backends.base import truncate_with_note
from paid_review.ingest_backends.lark_doc import extract_lark_resource


# ---------------------------------------------------------------------------
# BackendResult
# ---------------------------------------------------------------------------


def test_backend_result_ok_property():
    assert BackendResult(normalized="hi", backend="test").ok is True
    assert BackendResult(normalized="", backend="test").ok is False
    assert BackendResult(normalized="   ", backend="test").ok is False


def test_backend_result_default_fields():
    r = BackendResult(normalized="x", backend="test")
    assert r.source == ""
    assert r.note == ""
    assert r.errors == []


# ---------------------------------------------------------------------------
# IngestBackend ABC defaults
# ---------------------------------------------------------------------------


def test_abstract_backend_cannot_handle_anything_by_default():
    class _Bare(IngestBackend):
        name = "bare"

    b = _Bare()
    assert b.can_handle_file(mime="text/plain", ext=".txt") is False
    assert b.can_handle_url("https://example.com") is False


def test_abstract_backend_ingest_methods_raise():
    class _Bare(IngestBackend):
        name = "bare"

    b = _Bare()
    with pytest.raises(NotImplementedError):
        b.ingest_file(Path("/tmp/x"))
    with pytest.raises(NotImplementedError):
        b.ingest_url("https://example.com")


# ---------------------------------------------------------------------------
# truncate_with_note
# ---------------------------------------------------------------------------


def test_truncate_no_truncation_when_under_cap():
    text = "x" * 100
    out, note = truncate_with_note(text, max_bytes=1000)
    assert out == text
    assert note == ""


def test_truncate_produces_note_with_sizes():
    text = "y" * 5000
    out, note = truncate_with_note(text, max_bytes=1024)
    assert len(out.encode("utf-8")) <= 1024
    assert "truncated to" in note
    assert "1.0KB" in note  # capped size
    assert "4.9KB" in note or "5.0KB" in note  # original-ish size


# ---------------------------------------------------------------------------
# TextBackend
# ---------------------------------------------------------------------------


def test_text_backend_can_handle_textlike_ext():
    b = TextBackend()
    for ext in (".txt", ".md", ".markdown", ".csv", ".json", ".log", ".rst"):
        assert b.can_handle_file(mime="", ext=ext)


def test_text_backend_handles_text_mime():
    b = TextBackend()
    assert b.can_handle_file(mime="text/plain", ext="")
    assert b.can_handle_file(mime="text/markdown", ext="")


def test_text_backend_rejects_binary():
    b = TextBackend()
    assert not b.can_handle_file(mime="application/pdf", ext=".pdf")
    assert not b.can_handle_file(mime="image/png", ext=".png")


def test_text_backend_ingest_file_happy(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("body content", encoding="utf-8")
    r = TextBackend().ingest_file(p)
    assert r.ok
    assert r.normalized == "body content"
    assert r.source == str(p)
    assert r.note == ""


def test_text_backend_ingest_file_truncates_large():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "huge.txt"
        p.write_bytes(b"a" * (300 * 1024))
        r = TextBackend().ingest_file(p)
        assert r.ok
        assert "truncated to" in r.note
        assert "300.0KB" in r.note


def test_text_backend_ingest_file_unreadable(tmp_path):
    p = tmp_path / "nonexistent.txt"
    r = TextBackend().ingest_file(p)
    assert not r.ok
    assert r.errors


def test_text_backend_ingest_string_empty():
    r = TextBackend().ingest_string("")
    assert not r.ok
    assert r.errors == []


def test_text_backend_ingest_string_strips():
    r = TextBackend().ingest_string("  hi there  \n", label="msg")
    assert r.normalized == "hi there"
    assert r.source == "msg"


# ---------------------------------------------------------------------------
# LarkDocBackend — URL extraction
# ---------------------------------------------------------------------------


def test_extract_lark_doc_url():
    assert extract_lark_resource(
        "https://example.feishu.cn/docx/ABCDEF123456"
    ) == ("doc", "ABCDEF123456")
    assert extract_lark_resource(
        "https://xxx.larksuite.com/docx/XyZ"
    ) == ("doc", "XyZ")


def test_extract_lark_wiki_url():
    assert extract_lark_resource(
        "https://example.feishu.cn/wiki/W123abc"
    ) == ("wiki", "W123abc")
    assert extract_lark_resource(
        "https://x.jp.larksuite.com/wiki/LUfZwGot5if"
    ) == ("wiki", "LUfZwGot5if")


def test_extract_lark_rejects_non_lark():
    assert extract_lark_resource("https://notion.so/page") is None
    assert extract_lark_resource("https://example.com/wiki/x") is None
    assert extract_lark_resource("https://feishu.cn/base/X") is None  # bitable not in T1
    assert extract_lark_resource("") is None
    assert extract_lark_resource(None) is None


# ---------------------------------------------------------------------------
# LarkDocBackend — happy paths
# ---------------------------------------------------------------------------


def test_lark_doc_backend_doc_happy():
    lark = MagicMock()
    lark.get_doc_raw.return_value = "Q3 budget proposal\n240k total"
    b = LarkDocBackend(lark)
    r = b.ingest_url("https://example.feishu.cn/docx/DOCABC")
    assert r.ok
    assert r.backend == "lark_doc"
    assert "Q3 budget proposal" in r.normalized
    assert r.source.endswith("docx/DOCABC")
    lark.get_doc_raw.assert_called_once_with("DOCABC")


def test_lark_doc_backend_wiki_to_docx_chain():
    lark = MagicMock()
    lark.get_wiki_node.return_value = {
        "obj_type": "docx",
        "obj_token": "doc_inner",
        "title": "Q3 Planning",
    }
    lark.get_doc_raw.return_value = "wiki body content"
    b = LarkDocBackend(lark)
    r = b.ingest_url("https://x.feishu.cn/wiki/W001")
    assert r.ok
    assert "wiki body content" in r.normalized
    assert "Q3 Planning" in r.normalized  # title prefix
    assert "wiki → docx doc_inner" in r.note
    lark.get_wiki_node.assert_called_once_with("W001")
    lark.get_doc_raw.assert_called_once_with("doc_inner")


def test_lark_doc_backend_wiki_non_docx():
    """Wiki node pointing to a sheet/bitable — show note, no chain."""
    lark = MagicMock()
    lark.get_wiki_node.return_value = {
        "obj_type": "sheet",
        "obj_token": "sheet_xyz",
        "title": "Roadmap Sheet",
    }
    b = LarkDocBackend(lark)
    r = b.ingest_url("https://x.feishu.cn/wiki/W002")
    assert r.ok  # has the title + note even if not the data
    assert r.errors == []  # sheet is "known unsupported", not an error
    assert "Roadmap Sheet" in r.normalized
    assert "obj_type=sheet" in r.normalized or "sheet" in r.note
    lark.get_doc_raw.assert_not_called()


def test_lark_doc_backend_wiki_empty_response():
    lark = MagicMock()
    lark.get_wiki_node.return_value = {}
    b = LarkDocBackend(lark)
    r = b.ingest_url("https://x.feishu.cn/wiki/W003")
    assert not r.ok
    assert r.errors


# ---------------------------------------------------------------------------
# LarkDocBackend — error paths
# ---------------------------------------------------------------------------


def test_lark_doc_backend_get_doc_raw_failure_recorded():
    lark = MagicMock()
    lark.get_doc_raw.side_effect = RuntimeError("Lark 403 no permission")
    b = LarkDocBackend(lark)
    r = b.ingest_url("https://x.feishu.cn/docx/D404")
    assert not r.ok
    assert r.errors
    assert "Lark Doc fetch failed" in r.errors[0]


def test_lark_doc_backend_wiki_chain_doc_failure():
    lark = MagicMock()
    lark.get_wiki_node.return_value = {
        "obj_type": "docx",
        "obj_token": "doc_x",
        "title": "Plan",
    }
    lark.get_doc_raw.side_effect = RuntimeError("doc 403")
    b = LarkDocBackend(lark)
    r = b.ingest_url("https://x.feishu.cn/wiki/W005")
    assert not r.ok
    assert r.errors
    assert "doc 403" in r.errors[0]


def test_lark_doc_backend_can_handle_url():
    lark = MagicMock()
    b = LarkDocBackend(lark)
    assert b.can_handle_url("https://x.feishu.cn/docx/ABC")
    assert b.can_handle_url("https://x.larksuite.com/wiki/W")
    assert not b.can_handle_url("https://notion.so/p")
    assert not b.can_handle_url("not a url")


def test_lark_doc_backend_not_a_lark_url():
    lark = MagicMock()
    b = LarkDocBackend(lark)
    r = b.ingest_url("https://notion.so/page")
    assert not r.ok
    assert r.errors
    lark.get_doc_raw.assert_not_called()


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def test_dispatcher_lark_doc_url_routed(tmp_path):
    lark = MagicMock()
    lark.get_doc_raw.return_value = "doc content here"
    sid_dir = tmp_path / "sid_url"
    out = ingest_mod.ingest(
        "please review https://x.feishu.cn/docx/ABC123 thoughts?",
        [],
        sid_dir,
        lark_client=lark,
    )
    assert out.ok
    # Doc content fetched + included
    assert "doc content here" in out.normalized_text
    # URL appears in source header but NOT in the user message section
    lines = out.normalized_text.split("\n")
    message_section = [l for l in lines if "please review" in l]
    assert message_section
    assert "https://" not in message_section[0]  # URL stripped from leftover text
    # Source audit recorded
    assert any(s["backend"] == "lark_doc" for s in out.sources)
    # The leftover human text also retained
    assert "please review" in out.normalized_text or "thoughts" in out.normalized_text


def test_dispatcher_no_lark_client_skips_url_backend(tmp_path):
    sid_dir = tmp_path / "sid_nolark"
    out = ingest_mod.ingest(
        "review https://x.feishu.cn/docx/ABC",
        [],
        sid_dir,
        lark_client=None,
    )
    # URL stays as part of normalized text (no backend claimed it)
    assert "https://x.feishu.cn/docx/ABC" in out.normalized_text


def test_dispatcher_lark_url_failure_partial_success(tmp_path):
    """Lark API errors are surfaced in result.errors but text still
    flows through. Per doc 09 §5.5."""
    lark = MagicMock()
    lark.get_doc_raw.side_effect = RuntimeError("403")
    sid_dir = tmp_path / "sid_pf"
    out = ingest_mod.ingest(
        "context text here. see https://x.feishu.cn/docx/ABC for details.",
        [],
        sid_dir,
        lark_client=lark,
    )
    # Lark URL failed but the surrounding text still in normalized
    assert "context text here" in out.normalized_text
    # error recorded
    assert any("403" in e for e in out.errors)


def test_dispatcher_sources_record_per_backend(tmp_path):
    """IngestResult.sources captures each backend invocation for audit."""
    lark = MagicMock()
    lark.get_doc_raw.return_value = "x"
    sid_dir = tmp_path / "sid_srcs"
    out = ingest_mod.ingest(
        "hi https://x.feishu.cn/docx/A",
        [],
        sid_dir,
        lark_client=lark,
    )
    backends = [s["backend"] for s in out.sources]
    assert "lark_doc" in backends
    assert "text" in backends  # the leftover "hi" message


def test_dispatcher_truncation_note_renders_in_normalized(tmp_path):
    """Note from BackendResult should appear as _(truncated...)_ in body."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        big = Path(td) / "big.txt"
        big.write_bytes(b"z" * (260 * 1024))
        sid_dir = Path(td) / "sid_tx"
        out = ingest_mod.ingest("", [{"path": str(big), "name": "big.txt"}], sid_dir)
        assert "truncated to" in out.normalized_text
        # Italic markdown markers around the note
        assert "_(truncated to" in out.normalized_text


def test_dispatcher_unsupported_attachment_still_breadcrumbed(tmp_path):
    """Binary file with no backend → placeholder, file still archived.

    Use .docx (not in any Phase 3 backend) to test the placeholder
    fallback path. PDF is now handled by PdfBackend (Phase 3) so it
    wouldn't trigger the placeholder branch.
    """
    docx = tmp_path / "x.docx"
    docx.write_bytes(b"PK\x03\x04 docx-zip-header")
    sid_dir = tmp_path / "sid_docx"
    out = ingest_mod.ingest(
        "ask",
        [{"path": str(docx), "name": "x.docx",
          "mimetype": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}],
        sid_dir,
    )
    assert "[attachment: x.docx]" in out.normalized_text
    # File saved despite no backend
    assert (sid_dir / "input" / "x.docx").exists()
    # Source audit has a placeholder entry
    assert any(s["backend"] == "placeholder" for s in out.sources)
