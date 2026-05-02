"""Module H — Hermes IO helpers.

Calls the Hermes-configured LLM via OpenAI-compatible chat completion API.

Configuration is read from ``~/.hermes/config.yaml``. The relevant keys
(top-level ``model.*``) are::

    model:
      provider: deepseek          # or openai / anthropic / openrouter / ...
      default: deepseek-v4-flash  # model name
      base_url: https://api.deepseek.com
      api_key: sk-...

This module exposes a single function ``call_llm`` which posts a chat
completion request and returns the assistant's text reply.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import yaml

HERMES_CONFIG_PATH: Path = Path.home() / ".hermes" / "config.yaml"
DEFAULT_TIMEOUT: float = 60.0

# Providers whose base_url is the API root (no /v1 implied) → endpoint /v1/chat/completions
# Providers whose base_url already includes /v1 (e.g. openrouter) → /chat/completions
_BASE_URL_HAS_V1_HINTS: tuple[str, ...] = ("/v1",)


class HermesConfigError(Exception):
    """Raised when ~/.hermes/config.yaml is missing or malformed."""


class LLMCallError(Exception):
    """Raised when the chat-completion HTTP call fails or returns junk."""


def _load_hermes_config(path: Path | None = None) -> dict[str, Any]:
    """Load the Hermes YAML config and return the full dict.

    If ``path`` is None, reads the module-level ``HERMES_CONFIG_PATH`` at call
    time (so monkeypatching that module attribute in tests works).
    """
    if path is None:
        path = HERMES_CONFIG_PATH
    if not path.exists():
        raise HermesConfigError(f"Hermes config not found at {path}")
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise HermesConfigError(f"Failed to parse {path}: {e}") from e
    if not isinstance(data, dict):
        raise HermesConfigError(f"Top level of {path} is not a mapping")
    return data


def _resolve_model_section(config: dict[str, Any]) -> dict[str, Any]:
    """Extract the top-level 'model' mapping with required keys."""
    model = config.get("model")
    if not isinstance(model, dict):
        raise HermesConfigError("config.yaml has no top-level 'model:' mapping")
    base_url = model.get("base_url")
    api_key = model.get("api_key")
    default_model = model.get("default")
    if not base_url or not api_key or not default_model:
        raise HermesConfigError(
            "model section missing one of base_url / api_key / default "
            f"(got base_url={bool(base_url)}, api_key={bool(api_key)}, "
            f"default={bool(default_model)})"
        )
    return {
        "provider": model.get("provider") or "openai",
        "model": default_model,
        "base_url": str(base_url).rstrip("/"),
        "api_key": api_key,
        "max_tokens": model.get("max_tokens") or 4096,
    }


def _build_chat_url(base_url: str) -> str:
    """Return the chat-completions endpoint URL.

    If base_url already contains '/v1' we append '/chat/completions' only.
    Otherwise we use '/v1/chat/completions'.
    """
    base = base_url.rstrip("/")
    if any(hint in base for hint in _BASE_URL_HAS_V1_HINTS):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def call_llm(
    prompt: str,
    system: str = "",
    json_mode: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    temperature: float | None = None,
) -> str:
    """Call the Hermes-configured LLM and return its text reply.

    Args:
        prompt: User prompt content.
        system: Optional system-message content (empty string skips it).
        json_mode: If True, request a JSON object response
            (sets ``response_format={"type":"json_object"}``).
        timeout: HTTP timeout in seconds.
        temperature: If provided, sent in the request body. Callers that need
            deterministic structured output (e.g. classifier) pass a low value
            like 0.1; if None, the provider's default applies.

    Returns:
        The assistant's message content as a string.

    Raises:
        HermesConfigError: ``~/.hermes/config.yaml`` is missing or malformed.
        LLMCallError: HTTP error or unexpected response shape.
    """
    config = _load_hermes_config()
    model_cfg = _resolve_model_section(config)

    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    body: dict[str, Any] = {
        "model": model_cfg["model"],
        "messages": messages,
        "max_tokens": model_cfg["max_tokens"],
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if temperature is not None:
        body["temperature"] = temperature

    headers = {
        "Authorization": f"Bearer {model_cfg['api_key']}",
        "Content-Type": "application/json",
    }
    url = _build_chat_url(model_cfg["base_url"])

    try:
        resp = httpx.post(url, headers=headers, json=body, timeout=timeout)
    except httpx.HTTPError as e:
        raise LLMCallError(f"HTTP request to {url} failed: {e}") from e

    if resp.status_code >= 400:
        raise LLMCallError(
            f"LLM call to {url} returned {resp.status_code}: {resp.text[:500]}"
        )

    try:
        payload = resp.json()
    except ValueError as e:
        raise LLMCallError(f"LLM response was not JSON: {resp.text[:500]}") from e

    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise LLMCallError(
            f"LLM response missing choices[0].message.content: {payload}"
        ) from e

    if not isinstance(content, str):
        raise LLMCallError(f"LLM returned non-string content: {type(content).__name__}")

    return content


# ---------------------------------------------------------------------------
# Outbound IM send — v0.5 implementation via Hermes gateway adapter
# ---------------------------------------------------------------------------


class SendDmError(Exception):
    """Raised when send_dm fails (no gateway, no adapter, adapter raised)."""


def _get_gateway_adapter(platform: str):
    """Look up the live BasePlatformAdapter for *platform* on the running gateway.

    Returns the adapter instance, or raises SendDmError if anything is missing.
    Lazy imports keep this module importable outside the Hermes process (tests).
    """
    try:
        from gateway import run as _gw  # type: ignore
        from gateway.session_context import Platform  # type: ignore
    except Exception as exc:
        raise SendDmError(f"hermes gateway modules not importable: {exc}") from exc

    runner = _gw._gateway_runner_ref()  # type: ignore[attr-defined]
    if runner is None:
        raise SendDmError("no live GatewayRunner — gateway not running in this process")

    try:
        plat_enum = Platform(platform)
    except Exception as exc:
        raise SendDmError(f"unknown platform '{platform}': {exc}") from exc

    adapter = runner.adapters.get(plat_enum)
    if adapter is None:
        raise SendDmError(
            f"no active adapter for platform={platform}; "
            f"loaded={[p.value for p in runner.adapters.keys()]}"
        )
    return adapter


def _enqueue_outbound_fallback(platform: str, user_id: str, message: str) -> Path:
    """Append a queued outbound DM to disk so the owner can hand-deliver.

    Used when send_dm cannot reach the gateway (tests, gateway down). The owner
    can ``tail -f outbound_queue.jsonl`` and copy/paste manually.
    """
    from . import storage  # local to avoid circular at module import
    path = storage.PAID_DIR / "outbound_queue.jsonl"
    storage.append_jsonl(
        path,
        {
            "ts": __import__("time").time(),
            "platform": platform,
            "user_id": user_id,
            "message": message,
        },
    )
    return path


def send_dm(
    platform: str,
    user_id: str,
    message: str,
    *,
    fallback_to_queue: bool = True,
) -> dict[str, Any]:
    """Send a DM via the live Hermes gateway adapter.

    On failure, by default appends to ``outbound_queue.jsonl`` so the owner can
    hand-deliver. Pass ``fallback_to_queue=False`` to surface the error.

    Args:
        platform: "telegram" / "feishu" / "wecom" / "whatsapp" / "slack" — must
            match the Platform enum value used by the gateway.
        user_id: Platform-native chat / user id (becomes ``chat_id`` for adapter).
        message: Plain-text body.
        fallback_to_queue: When True (default) and the live send fails, write
            to outbound_queue.jsonl and return a queued result instead of
            raising.

    Returns:
        ``{"ok": True, "msg_id": str|None, "platform": platform}`` on live send
        or ``{"ok": False, "queued": str(path), "error": str}`` on fallback.

    Raises:
        SendDmError: only if ``fallback_to_queue=False`` and the send fails.
    """
    import asyncio

    try:
        adapter = _get_gateway_adapter(platform)
    except SendDmError as exc:
        if fallback_to_queue:
            qp = _enqueue_outbound_fallback(platform, user_id, message)
            return {"ok": False, "queued": str(qp), "error": str(exc)}
        raise

    coro = adapter.send(user_id, message)

    try:
        # If we are inside a running event loop (gateway thread), schedule the
        # coroutine on it and wait. Otherwise, spin up a fresh loop.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
            result = fut.result(timeout=30)
        else:
            result = asyncio.run(coro)
    except Exception as exc:
        if fallback_to_queue:
            qp = _enqueue_outbound_fallback(platform, user_id, message)
            return {"ok": False, "queued": str(qp), "error": f"adapter.send raised: {exc}"}
        raise SendDmError(f"adapter.send raised: {exc}") from exc

    msg_id = getattr(result, "message_id", None) or getattr(result, "msg_id", None)
    return {"ok": True, "msg_id": msg_id, "platform": platform, "raw": repr(result)[:200]}
