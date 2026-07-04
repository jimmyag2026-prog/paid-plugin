"""Tests for scripts/patch_hermes_feishu_rich_text.py.

The patch is applied at deploy time to the live hermes-agent venv; we can
unit-test the transformation by feeding a hand-crafted fixture that
mirrors hermes-agent's relevant lines.
"""
from __future__ import annotations

import re
import subprocess
import sys
import textwrap
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "patch_hermes_feishu_rich_text.py"


# v0.13.0 fixture — has the table → text bail block.
FIXTURE_V013 = textwrap.dedent('''
    import json
    import re

    _MARKDOWN_HINT_RE = re.compile(r"\\*\\*", re.MULTILINE)
    _MARKDOWN_TABLE_RE = re.compile(r"^\\|.*\\|\\n\\|[-|: ]+\\|", re.MULTILINE)


    def _build_markdown_post_payload(content):
        return json.dumps({"zh_cn": {"content": [[{"tag": "md", "text": content}]]}}, ensure_ascii=False)


    class FakeAdapter:
        def _build_outbound_payload(self, content: str) -> tuple[str, str]:
            # Feishu post-type 'md' elements do not render markdown tables; sending
            # table content as post causes the message to appear blank on the client.
            # Force plain text for anything that looks like a markdown table.
            if _MARKDOWN_TABLE_RE.search(content):
                text_payload = {"text": content}
                return "text", json.dumps(text_payload, ensure_ascii=False)
            if _MARKDOWN_HINT_RE.search(content):
                return "post", _build_markdown_post_payload(content)
            text_payload = {"text": content}
            return "text", json.dumps(text_payload, ensure_ascii=False)
''').lstrip()


# v0.12.0 fixture — no table bail; _MARKDOWN_TABLE_RE doesn't exist at
# module level. Helper anchor for this shape is _MARKDOWN_LINK_RE.
FIXTURE_V012 = textwrap.dedent('''
    import json
    import re

    _MARKDOWN_HINT_RE = re.compile(r"\\*\\*", re.MULTILINE)
    _MARKDOWN_LINK_RE = re.compile(r"\\[([^\\]]+)\\]\\(([^)]+)\\)")


    def _build_markdown_post_payload(content):
        return json.dumps({"zh_cn": {"content": [[{"tag": "md", "text": content}]]}}, ensure_ascii=False)


    class FakeAdapter:
        def _build_outbound_payload(self, content: str) -> tuple[str, str]:
            if _MARKDOWN_HINT_RE.search(content):
                return "post", _build_markdown_post_payload(content)
            text_payload = {"text": content}
            return "text", json.dumps(text_payload, ensure_ascii=False)
''').lstrip()


# Backward-compat alias for tests that used FIXTURE before the v0.12 split.
FIXTURE = FIXTURE_V013


def _run_patch(target: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(target), *extra],
        capture_output=True,
        text=True,
    )


def test_patch_inserts_helper_and_rewrites_build(tmp_path):
    target = tmp_path / "feishu.py"
    target.write_text(FIXTURE, encoding="utf-8")

    res = _run_patch(target)
    assert res.returncode == 0, res.stderr
    assert "[ok] patched" in res.stdout

    patched = target.read_text(encoding="utf-8")
    assert "_MARKDOWN_TABLE_BLOCK_RE" in patched
    assert "_wrap_tables_in_code_fences" in patched
    assert "PAID_RICH_TEXT_PATCH_VERSION" in patched
    assert "_wrap_tables_in_code_fences(content)" in patched
    # A backup must exist.
    backups = list(tmp_path.glob("feishu.py.bak.pre-*"))
    assert len(backups) == 1


def test_patch_is_idempotent(tmp_path):
    target = tmp_path / "feishu.py"
    target.write_text(FIXTURE, encoding="utf-8")
    assert _run_patch(target).returncode == 0
    first = target.read_text(encoding="utf-8")
    # Second run must skip without modifying.
    res2 = _run_patch(target)
    assert res2.returncode == 0
    assert "already patched" in res2.stdout
    assert target.read_text(encoding="utf-8") == first


def test_patch_dry_run_does_not_write(tmp_path):
    target = tmp_path / "feishu.py"
    target.write_text(FIXTURE, encoding="utf-8")
    original = target.read_text(encoding="utf-8")
    res = _run_patch(target, "--dry-run")
    assert res.returncode == 0
    assert "[dry-run]" in res.stdout
    assert target.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob("*.bak.*"))


def test_patch_fails_loudly_when_shape_changed(tmp_path):
    target = tmp_path / "feishu.py"
    target.write_text("# unrelated content with no anchor\n", encoding="utf-8")
    res = _run_patch(target)
    assert res.returncode != 0
    assert "no known hermes-agent shape detected" in res.stderr


def test_patch_handles_v012_shape(tmp_path):
    """v0.12.0 hermes had no table → text bail and no _MARKDOWN_TABLE_RE
    module-level regex. The patcher must detect this shape and use the
    _MARKDOWN_LINK_RE anchor instead."""
    target = tmp_path / "feishu.py"
    target.write_text(FIXTURE_V012, encoding="utf-8")
    res = _run_patch(target)
    assert res.returncode == 0, res.stderr
    assert "[ok] patched" in res.stdout

    patched = target.read_text(encoding="utf-8")
    assert "_MARKDOWN_TABLE_BLOCK_RE" in patched
    assert "_wrap_tables_in_code_fences" in patched
    assert "PAID_RICH_TEXT_PATCH_VERSION" in patched
    # Helper was inserted after _MARKDOWN_LINK_RE (the v0.12 anchor), not
    # _MARKDOWN_TABLE_RE which doesn't exist in v0.12.
    assert "_MARKDOWN_TABLE_RE" not in patched.split("def _build_markdown_post_payload")[0]


def test_v012_patched_build_uses_post_for_table_plus_bold(tmp_path):
    """v0.12 + patch: table+bold content routes to post and table is fenced."""
    target = tmp_path / "feishu.py"
    target.write_text(FIXTURE_V012, encoding="utf-8")
    assert _run_patch(target).returncode == 0

    import importlib.util
    spec = importlib.util.spec_from_file_location("patched_feishu_v012", target)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    adapter = mod.FakeAdapter()
    content = (
        "**SG/LA schedule**\n\n"
        "| SG | LA |\n"
        "|----|----|\n"
        "| 10:00 | 19:00 |\n"
    )
    msg_type, payload = adapter._build_outbound_payload(content)
    assert msg_type == "post"
    assert "```" in payload


def test_patched_helper_logic_wraps_tables(tmp_path):
    """After patching, the inserted _wrap_tables_in_code_fences helper
    must wrap a markdown table in ``` fences while leaving other content
    untouched."""
    target = tmp_path / "feishu.py"
    target.write_text(FIXTURE, encoding="utf-8")
    assert _run_patch(target).returncode == 0

    # Load patched module to exercise the helper.
    import importlib.util
    spec = importlib.util.spec_from_file_location("patched_feishu", target)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    table_in = (
        "Heading\n\n"
        "| col1 | col2 |\n"
        "|------|------|\n"
        "| a    | b    |\n"
        "| c    | d    |\n\n"
        "footer"
    )
    out = mod._wrap_tables_in_code_fences(table_in)
    # Table is fenced.
    assert "```" in out
    assert out.count("```") == 2  # opening + closing
    # Surrounding text preserved.
    assert "Heading" in out
    assert "footer" in out


def test_patched_build_uses_post_for_table_plus_bold(tmp_path):
    """Integration: table + bold no longer falls back to text mode."""
    target = tmp_path / "feishu.py"
    target.write_text(FIXTURE, encoding="utf-8")
    assert _run_patch(target).returncode == 0

    import importlib.util
    spec = importlib.util.spec_from_file_location("patched_feishu", target)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    adapter = mod.FakeAdapter()
    content = (
        "**Today's To-Do**\n\n"
        "| # | Task | Owner |\n"
        "|---|------|-------|\n"
        "| 1 | Write PR | Evie |\n"
    )
    msg_type, payload = adapter._build_outbound_payload(content)
    assert msg_type == "post", "table+bold should now route to post, not text"
    # The fenced table must be inside the post payload.
    assert "```" in payload


def test_patched_build_plain_text_stays_text(tmp_path):
    target = tmp_path / "feishu.py"
    target.write_text(FIXTURE, encoding="utf-8")
    assert _run_patch(target).returncode == 0

    import importlib.util
    spec = importlib.util.spec_from_file_location("patched_feishu", target)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    adapter = mod.FakeAdapter()
    msg_type, payload = adapter._build_outbound_payload("just a plain sentence.")
    assert msg_type == "text"
    assert "just a plain sentence." in payload
