"""Module Doc — PAID self-check (v1.5.5 A1).

Seven checks designed to surface the failure modes that lock up onboarding
or silently degrade a running PAID:

  1. hermes_config        — ~/.hermes/config.yaml present + 3 required fields
  2. owner_json           — ~/.hermes/paid/owner.json schema_version=2 + identities
  3. hermes_version       — ctx.register_command available (≥ v0.11)
  4. systemd_timers       — paid-sweep / paid-review-sweep / paid-daily-snapshot active (Linux)
  5. data_files           — audit_log / cost_ledger / pending_approvals writable
  6. settings_schema      — settings.json field types + ranges sane
  7. recent_errors        — no fatal events in audit_log in past 1h

``run_checks()`` returns a structured list — the same shape feeds the CLI
(``bin/paid_doctor.py`` prints it) and the slash command (``__init__.py
_cmd_paid_doctor`` renders as a Lark card on Lark, plain text elsewhere).

Each check returns ``(ok: bool, detail: str, fix_hint: str)``. The check
helper wraps any exception into a fail row so doctor itself never crashes.
"""

from __future__ import annotations

import json
import os
import platform as _platform
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import storage


_HERMES_CONFIG = Path.home() / ".hermes" / "config.yaml"
_REQUIRED_HERMES_FIELDS = ("default", "base_url")  # api_key checked separately (yaml or env)
_PROVIDER_ENV_KEYS = {
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}
_SYSTEMD_UNITS = (
    "paid-sweep.timer",
    "paid-review-sweep.timer",
    "paid-daily-snapshot.timer",
)
_DATA_FILES = (
    "audit_log.jsonl",
    "cost_ledger.jsonl",
    "pending_approvals.jsonl",
)
_RECENT_ERROR_WINDOW_SECONDS = 3600


def run_checks() -> list[dict[str, Any]]:
    """Run all checks; return [{'id', 'ok', 'detail', 'fix_hint'}, ...]."""
    checks: list[tuple[str, Callable[[], tuple[bool, str, str]]]] = [
        ("hermes_config", _check_hermes_config),
        ("owner_json", _check_owner_json),
        ("hermes_version", _check_hermes_version),
        ("systemd_timers", _check_systemd_timers),
        ("data_files", _check_data_files),
        ("settings_schema", _check_settings_schema),
        ("primary_channel", _check_primary_channel),
        ("recent_errors", _check_recent_errors),
    ]
    return [_run_one(name, fn) for name, fn in checks]


def _run_one(name: str, fn: Callable[[], tuple[bool, str, str]]) -> dict[str, Any]:
    try:
        ok, detail, fix = fn()
        return {"id": name, "ok": ok, "detail": detail, "fix_hint": fix}
    except Exception as exc:
        return {
            "id": name,
            "ok": False,
            "detail": f"check crashed: {exc.__class__.__name__}: {exc}",
            "fix_hint": "",
        }


def overall_ok(rows: list[dict[str, Any]]) -> bool:
    return all(r["ok"] for r in rows)


def n_passed(rows: list[dict[str, Any]]) -> int:
    return sum(1 for r in rows if r["ok"])


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_hermes_config() -> tuple[bool, str, str]:
    if not _HERMES_CONFIG.exists():
        return False, f"missing {_HERMES_CONFIG}", "run: hermes init"
    try:
        import yaml  # type: ignore
    except ImportError:
        return False, "PyYAML not installed", "pip install PyYAML"
    try:
        raw = _HERMES_CONFIG.read_text(encoding="utf-8")
        data = yaml.safe_load(raw) or {}
    except Exception as exc:
        return False, f"failed to parse: {exc}", f"fix YAML at {_HERMES_CONFIG}"
    model = data.get("model") or {}
    if not isinstance(model, dict):
        return False, "config.yaml: 'model' is not a mapping", "see hermes docs"
    missing = [f for f in _REQUIRED_HERMES_FIELDS if not model.get(f)]
    if missing:
        return (
            False,
            f"config.yaml model section missing: {missing}",
            f"add {missing} under model: in {_HERMES_CONFIG}",
        )
    # api_key: yaml > provider-specific env var
    api_key_yaml = model.get("api_key")
    provider = (model.get("provider") or "openai").strip().lower()
    env_var = _PROVIDER_ENV_KEYS.get(provider, "")
    api_key_env = os.environ.get(env_var, "") if env_var else ""
    if not api_key_yaml and not api_key_env:
        return (
            False,
            f"no api_key (yaml model.api_key empty, env {env_var or '?'} unset)",
            f"set model.api_key in {_HERMES_CONFIG} or export {env_var or 'provider API key env'}",
        )
    key_src = "yaml" if api_key_yaml else f"env:{env_var}"
    base = str(model.get("base_url") or "")[:40]
    return True, f"model={model.get('default')} base_url={base}... key={key_src}", ""


def _check_owner_json() -> tuple[bool, str, str]:
    path = storage.PAID_DIR / "owner.json"
    if not path.exists():
        return False, f"missing {path}", "run: hermes paid setup-owner (or write owner.json)"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"owner.json parse error: {exc}", f"fix JSON at {path}"
    schema = data.get("schema_version")
    if schema != 2:
        return (
            False,
            f"owner.json schema_version={schema!r} (expected 2)",
            "run: python -m bin.migrate_owner_v1_to_v2",
        )
    idents = data.get("identities") or []
    if not idents:
        return False, "owner.json has no identities[]", "add at least one identity"
    return True, f"schema=2 identities={len(idents)}", ""


def _check_primary_channel() -> tuple[bool, str, str]:
    """v1.7.0: warn when owner has ≥2 enabled identities but no
    primary channel set — PAID falls back to first-enabled which can
    be surprising on multi-channel setups.

    Single-channel owners always pass (nothing to pick).
    """
    path = storage.PAID_DIR / "owner.json"
    if not path.exists():
        # owner_json check already covers this; don't double-report.
        return True, "skipped (owner.json missing — see owner_json check)", ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return True, "skipped (owner.json parse error — see owner_json check)", ""
    enabled = [
        str(i.get("platform", "") or "")
        for i in (data.get("identities") or [])
        if isinstance(i, dict) and i.get("enabled", True)
        and i.get("platform")
    ]
    enabled = [p for p in enabled if p]  # drop blanks
    preferred = str(data.get("preferred_platform", "") or "").strip()
    if len(enabled) < 2:
        return True, f"single-channel ({enabled[0] if enabled else 'none'})", ""
    if not preferred:
        return (
            False,
            f"{len(enabled)} channels enabled ({', '.join(enabled)}) "
            f"but no primary set — PAID uses {enabled[0]!r} by default",
            f"run /paid-set-primary <platform> in any owner DM",
        )
    if preferred not in enabled:
        return (
            False,
            f"primary={preferred!r} not in enabled channels "
            f"({', '.join(enabled)}) — falling back to {enabled[0]!r}",
            f"run /paid-set-primary {enabled[0]} (or add {preferred} identity)",
        )
    return True, f"primary={preferred} (of {len(enabled)} enabled)", ""


def _check_hermes_version() -> tuple[bool, str, str]:
    """Proxy hermes ≥ 0.11 via ctx.register_command presence.

    Doctor is callable from CLI (no ctx) or from inside a plugin (ctx).
    We don't have ctx here, so import the plugins module that ships with
    hermes and look for register_command capability indirectly. If the
    import fails, hermes-agent isn't installed (or is too old to use as a
    library) — both are failures we want to surface.
    """
    try:
        import hermes_cli.plugins as _hp  # type: ignore
    except ImportError:
        return False, "hermes_cli not importable", "pip install hermes-agent>=0.11.0"
    # KNOWN_HOOKS / plugin manager APIs are v0.11+ surface — presence
    # is our proxy. Older hermes lacks invoke_hook altogether.
    if not hasattr(_hp, "invoke_hook"):
        return False, "hermes_cli too old (no invoke_hook)", "upgrade to hermes-agent ≥ 0.11.0"
    return True, "hermes_cli importable + invoke_hook present", ""


def _check_systemd_timers() -> tuple[bool, str, str]:
    if _platform.system() != "Linux":
        return True, f"skipped on {_platform.system()} (systemd N/A)", ""
    if not shutil.which("systemctl"):
        return True, "systemctl not available (skipping)", ""
    inactive: list[str] = []
    missing: list[str] = []
    for unit in _SYSTEMD_UNITS:
        try:
            res = subprocess.run(
                ["systemctl", "--user", "is-active", unit],
                capture_output=True, text=True, timeout=5,
            )
        except Exception as exc:
            inactive.append(f"{unit}: query failed ({exc})")
            continue
        out = (res.stdout or "").strip()
        # "inactive", "failed" → bad; "active" → good; unknown unit → "Failed to get..."
        err = (res.stderr or "").strip()
        if "could not be found" in err.lower() or "not-found" in out.lower():
            missing.append(unit)
        elif out != "active":
            inactive.append(f"{unit}={out or 'unknown'}")
    parts = []
    if missing:
        parts.append(f"missing units: {missing}")
    if inactive:
        parts.append(f"not active: {inactive}")
    if parts:
        return (
            False,
            "; ".join(parts),
            "run: bash bin/install_timers.sh (or systemctl --user enable --now <unit>)",
        )
    return True, f"all {len(_SYSTEMD_UNITS)} timers active", ""


def _check_data_files() -> tuple[bool, str, str]:
    storage.ensure_dirs()
    bad: list[str] = []
    for fname in _DATA_FILES:
        p = storage.PAID_DIR / fname
        try:
            # Touch (create empty if absent) + open append.
            p.touch(exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write("")  # no-op write to verify writability
        except Exception as exc:
            bad.append(f"{fname}: {exc.__class__.__name__}")
    if bad:
        return (
            False,
            f"unwritable: {bad}",
            f"check permissions on {storage.PAID_DIR}",
        )
    return True, f"{len(_DATA_FILES)} data files writable", ""


def _check_settings_schema() -> tuple[bool, str, str]:
    """Validate types + ranges of known settings.json fields.

    We're forgiving: unknown fields are ignored (forward-compat). Known
    fields must have the right type and live in the right range.
    """
    from . import settings as _settings
    cfg = _settings.load()
    issues: list[str] = []

    # Top-level known scalars
    ct = cfg.get("confidence_threshold_direct")
    if not isinstance(ct, (int, float)) or not (0.0 <= float(ct) <= 1.0):
        issues.append(f"confidence_threshold_direct={ct!r} (expected 0..1)")

    to = cfg.get("approval_timeout_minutes")
    if not isinstance(to, int) or to <= 0:
        issues.append(f"approval_timeout_minutes={to!r} (expected positive int)")

    # Nested cost section
    cost_cfg = cfg.get("cost") or {}
    if cost_cfg:
        for k in ("daily_soft_cap_usd", "daily_hard_cap_usd", "weekly_soft_cap_usd"):
            v = cost_cfg.get(k)
            if v is not None and (not isinstance(v, (int, float)) or float(v) < 0):
                issues.append(f"cost.{k}={v!r} (expected non-negative number)")
        soft = float(cost_cfg.get("daily_soft_cap_usd", 5.0) or 0)
        hard = float(cost_cfg.get("daily_hard_cap_usd", 20.0) or 0)
        if hard and soft and soft > hard:
            issues.append(f"cost.daily_soft_cap_usd ({soft}) > daily_hard_cap_usd ({hard})")

    # Nested safety
    safety_cfg = cfg.get("safety") or {}
    if safety_cfg:
        l4c = safety_cfg.get("l4c_enabled")
        if l4c is not None and not isinstance(l4c, bool):
            issues.append(f"safety.l4c_enabled={l4c!r} (expected bool)")

    if issues:
        return False, "; ".join(issues), "fix settings.json or restore defaults"
    return True, "settings.json schema clean", ""


def _check_recent_errors() -> tuple[bool, str, str]:
    """Scan last 1h of audit for fatal events.

    v1.6.6: reads via ``audit.read_all_entries`` so both per-cp files
    (v1.6.4 layout) and any remaining legacy ``audit_log.jsonl`` are
    covered. Before this fix, post-migration installs always reported
    "fresh install" because the legacy file had been renamed.

    Conservative: counts rows where ``extra.fatal=true`` OR
    ``extra.l4_ok=false`` (output safety failure).
    """
    from . import audit as _audit
    try:
        rows = _audit.read_all_entries(lookback_days=1)
    except Exception as exc:
        return False, f"failed to read audit: {exc}", ""

    if not rows:
        return True, "no audit entries yet (fresh install)", ""

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=_RECENT_ERROR_WINDOW_SECONDS)
    fatal_count = 0
    l4_fail_count = 0
    samples: list[str] = []
    for row in rows:
        ts = row.get("ts", "")
        try:
            row_dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except Exception:
            continue
        if row_dt < cutoff:
            continue
        extra = row.get("extra") or {}
        if extra.get("fatal") is True:
            fatal_count += 1
            if len(samples) < 3:
                samples.append(f"fatal@{ts}: {str(row.get('reason', ''))[:80]}")
        elif extra.get("l4_ok") is False:
            l4_fail_count += 1
    if fatal_count or l4_fail_count:
        detail = f"fatal={fatal_count} l4_fail={l4_fail_count} in past 1h"
        if samples:
            detail += f" — e.g. {samples[0]}"
        return False, detail, "see counterparties/*/audit.jsonl"
    return True, "no fatal events in past 1h", ""


# ---------------------------------------------------------------------------
# Plain-text rendering (CLI + IM fallback)
# ---------------------------------------------------------------------------

def format_plain_text(rows: list[dict[str, Any]]) -> str:
    lines = []
    n_pass = n_passed(rows)
    overall = "✓ all pass" if overall_ok(rows) else f"✗ {len(rows) - n_pass} failing"
    lines.append(f"PAID doctor — {n_pass}/{len(rows)} checks passed ({overall})")
    lines.append("")
    for r in rows:
        mark = "✓" if r["ok"] else "✗"
        lines.append(f"[{mark}] {r['id']}: {r['detail']}")
        if not r["ok"] and r["fix_hint"]:
            lines.append(f"    fix: {r['fix_hint']}")
    return "\n".join(lines)
