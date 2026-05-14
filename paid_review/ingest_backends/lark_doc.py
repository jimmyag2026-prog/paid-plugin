"""LarkDocBackend — read Lark Docs and Wiki pages by URL.

Activates when initial_message text contains a Lark Doc / Wiki URL.
Calls Lark Open API via :class:`paid.lark_client.LarkClient` to fetch
raw text content, then surfaces it to the review pipeline as if the
junior had pasted the doc body.

URL patterns supported (per v3 review-agent precedent):
  - Lark international:  https://*.larksuite.com/docx/<doc_id>
  - Feishu CN:           https://*.feishu.cn/docx/<doc_id>
  - Wiki:                .../wiki/<wiki_token>
                         → chains via get_wiki_node to underlying docx
  - Bitable (later):     .../base/<base_token>   ← Tier 3, NOT in this backend
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .base import BackendResult, IngestBackend, truncate_with_note

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------


# Docs:  https://<host>.feishu.cn/docx/<id> or .larksuite.com/docx/<id>
_DOC_URL_RE = re.compile(
    r"https?://[\w.-]+\.(?:feishu\.cn|larksuite\.com)/docx/([A-Za-z0-9]+)"
)
# Wiki:  https://<host>/wiki/<token>
_WIKI_URL_RE = re.compile(
    r"https?://[\w.-]+\.(?:feishu\.cn|larksuite\.com)/wiki/([A-Za-z0-9]+)"
)


def extract_lark_resource(url: str) -> tuple[str, str] | None:
    """Returns (resource_type, resource_id) for a Lark URL, or None.

    resource_type ∈ {"doc", "wiki"}.
    """
    if not isinstance(url, str):
        return None
    m = _DOC_URL_RE.search(url)
    if m:
        return ("doc", m.group(1))
    m = _WIKI_URL_RE.search(url)
    if m:
        return ("wiki", m.group(1))
    return None


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class LarkDocBackend(IngestBackend):
    """Ingests Lark Doc + Wiki URLs via LarkClient.

    Constructor takes a LarkClient (real or stub) so tests can inject
    a mock without touching env vars. Production usage typically calls
    :func:`paid.lark_client.get_lark_client` and passes the singleton.
    """

    name = "lark_doc"

    def __init__(self, lark_client: Any):
        self._lark = lark_client

    def can_handle_url(self, url: str) -> bool:
        return extract_lark_resource(url) is not None

    def ingest_url(self, url: str) -> BackendResult:
        resource = extract_lark_resource(url)
        if resource is None:
            return BackendResult(
                normalized="",
                backend=self.name,
                source=url,
                errors=[f"not a Lark URL: {url[:120]}"],
            )

        rtype, rid = resource
        if rtype == "doc":
            return self._ingest_doc(rid, url)
        if rtype == "wiki":
            return self._ingest_wiki(rid, url)
        # Defensive: extract_lark_resource only returns known types.
        return BackendResult(
            normalized="",
            backend=self.name,
            source=url,
            errors=[f"unsupported Lark resource type: {rtype}"],
        )

    # --- internals -------------------------------------------------------

    def _ingest_doc(self, doc_id: str, url: str) -> BackendResult:
        try:
            content = self._lark.get_doc_raw(doc_id)
        except Exception as exc:
            logger.warning("[lark_doc] get_doc_raw %s failed: %s", doc_id, exc)
            return BackendResult(
                normalized="",
                backend=self.name,
                source=url,
                errors=[f"Lark Doc fetch failed: {exc}"],
            )
        truncated, note = truncate_with_note(content or "")
        return BackendResult(
            normalized=truncated,
            backend=self.name,
            source=url,
            note=note,
        )

    def _ingest_wiki(self, wiki_token: str, url: str) -> BackendResult:
        try:
            node = self._lark.get_wiki_node(wiki_token)
        except Exception as exc:
            logger.warning("[lark_doc] get_wiki_node %s failed: %s", wiki_token, exc)
            return BackendResult(
                normalized="",
                backend=self.name,
                source=url,
                errors=[f"Lark Wiki node lookup failed: {exc}"],
            )

        if not isinstance(node, dict) or not node:
            return BackendResult(
                normalized="",
                backend=self.name,
                source=url,
                errors=[f"Lark Wiki node {wiki_token} returned empty payload"],
            )

        obj_type = (node.get("obj_type") or "").strip()
        obj_token = (node.get("obj_token") or "").strip()
        title = (node.get("title") or "").strip()

        # Only docx-backed wiki nodes are followable in v1.5.0. Sheets /
        # bitables show as note; Tier 3 will add Bitable backend.
        if obj_type != "docx" or not obj_token:
            return BackendResult(
                normalized=(f"# {title}\n\n" if title else "")
                          + f"_(Lark Wiki node, obj_type={obj_type or 'unknown'}; "
                          + "content not retrievable in v1.5.0 — Bitable/Sheet support is Tier 3)_",
                backend=self.name,
                source=url,
                note=f"wiki node obj_type={obj_type or 'unknown'}",
                errors=[]
                if obj_type in ("sheet", "bitable", "")
                else [f"unexpected wiki obj_type: {obj_type}"],
            )

        # Chain to get_doc_raw.
        try:
            content = self._lark.get_doc_raw(obj_token)
        except Exception as exc:
            logger.warning(
                "[lark_doc] wiki %s → doc %s get_doc_raw failed: %s",
                wiki_token, obj_token, exc,
            )
            return BackendResult(
                normalized="",
                backend=self.name,
                source=url,
                note=f"wiki → docx {obj_token}",
                errors=[f"Wiki points to docx {obj_token} but doc fetch failed: {exc}"],
            )
        truncated, base_note = truncate_with_note(content or "")
        chain_note = f"wiki → docx {obj_token}"
        note = f"{chain_note}; {base_note}" if base_note else chain_note
        prefix = f"# {title}\n\n" if title else ""
        return BackendResult(
            normalized=prefix + truncated,
            backend=self.name,
            source=url,
            note=note,
        )
