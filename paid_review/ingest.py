"""paid_review.ingest — turn (initial_message, attachments) into normalized.md.

v0.1 backends (per spec §10 exclusions):
  - text passthrough: initial_message string
  - text/markdown/csv/json files: read, decoded utf-8 (best effort)
  - any other file: saved into sid_dir/input/, listed as `[attachment: name]`
    placeholder in normalized.md (no PDF/image OCR in v0.1)

R5 mitigation (§8): each backend independent try/except. All-fail with empty
initial_message → caller stays at INTAKE stage and asks junior to retype.

Hermes attachment dict shape (observed in adapters/wechat/lark/tg):
    {"path": "/tmp/foo.pdf", "name": "foo.pdf", "mimetype": "application/pdf"}
We tolerate missing fields — only `path` is required to copy a file in.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_TEXTLIKE_EXTS = {".txt", ".md", ".markdown", ".csv", ".json", ".log", ".rst"}
_TEXTLIKE_MIME_PREFIXES = ("text/",)
_MAX_TEXT_BYTES = 256 * 1024  # 256 KiB cap per file to keep prompts sane


@dataclass
class IngestResult:
    normalized_text: str
    saved_inputs: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True if we have *any* content (initial_message or any backend output)."""
        return bool(self.normalized_text.strip())


def ingest(
    initial_message: str,
    attachments: list[dict[str, Any]] | None,
    sid_dir: Path,
) -> IngestResult:
    """Build normalized.md content from initial_message + attachments.

    Always returns; never raises. Caller writes the returned text to disk.
    """
    parts: list[str] = []
    saved: list[Path] = []
    errors: list[str] = []

    if initial_message and initial_message.strip():
        parts.append(initial_message.strip())

    input_dir = sid_dir / "input"

    for idx, att in enumerate(attachments or []):
        try:
            piece, copied = _process_attachment(att, idx, input_dir)
        except Exception as exc:  # backend-level safety net
            err = f"[ingest] attachment #{idx} failed: {exc}"
            logger.warning(err)
            errors.append(err)
            continue
        if copied is not None:
            saved.append(copied)
        if piece:
            parts.append(piece)

    return IngestResult(
        normalized_text="\n\n".join(parts),
        saved_inputs=saved,
        errors=errors,
    )


def _process_attachment(
    att: dict[str, Any], idx: int, input_dir: Path
) -> tuple[str, Path | None]:
    """Returns (markdown_chunk, copied_path).
    chunk may be empty if file unreadable; copied may be None if no path."""
    name = (att.get("name") or att.get("filename") or "").strip()
    path_str = att.get("path") or att.get("file_path") or ""
    mimetype = (att.get("mimetype") or att.get("mime_type") or "").lower()

    src = Path(path_str) if path_str else None
    copied: Path | None = None

    if src and src.exists() and src.is_file():
        input_dir.mkdir(parents=True, exist_ok=True)
        safe_name = name or src.name or f"attachment_{idx}"
        dest = input_dir / safe_name
        # Avoid clobbering same-name files in same intake
        if dest.exists():
            stem, suffix = dest.stem, dest.suffix
            dest = input_dir / f"{stem}_{idx}{suffix}"
        try:
            shutil.copy2(src, dest)
            copied = dest
        except Exception as exc:
            logger.warning("[ingest] copy failed for %s: %s", src, exc)
            copied = None
        suffix = src.suffix.lower()
        if suffix in _TEXTLIKE_EXTS or any(
            mimetype.startswith(p) for p in _TEXTLIKE_MIME_PREFIXES
        ):
            text = _read_textfile(src)
            if text:
                return (
                    f"### Attachment: {safe_name}\n\n{text}",
                    copied,
                )
        # Binary / unsupported in v0.1 → placeholder
        return (f"[attachment: {safe_name}]", copied)

    # Inline-text attachments (no file on disk)
    inline = att.get("text") or att.get("content")
    if isinstance(inline, str) and inline.strip():
        label = name or f"inline_{idx}"
        return (f"### Attachment: {label}\n\n{inline.strip()}", None)

    # Unknown format / no path / no inline → leave a breadcrumb so the
    # downstream prompt can still note it
    label = name or f"attachment_{idx}"
    return (f"[attachment: {label}]", None)


def _read_textfile(path: Path) -> str:
    try:
        data = path.read_bytes()
    except Exception as exc:
        logger.warning("[ingest] read failed for %s: %s", path, exc)
        return ""
    if len(data) > _MAX_TEXT_BYTES:
        data = data[:_MAX_TEXT_BYTES]
        truncated = True
    else:
        truncated = False
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            return ""
    if truncated:
        text += "\n\n[…truncated…]"
    return text
