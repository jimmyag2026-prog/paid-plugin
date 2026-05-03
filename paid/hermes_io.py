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


# ---------------------------------------------------------------------------
# Retry policy for transient LLM failures.
#
# DeepSeek / OpenRouter / Anthropic occasionally 5xx or close the connection
# mid-stream. Without retry every PAID classifier call goes to the
# conservative `[fallback]` branch on a single transient error, cascading
# every counterparty's request to the "request" state — silent quality
# regression that's hard to diagnose later.
#
# We retry up to 3 times with exponential backoff (~0.5s, 1.5s, 4s) on:
#   * httpx.RequestError (DNS, TCP reset, read/write timeout)
#   * 5xx response status
# 4xx responses are deterministic (bad key, bad body) — no retry.
# ---------------------------------------------------------------------------

_RETRY_BACKOFFS_S: tuple[float, ...] = (0.5, 1.5, 4.0)
_RETRY_STATUS_CODES: frozenset[int] = frozenset({500, 502, 503, 504, 408, 429})


def _resolve_retry_backoffs() -> tuple[float, ...]:
    """Resolve retry backoffs.

    Priority:
      1. ``_RETRY_BACKOFFS_S`` module constant if monkeypatched in a test
         context (we detect this by comparing identity).
      2. ``settings.json`` ``llm_retry_backoffs_seconds`` if the file exists
         on disk.
      3. Module default otherwise.

    The "settings only when file exists on disk" rule keeps unit tests
    deterministic — tests don't have to mock storage to override retry
    behaviour, they just monkeypatch ``_RETRY_BACKOFFS_S``.
    """
    try:
        from . import settings as _settings, storage as _storage  # lazy
        # If the test has monkeypatched _RETRY_BACKOFFS_S, let that win.
        if _RETRY_BACKOFFS_S != (0.5, 1.5, 4.0):
            return _RETRY_BACKOFFS_S
        if (_storage.PAID_DIR / "settings.json").exists():
            cfg = _settings.llm_retry_backoffs()
            if cfg:
                return cfg
    except Exception:
        pass
    return _RETRY_BACKOFFS_S


def _post_with_retry(
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float,
) -> "httpx.Response":
    import time as _time

    backoffs = _resolve_retry_backoffs()
    last_exc: Exception | None = None
    for attempt, backoff in enumerate((0.0,) + backoffs):
        if backoff > 0:
            _time.sleep(backoff)
        try:
            resp = httpx.post(url, headers=headers, json=body, timeout=timeout)
        except httpx.RequestError as exc:
            last_exc = exc
            if attempt >= len(backoffs):
                raise LLMCallError(
                    f"HTTP request to {url} failed after "
                    f"{len(backoffs) + 1} attempts: {exc}"
                ) from exc
            continue
        if resp.status_code in _RETRY_STATUS_CODES and attempt < len(backoffs):
            last_exc = LLMCallError(
                f"transient {resp.status_code} on attempt {attempt + 1}"
            )
            continue
        return resp

    # Loop exit without a successful response — last_exc must be set.
    raise LLMCallError(f"exhausted retries for {url}: {last_exc}")


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

    # Retry transient failures (5xx, connection errors, timeouts) with
    # exponential backoff. Don't retry 4xx — those are deterministic
    # (auth, malformed body, model-not-found, etc.).
    resp = _post_with_retry(url, headers=headers, body=body, timeout=timeout)

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
        # Platform enum lives in gateway.config (verified against
        # hermes-agent v0.12.0). Older drafts referenced session_context,
        # which never exported Platform — that import always raised.
        from gateway.config import Platform  # type: ignore
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


# ---------------------------------------------------------------------------
# Lark / Feishu — receive_id_type inference + direct API send
# ---------------------------------------------------------------------------

# The hermes feishu adapter's ``send()`` (gateway/platforms/feishu.py:4088 in
# v0.12.0) hard-codes ``receive_id_type="chat_id"``, so a bare tenant
# ``user_id`` from ``owner.json`` / counterparty profiles fails with
# ``[230001] invalid receive_id``. To keep PAID functional without forking
# hermes, we detect the receive_id format and bypass the adapter's ``send``
# when we don't have a chat_id, calling Lark's IM API directly via the
# adapter's already-authenticated ``_client``.
#
# Once the upstream issue (chat_id in pre_llm_call kwargs) lands, we will be
# able to capture chat_id at hook time and store it on counterparty profiles —
# this branch then becomes redundant but harmless.

_LARK_RECEIVE_ID_TYPES = {
    "oc_": "chat_id",
    "ou_": "open_id",
    "on_": "union_id",
}


def _detect_lark_receive_id_type(receive_id: str) -> str:
    """Infer ``receive_id_type`` for Lark's ``/im/v1/messages`` from the prefix.

    Returns one of: ``chat_id`` / ``open_id`` / ``union_id`` / ``email`` /
    ``user_id`` (default fallback for IDs without a known prefix).
    """
    if not receive_id:
        return "user_id"
    if "@" in receive_id and "." in receive_id.split("@", 1)[1]:
        return "email"
    for prefix, kind in _LARK_RECEIVE_ID_TYPES.items():
        if receive_id.startswith(prefix):
            return kind
    return "user_id"


def _load_lark_env_creds() -> dict[str, str] | None:
    """Read FEISHU_APP_ID + FEISHU_APP_SECRET (+ optional FEISHU_DOMAIN) from
    ``~/.hermes/.env`` for standalone Lark API calls when the gateway adapter
    isn't reachable from this process.

    Returns None when the file is missing or required keys aren't present.
    Format: standard ``KEY=VALUE`` lines, ``#`` comments tolerated.
    """
    env_path = Path.home() / ".hermes" / ".env"
    if not env_path.exists():
        return None
    creds: dict[str, str] = {}
    try:
        for ln in env_path.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_DOMAIN"):
                creds[k] = v
    except Exception:
        return None
    if not creds.get("FEISHU_APP_ID") or not creds.get("FEISHU_APP_SECRET"):
        return None
    return creds


# Cached standalone client — building one is cheap but token refresh state
# is per-instance, so reuse across calls within the same process.
_STANDALONE_LARK_CLIENT: Any = None


def _build_standalone_lark_client() -> Any:
    """Build a lark_oapi.Client from ``~/.hermes/.env`` creds.

    Used by ``send_dm`` / ``send_lark_card`` when the in-process gateway
    runner isn't available — e.g. ``bin/sweep_pending.py`` running as a
    cron / systemd timer separately from the gateway service.

    Returns the cached client on subsequent calls. Raises ``SendDmError`` if
    creds aren't readable from .env or lark_oapi isn't importable.
    """
    global _STANDALONE_LARK_CLIENT
    if _STANDALONE_LARK_CLIENT is not None:
        return _STANDALONE_LARK_CLIENT

    creds = _load_lark_env_creds()
    if creds is None:
        raise SendDmError(
            "no FEISHU_APP_ID / FEISHU_APP_SECRET in ~/.hermes/.env "
            "(needed for standalone Lark send when gateway is out of process)"
        )

    try:
        import lark_oapi as lark  # type: ignore
    except Exception as exc:
        raise SendDmError(f"lark_oapi not importable: {exc}") from exc

    builder = lark.Client.builder().app_id(creds["FEISHU_APP_ID"]).app_secret(
        creds["FEISHU_APP_SECRET"]
    )
    # FEISHU_DOMAIN: "feishu" → CN endpoint, "lark" → international.
    domain_label = (creds.get("FEISHU_DOMAIN") or "feishu").lower()
    try:
        from lark_oapi.core.const import FEISHU_DOMAIN, LARK_DOMAIN  # type: ignore
        builder = builder.domain(LARK_DOMAIN if domain_label == "lark" else FEISHU_DOMAIN)
    except Exception:
        # Older lark_oapi versions take the domain string directly.
        pass

    _STANDALONE_LARK_CLIENT = builder.build()
    return _STANDALONE_LARK_CLIENT


def _send_with_lark_client(client: Any, receive_id: str, message: str) -> dict[str, Any]:
    """Common Lark text-send path used by both adapter-based and standalone
    branches. Returns the dict-shape send_dm callers expect."""
    import json as _json

    try:
        from lark_oapi.api.im.v1 import (  # type: ignore
            CreateMessageRequest,
            CreateMessageRequestBody,
        )
    except Exception as exc:
        raise SendDmError(f"lark_oapi not importable: {exc}") from exc

    rid_type = _detect_lark_receive_id_type(receive_id)
    body = (
        CreateMessageRequestBody.builder()
        .receive_id(receive_id)
        .msg_type("text")
        .content(_json.dumps({"text": message}, ensure_ascii=False))
        .build()
    )
    req = (
        CreateMessageRequest.builder()
        .receive_id_type(rid_type)
        .request_body(body)
        .build()
    )

    resp = client.im.v1.message.create(req)

    success = (
        resp.success()  # type: ignore[attr-defined]
        if hasattr(resp, "success") and callable(resp.success)
        else (getattr(resp, "code", 1) == 0)
    )
    if not success:
        return {
            "ok": False,
            "error": f"lark api: code={getattr(resp, 'code', '?')} "
                     f"msg={getattr(resp, 'msg', '') or getattr(resp, 'message', '')}",
            "platform": "feishu",
            "raw": repr(resp)[:300],
        }

    data = getattr(resp, "data", None)
    msg_id = (
        getattr(data, "message_id", None)
        or getattr(data, "msg_id", None)
        or (data.get("message_id") if isinstance(data, dict) else None)
    )
    return {
        "ok": True,
        "msg_id": msg_id,
        "platform": "feishu",
        "receive_id_type": rid_type,
        "raw": repr(resp)[:200],
    }


def _send_lark_direct(adapter: Any, receive_id: str, message: str) -> dict[str, Any]:
    """Send via the adapter's authenticated client (gateway in-process)."""
    client = getattr(adapter, "_client", None)
    if client is None:
        raise SendDmError("feishu adapter exposes no _client (hermes API drift?)")
    return _send_with_lark_client(client, receive_id, message)


def _send_lark_standalone(receive_id: str, message: str) -> dict[str, Any]:
    """Send via a freshly-built lark_oapi.Client read from ~/.hermes/.env.

    Used when ``_get_gateway_adapter`` raises (no live gateway runner in
    this process) — e.g. ``bin/sweep_pending.py`` invoked from cron or
    one-off CLI usage. This lets PAID actually deliver timeout / digest
    messages even when the gateway is in a different process.
    """
    client = _build_standalone_lark_client()
    return _send_with_lark_client(client, receive_id, message)


def send_lark_card(
    platform: str,
    receive_id: str,
    card: dict[str, Any],
    *,
    fallback_to_queue: bool = True,
) -> dict[str, Any]:
    """Send a Lark **interactive card** (``msg_type=interactive``).

    Same plumbing as ``_send_lark_direct`` but lets the caller pass a card
    JSON dict instead of plain text. The card schema is whatever Lark's
    Open Platform expects — see ``__init__._format_lark_approval_card``
    for PAID's approval card shape.

    Returns the same shape as ``send_dm``.
    """
    import json as _json

    if platform not in ("feishu", "lark"):
        # Other platforms don't have an analogous "card" concept — treat as
        # a hard error so the caller picks the text fallback explicitly.
        raise SendDmError(f"send_lark_card unsupported on platform={platform!r}")

    # Resolve a usable lark_oapi.Client. Prefer the in-process gateway adapter
    # (cheaper, shares token cache); fall back to a standalone client built
    # from ~/.hermes/.env when this code path is invoked outside the gateway
    # process (e.g. cron-driven sweep).
    client: Any = None
    try:
        adapter = _get_gateway_adapter(platform)
        client = getattr(adapter, "_client", None)
    except SendDmError:
        client = None

    if client is None:
        try:
            client = _build_standalone_lark_client()
        except SendDmError as exc:
            if fallback_to_queue:
                qp = _enqueue_outbound_fallback(
                    platform, receive_id, "[card] " + _json.dumps(card, ensure_ascii=False)
                )
                return {"ok": False, "queued": str(qp), "error": str(exc)}
            raise

    try:
        from lark_oapi.api.im.v1 import (  # type: ignore
            CreateMessageRequest,
            CreateMessageRequestBody,
        )
    except Exception as exc:
        if fallback_to_queue:
            qp = _enqueue_outbound_fallback(
                platform, receive_id, "[card] " + _json.dumps(card, ensure_ascii=False)
            )
            return {"ok": False, "queued": str(qp),
                    "error": f"lark_oapi import failed: {exc}"}
        raise SendDmError(f"lark_oapi import failed: {exc}") from exc

    rid_type = _detect_lark_receive_id_type(receive_id)
    body = (
        CreateMessageRequestBody.builder()
        .receive_id(receive_id)
        .msg_type("interactive")
        .content(_json.dumps(card, ensure_ascii=False))
        .build()
    )
    req = (
        CreateMessageRequest.builder()
        .receive_id_type(rid_type)
        .request_body(body)
        .build()
    )

    try:
        resp = client.im.v1.message.create(req)
    except Exception as exc:
        if fallback_to_queue:
            qp = _enqueue_outbound_fallback(
                platform, receive_id, "[card] " + _json.dumps(card, ensure_ascii=False)
            )
            return {"ok": False, "queued": str(qp),
                    "error": f"lark card send raised: {exc}"}
        raise SendDmError(f"lark card send raised: {exc}") from exc

    success = (
        resp.success()  # type: ignore[attr-defined]
        if hasattr(resp, "success") and callable(resp.success)
        else (getattr(resp, "code", 1) == 0)
    )
    if not success:
        result = {
            "ok": False,
            "error": f"lark api: code={getattr(resp, 'code', '?')} "
                     f"msg={getattr(resp, 'msg', '') or getattr(resp, 'message', '')}",
            "platform": "feishu",
            "raw": repr(resp)[:300],
        }
        if fallback_to_queue:
            qp = _enqueue_outbound_fallback(
                platform, receive_id, "[card] " + _json.dumps(card, ensure_ascii=False)
            )
            result["queued"] = str(qp)
        return result

    data = getattr(resp, "data", None)
    msg_id = (
        getattr(data, "message_id", None)
        or getattr(data, "msg_id", None)
        or (data.get("message_id") if isinstance(data, dict) else None)
    )
    return {
        "ok": True,
        "msg_id": msg_id,
        "platform": "feishu",
        "receive_id_type": rid_type,
        "msg_type": "interactive",
        "raw": repr(resp)[:200],
    }


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


def render_options_block(options: list[dict] | None) -> str:
    """Render an options-block list as plain-text bullets appended to a message.

    Each option is a ``{"key": "<short>", "label": "<text>"}`` dict. Output:

        (a) accept — 接受会改
        (pass) skip
        (custom) free text

    The rendered string is empty when *options* is None or empty. Centralised
    here so the paid-review skill, approval cards, and any future caller all
    produce identical option syntax — and the junior-side reply parser can
    rely on a single ``(key)`` shape across platforms.
    """
    if not options:
        return ""
    lines: list[str] = []
    for opt in options:
        if not isinstance(opt, dict):
            continue
        key = str(opt.get("key", "")).strip()
        if not key:
            continue
        label = str(opt.get("label", "")).strip()
        if label:
            lines.append(f"({key}) {label}")
        else:
            lines.append(f"({key})")
    if not lines:
        return ""
    return "\n".join(lines)


def _maybe_append_options(message: str, options: list[dict] | None) -> str:
    """Return *message* with the rendered options block appended, if any."""
    block = render_options_block(options)
    if not block:
        return message
    sep = "\n\n" if message and not message.endswith("\n") else ""
    return f"{message}{sep}{block}"


def send_dm(
    platform: str,
    user_id: str,
    message: str,
    *,
    options_block: list[dict] | None = None,
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
        options_block: Optional list of ``{"key", "label"}`` dicts the recipient
            can reply with. Rendered as plain-text bullets appended to the body
            on every platform (universal lowest-common-denominator). Lark and
            Telegram have richer affordances (interactive cards / inline
            keyboards) — wiring those in is a follow-up; the plain-text form is
            still parseable so the recipient can reply ``a`` / ``pass`` / etc.
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

    # Render options block into the message body BEFORE platform dispatch so
    # the queued-fallback path also captures it (otherwise an owner doing a
    # manual deliver from outbound_queue.jsonl wouldn't see the options).
    message = _maybe_append_options(message, options_block)

    # Resolution order for the lark / feishu non-chat_id branch:
    #   1. Gateway adapter in this process (cheapest, shared token cache).
    #   2. Standalone lark_oapi.Client built from ~/.hermes/.env (lets
    #      out-of-process callers — bin/sweep_pending.py, ad-hoc CLI, cron —
    #      actually deliver messages instead of always queuing).
    #   3. Fallback to outbound_queue.jsonl as a last resort.
    #
    # For other platforms (telegram / wecom / etc), only step 1 applies; we
    # don't have standalone clients for them yet.
    adapter: Any = None
    try:
        adapter = _get_gateway_adapter(platform)
    except SendDmError as adapter_exc:
        # Non-Lark platforms have no standalone fallback — go straight to queue.
        if platform not in ("feishu", "lark"):
            if fallback_to_queue:
                qp = _enqueue_outbound_fallback(platform, user_id, message)
                return {"ok": False, "queued": str(qp), "error": str(adapter_exc)}
            raise

    # Lark / Feishu: when receive_id is NOT a chat_id (oc_…), the adapter's
    # hard-coded receive_id_type=chat_id rejects with [230001]. Bypass the
    # adapter and call Lark's IM API directly with the inferred type.
    if platform in ("feishu", "lark"):
        rid_type = _detect_lark_receive_id_type(user_id)
        if rid_type != "chat_id":
            try:
                if adapter is not None:
                    result = _send_lark_direct(adapter, user_id, message)
                else:
                    result = _send_lark_standalone(user_id, message)
            except Exception as exc:
                # If gateway path raised AND we haven't tried standalone yet,
                # one more attempt before giving up.
                if adapter is not None:
                    try:
                        result = _send_lark_standalone(user_id, message)
                    except Exception as exc2:
                        if fallback_to_queue:
                            qp = _enqueue_outbound_fallback(platform, user_id, message)
                            return {"ok": False, "queued": str(qp),
                                    "error": f"lark direct send raised: {exc}; "
                                             f"standalone fallback raised: {exc2}"}
                        raise SendDmError(
                            f"lark direct send raised: {exc}; "
                            f"standalone fallback raised: {exc2}"
                        ) from exc2
                else:
                    if fallback_to_queue:
                        qp = _enqueue_outbound_fallback(platform, user_id, message)
                        return {"ok": False, "queued": str(qp),
                                "error": f"lark standalone send raised: {exc}"}
                    raise SendDmError(f"lark standalone send raised: {exc}") from exc
            if not result.get("ok") and fallback_to_queue:
                qp = _enqueue_outbound_fallback(platform, user_id, message)
                result["queued"] = str(qp)
            return result

    # Non-Lark platforms below this point — adapter must exist.
    if adapter is None:
        # Should be unreachable because we returned/raised above.
        if fallback_to_queue:
            qp = _enqueue_outbound_fallback(platform, user_id, message)
            return {"ok": False, "queued": str(qp), "error": "no adapter and platform has no standalone client"}
        raise SendDmError(f"no adapter for platform={platform!r}")

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

    # Don't trust "no exception raised" as success — adapters return a
    # SendResult-shaped object whose .success may be False even when the
    # coroutine resolved cleanly. Check both. Falling for HTTP 200 = success
    # is one of the five IM-bot traps (see project memory
    # feedback_im_bot_api_traps.md): the Lark API returned [230001] invalid
    # receive_id while SendResult wrapped it as success=False, but the old
    # code happily reported ok=True with msg_id=None.
    success = getattr(result, "success", None)
    error = getattr(result, "error", None)
    msg_id = getattr(result, "message_id", None) or getattr(result, "msg_id", None)

    if success is False or (success is None and not msg_id):
        # Adapter signalled failure (or returned a shape we can't classify).
        if fallback_to_queue:
            qp = _enqueue_outbound_fallback(platform, user_id, message)
            return {
                "ok": False,
                "queued": str(qp),
                "error": str(error) if error else f"adapter.send returned no msg_id: {result!r}"[:300],
                "platform": platform,
            }
        raise SendDmError(
            f"adapter.send returned failure: success={success!r} error={error!r}"
        )

    return {"ok": True, "msg_id": msg_id, "platform": platform, "raw": repr(result)[:200]}
