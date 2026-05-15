"""Tests for paid.doc_ingest — Doc/Link ingest for Owner Profile (v1.6.1)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from paid import doc_ingest as di
from paid import profile as p
from paid import storage


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "PAID_DIR", tmp_path)


# ---------------------------------------------------------------------------
# fetch_content — file:// backend
# ---------------------------------------------------------------------------


def test_fetch_content_file_ok(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("hello profile")
    kind, text = di.fetch_content(f"file://{f}")
    assert kind == "file"
    assert "hello profile" in text


def test_fetch_content_file_missing(tmp_path):
    with pytest.raises(ValueError, match="Cannot read"):
        di.fetch_content(f"file://{tmp_path}/nonexistent.txt")


def test_fetch_content_unsupported_scheme():
    with pytest.raises(ValueError, match="Unsupported"):
        di.fetch_content("ftp://example.com/doc")


# ---------------------------------------------------------------------------
# fetch_content — web backend (mocked)
# ---------------------------------------------------------------------------


def test_fetch_content_web_ok(monkeypatch):
    class FakeResp:
        text = "<html><body><p>Owner is a founder.</p></body></html>"
        def raise_for_status(self): pass

    def fake_get(url, **kw):
        return FakeResp()

    monkeypatch.setattr(di, "__name__", "paid.doc_ingest")
    import httpx
    monkeypatch.setattr(httpx, "get", fake_get)

    kind, text = di.fetch_content("https://example.com/about")
    assert kind == "web"
    assert "Owner is a founder" in text


def test_fetch_content_web_error(monkeypatch):
    import httpx
    def fail_get(url, **kw):
        raise httpx.HTTPError("connect failed")

    monkeypatch.setattr(httpx, "get", fail_get)
    with pytest.raises(ValueError, match="Failed to fetch"):
        di.fetch_content("https://example.com/doc")


# ---------------------------------------------------------------------------
# _strip_html
# ---------------------------------------------------------------------------


def test_strip_html_removes_tags():
    html = "<h1>Title</h1><p>Body text &amp; more</p><script>evil()</script>"
    text = di._strip_html(html)
    assert "<" not in text
    assert "Title" in text
    assert "Body text & more" in text
    assert "evil" not in text


def test_strip_html_preserves_content():
    html = "<div>工作 SOP：客户问题 2 小时内回复</div>"
    text = di._strip_html(html)
    assert "工作 SOP" in text
    assert "2 小时" in text


# ---------------------------------------------------------------------------
# extract_profile_updates — LLM mocked
# ---------------------------------------------------------------------------


def test_extract_returns_empty_on_llm_error(monkeypatch):
    from paid import hermes_io
    monkeypatch.setattr(hermes_io, "call_llm", lambda **kw: (_ for _ in ()).throw(RuntimeError("fail")))

    prof = p.new_profile("jimmy", name="Jimmy")
    result = di.extract_profile_updates("some content", prof)
    assert result == []


def test_extract_parses_valid_json(monkeypatch):
    llm_resp = json.dumps([
        {
            "field": "voice.do_not_say",
            "proposed": ["按规定", "依据条款"],
            "rationale": "Doc says avoid bureaucratic language"
        }
    ])
    from paid import hermes_io
    monkeypatch.setattr(hermes_io, "call_llm", lambda **kw: llm_resp)

    prof = p.new_profile("jimmy", name="Jimmy")
    proposals = di.extract_profile_updates("doc content", prof)
    assert len(proposals) == 1
    assert proposals[0].field == "voice.do_not_say"
    assert proposals[0].proposed == ["按规定", "依据条款"]
    assert not proposals[0].accepted


def test_extract_ignores_disallowed_fields(monkeypatch):
    llm_resp = json.dumps([
        {"field": "FEISHU_APP_SECRET", "proposed": "leak", "rationale": "bad"},
        {"field": "preferences.daily_cost_cap_usd", "proposed": 20.0, "rationale": "ok"},
    ])
    from paid import hermes_io
    monkeypatch.setattr(hermes_io, "call_llm", lambda **kw: llm_resp)

    prof = p.new_profile("jimmy", name="Jimmy")
    proposals = di.extract_profile_updates("content", prof)
    # Only the allowed field passes
    assert len(proposals) == 1
    assert proposals[0].field == "preferences.daily_cost_cap_usd"


def test_extract_handles_markdown_fences(monkeypatch):
    llm_resp = '```json\n[{"field": "name", "proposed": "Bob", "rationale": "says so"}]\n```'
    from paid import hermes_io
    monkeypatch.setattr(hermes_io, "call_llm", lambda **kw: llm_resp)

    prof = p.new_profile("jimmy", name="Jimmy")
    proposals = di.extract_profile_updates("content", prof)
    assert len(proposals) == 1
    assert proposals[0].field == "name"


def test_extract_handles_malformed_json(monkeypatch):
    from paid import hermes_io
    monkeypatch.setattr(hermes_io, "call_llm", lambda **kw: "not json at all")

    prof = p.new_profile("jimmy", name="Jimmy")
    proposals = di.extract_profile_updates("content", prof)
    assert proposals == []


# ---------------------------------------------------------------------------
# apply_proposals
# ---------------------------------------------------------------------------


def test_apply_proposals_updates_field(monkeypatch):
    from paid import profile_sync
    monkeypatch.setattr(profile_sync, "derive_from_profile", lambda pr: {"wrote": []})

    prof = p.new_profile("jimmy", name="Jimmy")
    p.save_profile(prof)

    proposals = [
        di.UpdateProposal(
            field="voice.do_not_say",
            current=[],
            proposed=["按规定"],
            rationale="test",
            accepted=True,
        )
    ]
    updated = di.apply_proposals(prof, proposals)
    assert "按规定" in updated.voice.do_not_say


def test_apply_proposals_skips_rejected():
    prof = p.new_profile("jimmy", name="Jimmy")
    p.save_profile(prof)

    proposals = [
        di.UpdateProposal(
            field="name",
            current="Jimmy",
            proposed="Bob",
            rationale="test",
            accepted=False,
        )
    ]
    from paid import profile_sync
    from unittest.mock import patch as _patch
    with _patch.object(profile_sync, "derive_from_profile", return_value={"wrote": []}):
        updated = di.apply_proposals(prof, proposals)
    assert updated.name == "Jimmy"


# ---------------------------------------------------------------------------
# format_confirm_prompt + parse_confirm_reply
# ---------------------------------------------------------------------------


def test_format_confirm_prompt_empty():
    msg = di.format_confirm_prompt([])
    assert "没有" in msg


def test_format_confirm_prompt_lists_items():
    proposals = [
        di.UpdateProposal("voice.do_not_say", [], ["按规定"], "doc says so"),
        di.UpdateProposal("name", "Jimmy", "Jimmy Yin", "full name used"),
    ]
    msg = di.format_confirm_prompt(proposals)
    assert "1." in msg
    assert "2." in msg
    assert "voice.do_not_say" in msg
    assert "name" in msg


def test_parse_confirm_reply_all():
    assert di.parse_confirm_reply("all", 3) == [1, 2, 3]


def test_parse_confirm_reply_none():
    assert di.parse_confirm_reply("none", 3) == []


def test_parse_confirm_reply_numbers():
    assert di.parse_confirm_reply("1 3", 3) == [1, 3]


def test_parse_confirm_reply_comma_sep():
    assert di.parse_confirm_reply("1,2", 3) == [1, 2]


def test_parse_confirm_reply_out_of_range():
    assert di.parse_confirm_reply("5", 3) == []


def test_parse_confirm_reply_chinese_all():
    assert di.parse_confirm_reply("全部", 2) == [1, 2]


# ---------------------------------------------------------------------------
# URL utilities
# ---------------------------------------------------------------------------


def test_extract_urls():
    text = "看这个 https://example.feishu.cn/docx/abc 还有 https://example.com"
    urls = di.extract_urls(text)
    assert "https://example.feishu.cn/docx/abc" in urls
    assert "https://example.com" in urls


def test_is_ingest_url_lark_doc():
    assert di.is_ingest_url("https://company.feishu.cn/docx/Abc123")


def test_is_ingest_url_web():
    assert di.is_ingest_url("https://example.com/about")


def test_is_ingest_url_image_excluded():
    assert not di.is_ingest_url("https://example.com/photo.jpg")


def test_is_ingest_url_pdf_excluded():
    assert not di.is_ingest_url("https://example.com/doc.pdf")
