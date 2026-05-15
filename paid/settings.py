"""Module Set — runtime configuration loaded from ``~/.hermes/paid/settings.json``.

Single source of truth for PAID's tunables. Modules import the helpers below
instead of hard-coding constants, so an operator can adjust behaviour without
patching code:

  - ``confidence_threshold_direct`` — minimum classifier confidence (in_scope +
    stakes=low) needed to skip the approval queue and answer directly.
  - ``approval_timeout_minutes`` — how long a pending approval is allowed to
    sit before ``bin/sweep_pending.py`` flips it to ``timed_out``.
  - ``llm_retry_backoffs_seconds`` — list of sleep durations between retry
    attempts on transient LLM failures.
  - ``model_override`` — per-PAID model name (passed to hermes_io.call_llm
    via classifier when set; empty string means "use Hermes default").
  - ``update_mode`` — placeholder for future J5 dashboard.

All getters cache nothing: settings.json edits take effect on next call.
This is fine — reads are cheap (small JSON, fsynced) and operators expect
the file to be the ground truth.
"""

from __future__ import annotations

from typing import Any

from . import storage


_DEFAULTS: dict[str, Any] = {
    "schema_version": 1,
    "confidence_threshold_direct": 0.75,
    "approval_timeout_minutes": 30,
    "llm_retry_backoffs_seconds": [0.5, 1.5, 4.0],
    "model_override": "",
    "update_mode": "confirm-each",
    "media_enrichment_mode": "off",
}


def _settings_path():
    return storage.PAID_DIR / "settings.json"


def load() -> dict[str, Any]:
    """Read settings.json into a dict, layered on top of ``_DEFAULTS``.

    Missing or malformed file → defaults only. This is forgiving on purpose:
    PAID must keep working even when an operator typos JSON.
    """
    data = storage.read_json(_settings_path()) or {}
    out = dict(_DEFAULTS)
    out.update(data)
    return out


def confidence_threshold_direct() -> float:
    """Minimum classifier confidence needed for the ``direct`` state.
    Clamped to ``[0.0, 1.0]`` so a typo'd setting can't silently deny everything
    or auto-approve the world."""
    raw = load().get("confidence_threshold_direct", _DEFAULTS["confidence_threshold_direct"])
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return _DEFAULTS["confidence_threshold_direct"]
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


def approval_timeout_seconds() -> float:
    """Seconds a pending approval may sit before being auto-marked ``timed_out``.
    Negative or non-numeric values disable the sweeper for that operator."""
    raw = load().get("approval_timeout_minutes", _DEFAULTS["approval_timeout_minutes"])
    try:
        minutes = float(raw)
    except (TypeError, ValueError):
        minutes = float(_DEFAULTS["approval_timeout_minutes"])
    return max(0.0, minutes * 60.0)


def llm_retry_backoffs() -> tuple[float, ...]:
    """Tuple of backoff seconds for ``hermes_io._post_with_retry``.
    Empty tuple disables retries entirely."""
    raw = load().get("llm_retry_backoffs_seconds", _DEFAULTS["llm_retry_backoffs_seconds"])
    if not isinstance(raw, list):
        return tuple(_DEFAULTS["llm_retry_backoffs_seconds"])
    out: list[float] = []
    for x in raw:
        try:
            v = float(x)
        except (TypeError, ValueError):
            continue
        if v < 0.0:
            continue
        out.append(v)
    return tuple(out) if out else tuple(_DEFAULTS["llm_retry_backoffs_seconds"])


def model_override() -> str:
    """Optional model name override (empty string = use Hermes default)."""
    raw = load().get("model_override", "")
    return str(raw or "").strip()


def media_enrichment_mode() -> str:
    """OCR mode for non-review media enrichment.

    Valid values: "off" (default), "tesseract", "pdftotext", "both".
    Any unrecognised value returns "off" so a typo is always safe.
    """
    raw = str(load().get("media_enrichment_mode", "off") or "").strip().lower()
    if raw in ("tesseract", "pdftotext", "both"):
        return raw
    return "off"


def update_mode() -> str:
    """One of: confirm-each / auto-direct / silent-only — for future use."""
    return str(load().get("update_mode", _DEFAULTS["update_mode"]))
