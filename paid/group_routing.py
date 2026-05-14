"""Module G — group chat routing core (v1.5 Phase 6).

Owner pilots so far ran exclusively in P2P (direct-message) chats:
junior DMs the PAID bot, PAID DMs owner, owner approves, PAID DMs back.
v1.5 opens group chats too — but a bot that auto-replies to every
group message would be disastrous. We need explicit opt-in per group.

Behavior contract
-----------------
For every inbound event PAID's pre_gateway_dispatch hook sees, this
module tells it one of:

  - ``"p2p"`` — not a group, existing P2P logic applies (no change).
  - ``"group_disabled"`` — group is unknown / not enabled, drop the
    event silently (return ``{"action": "skip"}`` upstream).
  - ``"group_review"`` — group is enabled in review-only mode; only
    /review and active-session messages go through; everyday chatter
    is dropped.
  - ``"group_everyday"`` — group is enabled in everyday mode; PAID
    acts as if it were a P2P chat for this group (Claude responds to
    @-mentions, etc). Reserved for v1.6+ — Phase 6 only ships the
    plumbing; the everyday path still drops for now to avoid surprise.

State
-----
Per-group config lives at ``$PAID_DIR/groups/<platform>_<group_id>.json``:

    {
      "schema_version": 1,
      "group_key": "feishu_oc_xxx",
      "platform": "feishu",
      "group_id": "oc_xxx",
      "enabled": true,
      "mode": "review-only",            # "review-only" | "everyday" | "both"
      "owner_user_id": "ou_xxx",        # who enabled the group; for audit
      "display_name": "JELabs eng",     # optional, set by /paid-set-group-name
      "created_at": "2026-05-13T...",
      "updated_at": "2026-05-13T..."
    }

Group detection
---------------
Hermes' MessageEvent.source carries platform + user_id but the
chat-vs-DM distinction varies by platform:

- Lark / Feishu: chat_type field, "p2p" vs "group". The current
  hermes-agent (0.12.x) populates ``source.chat_type``; older releases
  don't, so we fall back to chat_id prefix detection.
- Telegram: ``chat.type`` is ``"private"|"group"|"supergroup"``.
  Hermes' adapter sets ``source.chat_type`` accordingly.
- WhatsApp / Slack: future Phase 6.x — not pilot-blocking, return
  ``"p2p"`` by default until adapter exposes a reliable signal.

``classify_chat()`` reads in priority: source.chat_type → source.is_group
→ source.chat_id prefix heuristics. Each PHASE 6 caller passes the
hermes event directly, and we duck-type defensively.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import storage

logger = logging.getLogger(__name__)


_GROUPS_SCHEMA_VERSION = 1
_VALID_MODES = ("review-only", "everyday", "both")


# ---------------------------------------------------------------------------
# Chat-type detection
# ---------------------------------------------------------------------------


# Lark / Feishu group chat IDs start with oc_ ; so do P2P chats with a bot.
# However, when a Lark adapter is well-implemented, source.chat_type carries
# "p2p" vs "group". We trust source.chat_type when present.
_LARK_GROUP_HINTS = ("group",)

# Telegram private chat IDs are positive integers; groups are negative.
_TG_GROUP_TYPES = ("group", "supergroup", "channel")
_TG_PRIVATE_TYPES = ("private",)


def classify_chat(event: Any) -> str:
    """Return ``"p2p"`` or ``"group"`` for *event*.

    Defensive duck-typing — handles missing / partial source objects
    by defaulting to ``"p2p"`` (safest assumption: existing flows
    continue to work).
    """
    if event is None:
        return "p2p"
    source = getattr(event, "source", None)
    if source is None:
        return "p2p"

    # Highest-priority signal: explicit chat_type
    chat_type = (getattr(source, "chat_type", None) or "")
    if isinstance(chat_type, str):
        ct = chat_type.lower().strip()
        if ct in _TG_GROUP_TYPES or ct in _LARK_GROUP_HINTS or ct == "group":
            return "group"
        if ct in _TG_PRIVATE_TYPES or ct == "p2p":
            return "p2p"

    # Next: explicit is_group bool
    is_group = getattr(source, "is_group", None)
    if isinstance(is_group, bool):
        return "group" if is_group else "p2p"

    # Last resort: heuristic on chat_id (negative TG ids = groups)
    chat_id_raw = getattr(source, "chat_id", "") or ""
    chat_id = str(chat_id_raw).strip()
    if chat_id.startswith("-"):
        return "group"
    # Lark chat_id is oc_xxx for both group and DM-with-bot — we can't
    # disambiguate without chat_type. Default to p2p to avoid disrupting
    # existing P2P-only pilots.
    return "p2p"


def get_group_key(event: Any) -> str:
    """Stable per-group key combining platform + chat_id.

    Returns "" when *event* lacks a usable platform/chat_id (caller
    should skip group config lookup in that case).
    """
    if event is None:
        return ""
    source = getattr(event, "source", None)
    if source is None:
        return ""
    plat_val = getattr(source, "platform", None)
    platform = (
        getattr(plat_val, "value", str(plat_val)) if plat_val else ""
    ) or ""
    chat_id = str(getattr(source, "chat_id", "") or "")
    if not platform or not chat_id:
        return ""
    return f"{platform.lower()}_{chat_id}"


# ---------------------------------------------------------------------------
# GroupConfig dataclass + persistence
# ---------------------------------------------------------------------------


@dataclass
class GroupConfig:
    group_key: str
    platform: str
    group_id: str
    enabled: bool = False
    mode: str = "review-only"
    owner_user_id: str = ""
    display_name: str = ""
    created_at: str = ""
    updated_at: str = ""
    schema_version: int = _GROUPS_SCHEMA_VERSION


def _groups_dir() -> Path:
    return storage.PAID_DIR / "groups"


def _group_path(group_key: str) -> Path:
    return _groups_dir() / f"{group_key}.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_group_config(group_key: str) -> GroupConfig | None:
    """Read per-group config. None if not configured."""
    if not group_key:
        return None
    data = storage.read_json(_group_path(group_key))
    if data is None:
        return None
    return GroupConfig(
        group_key=str(data.get("group_key") or group_key),
        platform=str(data.get("platform", "") or ""),
        group_id=str(data.get("group_id", "") or ""),
        enabled=bool(data.get("enabled", False)),
        mode=str(data.get("mode") or "review-only"),
        owner_user_id=str(data.get("owner_user_id") or ""),
        display_name=str(data.get("display_name") or ""),
        created_at=str(data.get("created_at") or ""),
        updated_at=str(data.get("updated_at") or ""),
        schema_version=int(data.get("schema_version") or _GROUPS_SCHEMA_VERSION),
    )


def save_group_config(cfg: GroupConfig) -> GroupConfig:
    if not cfg.group_key:
        raise ValueError("group_key must be non-empty")
    if cfg.mode not in _VALID_MODES:
        raise ValueError(f"mode must be one of {_VALID_MODES}; got {cfg.mode!r}")
    if not cfg.created_at:
        cfg.created_at = _now_iso()
    cfg.updated_at = _now_iso()
    storage.write_json(_group_path(cfg.group_key), asdict(cfg))
    return cfg


def list_group_configs() -> list[GroupConfig]:
    root = _groups_dir()
    if not root.exists():
        return []
    out: list[GroupConfig] = []
    for child in sorted(root.iterdir()):
        if not child.is_file() or not child.name.endswith(".json"):
            continue
        data = storage.read_json(child)
        if not data:
            continue
        try:
            out.append(GroupConfig(**{
                "group_key": str(data.get("group_key") or child.stem),
                "platform": str(data.get("platform", "") or ""),
                "group_id": str(data.get("group_id", "") or ""),
                "enabled": bool(data.get("enabled", False)),
                "mode": str(data.get("mode") or "review-only"),
                "owner_user_id": str(data.get("owner_user_id") or ""),
                "display_name": str(data.get("display_name") or ""),
                "created_at": str(data.get("created_at") or ""),
                "updated_at": str(data.get("updated_at") or ""),
                "schema_version": int(
                    data.get("schema_version") or _GROUPS_SCHEMA_VERSION
                ),
            }))
        except Exception as exc:
            logger.warning(
                "[group_routing] skipping malformed group config %s: %s",
                child, exc,
            )
    return out


def delete_group_config(group_key: str) -> bool:
    """Remove on-disk config. Returns True if a file was deleted."""
    if not group_key:
        return False
    p = _group_path(group_key)
    if not p.exists():
        return False
    try:
        p.unlink()
        return True
    except OSError as exc:
        logger.warning("[group_routing] delete %s failed: %s", p, exc)
        return False


# ---------------------------------------------------------------------------
# Routing decision
# ---------------------------------------------------------------------------


_REVIEW_CMD_RE = re.compile(r"^/r(?:eview)?(?:\s|$)", re.IGNORECASE)


def classify_routing(event: Any, text: str = "") -> str:
    """Return one of:
      ``"p2p"`` — DM; pre_gateway_dispatch should proceed as before.
      ``"group_disabled"`` — group not configured / disabled; drop.
      ``"group_review"`` — group enabled in review-only mode; only
            /review-prefixed messages and active-session replies are
            allowed. Caller still has to check active-session.
      ``"group_everyday"`` — enabled in everyday mode; let dispatch
            proceed normally for now (Phase 6 reserves; Phase 6.x
            wires the actual Claude flow).
      ``"group_both"`` — enabled in both modes; treat as everyday but
            still notice /review.
    """
    if classify_chat(event) != "group":
        return "p2p"

    key = get_group_key(event)
    if not key:
        # Group chat but we couldn't identify which group → safest is to drop
        return "group_disabled"

    cfg = load_group_config(key)
    if cfg is None or not cfg.enabled:
        return "group_disabled"

    if cfg.mode == "review-only":
        # Only /review or active-session traffic should proceed. Active-session
        # check happens in caller (needs counterparty lookup); here we only
        # gate the prefix.
        if text and _REVIEW_CMD_RE.match(text.lstrip()):
            return "group_review"
        return "group_review"
    if cfg.mode == "everyday":
        return "group_everyday"
    if cfg.mode == "both":
        return "group_both"
    # Unknown mode (forward-compat) — be conservative
    return "group_disabled"


# ---------------------------------------------------------------------------
# Reply-in-thread helpers
# ---------------------------------------------------------------------------


def extract_message_id(event: Any) -> str:
    """Pull the inbound message_id off *event* so caller can reply in
    thread / quote. Returns "" when not available."""
    if event is None:
        return ""
    # Direct attribute
    mid = getattr(event, "message_id", "") or ""
    if mid:
        return str(mid)
    source = getattr(event, "source", None)
    if source is None:
        return ""
    return str(getattr(source, "message_id", "") or "")
