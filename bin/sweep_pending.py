#!/usr/bin/env python3
"""Mark stale pending approvals as ``timed_out`` and notify junior + owner.

Run periodically (cron / launchd / systemd timer) to handle the case where
the owner isn't watching their IM:

  - Find pending approvals whose age >= settings.approval_timeout_seconds.
  - Mark them ``timed_out`` (terminal status — same audit shape as approve/reject).
  - Best-effort DM the junior:
      "Jimmy 暂时不方便，会直接联系你。"
      "Jimmy is unavailable right now; he'll reach out directly."
  - Best-effort DM the owner with the request_id list so they know what slipped.

Idempotency: re-runs are safe — already-resolved requests are skipped.

Exit code 0 on success (whether or not anything was swept). Non-zero only on
unexpected exceptions.

Recommended cadence:
  - Linux: systemd --user timer every 5 min.
  - macOS: launchd plist with StartInterval 300 (template at
    bin/com.paid.sweep_pending.plist).

Configuration via ``~/.hermes/paid/settings.json``:

    {"approval_timeout_minutes": 30}

Default if missing: 30 minutes.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from paid import approval, hermes_io, identity, storage  # noqa: E402


_DEFAULT_TIMEOUT_MIN = 30


def _load_timeout_seconds() -> float:
    settings_path = storage.PAID_DIR / "settings.json"
    data = storage.read_json(settings_path) or {}
    minutes = data.get("approval_timeout_minutes", _DEFAULT_TIMEOUT_MIN)
    try:
        minutes = float(minutes)
    except (TypeError, ValueError):
        minutes = _DEFAULT_TIMEOUT_MIN
    return minutes * 60.0


def _detect_lang(text: str) -> str:
    """Lightweight CJK detector to pick the timeout copy language."""
    if not text:
        return "en"
    cjk = sum(1 for c in text if "一" <= c <= "鿿")
    return "zh" if cjk >= 2 else "en"


_TIMEOUT_LINE_ZH = "嗨，Jimmy 暂时没办法看消息，会在方便的时候直接联系你。— Jimmy's PAID"
_TIMEOUT_LINE_EN = (
    "Hi — Jimmy isn't reachable right now; he'll get back to you directly "
    "when he can. — Jimmy's PAID"
)


def _send_junior_timeout_msg(req: approval.PendingApproval) -> dict:
    """Best-effort DM to the junior. Failures fall back to outbound queue."""
    lang = _detect_lang(req.junior_question)
    text = _TIMEOUT_LINE_ZH if lang == "zh" else _TIMEOUT_LINE_EN
    try:
        return hermes_io.send_dm(
            req.counterparty_platform,
            req.counterparty_user_id,
            text,
            fallback_to_queue=True,
        )
    except Exception as exc:
        return {"ok": False, "error": f"send raised: {exc}"}


def _alert_owner(swept: list[approval.PendingApproval]) -> None:
    if not swept:
        return
    owner = identity.load_owner()
    if owner is None:
        return
    target = None
    for ident in owner.identities:
        if isinstance(ident, dict) and ident.get("platform") and ident.get("user_id"):
            target = (ident["platform"], ident["user_id"])
            break
    if target is None:
        return
    plat, uid = target
    # Use the same Lark routing helper as __init__._alert_owner /
    # _notify_owner_about_request — prefer a routable owner identity
    # over the FEISHU_HOME_CHANNEL env override.
    if plat in ("feishu", "lark"):
        uid = identity.resolve_owner_lark_target(uid)

    summary = "📭 PAID timeout sweep:\n" + "\n".join(
        f"  #{r.request_id}  {r.counterparty_display or r.counterparty_user_id} "
        f"({r.counterparty_platform})  topic={r.topic}  age={int((time.time() - r.ts_created)/60)}m"
        for r in swept
    )
    try:
        hermes_io.send_dm(plat, uid, summary, fallback_to_queue=True)
    except Exception:
        pass


def main() -> int:
    storage.ensure_dirs()
    timeout_s = _load_timeout_seconds()
    overdue = approval.list_overdue(timeout_s)
    if not overdue:
        print("nothing overdue")
        return 0

    swept: list[approval.PendingApproval] = []
    for req in overdue:
        approval.set_status(
            req.request_id,
            "timed_out",
            final_text="auto-defer: owner did not respond in time",
        )
        result = _send_junior_timeout_msg(req)
        print(
            f"timed_out #{req.request_id}  cp={req.counterparty_id}  "
            f"junior_send={'ok' if result.get('ok') else 'queued/error'}"
        )
        swept.append(req)

    _alert_owner(swept)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
