"""Tests for M2.10 — _check_hermes_capability + register() degradation path.

Verifies PAID gracefully handles too-old hermes (no register_command surface)
without silently leaving the owner with a J3 dead-end. Loads the plugin
entry via importlib (matches test_alert_owner.py pattern).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

_spec = importlib.util.spec_from_file_location(
    "paid_min_hermes_check", _PLUGIN_ROOT / "__init__.py"
)
_plug = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_plug)


class _GoodCtx:
    """v0.11+ hermes context: both register_hook and register_command exist."""
    class _Manifest:
        path = "/fake/path"
    manifest = _Manifest()

    def __init__(self):
        self.hooks_registered = []
        self.commands_registered = []

    def register_hook(self, name, fn):
        self.hooks_registered.append(name)

    def register_command(self, name, fn, *, description="", args_hint=""):
        self.commands_registered.append(name)


class _PreV011Ctx:
    """Pre-v0.11 hermes: register_hook present, register_command MISSING."""
    class _Manifest:
        path = "/fake/path"
    manifest = _Manifest()

    def __init__(self):
        self.hooks_registered = []

    def register_hook(self, name, fn):
        self.hooks_registered.append(name)


class _AncientCtx:
    """Truly ancient: even register_hook missing (would fail catastrophically
    in real hermes startup, but PAID should refuse to half-load instead of
    raising AttributeError on first hook registration)."""
    class _Manifest:
        path = "/fake/path"
    manifest = _Manifest()


# --------------------------------------------------------------------------
# _check_hermes_capability
# --------------------------------------------------------------------------


def test_check_capability_good_ctx_passes():
    cap = _plug._check_hermes_capability(_GoodCtx())
    assert cap["has_register_command"] is True
    assert cap["has_register_hook"] is True
    assert cap["hermes_version_ok"] is True


def test_check_capability_pre_v0_11_flagged():
    cap = _plug._check_hermes_capability(_PreV011Ctx())
    assert cap["has_register_command"] is False
    assert cap["has_register_hook"] is True
    assert cap["hermes_version_ok"] is False


def test_check_capability_ancient_ctx_flagged():
    cap = _plug._check_hermes_capability(_AncientCtx())
    assert cap["has_register_command"] is False
    assert cap["has_register_hook"] is False
    assert cap["hermes_version_ok"] is False


def test_check_capability_reports_minimum_required():
    cap = _plug._check_hermes_capability(_GoodCtx())
    assert cap["minimum_required"] == "0.11.0"


# --------------------------------------------------------------------------
# register() — degradation paths
# --------------------------------------------------------------------------


def test_register_good_ctx_registers_all_commands(paid_tmp, monkeypatch):
    # Stub _alert_owner so we don't try real send_dm.
    monkeypatch.setattr(_plug, "_alert_owner", lambda **kw: None)
    # v1.4.2: classifier health-check would otherwise try a real LLM call.
    # Mock — this test is scoped to hermes capability handling.
    monkeypatch.setattr(_plug, "_classifier_health_check", lambda: None)
    ctx = _GoodCtx()
    _plug.register(ctx)
    # All 3 hooks (some platforms may degrade pre_gateway_dispatch but we
    # still register; on _GoodCtx all 3 land).
    assert "pre_llm_call" in ctx.hooks_registered
    assert "post_llm_call" in ctx.hooks_registered
    # v1.3.4: /review + /r moved to pre_gateway_dispatch (cp identity
    # requires event.source, which slash dispatcher doesn't pass).
    # v1.4.0: + /paid-cancel-input (clears an armed awaiting_input slot
    # after the owner clicked ✅/✏️ on a card but doesn't want to type).
    assert set(ctx.commands_registered) == {
        "paid-pending", "paid-approve", "paid-reject", "paid-status",
        "paid-cancel-input", "card",
    }


def test_register_pre_v0_11_skips_commands_but_keeps_hooks(paid_tmp, monkeypatch):
    """Older hermes — register hooks (J2 still works) but skip slash
    commands (would raise AttributeError if we tried). Owner gets a loud
    log warning + _alert_owner ping."""
    alerts: list[dict] = []

    def fake_alert(reason, detail):
        alerts.append({"reason": reason, "detail": detail})

    monkeypatch.setattr(_plug, "_alert_owner", fake_alert)
    # v1.4.2: scope this test to version-handling; mock the health-check
    # which is exercised by its own tests below.
    monkeypatch.setattr(_plug, "_classifier_health_check", lambda: None)
    ctx = _PreV011Ctx()
    _plug.register(ctx)
    # Hooks DID register.
    assert "pre_llm_call" in ctx.hooks_registered
    assert "post_llm_call" in ctx.hooks_registered
    # No commands_registered list at all — register_command doesn't exist.
    assert not hasattr(ctx, "commands_registered")
    # _alert_owner was called with a clear reason.
    assert len(alerts) == 1
    assert alerts[0]["reason"] == "paid_minimum_hermes_version"
    assert "0.11.0" in alerts[0]["detail"]


def test_register_ancient_ctx_bails_out(paid_tmp, monkeypatch):
    """Truly ancient hermes (no register_hook either) — refuse to half-load.
    Better to crash visibly than leave PAID in a partial-broken state."""
    monkeypatch.setattr(_plug, "_alert_owner", lambda **kw: None)
    monkeypatch.setattr(_plug, "_classifier_health_check", lambda: None)
    ctx = _AncientCtx()
    # Should not raise — just bail silently after logging.
    _plug.register(ctx)
    # Nothing got registered.
    assert not hasattr(ctx, "hooks_registered")
    assert not hasattr(ctx, "commands_registered")


def test_register_alert_owner_failure_swallowed(paid_tmp, monkeypatch):
    """Even if _alert_owner itself fails (e.g. ancient hermes can't reach
    send_dm), register() still proceeds with whatever it can. A failed
    alert path must not prevent J2 hooks from going up."""
    def raising_alert(reason, detail):
        raise RuntimeError("alert path itself broken")

    monkeypatch.setattr(_plug, "_alert_owner", raising_alert)
    monkeypatch.setattr(_plug, "_classifier_health_check", lambda: None)
    ctx = _PreV011Ctx()
    # Should not raise.
    _plug.register(ctx)
    # Hooks still went up despite alert failure.
    assert "pre_llm_call" in ctx.hooks_registered


def test_plugin_yaml_declares_minimum_hermes_version():
    """Sanity: plugin.yaml is the source of truth that hermes itself can
    inspect (when it eventually grows that capability) — keep it in sync
    with _MINIMUM_HERMES_VERSION constant."""
    yaml_path = _PLUGIN_ROOT / "plugin.yaml"
    content = yaml_path.read_text()
    assert "minimum_hermes_version: " in content
    assert _plug._MINIMUM_HERMES_VERSION in content


# ---------------------------------------------------------------------------
# v1.4.2: classifier dry-run health check at plugin register.
# ---------------------------------------------------------------------------


def test_classifier_health_check_success_no_alert(monkeypatch):
    """LLM ping returns OK → log it, no alert."""
    alerts: list[dict] = []

    def fake_alert(reason, detail):
        alerts.append({"reason": reason, "detail": detail})

    # Patch the call_llm import inside _classifier_health_check.
    from paid import hermes_io as _hio
    monkeypatch.setattr(_hio, "call_llm", lambda *a, **kw: "ok")
    monkeypatch.setattr(_plug, "_alert_owner", fake_alert)

    _plug._classifier_health_check()
    assert alerts == []


def test_classifier_health_check_config_error_alerts(monkeypatch):
    """HermesConfigError (missing yaml/env api_key) → loud alert."""
    alerts: list[dict] = []

    def fake_alert(reason, detail):
        alerts.append({"reason": reason, "detail": detail})

    from paid import hermes_io as _hio

    def _raise_config(*a, **kw):
        raise _hio.HermesConfigError(
            "model section missing one of base_url / api_key / default"
        )

    monkeypatch.setattr(_hio, "call_llm", _raise_config)
    monkeypatch.setattr(_plug, "_alert_owner", fake_alert)

    _plug._classifier_health_check()
    assert len(alerts) == 1
    assert alerts[0]["reason"] == "classifier_config_invalid"
    assert "missing one of" in alerts[0]["detail"]


def test_classifier_health_check_llm_unreachable_alerts(monkeypatch):
    """Generic LLM exception (network / 4xx) → alert with different reason."""
    alerts: list[dict] = []

    def fake_alert(reason, detail):
        alerts.append({"reason": reason, "detail": detail})

    from paid import hermes_io as _hio
    monkeypatch.setattr(_hio, "call_llm",
                        lambda *a, **kw: (_ for _ in ()).throw(_hio.LLMCallError("DNS fail")))
    monkeypatch.setattr(_plug, "_alert_owner", fake_alert)

    _plug._classifier_health_check()
    assert len(alerts) == 1
    assert alerts[0]["reason"] == "classifier_llm_unreachable"


def test_classifier_health_check_swallows_alert_failures(monkeypatch):
    """If _alert_owner itself raises, health-check must not propagate."""
    from paid import hermes_io as _hio
    monkeypatch.setattr(_hio, "call_llm",
                        lambda *a, **kw: (_ for _ in ()).throw(_hio.HermesConfigError("bad")))

    def raising_alert(reason, detail):
        raise RuntimeError("alert broken")

    monkeypatch.setattr(_plug, "_alert_owner", raising_alert)
    # Should NOT raise.
    _plug._classifier_health_check()
