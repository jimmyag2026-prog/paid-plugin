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


def _flock_worker(args):
    """Top-level so ProcessPoolExecutor can pickle it on macOS spawn-mode."""
    worker_id, target_dir, target_file = args
    import sys
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root))
    from paid import storage as _s
    _s.PAID_DIR = Path(target_dir)
    big = "x" * 2048
    for i in range(50):
        _s.append_jsonl(Path(target_file), {"w": worker_id, "i": i, "pad": big})
    return worker_id


def test_append_jsonl_concurrent_writers_no_torn_lines(paid_tmp):
    """Multi-process flock regression: 8 workers each appending 50 lines
    of >2KB payloads must produce 400 valid JSON lines with no interleaving.
    Without ``fcntl.flock`` the long lines can tear past PIPE_BUF on Linux."""
    import json as _json
    from concurrent.futures import ProcessPoolExecutor

    p = paid_tmp / "concurrent.jsonl"
    args = [(w, str(paid_tmp), str(p)) for w in range(8)]
    with ProcessPoolExecutor(max_workers=8) as ex:
        list(ex.map(_flock_worker, args))

    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 8 * 50, f"got {len(lines)} lines, expected 400"
    for ln in lines:
        d = _json.loads(ln)  # any torn line raises JSONDecodeError
        assert "w" in d and "i" in d and len(d["pad"]) == 2048
