"""Module DI — Doc / Link ingest for Owner Profile (v1.6.1).

Fetches a Lark Doc or web URL and uses LLM extraction to propose
structured updates to the owner's profile. Part of the "/paid-setup add-doc"
flow, plus organic URL detection in owner DMs (v1.6.2).

Public API:
  fetch_content(url)           -> (kind, content_text)
  extract_profile_updates(content, profile) -> list[UpdateProposal]
  apply_proposals(profile, proposals)       -> OwnerProfile  (mutated copy)
  format_confirm_prompt(proposals)          -> str  (shown to owner before confirm)

URL kinds:
  "lark_doc"  — feishu.cn/docx/* or feishu.cn/wiki/*
  "web"       — anything else
  "file"      — local file:// (test/import path)

Fetch strategy:
  - lark_doc: LarkClient.get_doc_raw() via existing lark_client singleton
  - web: httpx GET, strip HTML tags, truncate at MAX_CONTENT_CHARS
  - file: plain read (used in tests / /paid-setup import)

Sensitive material is NEVER in proposals — all extractors are told to
exclude API keys, passwords, tokens, etc.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

MAX_CONTENT_CHARS = 12_000   # truncation limit before LLM call
_LARK_DOC_RE = re.compile(
    r"https?://[a-z0-9\-]+\.feishu\.cn/(?:docx|wiki)/([A-Za-z0-9]+)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# UpdateProposal
# ---------------------------------------------------------------------------

@dataclass
class UpdateProposal:
    """One proposed change to the owner profile."""
    field: str        # e.g. "voice.do_not_say", "topics.always_escalate"
    current: Any      # current value (None if unset)
    proposed: Any     # proposed new value
    rationale: str    # one sentence why
    accepted: bool = False


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def fetch_content(url: str) -> tuple[str, str]:
    """Fetch doc/URL content. Returns (kind, text).

    kind: "lark_doc" | "web" | "file"
    Raises ValueError on unsupported scheme or fetch failure.
    """
    url = url.strip()

    if url.startswith("file://"):
        path = url[len("file://"):]
        try:
            from pathlib import Path
            text = Path(path).read_text(encoding="utf-8")
            return ("file", text[:MAX_CONTENT_CHARS])
        except OSError as e:
            raise ValueError(f"Cannot read file {path}: {e}") from e

    lark_match = _LARK_DOC_RE.search(url)
    if lark_match:
        doc_id = lark_match.group(1)
        kind = "wiki" if "/wiki/" in url else "docx"
        return _fetch_lark_doc(url, doc_id, kind)

    if url.startswith("http://") or url.startswith("https://"):
        return _fetch_web(url)

    raise ValueError(f"Unsupported URL scheme: {url!r}")


def _fetch_lark_doc(url: str, token: str, kind: str) -> tuple[str, str]:
    """Fetch via LarkClient. Returns ("lark_doc", text)."""
    try:
        from . import lark_client as _lc
        client = _lc.get_client()
        if kind == "wiki":
            node = client.get_wiki_node(token)
            if node.get("obj_type") == "docx":
                token = node.get("obj_token", token)
        text = client.get_doc_raw(token)
    except Exception as e:
        raise ValueError(f"Failed to fetch Lark Doc {url}: {e}") from e
    if not text or not text.strip():
        raise ValueError(f"Lark Doc returned empty content for {url}")
    return ("lark_doc", text[:MAX_CONTENT_CHARS])


def _fetch_web(url: str) -> tuple[str, str]:
    """Fetch via httpx, strip HTML, truncate. Returns ("web", text)."""
    try:
        import httpx
        resp = httpx.get(url, timeout=15.0, follow_redirects=True, headers={
            "User-Agent": "PAID-profile-ingest/1.0",
        })
        resp.raise_for_status()
        raw = resp.text
    except Exception as e:
        raise ValueError(f"Failed to fetch {url}: {e}") from e
    text = _strip_html(raw)
    if not text.strip():
        raise ValueError(f"No readable text content at {url}")
    return ("web", text[:MAX_CONTENT_CHARS])


def _strip_html(html: str) -> str:
    """Very lightweight HTML stripper — remove tags, collapse whitespace."""
    # Remove script/style blocks first
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    # Remove all tags
    html = re.sub(r"<[^>]+>", " ", html)
    # Decode common HTML entities
    html = html.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    html = html.replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
    # Collapse whitespace
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()


# ---------------------------------------------------------------------------
# LLM extraction
# ---------------------------------------------------------------------------

_EXTRACT_SYSTEM = """\
You are a profile extraction assistant. You read documents and extract
structured owner profile updates for an AI delegation system (PAID).

PAID's owner profile has these updatable fields:
  name                   — owner's preferred name (string)
  voice.tone             — communication style: "direct-friendly", "professional", "casual", "minimal", or freeform
  voice.style_notes      — freeform style notes (string)
  voice.self_description — how owner describes themselves (string)
  voice.do_not_say       — list of banned phrases (list of strings)
  topics.always_escalate — topics that always need owner approval (list of strings)
  topics.always_direct   — topics bot can handle without approval (list of strings)
  topics.always_decline  — topics bot should reject outright (list of strings)
  preferred_language     — "zh", "en", "ko", or "auto"
  preferences.daily_cost_cap_usd — daily LLM budget in USD (number)

RULES:
- Only extract fields that are EXPLICITLY stated or very strongly implied.
- Do NOT infer, guess, or fill gaps.
- Do NOT include any credentials, tokens, API keys, passwords.
- Do NOT include personal health, financial account, or address data.
- Each proposal must have a clear rationale quoting the source text.
- Return valid JSON array. If nothing can be extracted, return empty array [].
"""

_EXTRACT_USER_TMPL = """\
Current profile snapshot:
{profile_summary}

Document content:
---
{content}
---

Extract owner profile update proposals. Return a JSON array:
[
  {{
    "field": "voice.do_not_say",
    "proposed": ["按规定", "依据条款"],
    "rationale": "Doc explicitly states '不要使用官僚措辞'"
  }},
  ...
]
Only include fields from the allowed list. Return [] if nothing clear.
"""


def extract_profile_updates(
    content: str,
    profile: "OwnerProfile",  # type: ignore[name-defined]  # forward ref
) -> list[UpdateProposal]:
    """Call LLM to extract proposed profile updates from document content.

    Returns list of UpdateProposal (may be empty if doc has nothing useful).
    Handles LLM errors gracefully — returns [] rather than raising.
    """
    from . import hermes_io
    from . import profile as _profile

    profile_summary = _summarize_profile(profile)
    prompt = _EXTRACT_USER_TMPL.format(
        profile_summary=profile_summary,
        content=content[:MAX_CONTENT_CHARS],
    )
    try:
        raw = hermes_io.call_llm(
            system=_EXTRACT_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
        )
    except Exception as e:
        logger.warning("doc_ingest: LLM extraction failed: %s", e)
        return []

    proposals = _parse_proposals(raw, profile)
    return proposals


def _summarize_profile(profile: Any) -> str:
    """Short text snapshot of current profile for LLM context."""
    lines = [
        f"name: {profile.name}",
        f"voice.tone: {profile.voice.tone}",
        f"voice.do_not_say: {profile.voice.do_not_say!r}",
        f"topics.always_escalate: {profile.topics.always_escalate!r}",
        f"topics.always_direct: {profile.topics.always_direct!r}",
        f"preferred_language: {profile.preferred_language}",
        f"preferences.daily_cost_cap_usd: {profile.preferences.daily_cost_cap_usd}",
    ]
    return "\n".join(lines)


def _parse_proposals(raw: str, profile: Any) -> list[UpdateProposal]:
    """Parse LLM JSON response into UpdateProposal list."""
    import json

    # Strip markdown fences
    text = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    text = re.sub(r"```\s*$", "", text.strip(), flags=re.MULTILINE)
    text = text.strip()

    # Find first [...] block
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []
    try:
        items = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        logger.warning("doc_ingest: JSON parse error: %s", e)
        return []

    if not isinstance(items, list):
        return []

    _ALLOWED_FIELDS = {
        "name", "voice.tone", "voice.style_notes", "voice.self_description",
        "voice.do_not_say", "topics.always_escalate", "topics.always_direct",
        "topics.always_decline", "preferred_language",
        "preferences.daily_cost_cap_usd",
    }

    results: list[UpdateProposal] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        f = item.get("field", "")
        if f not in _ALLOWED_FIELDS:
            continue
        proposed = item.get("proposed")
        rationale = str(item.get("rationale", ""))[:300]
        current = _get_current_value(profile, f)
        results.append(UpdateProposal(
            field=f,
            current=current,
            proposed=proposed,
            rationale=rationale,
        ))
    return results


def _get_current_value(profile: Any, field_path: str) -> Any:
    """Get current profile field value by dotted path."""
    parts = field_path.split(".", 1)
    obj = profile
    for part in parts:
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return obj


# ---------------------------------------------------------------------------
# Apply confirmed proposals
# ---------------------------------------------------------------------------


def apply_proposals(profile: Any, proposals: list[UpdateProposal]) -> Any:
    """Apply all accepted proposals to profile (mutates in place), save, derive.

    Returns updated profile.
    """
    from . import profile as _profile
    from . import profile_sync as _ps

    for p in proposals:
        if not p.accepted:
            continue
        _set_profile_field(profile, p.field, p.proposed)
        logger.info("doc_ingest: applied %s = %r", p.field, p.proposed)

    _profile.save_profile(profile)
    _ps.derive_from_profile(profile)
    return profile


def _set_profile_field(profile: Any, field_path: str, value: Any) -> None:
    """Set profile field by dotted path. Handles nested objects."""
    parts = field_path.split(".", 1)
    if len(parts) == 1:
        if hasattr(profile, field_path):
            setattr(profile, field_path, value)
        return
    parent_attr, child_attr = parts
    parent = getattr(profile, parent_attr, None)
    if parent is not None and hasattr(parent, child_attr):
        setattr(parent, child_attr, value)


# ---------------------------------------------------------------------------
# Format confirm prompt (shown to owner)
# ---------------------------------------------------------------------------


def format_confirm_prompt(proposals: list[UpdateProposal]) -> str:
    """Render list of proposals for owner review.

    Returns a DM-ready string listing each proposal with index numbers.
    Owner replies: "all", "1 3 5", "none", or individual numbers.
    """
    if not proposals:
        return "🔍 从文档中没有提取到可更新的 profile 字段。"

    lines = ["📋 从文档提取到以下 profile 更新建议，回复序号确认（`all` = 全接受，`none` = 全拒绝）：\n"]
    for i, p in enumerate(proposals, 1):
        current_str = repr(p.current) if p.current is not None else "（未设置）"
        proposed_str = repr(p.proposed)
        lines.append(f"{i}. **{p.field}**")
        lines.append(f"   现在: {current_str}")
        lines.append(f"   改为: {proposed_str}")
        lines.append(f"   理由: {p.rationale}")
        lines.append("")
    lines.append("回复 `all` / 序号（空格隔开）/ `none`：")
    return "\n".join(lines)


def parse_confirm_reply(reply: str, count: int) -> list[int]:
    """Parse owner's confirm reply into list of 1-based accepted indices.

    "all" → [1, 2, ..., count]
    "none" → []
    "1 3" → [1, 3]
    "1,2,3" → [1, 2, 3]
    """
    text = reply.strip().lower()
    if text in {"all", "全部", "是", "yes", "ok", "全接受"}:
        return list(range(1, count + 1))
    if text in {"none", "全拒绝", "否", "no", "skip", "不"}:
        return []
    nums: list[int] = []
    for tok in re.split(r"[\s,，、]+", text):
        tok = tok.strip()
        if tok.isdigit():
            n = int(tok)
            if 1 <= n <= count:
                nums.append(n)
    return sorted(set(nums))


# ---------------------------------------------------------------------------
# URL detection — is this text a profile-relevant doc link?
# ---------------------------------------------------------------------------

_DOC_URL_RE = re.compile(
    r"https?://\S+",
    re.IGNORECASE,
)


def extract_urls(text: str) -> list[str]:
    """Extract all URLs from a text string."""
    return _DOC_URL_RE.findall(text)


def is_ingest_url(url: str) -> bool:
    """True if url looks like a doc/link worth ingesting into profile."""
    url = url.lower()
    # Lark docs
    if "feishu.cn/docx/" in url or "feishu.cn/wiki/" in url:
        return True
    # Common content-rich web pages (not images/media/downloads)
    skip_exts = (".jpg", ".jpeg", ".png", ".gif", ".pdf", ".zip", ".tar", ".mp4")
    for ext in skip_exts:
        if url.split("?")[0].endswith(ext):
            return False
    # Generic http/https
    return url.startswith("http://") or url.startswith("https://")
