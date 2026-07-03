"""Module PS — render Owner Profile into legacy file surface (v1.6.0).

The single canonical ``owner_profile.json`` is the source of truth; this
module's ``derive_from_profile()`` writes it OUT to the legacy 4-file
surface that the rest of PAID still reads:

  - ``owner.json``        — identities + preferred_platform (read by every IM hook)
  - ``persona.md``        — voice template (read by classifier + reply formatter)
  - ``sop.md``            — topic policy (read by retrieval module)
  - ``settings.json``     — preferences (read by cost / hermes_io / decision)

Design contract:
  - **Idempotent** — calling derive() twice with same profile produces
    bit-identical output.
  - **Non-destructive merge** — when a legacy file already has hand-edited
    content NOT representable in the profile (e.g. extra prose in persona.md),
    we preserve it under a marked section. profile fields take precedence
    inside the marked section.
  - **Never writes secrets** — FEISHU_APP_SECRET etc. NEVER flow through
    profile, NEVER end up in derived files.
  - **Safe to call from hook context** — fast (file IO only, no LLM), no
    network, no locks beyond the per-file atomic write.

Called from:
  1. ``/paid-setup`` wizard at completion
  2. Owner confirms a captured profile update (v1.6.2)
  3. Background learner applies an auto-update (v1.6.3)
  4. Manual ``/paid-resync`` slash command (v1.6.0 ships this too)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import storage
from .profile import OwnerProfile, save_profile

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Marker tokens — used to find/preserve owner hand-edits inside derived files
# ---------------------------------------------------------------------------

_PERSONA_MANAGED_START = "<!-- paid:profile-managed:start -->"
_PERSONA_MANAGED_END = "<!-- paid:profile-managed:end -->"

_SOP_MANAGED_START = "<!-- paid:profile-managed:start -->"
_SOP_MANAGED_END = "<!-- paid:profile-managed:end -->"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def derive_from_profile(profile: OwnerProfile) -> dict[str, Any]:
    """Write profile out to legacy files. Returns audit dict::

        {
          "wrote": ["owner.json", "persona.md", "sop.md", "settings.json"],
          "skipped": [],   # files preserved as-is (hand-edited, no managed section)
          "synced_at": ISO timestamp,
        }

    Stamps ``profile.synced_to_files_at`` and persists profile.
    """
    audit = {"wrote": [], "skipped": [], "synced_at": _now_iso()}

    _write_owner_json(profile, audit)
    _write_persona_md(profile, audit)
    _write_sop_md(profile, audit)
    _write_settings_json(profile, audit)

    profile.synced_to_files_at = audit["synced_at"]
    save_profile(profile)
    return audit


# ---------------------------------------------------------------------------
# Per-file writers
# ---------------------------------------------------------------------------


def _write_owner_json(profile: OwnerProfile, audit: dict) -> None:
    """owner.json schema is owned by ``paid.identity`` (v2 schema). Render
    from profile while preserving v2 field shape so existing readers don't
    notice anything changed.

    Preferred-platform resolution order (v1.7.0):
      1. profile.preferred_platform if it names an enabled identity
      2. first enabled identity (legacy v1.6 behavior — zero-regression)
    """
    path = storage.PAID_DIR / "owner.json"
    enabled_platforms = [
        str(ident.get("platform", "") or "")
        for ident in profile.identities
        if isinstance(ident, dict) and ident.get("enabled", True)
        and ident.get("platform")
    ]
    explicit = (profile.preferred_platform or "").strip()
    if explicit and explicit in enabled_platforms:
        preferred = explicit
    else:
        # Legacy fallback: first enabled identity wins.
        preferred = enabled_platforms[0] if enabled_platforms else ""

    payload = {
        "schema_version": 2,
        "owner_id": profile.owner_id,
        "name": profile.name,
        "preferred_platform": preferred,
        "identities": [
            {
                "platform": str(i.get("platform", "")),
                "user_id": str(i.get("user_id", "")),
                "home_chat_id": str(i.get("home_chat_id", "") or i.get("user_id", "")),
                "enabled": bool(i.get("enabled", True)),
                "name": str(i.get("name", "") or ""),
            }
            for i in profile.identities if isinstance(i, dict)
        ],
    }
    storage.write_json(path, payload)
    audit["wrote"].append("owner.json")


def _write_persona_md(profile: OwnerProfile, audit: dict) -> None:
    """persona.md is freeform markdown read by classifier prompts. We render
    the profile-derived section between markers, and preserve anything
    outside the markers from the existing file (so hand-edited prose +
    examples survive derive() calls).
    """
    path = storage.PAID_DIR / "persona.md"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""

    managed_block = _render_persona_block(profile)
    new_content = _merge_managed_block(
        existing,
        managed_block,
        _PERSONA_MANAGED_START,
        _PERSONA_MANAGED_END,
        title="Persona",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_content, encoding="utf-8")
    audit["wrote"].append("persona.md")


def _render_persona_block(profile: OwnerProfile) -> str:
    """Compose the managed-section content for persona.md."""
    lines: list[str] = []
    lines.append("## Voice (managed by paid_profile)")
    lines.append("")
    if profile.name:
        lines.append(f"**Name:** {profile.name}")
    lines.append(f"**Tone:** {profile.voice.tone}")
    lines.append(f"**Preferred reply language:** {profile.preferred_language}")
    if profile.voice.self_description:
        lines.append("")
        lines.append(f"**Self description:** {profile.voice.self_description}")
    if profile.voice.style_notes:
        lines.append("")
        lines.append(f"**Style notes:** {profile.voice.style_notes}")
    if profile.voice.do_not_say:
        lines.append("")
        lines.append("**Never use these phrases:**")
        for phrase in profile.voice.do_not_say:
            lines.append(f"  - `{phrase}`")
    return "\n".join(lines)


def _write_sop_md(profile: OwnerProfile, audit: dict) -> None:
    """sop.md is similar — freeform but with a managed topic policy section."""
    path = storage.PAID_DIR / "sop.md"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""

    managed_block = _render_sop_block(profile)
    new_content = _merge_managed_block(
        existing,
        managed_block,
        _SOP_MANAGED_START,
        _SOP_MANAGED_END,
        title="SOP",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_content, encoding="utf-8")
    audit["wrote"].append("sop.md")


def _render_sop_block(profile: OwnerProfile) -> str:
    lines: list[str] = []
    lines.append("## Topic policy (managed by paid_profile)")
    lines.append("")
    if profile.topics.always_direct:
        lines.append("**Always answer directly (no owner approval):**")
        for t in profile.topics.always_direct:
            lines.append(f"  - {t}")
        lines.append("")
    if profile.topics.always_escalate:
        lines.append("**Always escalate to owner approval:**")
        for t in profile.topics.always_escalate:
            lines.append(f"  - {t}")
        lines.append("")
    if profile.topics.always_decline:
        lines.append("**Always decline (do not answer at all):**")
        for t in profile.topics.always_decline:
            lines.append(f"  - {t}")
        lines.append("")
    lines.append(
        f"**Default blacklist action:** `{profile.topics.default_blacklist_action}` "
        "(per-cp override via Counterparty.blacklist_action)"
    )
    return "\n".join(lines)


def _write_settings_json(profile: OwnerProfile, audit: dict) -> None:
    """settings.json is consumed by paid.cost / paid.hermes_io / paid.decision.
    Merge profile.preferences with existing settings (preserving any fields
    we don't manage — like llm_retry_backoffs_seconds, l4c_enabled, etc.)."""
    path = storage.PAID_DIR / "settings.json"
    existing = storage.read_json(path) or {}

    # Profile-managed fields (write authoritatively from profile)
    managed_fields = {
        "model_override": profile.preferences.model_primary or "",
        "model_fallback": profile.preferences.model_fallback or "",
        "daily_cost_cap_usd": float(profile.preferences.daily_cost_cap_usd),
        "review_max_rounds": int(profile.preferences.review_max_rounds),
        "ocr_languages": profile.preferences.ocr_languages,
        "update_mode": profile.preferences.update_mode,
    }
    # Preserve unmanaged fields (l4c_enabled, retry policy, custom knobs, etc.)
    merged = dict(existing)
    merged.update(managed_fields)
    storage.write_json(path, merged)
    audit["wrote"].append("settings.json")


# ---------------------------------------------------------------------------
# Managed-block merge — preserve hand-edited content outside markers
# ---------------------------------------------------------------------------


def _merge_managed_block(
    existing: str,
    block: str,
    start_marker: str,
    end_marker: str,
    *,
    title: str = "",
) -> str:
    """Replace content between ``start_marker`` and ``end_marker`` with
    ``block``. If markers don't exist in *existing*, append the managed
    section at the end (preserving everything that was there).

    Idempotent: running twice with same inputs produces identical output.
    """
    wrapped = (
        f"{start_marker}\n"
        f"<!-- DO NOT EDIT BELOW — managed by paid.profile_sync.derive_from_profile() -->\n"
        f"<!-- Edit owner_profile.json or use /paid-setup instead. -->\n"
        f"\n{block}\n\n"
        f"{end_marker}"
    )

    if start_marker in existing and end_marker in existing:
        start_idx = existing.index(start_marker)
        end_idx = existing.index(end_marker) + len(end_marker)
        return existing[:start_idx] + wrapped + existing[end_idx:]

    # Append on first call
    suffix = "\n\n" if existing.strip() else ""
    header = f"\n# {title}\n\n" if title and not existing.strip() else ""
    return existing + suffix + header + wrapped + "\n"
