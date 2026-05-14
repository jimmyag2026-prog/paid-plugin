"""Module P — Owner Profile schema + load/save (v1.6.0).

Single canonical record of all owner-side personalization. Replaces the
scattered fan-out across owner.json / persona.md / sop.md / settings.json
as **source of truth**. Those legacy files become derived views written
by ``paid.profile_sync.derive_from_profile``.

Schema design (see docs/v1.6_owner_profile_design.md §Schema):
  - Top-level OwnerProfile dataclass mirrors the JSON layout
  - Nested dataclasses for voice / topics / preferences / observed
  - identities + references are lists of dicts (free-shape, persisted as-is)
  - All fields have safe defaults so a partial profile still loads
  - schema_version stamped on save; load tolerates missing fields

Sensitive material (FEISHU_APP_SECRET, OPENROUTER_API_KEY, etc.) is
**NOT** stored in profile — those live in ``~/.hermes/.env`` and never
get exported. The profile is shareable / committable in theory.

Storage path: ``~/.hermes/paid/owner_profile.json``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import storage

logger = logging.getLogger(__name__)


PROFILE_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Nested dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Voice:
    """Owner's voice / persona attributes. Drives persona.md generation."""
    tone: str = "direct-friendly"
    """One of preset labels or freeform string. Suggested presets:
    direct-friendly / professional / casual / formal / minimal."""

    style_notes: str = ""
    """Freeform owner-written notes on style preferences. E.g.
    '短句，少用感叹号，工程师风格'."""

    self_description: str = ""
    """Short freeform self-description owner wants the bot to embody.
    E.g. 'Founder of X, 10 years infra background, currently shipping v3'."""

    do_not_say: list[str] = field(default_factory=list)
    """Banned phrases — bot should avoid these in cp-facing replies.
    E.g. ['按规定', '依据条款', 'I'm sorry']."""


@dataclass
class Topics:
    """Topic policy. Drives sop.md generation + classifier hints."""
    always_direct: list[str] = field(default_factory=list)
    """Topics PAID can handle directly without owner approval."""

    always_escalate: list[str] = field(default_factory=lambda: [
        "equity", "salary", "hiring", "customer", "finance",
    ])
    """Topics that always require owner approval (J2 request state).
    Default 5 matches v1.4.x baseline `Counterparty.topics_always_escalate`."""

    always_decline: list[str] = field(default_factory=list)
    """Topics PAID should reject outright (J2 decline state)."""

    default_blacklist_action: str = "decline"
    """When classifier flags an out-of-scope topic: 'decline' (reject) or
    'request' (escalate to owner). Per-cp override possible via
    Counterparty.blacklist_action."""


@dataclass
class Preferences:
    """Operational / runtime preferences. Drives settings.json generation."""
    model_primary: str = ""
    """Primary LLM model id (e.g. 'deepseek-v4-pro'). Empty → use
    hermes profile's default."""

    model_fallback: str = ""
    """Fallback LLM when primary fails. Empty → no fallback."""

    daily_cost_cap_usd: float = 5.0
    """Daily LLM spend cap. Reaches → PAID short-circuits to system-
    unavailable reply (v1.5.5 enforce path)."""

    review_max_rounds: int = 3
    """Max QA rounds in paid_review skill before force-close. v1.5
    capped at 5 absolute."""

    ocr_languages: str = "chi_sim+eng"
    """tesseract lang pack spec for image OCR (v1.5 ImageBackend).
    Common values: chi_sim+eng / jpn+eng / kor+eng / eng."""

    update_mode: str = "confirm-each"
    """Owner-side approval card mode: 'confirm-each' / 'auto-direct' /
    'auto-decline'. v1.4.x default."""


@dataclass
class Observed:
    """Usage observations filled by the background learner (v1.6.3).
    Read-only for owner; suggestion engine writes here."""
    approval_rate: float = 0.0
    """Last 30d: fraction of pending approvals owner approved (vs rejected/timed-out)."""

    top_escalated_topics: list[str] = field(default_factory=list)
    """Last 30d: top 5 topics that hit J2 request state."""

    avg_reply_length_chars: int = 0
    """Median char count of owner's approved replies."""

    preferred_decision_window_hrs: float = 0.0
    """Median time from card_sent to approve_clicked."""

    last_updated_at: str = ""
    """ISO-8601 UTC. Learner stamps every run."""


# ---------------------------------------------------------------------------
# Top-level OwnerProfile
# ---------------------------------------------------------------------------


@dataclass
class OwnerProfile:
    """Single canonical owner personalization record."""

    owner_id: str
    name: str = ""
    preferred_language: str = "auto"
    """zh / en / ko / auto. 'auto' → detect per-cp from cp input
    (v1.5.3 behavior). Explicit value forces all replies to use that lang."""

    identities: list[dict] = field(default_factory=list)
    """[{platform, user_id, home_chat_id?, enabled?, name?}, ...]
    Same shape as legacy owner.json identities."""

    voice: Voice = field(default_factory=Voice)
    topics: Topics = field(default_factory=Topics)
    preferences: Preferences = field(default_factory=Preferences)

    references: list[dict] = field(default_factory=list)
    """External context sources owner shared. Each:
    {kind: 'lark_doc'|'web'|'file', url, label, last_synced_at, extracted_topics: []}
    See v1.6.1 design."""

    observed: Observed = field(default_factory=Observed)

    schema_version: int = PROFILE_SCHEMA_VERSION
    created_at: str = ""
    updated_at: str = ""
    synced_to_files_at: str = ""
    """Last time derive_from_profile ran. Useful for "is the legacy
    file surface stale" diagnostic."""


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _profile_path() -> Path:
    return storage.PAID_DIR / "owner_profile.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_profile() -> OwnerProfile | None:
    """Read profile from disk. Returns None when file missing or unreadable.

    Backward-compat tolerant: missing fields fill from dataclass defaults,
    extra fields ignored (so adding fields in a future schema_version
    doesn't break older readers loading newer files).
    """
    data = storage.read_json(_profile_path())
    if data is None:
        return None
    try:
        return _profile_from_dict(data)
    except Exception as exc:
        logger.warning("load_profile: malformed profile.json → %s", exc)
        return None


def save_profile(profile: OwnerProfile) -> OwnerProfile:
    """Persist profile to disk. Stamps timestamps + schema_version.

    Returns the saved profile (timestamps updated in place).
    """
    if not profile.created_at:
        profile.created_at = _now_iso()
    profile.updated_at = _now_iso()
    profile.schema_version = PROFILE_SCHEMA_VERSION
    storage.write_json(_profile_path(), _profile_to_dict(profile))
    return profile


def _profile_from_dict(data: dict[str, Any]) -> OwnerProfile:
    """Reconstruct nested dataclasses from a plain dict (e.g. JSON load)."""
    voice_data = data.get("voice", {}) or {}
    topics_data = data.get("topics", {}) or {}
    prefs_data = data.get("preferences", {}) or {}
    observed_data = data.get("observed", {}) or {}

    return OwnerProfile(
        owner_id=str(data.get("owner_id", "") or ""),
        name=str(data.get("name", "") or ""),
        preferred_language=str(data.get("preferred_language", "auto") or "auto"),
        identities=list(data.get("identities", []) or []),
        voice=Voice(
            tone=str(voice_data.get("tone", "direct-friendly") or "direct-friendly"),
            style_notes=str(voice_data.get("style_notes", "") or ""),
            self_description=str(voice_data.get("self_description", "") or ""),
            do_not_say=list(voice_data.get("do_not_say", []) or []),
        ),
        topics=Topics(
            always_direct=list(topics_data.get("always_direct", []) or []),
            always_escalate=list(topics_data.get(
                "always_escalate",
                ["equity", "salary", "hiring", "customer", "finance"],
            ) or []),
            always_decline=list(topics_data.get("always_decline", []) or []),
            default_blacklist_action=str(
                topics_data.get("default_blacklist_action", "decline")
                or "decline"
            ),
        ),
        preferences=Preferences(
            model_primary=str(prefs_data.get("model_primary", "") or ""),
            model_fallback=str(prefs_data.get("model_fallback", "") or ""),
            daily_cost_cap_usd=float(prefs_data.get("daily_cost_cap_usd", 5.0) or 5.0),
            review_max_rounds=int(prefs_data.get("review_max_rounds", 3) or 3),
            ocr_languages=str(prefs_data.get("ocr_languages", "chi_sim+eng") or "chi_sim+eng"),
            update_mode=str(prefs_data.get("update_mode", "confirm-each") or "confirm-each"),
        ),
        references=list(data.get("references", []) or []),
        observed=Observed(
            approval_rate=float(observed_data.get("approval_rate", 0.0) or 0.0),
            top_escalated_topics=list(observed_data.get("top_escalated_topics", []) or []),
            avg_reply_length_chars=int(observed_data.get("avg_reply_length_chars", 0) or 0),
            preferred_decision_window_hrs=float(
                observed_data.get("preferred_decision_window_hrs", 0.0) or 0.0
            ),
            last_updated_at=str(observed_data.get("last_updated_at", "") or ""),
        ),
        schema_version=int(data.get("schema_version", PROFILE_SCHEMA_VERSION)
                          or PROFILE_SCHEMA_VERSION),
        created_at=str(data.get("created_at", "") or ""),
        updated_at=str(data.get("updated_at", "") or ""),
        synced_to_files_at=str(data.get("synced_to_files_at", "") or ""),
    )


def _profile_to_dict(profile: OwnerProfile) -> dict[str, Any]:
    """Inverse of _profile_from_dict. Uses asdict() for nested cleanup."""
    return asdict(profile)


# ---------------------------------------------------------------------------
# Convenience constructors — preset templates
# ---------------------------------------------------------------------------


_VOICE_PRESETS: dict[str, dict] = {
    "founder": {
        "tone": "direct-friendly",
        "style_notes": "短句，工程师风格；不要 jargon；先说结论后说细节",
        "do_not_say": ["按规定", "依据条款", "请理解"],
    },
    "professional": {
        "tone": "professional",
        "style_notes": "正式但不冷；完整句子；标点准确",
        "do_not_say": ["哈哈", "嗯嗯", "👌"],
    },
    "casual": {
        "tone": "casual-friendly",
        "style_notes": "口语化；可以用表情和短缩写；先体贴语气后正事",
        "do_not_say": ["此致", "敬礼"],
    },
    "minimal": {
        "tone": "minimal",
        "style_notes": "最短回答；3 句话以内；不寒暄",
        "do_not_say": [],
    },
}


def new_profile(
    owner_id: str,
    *,
    name: str = "",
    voice_preset: str = "founder",
    preferred_language: str = "auto",
) -> OwnerProfile:
    """Build a fresh OwnerProfile with a preset voice template applied.

    Used by ``/paid-setup`` wizard's first-question answer + by
    ``bin/migrate_to_owner_profile.py`` when no legacy persona.md exists.
    """
    preset = _VOICE_PRESETS.get(voice_preset, _VOICE_PRESETS["founder"])
    return OwnerProfile(
        owner_id=owner_id,
        name=name,
        preferred_language=preferred_language,
        voice=Voice(
            tone=preset["tone"],
            style_notes=preset["style_notes"],
            do_not_say=list(preset["do_not_say"]),
        ),
    )


def list_voice_presets() -> list[str]:
    """Available voice preset names — for /paid-setup wizard menu."""
    return list(_VOICE_PRESETS.keys())
