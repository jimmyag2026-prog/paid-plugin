"""v1.5 build_summary ingest_audit rendering tests.

Verifies the ⚠️ ingest_errors header + ## Sources footer per doc 09 §5.5.
build_summary's main 6-section body is covered by existing tests
(test_review_summary*.py); these tests only exercise the new attach-audit
shim.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from paid_review.core.build_summary import _attach_ingest_audit


def test_attach_no_sources_no_errors_returns_brief_unchanged():
    brief = "# 1. Decision\n\napprove"
    out = _attach_ingest_audit(brief, [], [])
    assert out == brief


def test_attach_errors_prepends_warning_block():
    brief = "# 1. Decision\n\napprove"
    errors = [
        "[lark_doc:https://x.feishu.cn/docx/A] Lark 403 no permission",
        "[pdf:doc.pdf] pdftotext crashed: bad file header",
    ]
    out = _attach_ingest_audit(brief, [], errors)
    assert out.startswith("⚠️")
    assert "Ingest errors (2)" in out
    assert "Lark 403" in out
    assert "pdftotext crashed" in out
    # original brief still present after the warning block
    assert "# 1. Decision" in out
    assert "approve" in out


def test_attach_sources_appends_footer():
    brief = "# 1. Decision\n\napprove"
    sources = [
        {"backend": "text", "source": "message", "note": ""},
        {"backend": "lark_doc", "source": "https://x.feishu.cn/docx/A", "note": ""},
        {"backend": "text", "source": "/tmp/inputs/notes.md", "note": "truncated to 256.0KB from 1.2MB"},
    ]
    out = _attach_ingest_audit(brief, sources, [])
    # ## Sources footer appears
    assert "## Sources" in out
    # All real sources rendered as bullet list
    assert "`message` via `text`" in out
    assert "`https://x.feishu.cn/docx/A` via `lark_doc`" in out
    assert "/tmp/inputs/notes.md" in out
    assert "truncated to 256.0KB" in out


def test_attach_filters_empty_source_strings():
    brief = "# brief"
    sources = [
        {"backend": "text", "source": "", "note": ""},   # no source — skipped
        {"backend": "lark_doc", "source": "url1", "note": ""},
    ]
    out = _attach_ingest_audit(brief, sources, [])
    assert "## Sources" in out
    # Only the one with a real source rendered
    assert "url1" in out
    assert out.count("via") == 1


def test_attach_both_errors_and_sources():
    brief = "## verdict\n\nready"
    errors = ["[pdf:x.pdf] crashed"]
    sources = [{"backend": "lark_doc", "source": "U1", "note": ""}]
    out = _attach_ingest_audit(brief, sources, errors)
    # ⚠️ at top, brief in middle, ## Sources at bottom
    assert out.index("⚠️") < out.index("## verdict")
    assert out.index("## verdict") < out.index("## Sources")
