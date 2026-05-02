"""Module I — owner detection + counterparty profile load/create."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from pathlib import Path

from . import storage


@dataclass
class Owner:
    owner_id: str
    identities: list[dict]  # [{"platform": "telegram", "user_id": "..."}]
    name: str = ""  # display name shown to counterparties; falls back to owner_id


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


def _owner_path() -> Path:
    return storage.PAID_DIR / "owner.json"


def _cp_id(platform: str, user_id: str) -> str:
    return f"{platform}_{user_id}"


def _cp_profile_path(cp_id: str) -> Path:
    return storage.PAID_DIR / "counterparties" / cp_id / "profile.json"


def load_owner() -> Owner | None:
    """Read owner.json. Return None if missing or malformed."""
    data = storage.read_json(_owner_path())
    if data is None:
        return None
    owner_id = data.get("owner_id", "")
    identities = data.get("identities", [])
    if not isinstance(identities, list):
        identities = []
    name = data.get("name", "") or ""
    return Owner(owner_id=owner_id, identities=identities, name=name)


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
    )


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
