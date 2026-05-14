"""Tests for paid_review.api.add_attachments_to_session (v1.5.4).

Bullets the actual contract:
  - happy path: text-only session gets image attachment appended → normalized.md grows + ingest_sources extended
  - CLOSED session refuses with ok=False
  - missing session refuses
  - empty attachments → ok=False reason "no attachments"
  - failing backend (missing path) → ok=True but ingest_errors populated
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


from paid import storage
from paid_review import api as review_api
from paid_review.core.state import SessionState, save_state, session_dir, transition


@pytest.fixture
def paid_tmp_iso(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "PAID_DIR", tmp_path)
    return tmp_path


def _make_session(sid: str, stage: str = "SUBJECT") -> SessionState:
    """Create a SUBJECT-stage session with a tiny normalized.md."""
    sd = session_dir(sid)
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "normalized.md").write_text("initial text\n", encoding="utf-8")
    state = SessionState(
        sid=sid,
        stage="INTAKE",
        cp_id="feishu_x",
        platform="feishu",
        verdict="PENDING",
    )
    # Walk through stages to reach the requested one (transitions enforce
    # the legal-transition matrix)
    if stage != "INTAKE":
        transition(state, "SUBJECT")
    if stage in ("SCAN", "QA", "MERGE", "GATE"):
        transition(state, "SCAN")
    if stage in ("QA", "MERGE", "GATE"):
        transition(state, "QA")
    save_state(state)
    return state


# ---------------------------------------------------------------------------
# Happy + refuse paths
# ---------------------------------------------------------------------------


def test_add_attachments_empty_returns_ok_false(paid_tmp_iso):
    out = review_api.add_attachments_to_session("nonexistent_sid", [])
    assert out["ok"] is False
    assert "no attachments" in out["reason"].lower()


def test_add_attachments_missing_session(paid_tmp_iso, tmp_path):
    fake_file = tmp_path / "x.txt"
    fake_file.write_text("hello", encoding="utf-8")
    out = review_api.add_attachments_to_session(
        "no_such_sid",
        [{"path": str(fake_file), "mimetype": "text/plain", "name": "x.txt"}],
    )
    assert out["ok"] is False
    assert "not found" in out["reason"].lower()


def test_add_attachments_closed_session(paid_tmp_iso, tmp_path):
    state = _make_session("sid_closed")
    state.verdict = "FORCED_PARTIAL"
    transition(state, "CLOSED")
    save_state(state)

    fake_file = tmp_path / "x.txt"
    fake_file.write_text("hello", encoding="utf-8")
    out = review_api.add_attachments_to_session(
        "sid_closed",
        [{"path": str(fake_file), "mimetype": "text/plain", "name": "x.txt"}],
    )
    assert out["ok"] is False
    assert "closed" in out["reason"].lower()


def test_add_text_attachment_appends_to_normalized(paid_tmp_iso, tmp_path):
    """Add a .txt via TextBackend — should append to normalized.md +
    extend state.ingest_sources."""
    state = _make_session("sid_open")
    f = tmp_path / "more.txt"
    f.write_text("Additional context line 1\nAdditional context line 2", encoding="utf-8")

    out = review_api.add_attachments_to_session(
        "sid_open",
        [{"path": str(f), "mimetype": "text/plain", "name": "more.txt"}],
    )
    assert out["ok"] is True
    assert out["added_sources"] >= 1
    assert out["appended_chars"] > 0

    # Verify normalized.md
    norm = (session_dir("sid_open") / "normalized.md").read_text(encoding="utf-8")
    assert "initial text" in norm  # original preserved
    assert "Additional context line 1" in norm  # new appended
    assert "---" in norm  # separator inserted

    # Verify state extended
    from paid_review.core.state import load_state
    s2 = load_state("sid_open")
    assert any(s.get("source", "").endswith("more.txt") for s in s2.ingest_sources)


def test_add_attachment_qa_stage_still_allowed(paid_tmp_iso, tmp_path):
    """Even in QA stage, new attachments can be added (acts like a junior
    sharing extra context mid-review)."""
    state = _make_session("sid_qa", stage="QA")
    f = tmp_path / "ctx.txt"
    f.write_text("late context", encoding="utf-8")
    out = review_api.add_attachments_to_session(
        "sid_qa",
        [{"path": str(f), "mimetype": "text/plain", "name": "ctx.txt"}],
    )
    assert out["ok"] is True


def test_add_attachment_with_missing_file_path_records_breadcrumb(paid_tmp_iso):
    """Path does not exist → dispatcher produces a placeholder
    breadcrumb so the session at least notes 'something was attached'."""
    state = _make_session("sid_open")
    out = review_api.add_attachments_to_session(
        "sid_open",
        [{"path": "/nonexistent/path.png", "mimetype": "image/png", "name": "p.png"}],
    )
    # Dispatcher returns a placeholder result (ok=True at API level —
    # adding nothing useful is not a hard fail)
    assert out["ok"] is True
    # Should have produced an audit error for owner brief visibility
    from paid_review.core.state import load_state
    s2 = load_state("sid_open")
    assert any("no readable path" in e for e in s2.ingest_errors)


def test_add_attachment_multiple_files(paid_tmp_iso, tmp_path):
    state = _make_session("sid_multi")
    paths = []
    for i in range(3):
        f = tmp_path / f"part_{i}.txt"
        f.write_text(f"part {i} content", encoding="utf-8")
        paths.append({"path": str(f), "mimetype": "text/plain", "name": f"part_{i}.txt"})
    out = review_api.add_attachments_to_session("sid_multi", paths)
    assert out["ok"] is True
    assert out["added_sources"] >= 3

    norm = (session_dir("sid_multi") / "normalized.md").read_text(encoding="utf-8")
    for i in range(3):
        assert f"part {i} content" in norm
