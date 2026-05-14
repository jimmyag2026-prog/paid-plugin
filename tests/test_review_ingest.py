"""Tests for paid_review.ingest (Sprint E)."""

from __future__ import annotations

from pathlib import Path

import pytest

from paid_review import ingest as ingest_mod


def test_ingest_text_only_no_attachments(tmp_path):
    sid_dir = tmp_path / "sid_a"
    out = ingest_mod.ingest("hello world", [], sid_dir)
    assert out.ok
    assert out.normalized_text == "hello world"
    assert out.saved_inputs == []
    assert out.errors == []


def test_ingest_empty_inputs_returns_not_ok(tmp_path):
    sid_dir = tmp_path / "sid_b"
    out = ingest_mod.ingest("", None, sid_dir)
    assert out.normalized_text == ""
    assert not out.ok


def test_ingest_text_file_attachment_inlined(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("file body content", encoding="utf-8")
    sid_dir = tmp_path / "sid_c"
    out = ingest_mod.ingest(
        "subject ask",
        [{"path": str(src), "name": "src.txt", "mimetype": "text/plain"}],
        sid_dir,
    )
    assert "subject ask" in out.normalized_text
    # v1.5: backend system renders "# Source: <path>" headers for file
    # backends (text backend on the "message" source keeps the v1.4
    # bare-text rendering).
    assert "# Source:" in out.normalized_text
    assert "src.txt" in out.normalized_text
    assert "file body content" in out.normalized_text
    assert (sid_dir / "input" / "src.txt").exists()
    assert out.saved_inputs and out.saved_inputs[0].name == "src.txt"


def test_ingest_markdown_extension_inlined(tmp_path):
    src = tmp_path / "doc.md"
    src.write_text("# heading\n\nbody", encoding="utf-8")
    sid_dir = tmp_path / "sid_md"
    out = ingest_mod.ingest("", [{"path": str(src), "name": "doc.md"}], sid_dir)
    assert "# heading" in out.normalized_text
    assert "body" in out.normalized_text


def test_ingest_binary_attachment_becomes_placeholder(tmp_path):
    src = tmp_path / "x.pdf"
    src.write_bytes(b"%PDF-1.4 binary blob")
    sid_dir = tmp_path / "sid_d"
    out = ingest_mod.ingest(
        "ask", [{"path": str(src), "name": "x.pdf",
                 "mimetype": "application/pdf"}],
        sid_dir,
    )
    assert "[attachment: x.pdf]" in out.normalized_text
    # Binary blob NOT inlined
    assert "%PDF-1.4 binary blob" not in out.normalized_text
    # But it IS saved to input/
    assert (sid_dir / "input" / "x.pdf").exists()


def test_ingest_inline_text_attachment_no_path(tmp_path):
    sid_dir = tmp_path / "sid_e"
    out = ingest_mod.ingest(
        "main",
        [{"name": "memo", "text": "an inline note"}],
        sid_dir,
    )
    assert "main" in out.normalized_text
    # v1.5: inline text attachment routed through TextBackend.ingest_string
    # → rendered with "# Source: memo" header (not the legacy ### header).
    assert "memo" in out.normalized_text
    assert "an inline note" in out.normalized_text
    # No file copied (no path given)
    assert not (sid_dir / "input").exists() or list((sid_dir / "input").iterdir()) == []


def test_ingest_unknown_attachment_breadcrumb(tmp_path):
    sid_dir = tmp_path / "sid_f"
    out = ingest_mod.ingest(
        "main",
        [{"name": "missing.docx"}],  # no path, no inline text
        sid_dir,
    )
    assert "[attachment: missing.docx]" in out.normalized_text


def test_ingest_missing_file_path_does_not_raise(tmp_path):
    sid_dir = tmp_path / "sid_g"
    out = ingest_mod.ingest(
        "main",
        [{"path": "/does/not/exist.txt", "name": "ghost.txt"}],
        sid_dir,
    )
    # Falls through to breadcrumb (no path → unknown)
    assert "main" in out.normalized_text
    assert "[attachment: ghost.txt]" in out.normalized_text
    # No file saved
    assert not (sid_dir / "input").exists() or list((sid_dir / "input").iterdir()) == []


def test_ingest_truncates_large_textfile(tmp_path):
    src = tmp_path / "huge.txt"
    src.write_bytes(b"x" * (300 * 1024))  # 300 KiB > 256 KiB cap
    sid_dir = tmp_path / "sid_h"
    out = ingest_mod.ingest("", [{"path": str(src), "name": "huge.txt"}], sid_dir)
    # v1.5: truncation marker is human-readable "(truncated to 256.0KB
    # from 300.0KB)" appearing as a markdown italic line next to the
    # source header. Old "[…truncated…]" sentinel is gone.
    assert "truncated to" in out.normalized_text
    assert "300.0KB" in out.normalized_text  # original size shown
    # Original copy kept full (v1.5 unchanged)
    assert (sid_dir / "input" / "huge.txt").stat().st_size == 300 * 1024


def test_ingest_collision_avoided_in_input_dir(tmp_path):
    src1 = tmp_path / "a.txt"
    src1.write_text("one", encoding="utf-8")
    src2 = tmp_path / "subdir" / "a.txt"
    src2.parent.mkdir()
    src2.write_text("two", encoding="utf-8")
    sid_dir = tmp_path / "sid_i"
    out = ingest_mod.ingest(
        "",
        [{"path": str(src1), "name": "a.txt"},
         {"path": str(src2), "name": "a.txt"}],
        sid_dir,
    )
    files = sorted(p.name for p in (sid_dir / "input").iterdir())
    assert "a.txt" in files
    # Second one renamed
    assert any(f != "a.txt" and f.startswith("a") and f.endswith(".txt")
               for f in files)
    assert "one" in out.normalized_text and "two" in out.normalized_text


def test_ingest_one_backend_failure_doesnt_kill_others(tmp_path, monkeypatch):
    """v1.5 rename: _process_attachment → _route_attachment. Same
    contract: one attachment's failure must not kill others; failed
    one shows up in IngestResult.errors."""
    src_ok = tmp_path / "ok.txt"
    src_ok.write_text("survivor", encoding="utf-8")

    real = ingest_mod._route_attachment

    def flaky(att, idx, input_dir, backends):
        if att.get("name") == "boom":
            raise RuntimeError("disk read error")
        return real(att, idx, input_dir, backends)

    monkeypatch.setattr(ingest_mod, "_route_attachment", flaky)
    sid_dir = tmp_path / "sid_j"
    out = ingest_mod.ingest(
        "main",
        [{"name": "boom"}, {"path": str(src_ok), "name": "ok.txt"}],
        sid_dir,
    )
    assert "survivor" in out.normalized_text
    assert "main" in out.normalized_text
    assert any("boom" in e or "#0" in e for e in out.errors)


# --------------------------------------------------------------------------
# Wired into api.intake() — end-to-end seeding
# --------------------------------------------------------------------------


def _make_cp_for_intake(paid_tmp, monkeypatch):
    """Build a real Counterparty + ensure profile.json exists for fcntl."""
    from paid import identity, storage
    monkeypatch.setattr(storage, "PAID_DIR", paid_tmp)
    cp = identity.Counterparty(
        cp_id="feishu_jr",
        platform="feishu",
        user_id="jr",
        display_name="Junior",
        role="junior",
        topics_allowed=[],
        topics_always_escalate=[],
        web_search_allowed=False,
        notes="",
        active_review_session="",
    )
    identity.save_counterparty(cp)
    return cp


def test_intake_seeds_normalized_md_with_initial_message(paid_tmp, monkeypatch):
    from paid_review import api as review_api
    cp = _make_cp_for_intake(paid_tmp, monkeypatch)
    sid = review_api.intake(
        cp=cp, initial_message="Q3 plan draft", attachments=[],
    )
    sid_dir = paid_tmp / "review" / "sessions" / sid
    assert (sid_dir / "normalized.md").read_text(encoding="utf-8") == "Q3 plan draft"


def test_intake_seeds_normalized_md_with_attachment(paid_tmp, monkeypatch):
    from paid_review import api as review_api
    cp = _make_cp_for_intake(paid_tmp, monkeypatch)
    src = paid_tmp / "att.txt"
    src.write_text("attached body", encoding="utf-8")
    sid = review_api.intake(
        cp=cp, initial_message="header",
        attachments=[{"path": str(src), "name": "att.txt"}],
    )
    sid_dir = paid_tmp / "review" / "sessions" / sid
    body = (sid_dir / "normalized.md").read_text(encoding="utf-8")
    assert "header" in body
    assert "attached body" in body
    assert (sid_dir / "input" / "att.txt").exists()


def test_intake_ingest_failure_doesnt_block_session_creation(
    paid_tmp, monkeypatch
):
    """If ingest module raises, intake() still returns sid (R5 contract)."""
    from paid_review import api as review_api
    cp = _make_cp_for_intake(paid_tmp, monkeypatch)

    import paid_review.ingest as ingest_mod_to_break
    def boom(*a, **kw):
        raise RuntimeError("ingest crashed")
    monkeypatch.setattr(ingest_mod_to_break, "ingest", boom)

    sid = review_api.intake(
        cp=cp, initial_message="fallback msg", attachments=[],
    )
    assert sid  # session still created
    sid_dir = paid_tmp / "review" / "sessions" / sid
    # normalized.md may not exist; _handle_intake will create from trigger text
    # The key contract: intake() didn't raise.
