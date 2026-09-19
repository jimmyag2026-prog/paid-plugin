#!/usr/bin/env python3
"""Idempotent patch for hermes-agent's telegram adapter — read-phase retry.

# Problem

`TelegramFallbackTransport` retries a request against fallback IPs only when
`_is_retryable_connect_error` says so, and that predicate covers just
`httpx.ConnectTimeout` / `httpx.ConnectError` — i.e. failures during the
*connect* phase.

The dominant real-world failure on the DO box is a *read*-phase failure:
Telegram drops the long-poll connection mid-`getUpdates`, surfacing as
`httpx.ReadError` (152 of 176 telegram network warnings over 2026-05..09,
currently ~2/day). Those escape the transport entirely, propagate to PTB's
error callback, and tear the updater down for a full reconnect cycle:

    WARNING [Telegram] Telegram network error, scheduling reconnect: httpx.ReadError
    WARNING [Telegram] Telegram network error (attempt 1/10), reconnecting in 5s
    INFO    [Telegram] Telegram polling resumed after network error (attempt 1)

Net effect: a ~6s inbound outage plus two WARNING lines in errors.log per
event, which buries genuine errors in the same file.

# Fix

Absorb read-phase drops inside the transport with a single in-place retry,
before the connect-phase fallback-IP ladder runs.

Read-phase retry is **opt-in per transport instance** and is enabled only for
the `get_updates_request` pool. That distinction is the safety property: a
`sendMessage` whose response read fails may already have been delivered, so
replaying it would duplicate the message to the owner. `getUpdates` is
offset-based and safe to repeat. hermes already builds the two pools as
separate `TelegramFallbackTransport` instances, so the split needs no
request introspection.

Connect-phase behaviour, the fallback-IP ladder, and sticky-IP selection are
left exactly as they were.

# Usage

    python3 scripts/patch_hermes_telegram_read_retry.py [path/to/hermes-agent]

If no path given, tries the conventional VPS location
``~/.hermes/hermes-agent``. Patches two files under
``gateway/platforms/``: ``telegram_network.py`` and ``telegram.py``.
Re-running is safe — a marker comment (PAID_TELEGRAM_READ_RETRY_VERSION)
lets the script detect prior application and exit cleanly. Re-apply after a
hermes upgrade overwrites the adapter, then restart hermes-gateway.

# Status

Local patch until the equivalent change is upstreamed to hermes-agent.
Verified against hermes-agent as deployed 2026-09 (httpx 0.28.1,
python-telegram-bot 22.7, Python 3.11).
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

PATCH_VERSION = "paid-telegram-read-retry-1"
MARKER = f"PAID_TELEGRAM_READ_RETRY_VERSION = {PATCH_VERSION!r}"

DEFAULT_ROOT = Path.home() / ".hermes/hermes-agent"

# --------------------------------------------------------------------------
# telegram_network.py — opt-in read-phase retry inside the transport
# --------------------------------------------------------------------------

NET_OLD_INIT = '''    def __init__(self, fallback_ips: Iterable[str], **transport_kwargs):
        self._fallback_ips = [ip for ip in dict.fromkeys(_normalize_fallback_ips(fallback_ips))]'''

NET_NEW_INIT = f'''    def __init__(
        self,
        fallback_ips: Iterable[str],
        retry_read_errors: bool = False,
        **transport_kwargs,
    ):
        # {MARKER}
        # Read-phase retry is opt-in per transport instance. Only the
        # get_updates pool enables it: a sendMessage whose response read
        # fails may already have been delivered server-side, so replaying it
        # would duplicate the message. getUpdates is offset-based and safe to
        # repeat.
        self._retry_read_errors = retry_read_errors
        self._fallback_ips = [ip for ip in dict.fromkeys(_normalize_fallback_ips(fallback_ips))]'''

NET_OLD_SEND = '''            try:
                response = await transport.handle_async_request(candidate)'''

NET_NEW_SEND = '''            try:
                response = await self._send_with_read_retry(transport, candidate)'''

NET_HELPER_ANCHOR = '''        if last_error is None:
            raise RuntimeError("All Telegram fallback IPs exhausted but no error was recorded")
        raise last_error'''

NET_HELPER_BLOCK = '''

    async def _send_with_read_retry(
        self, transport: httpx.AsyncBaseTransport, request: httpx.Request
    ) -> httpx.Response:
        """Send ``request``, retrying once in place on a read-phase failure.

        Telegram routinely drops idle long-poll connections, which surfaces as
        httpx.ReadError part-way through getUpdates. Retrying the same path
        immediately keeps the updater alive instead of letting the error reach
        PTB's error callback, which tears polling down for a ~6s reconnect.

        Only enabled on transports constructed with retry_read_errors=True
        (the get_updates pool); everything else keeps the original behaviour of
        propagating read-phase errors untouched.
        """
        try:
            return await transport.handle_async_request(request)
        except _READ_PHASE_ERRORS as exc:
            if not self._retry_read_errors:
                raise
            logger.info(
                "[Telegram] Read-phase drop during polling (%s: %s); retrying same path once",
                type(exc).__name__,
                exc,
            )
            return await transport.handle_async_request(request)'''

NET_ERRORS_ANCHOR = '''def _is_retryable_connect_error(exc: Exception) -> bool:'''

NET_ERRORS_BLOCK = '''# Failures that occur after the request was written, while reading the
# response. Distinct from the connect-phase errors handled by
# _is_retryable_connect_error: these are only safe to retry for idempotent
# calls, so the transport gates them behind retry_read_errors.
_READ_PHASE_ERRORS = (
    httpx.ReadError,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
)


'''

# --------------------------------------------------------------------------
# telegram.py — enable read retry on the polling pool only
# --------------------------------------------------------------------------

TG_OLD_GETUPDATES = '''                get_updates_request = HTTPXRequest(
                    **request_kwargs,
                    httpx_kwargs={"transport": TelegramFallbackTransport(fallback_ips)},
                )'''

TG_NEW_GETUPDATES = f'''                get_updates_request = HTTPXRequest(
                    **request_kwargs,
                    # {MARKER}
                    # Polling is idempotent, so let the transport absorb
                    # read-phase drops instead of tearing the updater down for
                    # a ~6s reconnect cycle. The general `request` pool above
                    # deliberately does NOT set this: replaying a sendMessage
                    # after a failed response read would duplicate it.
                    httpx_kwargs={{
                        "transport": TelegramFallbackTransport(
                            fallback_ips, retry_read_errors=True
                        )
                    }},
                )'''


def _patch_network(src: str) -> tuple[str | None, str]:
    """Return (new_src, reason). new_src is None when the shape doesn't match."""
    for needle, label in (
        (NET_OLD_INIT, "__init__ signature"),
        (NET_OLD_SEND, "handle_async_request send call"),
        (NET_HELPER_ANCHOR, "fallback-exhausted tail"),
        (NET_ERRORS_ANCHOR, "_is_retryable_connect_error def"),
    ):
        if needle not in src:
            return None, f"telegram_network.py: {label} not found"

    out = src.replace(NET_OLD_INIT, NET_NEW_INIT, 1)
    out = out.replace(NET_OLD_SEND, NET_NEW_SEND, 1)
    out = out.replace(NET_HELPER_ANCHOR, NET_HELPER_ANCHOR + NET_HELPER_BLOCK, 1)
    out = out.replace(NET_ERRORS_ANCHOR, NET_ERRORS_BLOCK + NET_ERRORS_ANCHOR, 1)
    return out, "ok"


def _patch_adapter(src: str) -> tuple[str | None, str]:
    if TG_OLD_GETUPDATES not in src:
        return None, "telegram.py: get_updates_request HTTPXRequest block not found"
    return src.replace(TG_OLD_GETUPDATES, TG_NEW_GETUPDATES, 1), "ok"


def _write(target: Path, new_src: str, dry_run: bool) -> tuple[int, Path | None]:
    if dry_run:
        digest = hashlib.sha256(new_src.encode("utf-8")).hexdigest()[:16]
        print(f"[dry-run] would write {target} ({len(new_src)} bytes, sha256:{digest}...)")
        return 0, None

    backup = target.with_suffix(
        target.suffix + f".bak.pre-{PATCH_VERSION}.{int(time.time())}"
    )
    shutil.copy2(target, backup)
    target.write_text(new_src, encoding="utf-8")

    chk = subprocess.run(
        [sys.executable, "-c", f"import ast; ast.parse(open({str(target)!r}).read())"],
        capture_output=True,
        text=True,
    )
    if chk.returncode != 0:
        shutil.copy2(backup, target)
        print(
            f"ERROR: post-patch syntax check failed; rolled back from {backup}\n"
            f"       stderr: {chk.stderr}",
            file=sys.stderr,
        )
        return 5, backup

    print(f"[ok] patched {target}")
    print(f"     backup at {backup}")
    return 0, backup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "root",
        nargs="?",
        default=str(DEFAULT_ROOT),
        help=f"Path to hermes-agent checkout (default: {DEFAULT_ROOT})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing.",
    )
    args = parser.parse_args()

    root = Path(args.root).expanduser().resolve()
    platforms = root / "gateway/platforms"
    net_path = platforms / "telegram_network.py"
    tg_path = platforms / "telegram.py"

    for p in (net_path, tg_path):
        if not p.is_file():
            print(f"ERROR: target not found: {p}", file=sys.stderr)
            return 2

    net_src = net_path.read_text(encoding="utf-8")
    tg_src = tg_path.read_text(encoding="utf-8")

    if MARKER in net_src and MARKER in tg_src:
        print(f"[skip] already patched ({PATCH_VERSION})")
        return 0
    if MARKER in net_src or MARKER in tg_src:
        print(
            f"ERROR: partially patched — marker present in only one of\n"
            f"       {net_path}\n       {tg_path}\n"
            f"       Restore both from .bak.pre-{PATCH_VERSION}.* and re-run.",
            file=sys.stderr,
        )
        return 4

    new_net, reason = _patch_network(net_src)
    if new_net is None:
        print(f"ERROR: {reason} — hermes-agent shape changed, re-derive the anchors.", file=sys.stderr)
        return 3

    new_tg, reason = _patch_adapter(tg_src)
    if new_tg is None:
        print(f"ERROR: {reason} — hermes-agent shape changed, re-derive the anchors.", file=sys.stderr)
        return 3

    rc, net_backup = _write(net_path, new_net, args.dry_run)
    if rc != 0:
        return rc

    rc, _ = _write(tg_path, new_tg, args.dry_run)
    if rc != 0:
        # Keep the two files consistent: undo the network patch too.
        if net_backup is not None:
            shutil.copy2(net_backup, net_path)
            print(f"       rolled back {net_path} as well", file=sys.stderr)
        return rc

    if not args.dry_run:
        print(f"     marker: {PATCH_VERSION}")
        print("     restart hermes-gateway to pick up the change.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
