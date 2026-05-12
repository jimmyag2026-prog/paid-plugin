"""Module I — owner detection + counterparty profile load/create.

Schema notes
~~~~~~~~~~~~
Counterparty profile.json is **schema_version=2** as of W2 batch 1
(2026-05-02): added ``active_review_session`` and ``review_history`` for
paid-review skill integration.

Owner.json is **schema_version=2** as of multiplatform v0.1 (2026-05-03):
each identity gains optional ``home_chat_id`` (where to send PAID DMs;
distinct from ``user_id`` on Lark / Slack) and ``enabled`` (let owner
temporarily mute a platform without removing the identity entry). Owner
also gains ``preferred_platform`` (which identity to prefer when fanning
out approval cards / fatal alerts).

Both readers tolerate v1 records — missing fields default in (``home_chat_id
=user_id``, ``enabled=True``, ``preferred_platform=identities[0].platform``).
A migration helper ``bin/migrate_owner_v1_to_v2.py`` upgrades on disk.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

from . import storage


_OWNER_SCHEMA_VERSION = 2


def _is_lark_routable(uid: str) -> bool:
    """True iff ``uid`` is in a form Lark's IM API accepts directly.

    ``ou_`` = open_id, ``oc_`` = chat_id, ``on_`` = union_id, email = email.
    hermes_io._detect_lark_receive_id_type maps these to the right
    ``receive_id_type`` and ``_send_lark_direct`` bypasses the adapter's
    hard-coded chat_id, so any of these works without operator setup.
    """
    if not uid:
        return False
    if uid.startswith(("ou_", "oc_", "on_")):
        return True
    if "@" in uid:
        tail = uid.split("@", 1)[1]
        if "." in tail:
            return True
    return False


def resolve_owner_lark_target(fallback_user_id: str) -> str:
    """Pick the best Lark/feishu receive_id for owner DM delivery.

    Precedence:
      1. ``fallback_user_id`` if already Lark-routable (caller passed a
         well-formed identity from preferred_identity()).
      2. Any enabled feishu/lark identity in owner.json whose ``user_id``
         is Lark-routable — handles the common case where
         preferred_identity() returned a v1 bare-hex identity that comes
         first in the list but a routable ou_/oc_/email exists later.
      3. ``FEISHU_HOME_CHANNEL`` env var — legacy ``/sethome`` workaround
         for owner.json that has no routable identity at all.
      4. ``fallback_user_id`` unchanged — best effort; Lark will reject
         with [230001] and the call site will surface the error.

    This helper is the SINGLE source of truth for owner Lark routing.
    ``__init__._alert_owner``, ``__init__._notify_owner_about_request``
    (via ``_resolve_owner_send_target``), and ``bin/sweep_pending.py``
    all delegate here so behavior stays consistent.

    Why this matters: pre-v1.2.4, FEISHU_HOME_CHANNEL silently won
    against owner.json. If ``/sethome`` was run from a wrong chat (a
    counterparty's DM with the bot, or a group chat the counterparty
    can see), every J3 approval card leaked to that chat. After this
    fix, an ``ou_`` open_id in owner.json always wins — operator setup
    error can't override a correctly-configured identity.
    """
    if _is_lark_routable(fallback_user_id):
        return fallback_user_id
    owner = load_owner()
    if owner is not None:
        for ident in owner.enabled_identities():
            if ident.platform in ("feishu", "lark") and _is_lark_routable(ident.user_id):
                return ident.user_id
    chat_id = (os.environ.get("FEISHU_HOME_CHANNEL") or "").strip()
    if chat_id:
        return chat_id
    return fallback_user_id


@dataclass
class OwnerIdentity:
    """One (platform, user_id) record on the owner profile, with v2 fields.

    ``home_chat_id`` is where PAID actually DMs the owner (e.g. on Lark
    that's the bot↔owner chat, not the bare user_id which the adapter's
    send() rejects with [230001]). Default: same as user_id (correct for
    Telegram private chats; legacy v1 behaviour for everywhere else).

    ``enabled`` lets the owner mute a platform temporarily — disabled
    identities still appear in owner.json (so re-enabling is one bool flip)
    but are skipped by ``preferred_identity()`` and ``enabled_identities()``.
    """

    platform: str
    user_id: str
    home_chat_id: str = ""
    enabled: bool = True
    name: str = ""

    def __post_init__(self):
        if not self.home_chat_id:
            self.home_chat_id = self.user_id


@dataclass
class Owner:
    owner_id: str
    identities: list[dict]  # [{"platform", "user_id", "home_chat_id"?, "enabled"?, "name"?}]
    name: str = ""  # display name shown to counterparties; falls back to owner_id
    # v2 fields (default-safe so v1 owner.json loads without migration)
    schema_version: int = _OWNER_SCHEMA_VERSION
    preferred_platform: str = ""  # "" → falls back to first enabled identity

    # ------------------------------------------------------------------
    # v2 helpers — work on raw identities dicts so we don't need to keep
    # OwnerIdentity instances in sync with the persisted JSON.
    # ------------------------------------------------------------------

    def iter_identities(self) -> list[OwnerIdentity]:
        """Return identities as ``OwnerIdentity`` instances with defaults
        applied. Skips entries that are not dict-shaped."""
        out: list[OwnerIdentity] = []
        for ident in self.identities or []:
            if not isinstance(ident, dict):
                continue
            try:
                out.append(OwnerIdentity(
                    platform=str(ident.get("platform", "")),
                    user_id=str(ident.get("user_id", "")),
                    home_chat_id=str(ident.get("home_chat_id", "") or ""),
                    enabled=bool(ident.get("enabled", True)),
                    name=str(ident.get("name", "") or ""),
                ))
            except Exception:
                continue
        return out

    def enabled_identities(self) -> list[OwnerIdentity]:
        """All identities with ``enabled=True`` and non-empty platform/user_id."""
        return [
            i for i in self.iter_identities()
            if i.enabled and i.platform and i.user_id
        ]

    def preferred_identity(self) -> OwnerIdentity | None:
        """Pick the best identity for outbound owner DM.

        Order:
          1. enabled identity matching ``preferred_platform``
          2. first enabled identity (insertion order)
        Returns None if no enabled identity is configured.
        """
        enabled = self.enabled_identities()
        if not enabled:
            return None
        if self.preferred_platform:
            for i in enabled:
                if i.platform == self.preferred_platform:
                    return i
        return enabled[0]


def display_name(owner: Owner | None) -> str:
    """Best display name for an owner, with fallback chain.

    Priority: owner.name → titlecased owner_id stripped of "owner_" prefix
    → "the owner". Decouples callers from the legacy owner_id-as-name hack.
    """
    if owner is None:
        return "the owner"
    if owner.name.strip():
        return owner.name.strip()
    raw = owner.owner_id or ""
    if raw.startswith("owner_"):
        raw = raw[len("owner_"):]
    return raw.title() if raw else "the owner"


@dataclass
class Counterparty:
    cp_id: str
    platform: str
    user_id: str
    display_name: str
    role: str  # "junior" | "external" | "ignored" | "blocked" | "pending"
    topics_allowed: list[str]
    topics_always_escalate: list[str]
    web_search_allowed: bool
    notes: str
    # When role transitions to "ignored"/"blocked", these record why and when.
    # The dashboard / discovery flow surfaces them so the owner doesn't have
    # to remember why they ignored someone three months ago.
    ignore_reason: str = ""
    ignore_set_at: str = ""    # ISO-8601 UTC; empty when not ignored
    # Set on first inbound message; suppresses re-firing the discovery card
    # if the same sender pings again before owner classifies them.
    discovery_notified_at: str = ""
    # paid-review skill integration. Holds the session id of the currently
    # open review session for this counterparty (at most one concurrent
    # session per cp in v0.1). When set, the plugin routes inbound messages
    # to the skill's QA loop instead of running classification afresh.
    active_review_session: str = ""
    # Closed-session breadcrumbs. Append-only; bounded length so a chatty
    # counterparty doesn't blow up the profile json. The dashboard pulls
    # full session bodies from sessions/_closed/ on demand.
    review_history: list[dict] = field(default_factory=list)


def _owner_path() -> Path:
    return storage.PAID_DIR / "owner.json"


def _cp_id(platform: str, user_id: str) -> str:
    return f"{platform}_{user_id}"


def _cp_profile_path(cp_id: str) -> Path:
    return storage.PAID_DIR / "counterparties" / cp_id / "profile.json"


def load_owner() -> Owner | None:
    """Read owner.json. Return None if missing or malformed.

    Backward-compatible reader: v1 records (no ``schema_version``) load
    cleanly with ``preferred_platform`` empty (which makes
    ``preferred_identity()`` fall back to the first enabled identity —
    matches v1 ``_owner_primary_identity`` semantics)."""
    data = storage.read_json(_owner_path())
    if data is None:
        return None
    owner_id = data.get("owner_id", "")
    identities = data.get("identities", [])
    if not isinstance(identities, list):
        identities = []
    name = data.get("name", "") or ""
    schema_version = int(data.get("schema_version", 1) or 1)
    preferred_platform = str(data.get("preferred_platform", "") or "")
    return Owner(
        owner_id=owner_id,
        identities=identities,
        name=name,
        schema_version=schema_version,
        preferred_platform=preferred_platform,
    )


def save_owner(owner: Owner) -> None:
    """Persist an Owner back to owner.json — always writes v2 schema."""
    payload = {
        "schema_version": _OWNER_SCHEMA_VERSION,
        "owner_id": owner.owner_id,
        "name": owner.name,
        "preferred_platform": owner.preferred_platform,
        "identities": owner.identities,
    }
    storage.write_json(_owner_path(), payload)


def is_owner(platform: str, sender_id: str) -> bool:
    """True if (platform, sender_id) appears in owner.identities."""
    owner = load_owner()
    if owner is None:
        return False
    for ident in owner.identities:
        if not isinstance(ident, dict):
            continue
        if (
            str(ident.get("platform", "")) == platform
            and str(ident.get("user_id", "")) == sender_id
        ):
            return True
    return False


def load_counterparty(platform: str, sender_id: str) -> Counterparty | None:
    """Load existing counterparty profile, or None if not yet created."""
    cp_id = _cp_id(platform, sender_id)
    data = storage.read_json(_cp_profile_path(cp_id))
    if data is None:
        return None
    raw_history = data.get("review_history", [])
    if not isinstance(raw_history, list):
        raw_history = []
    return Counterparty(
        cp_id=data.get("cp_id", cp_id),
        platform=data.get("platform", platform),
        user_id=data.get("user_id", sender_id),
        display_name=data.get("display_name", ""),
        role=data.get("role", "pending"),
        topics_allowed=list(data.get("topics_allowed", [])),
        topics_always_escalate=list(
            data.get(
                "topics_always_escalate",
                ["equity", "salary", "hiring", "customer", "finance"],
            )
        ),
        web_search_allowed=bool(data.get("web_search_allowed", True)),
        notes=data.get("notes", ""),
        ignore_reason=data.get("ignore_reason", ""),
        ignore_set_at=data.get("ignore_set_at", ""),
        discovery_notified_at=data.get("discovery_notified_at", ""),
        active_review_session=data.get("active_review_session", "") or "",
        review_history=[h for h in raw_history if isinstance(h, dict)],
    )


def save_counterparty(cp: Counterparty) -> None:
    """Persist a Counterparty back to its profile.json."""
    storage.write_json(_cp_profile_path(cp.cp_id), asdict(cp))


def list_all_counterparties() -> list[Counterparty]:
    """Walk ``counterparties/`` and load every profile (impersonation lookup,
    dashboard, etc). Skips unreadable / non-dir entries silently."""
    root = storage.PAID_DIR / "counterparties"
    if not root.exists():
        return []
    out: list[Counterparty] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        data = storage.read_json(child / "profile.json")
        if not data:
            continue
        try:
            cp = load_counterparty(
                str(data.get("platform", "")),
                str(data.get("user_id", "")),
            )
        except Exception:
            continue
        if cp is not None:
            out.append(cp)
    return out


def detect_impersonation(cp: Counterparty) -> Counterparty | None:
    """Return another known counterparty whose ``display_name`` matches *cp*'s
    name (case-insensitive) on a DIFFERENT platform. Returns None if no clash.

    This lets PAID flag "someone calling themselves Alice on Telegram when
    we already have an Alice on Lark" so the owner doesn't auto-treat them
    as the same trust level.
    """
    if not cp.display_name.strip():
        return None
    needle = cp.display_name.strip().lower()
    for other in list_all_counterparties():
        if other.cp_id == cp.cp_id:
            continue
        if other.platform == cp.platform:
            continue
        if other.display_name.strip().lower() == needle:
            return other
    return None


# --------------------------------------------------------------------------
# paid-review skill helpers
# --------------------------------------------------------------------------

# How many closed-session entries to keep on the counterparty profile. Older
# ones get rotated out — full bodies live in sessions/_closed/<month>/<sid>/.
_REVIEW_HISTORY_MAX = 20


class ReviewSessionConflict(Exception):
    """Raised when set_active_review_session is called on a cp that already
    has an active session id, unless ``replace=True`` is passed."""


def set_active_review_session(
    cp: Counterparty, sid: str, *, replace: bool = False
) -> Counterparty:
    """Mark *cp* as having an open review session ``sid``. Persists profile.

    By default refuses to overwrite an existing active session — passes
    ``replace=True`` to force (e.g. when owner ``/review close`` already ran
    but cleanup got interrupted).
    """
    if not sid:
        raise ValueError("sid must be non-empty")
    if cp.active_review_session and cp.active_review_session != sid and not replace:
        raise ReviewSessionConflict(
            f"counterparty {cp.cp_id} already has active review session "
            f"{cp.active_review_session!r}; pass replace=True to overwrite"
        )
    cp.active_review_session = sid
    save_counterparty(cp)
    return cp


def clear_active_review_session(
    cp: Counterparty, *, archive: dict | None = None
) -> Counterparty:
    """Clear the active session pointer; optionally append a history record.

    ``archive`` is a free-form dict the caller (skill ``deliver``) supplies —
    typical fields: ``sid``, ``subject``, ``verdict``, ``rounds``,
    ``closed_at``. We stamp ``closed_at`` here if the caller forgot.
    Old entries beyond ``_REVIEW_HISTORY_MAX`` are rotated out.
    """
    cp.active_review_session = ""
    if archive:
        record = dict(archive)
        record.setdefault(
            "closed_at",
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        cp.review_history.append(record)
        if len(cp.review_history) > _REVIEW_HISTORY_MAX:
            cp.review_history = cp.review_history[-_REVIEW_HISTORY_MAX:]
    save_counterparty(cp)
    return cp


def mark_ignored(cp: Counterparty, reason: str) -> Counterparty:
    """Transition ``cp.role`` to ``ignored``, recording reason + UTC timestamp.
    Returns the saved counterparty."""
    cp.role = "ignored"
    cp.ignore_reason = reason or "(no reason given)"
    cp.ignore_set_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_counterparty(cp)
    return cp


def ensure_counterparty(
    platform: str, sender_id: str, display_name: str = ""
) -> Counterparty:
    """Load existing counterparty, or create one with default pending profile."""
    existing = load_counterparty(platform, sender_id)
    if existing is not None:
        # Backfill display_name if it was empty and we now have one
        if display_name and not existing.display_name:
            existing.display_name = display_name
            storage.write_json(_cp_profile_path(existing.cp_id), asdict(existing))
        return existing

    cp_id = _cp_id(platform, sender_id)
    cp = Counterparty(
        cp_id=cp_id,
        platform=platform,
        user_id=sender_id,
        display_name=display_name,
        role="pending",
        topics_allowed=[],
        topics_always_escalate=["equity", "salary", "hiring", "customer", "finance"],
        web_search_allowed=True,
        notes="",
    )
    storage.write_json(_cp_profile_path(cp_id), asdict(cp))
    return cp
