#!/usr/bin/env python3
"""bin/migrate_to_owner_profile.py — one-shot migration from v1.5.x
scattered config files to v1.6.x canonical Owner Profile.

Reads:
  ~/.hermes/paid/owner.json     (v1 or v2)
  ~/.hermes/paid/persona.md     (freeform; LLM extracts structured voice fields)
  ~/.hermes/paid/sop.md         (freeform; LLM extracts topic policy)
  ~/.hermes/paid/settings.json  (deterministic key map → preferences)

Writes:
  ~/.hermes/paid/owner_profile.json  (the v1.6 canonical record)

Original files are PRESERVED — derive_from_profile() from v1.6.0 sprint
1 will re-render them in the future, but the migration step is one-way
(profile is new source of truth from here on).

Usage:
  python -m bin.migrate_to_owner_profile [--dry-run] [--force] [--no-llm]

  --dry-run   Print what would be written without writing.
  --force     Overwrite an existing owner_profile.json.
  --no-llm    Skip persona/SOP LLM extraction; use defaults from voice
              preset (founder). Useful for offline/test environments or
              when the LLM is misconfigured.

Exit codes: 0 success, 1 already-exists w/o --force, 2 input dir missing,
3 LLM extraction failure (only when --no-llm not set).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

# Allow `python bin/migrate_to_owner_profile.py` and `python -m bin.migrate_...`
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from paid import profile as p
from paid import storage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLM extraction prompts
# ---------------------------------------------------------------------------


_VOICE_EXTRACT_PROMPT = """\
You are migrating an existing PAID v1.5 persona.md into a v1.6 Owner
Profile. Extract structured fields from the markdown below.

Return ONLY a JSON object with these keys (omit any you cannot infer):
  - tone: one of [direct-friendly, professional, casual, formal, minimal]
          OR a short custom label (max 25 chars)
  - style_notes: 1-2 sentence summary of the owner's stated style
                 preferences (e.g. "Short sentences, no exclamation marks,
                 engineer-style")
  - self_description: 1-2 sentence summary of the owner's self-description
                      (role, background, current focus)
  - do_not_say: list of explicit banned phrases the owner asked the bot
                NOT to use. Empty list if none mentioned.

If the persona.md is empty or has no extractable signal, return:
  {"tone": "direct-friendly", "style_notes": "", "self_description": "", "do_not_say": []}

persona.md content (delimited by ===):
===
{persona_md}
===

Return only the JSON object, no markdown fences, no commentary.
"""


_TOPICS_EXTRACT_PROMPT = """\
You are migrating an existing PAID v1.5 sop.md into a v1.6 Owner Profile.
Extract the topic policy from the markdown below.

Return ONLY a JSON object with these keys (omit any you cannot infer):
  - always_direct: list of topics PAID can answer directly without owner
                   approval (e.g. ["scheduling", "intro", "FAQ"])
  - always_escalate: list of topics that always require owner approval
                     (e.g. ["equity", "salary", "hiring", "customer", "finance"])
  - always_decline: list of topics PAID should refuse entirely
                    (e.g. ["legal advice", "medical advice"])
  - default_blacklist_action: one of ["decline", "request"]
                              (what to do when classifier flags out-of-scope)

If the sop.md is empty, return defaults:
  {"always_direct": [], "always_escalate": ["equity", "salary", "hiring",
                                            "customer", "finance"],
   "always_decline": [], "default_blacklist_action": "decline"}

sop.md content (delimited by ===):
===
{sop_md}
===

Return only the JSON object, no markdown fences, no commentary.
"""


def _llm_extract_voice(persona_md: str) -> dict:
    """Call hermes_io.call_llm for structured voice extraction.
    Falls back to safe defaults on any failure."""
    if not persona_md.strip():
        return {
            "tone": "direct-friendly",
            "style_notes": "",
            "self_description": "",
            "do_not_say": [],
        }
    try:
        from paid import hermes_io
        raw = hermes_io.call_llm(
            prompt=_VOICE_EXTRACT_PROMPT.replace("{persona_md}", persona_md[:8000]),
            system="You extract structured profile fields from markdown.",
        )
        return _parse_json_strict(raw)
    except Exception as exc:
        logger.warning("voice extract failed: %s", exc)
        raise


def _llm_extract_topics(sop_md: str) -> dict:
    """Call hermes_io.call_llm for structured topic extraction.
    Returns canonical 5-topic escalate default on empty / failure (so
    the migration result still matches v1.4.x semantics)."""
    if not sop_md.strip():
        return {
            "always_direct": [],
            "always_escalate": ["equity", "salary", "hiring", "customer", "finance"],
            "always_decline": [],
            "default_blacklist_action": "decline",
        }
    try:
        from paid import hermes_io
        raw = hermes_io.call_llm(
            prompt=_TOPICS_EXTRACT_PROMPT.replace("{sop_md}", sop_md[:8000]),
            system="You extract structured topic policy from markdown.",
        )
        return _parse_json_strict(raw)
    except Exception as exc:
        logger.warning("topics extract failed: %s", exc)
        raise


def _parse_json_strict(raw: str) -> dict:
    """Parse LLM JSON output. Tolerates code-fence wrappers."""
    s = (raw or "").strip()
    # Strip ```json … ``` fence if present
    if s.startswith("```"):
        lines = s.split("\n")
        s = "\n".join(lines[1:-1] if len(lines) > 2 else lines[1:])
        if s.endswith("```"):
            s = s[: s.rfind("```")]
    s = s.strip()
    return json.loads(s)


# ---------------------------------------------------------------------------
# File readers (deterministic — no LLM)
# ---------------------------------------------------------------------------


def _read_owner_json() -> dict:
    """Return dict of {owner_id, name, identities[]} or empty when absent."""
    data = storage.read_json(storage.PAID_DIR / "owner.json")
    if not data:
        return {"owner_id": "owner", "name": "", "identities": []}
    return {
        "owner_id": str(data.get("owner_id", "owner") or "owner"),
        "name": str(data.get("name", "") or ""),
        "identities": [
            d for d in (data.get("identities", []) or []) if isinstance(d, dict)
        ],
    }


def _read_settings_json() -> dict:
    """Map legacy settings.json keys to profile.preferences fields.
    Unknown keys ignored."""
    data = storage.read_json(storage.PAID_DIR / "settings.json") or {}
    return {
        "model_primary": str(data.get("model_override", "") or ""),
        "model_fallback": str(data.get("model_fallback", "") or ""),
        "daily_cost_cap_usd": float(data.get("daily_cost_cap_usd", 5.0) or 5.0),
        "review_max_rounds": int(data.get("review_max_rounds", 3) or 3),
        "ocr_languages": str(data.get("ocr_languages", "chi_sim+eng") or "chi_sim+eng"),
        "update_mode": str(data.get("update_mode", "confirm-each") or "confirm-each"),
    }


def _read_md(filename: str) -> str:
    path = storage.PAID_DIR / filename
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Migration core
# ---------------------------------------------------------------------------


def migrate(*, dry_run: bool = False, force: bool = False,
            use_llm: bool = True) -> dict:
    """Run the migration. Returns an audit dict::

        {
          "profile_path": "/path/to/owner_profile.json",
          "wrote": bool,
          "sources_read": {
            "owner.json": bool, "persona.md": bool,
            "sop.md": bool, "settings.json": bool,
          },
          "extract": {
            "voice_used_llm": bool,
            "topics_used_llm": bool,
          },
          "warnings": [str, ...],
        }

    Raises:
        FileExistsError  — owner_profile.json exists and force=False
        RuntimeError     — LLM extraction failed (only if use_llm=True)
    """
    audit: dict = {
        "profile_path": str(storage.PAID_DIR / "owner_profile.json"),
        "wrote": False,
        "sources_read": {},
        "extract": {"voice_used_llm": False, "topics_used_llm": False},
        "warnings": [],
    }

    profile_path = storage.PAID_DIR / "owner_profile.json"
    if profile_path.exists() and not force:
        raise FileExistsError(
            f"{profile_path} already exists. Use --force to overwrite, "
            "or back it up and remove first."
        )

    owner_data = _read_owner_json()
    settings_data = _read_settings_json()
    persona_md = _read_md("persona.md")
    sop_md = _read_md("sop.md")
    audit["sources_read"] = {
        "owner.json": bool(owner_data["identities"]),
        "persona.md": bool(persona_md.strip()),
        "sop.md": bool(sop_md.strip()),
        "settings.json": (storage.PAID_DIR / "settings.json").exists(),
    }

    # 1. Identities + name + owner_id — deterministic
    profile = p.OwnerProfile(owner_id=owner_data["owner_id"])
    profile.name = owner_data["name"]
    profile.identities = owner_data["identities"]

    # 2. Preferences — deterministic
    profile.preferences = p.Preferences(
        model_primary=settings_data["model_primary"],
        model_fallback=settings_data["model_fallback"],
        daily_cost_cap_usd=settings_data["daily_cost_cap_usd"],
        review_max_rounds=settings_data["review_max_rounds"],
        ocr_languages=settings_data["ocr_languages"],
        update_mode=settings_data["update_mode"],
    )

    # 3. Voice — LLM or defaults
    if use_llm:
        try:
            voice_dict = _llm_extract_voice(persona_md)
            audit["extract"]["voice_used_llm"] = True
        except Exception as exc:
            audit["warnings"].append(f"voice LLM extract failed: {exc}")
            raise RuntimeError(f"voice extract failed: {exc}") from exc
    else:
        voice_dict = {
            "tone": "direct-friendly",
            "style_notes": "",
            "self_description": "",
            "do_not_say": [],
        }
    profile.voice = p.Voice(
        tone=str(voice_dict.get("tone", "direct-friendly") or "direct-friendly"),
        style_notes=str(voice_dict.get("style_notes", "") or ""),
        self_description=str(voice_dict.get("self_description", "") or ""),
        do_not_say=list(voice_dict.get("do_not_say", []) or []),
    )

    # 4. Topics — LLM or defaults
    if use_llm:
        try:
            topics_dict = _llm_extract_topics(sop_md)
            audit["extract"]["topics_used_llm"] = True
        except Exception as exc:
            audit["warnings"].append(f"topics LLM extract failed: {exc}")
            raise RuntimeError(f"topics extract failed: {exc}") from exc
    else:
        topics_dict = {
            "always_direct": [],
            "always_escalate": ["equity", "salary", "hiring", "customer", "finance"],
            "always_decline": [],
            "default_blacklist_action": "decline",
        }
    profile.topics = p.Topics(
        always_direct=list(topics_dict.get("always_direct", []) or []),
        always_escalate=list(topics_dict.get(
            "always_escalate",
            ["equity", "salary", "hiring", "customer", "finance"],
        ) or []),
        always_decline=list(topics_dict.get("always_decline", []) or []),
        default_blacklist_action=str(
            topics_dict.get("default_blacklist_action", "decline") or "decline"
        ),
    )

    # 5. Pre-set preferred_language: 'auto' — let v1.5.3 per-cp detection drive
    profile.preferred_language = "auto"

    if dry_run:
        audit["dry_run_payload"] = asdict(profile)
        return audit

    p.save_profile(profile)
    audit["wrote"] = True
    return audit


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Migrate v1.5.x PAID config files to v1.6 owner_profile.json",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print would-write payload, don't write")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite existing owner_profile.json")
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip LLM extraction; use safe defaults for voice + topics")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress info logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    if not storage.PAID_DIR.exists():
        print(f"PAID_DIR not found: {storage.PAID_DIR}", file=sys.stderr)
        return 2

    try:
        audit = migrate(
            dry_run=args.dry_run,
            force=args.force,
            use_llm=not args.no_llm,
        )
    except FileExistsError as exc:
        print(f"abort: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"abort: {exc}", file=sys.stderr)
        return 3

    print("=== migration audit ===")
    print(json.dumps({k: v for k, v in audit.items()
                      if k != "dry_run_payload"}, indent=2, ensure_ascii=False))
    if args.dry_run:
        print()
        print("=== dry-run profile payload ===")
        print(json.dumps(audit.get("dry_run_payload", {}), indent=2, ensure_ascii=False))
    else:
        print()
        print(f"  ✓ wrote {audit['profile_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
