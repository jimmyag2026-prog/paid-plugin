"""IngestBackend ABC + BackendResult dataclass.

Backends are sync (per design 09 §5.3 — entire ingest pipeline runs
inside ``loop.run_in_executor`` from the gateway loop).

Each backend independently try/excepts inside its ingest_* method.
Failures populate ``BackendResult.errors`` instead of raising; the
dispatcher aggregates and writes them into review brief's
"⚠️ Ingest errors" section so partial failure doesn't block review.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar


# Truncation cap. Same value across all backends so the dispatcher
# doesn't have to special-case per source.
DEFAULT_MAX_TEXT_BYTES = 256 * 1024  # 256 KiB


@dataclass
class BackendResult:
    """A single backend's output for one source (file / URL / inline text)."""

    normalized: str
    """The extracted plain-text content. May be empty if backend failed
    or had nothing to extract."""

    backend: str
    """Name of the backend that produced this (e.g. ``"text"``,
    ``"lark_doc"``). For audit + brief rendering."""

    source: str = ""
    """Source identifier — file path, URL, or short description."""

    note: str = ""
    """Optional metadata shown to owner in the brief.
    Examples: ``"truncated to 256KB from 1.4MB"``, ``"Lark Wiki node
    points to docx_abc"``."""

    errors: list[str] = field(default_factory=list)
    """Failures encountered (e.g. ``"Lark API code=403: no permission"``).
    Surfaced in brief's ⚠️ block per doc 09 §5.5."""

    @property
    def ok(self) -> bool:
        return bool(self.normalized.strip())


class IngestBackend(ABC):
    """Interface for ingest backends. Each backend declares ``name`` +
    what it can handle, then dispatcher routes by capability.

    Subclasses MUST implement at least one of ``ingest_file`` /
    ``ingest_url``. The matching ``can_handle_*`` method should return
    True for inputs that backend will accept.
    """

    name: ClassVar[str] = "abstract"

    # --- Capability gates ------------------------------------------------

    def can_handle_file(self, *, mime: str = "", ext: str = "") -> bool:
        """True iff this backend can extract from a local file.

        Default: False. Override if backend handles attachments.
        """
        return False

    def can_handle_url(self, url: str) -> bool:
        """True iff this backend can extract from this URL.

        Default: False. Override for URL-driven backends (Lark Doc,
        web scrape, YouTube, …).
        """
        return False

    # --- Ingest entry points --------------------------------------------

    def ingest_file(self, path: Path, *, mime: str = "") -> BackendResult:
        """Extract text from a local file. Override in subclasses that
        return ``True`` from ``can_handle_file``.

        Backends MUST NOT raise — catch internally and return a
        BackendResult with ``errors``.
        """
        raise NotImplementedError(
            f"{self.name} does not support file ingest"
        )

    def ingest_url(self, url: str) -> BackendResult:
        """Extract text from a URL. Override in subclasses that
        return ``True`` from ``can_handle_url``."""
        raise NotImplementedError(
            f"{self.name} does not support URL ingest"
        )


def truncate_with_note(
    text: str, *, max_bytes: int = DEFAULT_MAX_TEXT_BYTES,
) -> tuple[str, str]:
    """Truncate UTF-8 text to *max_bytes* and return (truncated, note).

    note is empty when no truncation happened, else describes the
    original size in human terms. Used by backends to populate
    BackendResult.note so owner sees ``(truncated to 256KB from 1.4MB)``
    on the brief.
    """
    encoded = text.encode("utf-8", errors="ignore")
    if len(encoded) <= max_bytes:
        return text, ""
    truncated_bytes = encoded[:max_bytes]
    # Decode safely — drop incomplete trailing multi-byte.
    truncated_text = truncated_bytes.decode("utf-8", errors="ignore")
    original_size = _human_size(len(encoded))
    new_size = _human_size(len(truncated_bytes))
    return truncated_text, f"truncated to {new_size} from {original_size}"


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / (1024 * 1024):.2f}MB"
