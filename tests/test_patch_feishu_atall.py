"""Tests for scripts/patches/feishu_atall_not_bot_mention.py.

The patch script mutates a real ``hermes-agent`` checkout in-place, so
tests build a minimal fake hermes-agent tree per test and run the script
against that tree.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PATCH_PATH = REPO_ROOT / "scripts" / "patches" / "feishu_atall_not_bot_mention.py"


def _load_patch_module():
    spec = importlib.util.spec_from_file_location(
        "feishu_atall_patch", PATCH_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


PATCH_MOD = _load_patch_module()


FAKE_UPSTREAM_FEISHU = '''"""Stub feishu.py — only the function the patch touches.

The minimum context required is the exact ``_mentions_self`` block from
upstream hermes-agent. Surrounding ``class`` / imports just need to make
the file parse as Python.
"""

from typing import Any


def normalize_feishu_message(*, message_type, raw_content, mentions, bot):
    return type("N", (), {"mentions": []})()


class _Stub:
    def _bot_identity(self):
        return None

    def _message_mentions_bot(self, mentions):
        return False

    def _post_mentions_bot(self, mentions):
        return False

    def _mentions_self(self, message: Any) -> bool:
        # @_all is Feishu's @everyone placeholder.
        raw_content = getattr(message, "content", "") or ""
        if "@_all" in raw_content:
            return True
        mentions = getattr(message, "mentions", None) or []
        if mentions and self._message_mentions_bot(mentions):
            return True
        normalized = normalize_feishu_message(
            message_type=getattr(message, "message_type", "") or "",
            raw_content=raw_content,
            mentions=getattr(message, "mentions", None),
            bot=self._bot_identity(),
        )
        return self._post_mentions_bot(normalized.mentions)
'''


def _build_fake_hermes(root: Path) -> Path:
    target = root / "gateway" / "platforms" / "feishu.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(FAKE_UPSTREAM_FEISHU, encoding="utf-8")
    return target


def test_apply_patch_replaces_block_and_validates_syntax(tmp_path):
    hermes = tmp_path / "hermes-agent"
    target = _build_fake_hermes(hermes)

    rc = PATCH_MOD.apply_patch(hermes, dry_run=False, revert=False)
    assert rc == 0

    patched = target.read_text()
    assert PATCH_MOD.MARKER in patched
    assert 'if "@_all" in raw_content:\n            return True' not in patched

    # File still parses
    import ast
    ast.parse(patched)

    # And behavioural check: load the patched module, exercise _mentions_self
    # with an @_all-only message → must NOT report itself as mentioned.
    sys.path.insert(0, str(target.parent))
    try:
        if "feishu" in sys.modules:
            del sys.modules["feishu"]
        import importlib
        feishu = importlib.import_module("feishu")
        stub = feishu._Stub()
        msg = type("M", (), {"content": "@_all hello team", "mentions": []})()
        assert stub._mentions_self(msg) is False
    finally:
        sys.path.remove(str(target.parent))


def test_apply_patch_is_idempotent(tmp_path):
    hermes = tmp_path / "hermes-agent"
    _build_fake_hermes(hermes)

    assert PATCH_MOD.apply_patch(hermes, dry_run=False, revert=False) == 0
    # Second call: no-op, no new backup, no edit
    backups_before = list((hermes / "gateway" / "platforms").glob("*.bak.*"))
    assert PATCH_MOD.apply_patch(hermes, dry_run=False, revert=False) == 0
    backups_after = list((hermes / "gateway" / "platforms").glob("*.bak.*"))
    assert len(backups_after) == len(backups_before)


def test_revert_restores_original(tmp_path):
    hermes = tmp_path / "hermes-agent"
    target = _build_fake_hermes(hermes)
    original = target.read_text()

    assert PATCH_MOD.apply_patch(hermes, dry_run=False, revert=False) == 0
    assert PATCH_MOD.apply_patch(hermes, dry_run=False, revert=True) == 0
    assert target.read_text() == original


def test_unrecognized_layout_refuses(tmp_path):
    hermes = tmp_path / "hermes-agent"
    target = _build_fake_hermes(hermes)
    # Mutate the upstream block enough to break the exact-match contract.
    src = target.read_text().replace("@_all", "@_all_v2")
    target.write_text(src)

    rc = PATCH_MOD.apply_patch(hermes, dry_run=False, revert=False)
    assert rc == 1


def test_dry_run_does_not_modify(tmp_path, capsys):
    hermes = tmp_path / "hermes-agent"
    target = _build_fake_hermes(hermes)
    before = target.read_text()

    rc = PATCH_MOD.apply_patch(hermes, dry_run=True, revert=False)
    assert rc == 0
    assert target.read_text() == before
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "@_all" in out  # diff hunk content


def test_missing_target_errors(tmp_path):
    hermes = tmp_path / "no-such-hermes"
    rc = PATCH_MOD.apply_patch(hermes, dry_run=False, revert=False)
    assert rc == 1
