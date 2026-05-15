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
# fetch_content — file:// rejected (v1.6.6 security fix)
# ---------------------------------------------------------------------------


def test_fetch_content_file_rejected(tmp_path):
    """v1.6.6: file:// must be rejected to keep local file contents out of
    LLM context. The CLI import flow is the only sanctioned local path."""
    f = tmp_path / "test.txt"
    f.write_text("hello profile")
    with pytest.raises(ValueError, match="not supported"):
        di.fetch_content(f"file://{f}")


def test_fetch_content_file_missing_also_rejected(tmp_path):
    with pytest.raises(ValueError, match="not supported"):
        di.fetch_content(f"file://{tmp_path}/nonexistent.txt")


def test_fetch_content_unsupported_scheme():
    with pytest.raises(ValueError, match="Unsupported"):
        di.fetch_content("ftp://example.com/doc")


# ---------------------------------------------------------------------------
# fetch_content — web backend (mocked, with v1.6.6 SSRF guard bypassed)
# ---------------------------------------------------------------------------


def _bypass_ssrf_check(monkeypatch):
    """Make _assert_safe_url a no-op for unit tests that mock httpx."""
    monkeypatch.setattr(di, "_assert_safe_url", lambda _u: None)


def test_fetch_content_web_ok(monkeypatch):
    """v1.6.6: web fetch now uses httpx.Client (manual redirects).
    Mock the Client.get to return a fake response."""
    _bypass_ssrf_check(monkeypatch)

    class FakeResp:
        text = "<html><body><p>Owner is a founder.</p></body></html>"
        is_redirect = False
        def raise_for_status(self): pass

    class FakeClient:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url): return FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "Client", FakeClient)

    kind, text = di.fetch_content("https://example.com/about")
    assert kind == "web"
    assert "Owner is a founder" in text


def test_fetch_content_web_error(monkeypatch):
    _bypass_ssrf_check(monkeypatch)
    import httpx

    class FailingClient:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url): raise httpx.HTTPError("connect failed")

    monkeypatch.setattr(httpx, "Client", FailingClient)
    with pytest.raises(ValueError, match="Failed to fetch"):
        di.fetch_content("https://example.com/doc")


# ---------------------------------------------------------------------------
# v1.6.6: SSRF guard
# ---------------------------------------------------------------------------


def test_ssrf_blocks_localhost():
    with pytest.raises(ValueError, match="private/internal IP"):
        di._assert_safe_url("http://localhost/foo")


def test_ssrf_blocks_loopback_literal():
    with pytest.raises(ValueError, match="private/internal IP"):
        di._assert_safe_url("http://127.0.0.1/foo")


def test_ssrf_blocks_aws_metadata():
    """169.254.169.254 is link-local — AWS / GCP metadata endpoint."""
    with pytest.raises(ValueError, match="private/internal IP"):
        di._assert_safe_url("http://169.254.169.254/latest/meta-data/")


def test_ssrf_blocks_rfc1918():
    with pytest.raises(ValueError, match="private/internal IP"):
        di._assert_safe_url("http://10.0.0.5/api")
    with pytest.raises(ValueError, match="private/internal IP"):
        di._assert_safe_url("http://192.168.1.1/")


def test_ssrf_rejects_non_http_scheme():
    with pytest.raises(ValueError, match="Unsafe URL scheme"):
        di._assert_safe_url("gopher://1.2.3.4/")


def test_ssrf_rejects_redirect_to_internal(monkeypatch):
    """Redirect target gets re-validated, not just the initial URL."""
    _checked: list[str] = []

    def fake_check(url):
        _checked.append(url)
        if "169.254" in url:
            raise ValueError("Unsafe URL — host resolves to private/internal IP")

    monkeypatch.setattr(di, "_assert_safe_url", fake_check)
    import httpx

    class RedirectResp:
        is_redirect = True
        headers = {"location": "http://169.254.169.254/latest/meta-data/"}
        def raise_for_status(self): pass

    class RedirectClient:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url): return RedirectResp()

    monkeypatch.setattr(httpx, "Client", RedirectClient)
    with pytest.raises(ValueError, match="private/internal IP"):
        di.fetch_content("https://example.com/redirect")
    # both the initial URL AND the redirect target were checked
    assert any("example.com" in u for u in _checked)
    assert any("169.254" in u for u in _checked)


def test_ssrf_allows_public_ip(monkeypatch):
    """A real public DNS name should pass the IP check."""
    # 1.1.1.1 (Cloudflare DNS) is unambiguously public
    di._assert_safe_url("http://1.1.1.1/")


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
