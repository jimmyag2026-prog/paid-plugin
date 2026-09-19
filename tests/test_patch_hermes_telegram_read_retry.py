"""Tests for scripts/patch_hermes_telegram_read_retry.py.

The patch is applied at deploy time to the live hermes-agent checkout; we
unit-test the transformation against fixtures that mirror hermes-agent's
relevant lines, then exercise the patched transport to confirm the retry
only fires for the pool that opted in.
"""
from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "patch_hermes_telegram_read_retry.py"


# Mirrors the parts of gateway/platforms/telegram_network.py the patch anchors
# on: the transport __init__, the send call inside the fallback ladder, the
# fallback-exhausted tail, and the _is_retryable_connect_error def.
FIXTURE_NETWORK = textwrap.dedent('''
    from __future__ import annotations

    import asyncio
    import logging
    from typing import Iterable, Optional

    import httpx

    logger = logging.getLogger(__name__)

    _TELEGRAM_API_HOST = "api.telegram.org"


    def _normalize_fallback_ips(ips):
        return list(ips)


    def _rewrite_request_for_ip(request, ip):
        return request


    class TelegramFallbackTransport(httpx.AsyncBaseTransport):
        """Retry Telegram Bot API requests via fallback IPs."""

        def __init__(self, fallback_ips: Iterable[str], **transport_kwargs):
            self._fallback_ips = [ip for ip in dict.fromkeys(_normalize_fallback_ips(fallback_ips))]
            self._primary = None
            self._fallbacks = {}
            self._sticky_ip: Optional[str] = None
            self._sticky_lock = asyncio.Lock()

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            sticky_ip = self._sticky_ip
            attempt_order = [sticky_ip] if sticky_ip else [None]
            for ip in self._fallback_ips:
                if ip != sticky_ip:
                    attempt_order.append(ip)

            last_error = None
            for ip in attempt_order:
                candidate = request if ip is None else _rewrite_request_for_ip(request, ip)
                transport = self._primary if ip is None else self._fallbacks[ip]
                try:
                    response = await transport.handle_async_request(candidate)
                    return response
                except Exception as exc:
                    last_error = exc
                    if not _is_retryable_connect_error(exc):
                        raise
                    continue

            if last_error is None:
                raise RuntimeError("All Telegram fallback IPs exhausted but no error was recorded")
            raise last_error


    def _is_retryable_connect_error(exc: Exception) -> bool:
        return isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError))
''').lstrip()


# Mirrors the transport-construction block in gateway/platforms/telegram.py.
# The extra nesting is deliberate: in hermes the block sits inside a method and
# an `if`, so the anchor carries 16 spaces of indentation.
FIXTURE_ADAPTER = textwrap.dedent('''
    from telegram.request import HTTPXRequest

    from gateway.platforms.telegram_network import TelegramFallbackTransport


    class Adapter:
        async def _build(self, request_kwargs, fallback_ips, proxy_url):
            try:
                if fallback_ips and not proxy_url:
                    request = HTTPXRequest(
                        **request_kwargs,
                        httpx_kwargs={"transport": TelegramFallbackTransport(fallback_ips)},
                    )
                    get_updates_request = HTTPXRequest(
                        **request_kwargs,
                        httpx_kwargs={"transport": TelegramFallbackTransport(fallback_ips)},
                    )
                else:
                    request = HTTPXRequest(**request_kwargs)
                    get_updates_request = HTTPXRequest(**request_kwargs)
            except Exception:
                raise
            return request, get_updates_request
''').lstrip()


def _make_tree(tmp_path: Path) -> Path:
    platforms = tmp_path / "gateway" / "platforms"
    platforms.mkdir(parents=True)
    (platforms / "telegram_network.py").write_text(FIXTURE_NETWORK, encoding="utf-8")
    (platforms / "telegram.py").write_text(FIXTURE_ADAPTER, encoding="utf-8")
    return tmp_path


def _run(root: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(root), *extra],
        capture_output=True,
        text=True,
    )


def _load_patched_network(root: Path):
    path = root / "gateway" / "platforms" / "telegram_network.py"
    spec = importlib.util.spec_from_file_location("patched_telegram_network", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _StubTransport:
    """Raises `error` for the first `fail_times` calls, then returns a response."""

    def __init__(self, error: Exception | None, fail_times: int = 0):
        self.error = error
        self.fail_times = fail_times
        self.calls = 0

    async def handle_async_request(self, request):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.error
        return httpx.Response(200, request=request)


def test_patch_applies_and_is_idempotent(tmp_path):
    root = _make_tree(tmp_path)

    first = _run(root)
    assert first.returncode == 0, first.stderr
    assert "[ok] patched" in first.stdout

    second = _run(root)
    assert second.returncode == 0, second.stderr
    assert "[skip]" in second.stdout


def test_dry_run_writes_nothing(tmp_path):
    root = _make_tree(tmp_path)
    net = root / "gateway" / "platforms" / "telegram_network.py"
    before = net.read_text(encoding="utf-8")

    result = _run(root, "--dry-run")

    assert result.returncode == 0, result.stderr
    assert "[dry-run]" in result.stdout
    assert net.read_text(encoding="utf-8") == before


def test_unknown_shape_is_rejected(tmp_path):
    root = _make_tree(tmp_path)
    net = root / "gateway" / "platforms" / "telegram_network.py"
    net.write_text("# hermes changed shape entirely\n", encoding="utf-8")

    result = _run(root)

    assert result.returncode == 3
    assert "shape changed" in result.stderr


def test_polling_pool_opts_in_general_pool_does_not(tmp_path):
    root = _make_tree(tmp_path)
    assert _run(root).returncode == 0

    adapter = (root / "gateway" / "platforms" / "telegram.py").read_text(encoding="utf-8")
    # Only the get_updates pool gets the flag; replaying a sendMessage could
    # duplicate it, so the general pool must stay untouched.
    assert adapter.count("retry_read_errors=True") == 1
    before_polling = adapter.split("get_updates_request = HTTPXRequest", 1)[0]
    assert "retry_read_errors" not in before_polling


def _drive_send(module, *, retry_read_errors, error, fail_times, method):
    """Construct the transport and drive one send inside a live event loop.

    The transport builds an asyncio.Lock in __init__, which needs a running
    loop, so construction has to happen inside the coroutine.
    """
    stub = _StubTransport(error, fail_times=fail_times)
    request = httpx.Request("POST", f"https://api.telegram.org/botX/{method}")

    async def go():
        transport = module.TelegramFallbackTransport(
            [], retry_read_errors=retry_read_errors
        )
        return await transport._send_with_read_retry(stub, request)

    return asyncio.run(go()), stub


@pytest.mark.parametrize(
    "error",
    [httpx.ReadError("boom"), httpx.ReadTimeout("boom"), httpx.RemoteProtocolError("boom")],
)
def test_read_phase_error_retried_once_when_opted_in(tmp_path, error):
    root = _make_tree(tmp_path)
    assert _run(root).returncode == 0
    module = _load_patched_network(root)

    response, stub = _drive_send(
        module,
        retry_read_errors=True,
        error=error,
        fail_times=1,
        method="getUpdates",
    )

    assert response.status_code == 200
    assert stub.calls == 2


def test_read_phase_error_propagates_when_not_opted_in(tmp_path):
    root = _make_tree(tmp_path)
    assert _run(root).returncode == 0
    module = _load_patched_network(root)

    stub = _StubTransport(httpx.ReadError("boom"), fail_times=1)
    request = httpx.Request("POST", "https://api.telegram.org/botX/sendMessage")

    async def go():
        transport = module.TelegramFallbackTransport([])  # default: no read retry
        return await transport._send_with_read_retry(stub, request)

    with pytest.raises(httpx.ReadError):
        asyncio.run(go())

    assert stub.calls == 1


def test_read_retry_gives_up_after_one_attempt(tmp_path):
    root = _make_tree(tmp_path)
    assert _run(root).returncode == 0
    module = _load_patched_network(root)

    with pytest.raises(httpx.ReadError):
        _drive_send(
            module,
            retry_read_errors=True,
            error=httpx.ReadError("boom"),
            fail_times=99,
            method="getUpdates",
        )


def test_connect_phase_behaviour_unchanged(tmp_path):
    root = _make_tree(tmp_path)
    assert _run(root).returncode == 0
    module = _load_patched_network(root)

    assert module._is_retryable_connect_error(httpx.ConnectError("x")) is True
    assert module._is_retryable_connect_error(httpx.ConnectTimeout("x")) is True
    # Read-phase errors stay out of the fallback-IP ladder: they are handled
    # in-place by _send_with_read_retry, not by hopping to another IP.
    assert module._is_retryable_connect_error(httpx.ReadError("x")) is False
