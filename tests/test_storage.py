"""Tests for Module S (storage)."""

from __future__ import annotations

import json
from pathlib import Path

from paid import storage


def test_ensure_dirs_creates_subdirs(paid_tmp: Path):
    storage.ensure_dirs()
    assert paid_tmp.exists()
    assert (paid_tmp / "counterparties").is_dir()
    assert (paid_tmp / "persons").is_dir()


def test_read_json_missing_returns_none(paid_tmp: Path):
    assert storage.read_json(paid_tmp / "nope.json") is None


def test_write_then_read_json_roundtrip(paid_tmp: Path):
    p = paid_tmp / "sub" / "x.json"
    payload = {"hello": "世界", "n": 1, "list": [1, 2, 3]}
    storage.write_json(p, payload)
    assert p.exists()
    # Verify utf-8 + indent + ensure_ascii=False
    raw = p.read_text(encoding="utf-8")
    assert "世界" in raw
    assert "  " in raw  # indented
    loaded = storage.read_json(p)
    # write_json stamps schema_version when not present
    assert loaded == {"schema_version": storage.SCHEMA_VERSION, **payload}


def test_write_json_preserves_existing_schema_version(paid_tmp: Path):
    p = paid_tmp / "x.json"
    storage.write_json(p, {"schema_version": 99, "k": "v"})
    loaded = storage.read_json(p)
    assert loaded == {"schema_version": 99, "k": "v"}


def test_read_text_missing_returns_none(paid_tmp: Path):
    assert storage.read_text(paid_tmp / "no.txt") is None


def test_read_text_returns_content(paid_tmp: Path):
    p = paid_tmp / "a.txt"
    p.write_text("héllo", encoding="utf-8")
    assert storage.read_text(p) == "héllo"


def test_append_jsonl_creates_parent_and_appends(paid_tmp: Path):
    p = paid_tmp / "deep" / "logs" / "log.jsonl"
    storage.append_jsonl(p, {"a": 1})
    storage.append_jsonl(p, {"b": "中"})
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"a": 1}
    assert json.loads(lines[1]) == {"b": "中"}


def test_read_json_malformed_returns_none(paid_tmp: Path):
    p = paid_tmp / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    assert storage.read_json(p) is None
