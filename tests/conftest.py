"""Shared pytest fixtures — redirect PAID_DIR to a temp dir."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure the v1-sprint root is on sys.path so `import paid` works
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture
def paid_tmp(tmp_path, monkeypatch):
    """Redirect paid.storage.PAID_DIR to a tmp dir for isolation."""
    from paid import storage

    monkeypatch.setattr(storage, "PAID_DIR", tmp_path)
    return tmp_path
