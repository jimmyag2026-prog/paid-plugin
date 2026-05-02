"""Tests for paid.hermes_io — Module H.

All HTTP calls are mocked; no real network traffic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

# Add repo root so `from paid import ...` works when running pytest from anywhere
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from paid import hermes_io  # noqa: E402


def _fake_response(status: int = 200, content: str = "hello world"):
    """Build a mock httpx.Response-like object."""
    resp = mock.Mock()
    resp.status_code = status
    if status >= 400:
        resp.text = content
        resp.json.side_effect = ValueError("not json")
    else:
        body = {
            "choices": [
                {"message": {"role": "assistant", "content": content}}
            ]
        }
        resp.text = json.dumps(body)
        resp.json.return_value = body
    return resp


def _write_config(tmp_path: Path, model_block: dict) -> Path:
    """Write a minimal hermes config and return the path."""
    import yaml

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({"model": model_block}), encoding="utf-8")
    return cfg_path


def test_deepseek_provider_constructs_correct_request(tmp_path, monkeypatch):
    """Deepseek base_url has no /v1, so endpoint should be {base}/v1/chat/completions."""
    cfg_path = _write_config(
        tmp_path,
        {
            "provider": "deepseek",
            "default": "deepseek-v4-flash",
            "base_url": "https://api.deepseek.com",
            "api_key": "sk-deepseek-test",
            "max_tokens": 4096,
        },
    )
    monkeypatch.setattr(hermes_io, "HERMES_CONFIG_PATH", cfg_path)

    fake = _fake_response(content="ok")
    with mock.patch.object(hermes_io.httpx, "post", return_value=fake) as post_mock:
        result = hermes_io.call_llm("hi", system="you are helpful")

    assert result == "ok"
    args, kwargs = post_mock.call_args
    url = args[0] if args else kwargs["url"]
    assert url == "https://api.deepseek.com/v1/chat/completions"

    headers = kwargs["headers"]
    assert headers["Authorization"] == "Bearer sk-deepseek-test"
    assert headers["Content-Type"] == "application/json"

    body = kwargs["json"]
    assert body["model"] == "deepseek-v4-flash"
    assert body["messages"][0] == {"role": "system", "content": "you are helpful"}
    assert body["messages"][1] == {"role": "user", "content": "hi"}
    assert "response_format" not in body  # json_mode default False


def test_openai_provider_with_json_mode(tmp_path, monkeypatch):
    """OpenAI provider, json_mode=True should add response_format and use /v1 path."""
    cfg_path = _write_config(
        tmp_path,
        {
            "provider": "openai",
            "default": "gpt-4o-mini",
            "base_url": "https://api.openai.com",
            "api_key": "sk-openai-test",
        },
    )
    monkeypatch.setattr(hermes_io, "HERMES_CONFIG_PATH", cfg_path)

    fake = _fake_response(content='{"k":"v"}')
    with mock.patch.object(hermes_io.httpx, "post", return_value=fake) as post_mock:
        result = hermes_io.call_llm("classify this", json_mode=True)

    assert result == '{"k":"v"}'
    _, kwargs = post_mock.call_args
    body = kwargs["json"]
    assert body["model"] == "gpt-4o-mini"
    assert body["response_format"] == {"type": "json_object"}
    # No system message → only one user message
    assert len(body["messages"]) == 1
    assert body["messages"][0]["role"] == "user"
    args, _ = post_mock.call_args
    assert args[0] == "https://api.openai.com/v1/chat/completions"


def test_base_url_with_v1_already_does_not_double_v1(tmp_path, monkeypatch):
    """If base_url already includes /v1 (openrouter style), don't add another /v1."""
    cfg_path = _write_config(
        tmp_path,
        {
            "provider": "openrouter",
            "default": "minimax/minimax-m2.7",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "sk-or-test",
        },
    )
    monkeypatch.setattr(hermes_io, "HERMES_CONFIG_PATH", cfg_path)

    fake = _fake_response(content="hi")
    with mock.patch.object(hermes_io.httpx, "post", return_value=fake) as post_mock:
        hermes_io.call_llm("hi")

    args, _ = post_mock.call_args
    assert args[0] == "https://openrouter.ai/api/v1/chat/completions"


def test_http_error_status_raises_llmcallerror(tmp_path, monkeypatch):
    """4xx status (deterministic — bad key / body) must raise on first try.
    No retry, no waiting. (5xx retry is covered separately.)"""
    cfg_path = _write_config(
        tmp_path,
        {
            "provider": "deepseek",
            "default": "deepseek-v4-flash",
            "base_url": "https://api.deepseek.com",
            "api_key": "sk-x",
        },
    )
    monkeypatch.setattr(hermes_io, "HERMES_CONFIG_PATH", cfg_path)

    fake = _fake_response(status=401, content="unauthorized")
    with mock.patch.object(hermes_io.httpx, "post", return_value=fake) as posts:
        with pytest.raises(hermes_io.LLMCallError) as exc:
            hermes_io.call_llm("hi")
    assert "401" in str(exc.value)
    assert posts.call_count == 1  # no retry on 4xx


def test_missing_config_raises_hermesconfigerror(tmp_path, monkeypatch):
    """Pointing at a non-existent file should raise HermesConfigError."""
    monkeypatch.setattr(
        hermes_io, "HERMES_CONFIG_PATH", tmp_path / "does_not_exist.yaml"
    )
    with pytest.raises(hermes_io.HermesConfigError):
        hermes_io.call_llm("hi")


def test_malformed_response_shape_raises(tmp_path, monkeypatch):
    cfg_path = _write_config(
        tmp_path,
        {
            "provider": "deepseek",
            "default": "deepseek-v4-flash",
            "base_url": "https://api.deepseek.com",
            "api_key": "sk-x",
        },
    )
    monkeypatch.setattr(hermes_io, "HERMES_CONFIG_PATH", cfg_path)

    bad = mock.Mock()
    bad.status_code = 200
    bad.text = "{}"
    bad.json.return_value = {}  # no "choices"
    with mock.patch.object(hermes_io.httpx, "post", return_value=bad):
        with pytest.raises(hermes_io.LLMCallError) as exc:
            hermes_io.call_llm("hi")
    assert "choices" in str(exc.value)


# ---------------------------------------------------------------------------
# Lark / Feishu direct-send tests (Approach A — bypass adapter chat_id lock)
# ---------------------------------------------------------------------------


def test_detect_lark_receive_id_type_classification():
    f = hermes_io._detect_lark_receive_id_type
    assert f("oc_f4de22018c4a9f9480450ef9f8c13231") == "chat_id"
    assert f("ou_abc123def456789012345678") == "open_id"
    assert f("on_unionid001") == "union_id"
    assert f("4ed67983") == "user_id"        # tenant short-hex
    assert f("8ea86e3b") == "user_id"
    assert f("alice@example.com") == "email"
    assert f("") == "user_id"                 # empty falls back


def test_send_lark_direct_uses_correct_receive_id_type():
    """When given a user_id (no oc_/ou_ prefix), _send_lark_direct must
    construct the request with receive_id_type='user_id' — proving we no
    longer rely on the adapter's chat_id-only send()."""
    fake_resp = mock.Mock()
    fake_resp.success = lambda: True
    fake_resp.data = mock.Mock(message_id="om_TESTMSGID")

    fake_client = mock.Mock()
    fake_client.im.v1.message.create.return_value = fake_resp

    fake_adapter = mock.Mock()
    fake_adapter._client = fake_client

    # Fake out lark_oapi imports — capture the request that was built.
    captured = {}

    class _FakeBody:
        @classmethod
        def builder(cls):
            b = mock.Mock()
            b.receive_id = lambda v: (captured.setdefault("receive_id", v), b)[1]
            b.msg_type = lambda v: (captured.setdefault("msg_type", v), b)[1]
            b.content = lambda v: (captured.setdefault("content", v), b)[1]
            b.build = lambda: "BODY"
            return b

    class _FakeReq:
        @classmethod
        def builder(cls):
            r = mock.Mock()
            r.receive_id_type = lambda v: (captured.setdefault("receive_id_type", v), r)[1]
            r.request_body = lambda v: (captured.setdefault("request_body", v), r)[1]
            r.build = lambda: "REQ"
            return r

    fake_module = mock.Mock()
    fake_module.CreateMessageRequestBody = _FakeBody
    fake_module.CreateMessageRequest = _FakeReq

    with mock.patch.dict(sys.modules, {"lark_oapi.api.im.v1": fake_module}):
        result = hermes_io._send_lark_direct(fake_adapter, "4ed67983", "hello")

    assert captured["receive_id"] == "4ed67983"
    assert captured["receive_id_type"] == "user_id"
    assert captured["msg_type"] == "text"
    assert json.loads(captured["content"]) == {"text": "hello"}
    fake_client.im.v1.message.create.assert_called_once_with("REQ")
    assert result["ok"] is True
    assert result["msg_id"] == "om_TESTMSGID"
    assert result["receive_id_type"] == "user_id"


def test_send_lark_direct_chat_id_uses_chat_id_type():
    """When given a chat_id (oc_…), receive_id_type should be chat_id."""
    fake_resp = mock.Mock()
    fake_resp.success = lambda: True
    fake_resp.data = mock.Mock(message_id="om_X")

    fake_client = mock.Mock()
    fake_client.im.v1.message.create.return_value = fake_resp

    fake_adapter = mock.Mock(_client=fake_client)

    captured = {}

    class _FakeBody:
        @classmethod
        def builder(cls):
            b = mock.Mock()
            b.receive_id = lambda v: b
            b.msg_type = lambda v: b
            b.content = lambda v: b
            b.build = lambda: "B"
            return b

    class _FakeReq:
        @classmethod
        def builder(cls):
            r = mock.Mock()
            r.receive_id_type = lambda v: (captured.setdefault("receive_id_type", v), r)[1]
            r.request_body = lambda v: r
            r.build = lambda: "R"
            return r

    fake_module = mock.Mock()
    fake_module.CreateMessageRequestBody = _FakeBody
    fake_module.CreateMessageRequest = _FakeReq

    with mock.patch.dict(sys.modules, {"lark_oapi.api.im.v1": fake_module}):
        out = hermes_io._send_lark_direct(fake_adapter, "oc_chatid001", "hi")

    assert captured["receive_id_type"] == "chat_id"
    assert out["ok"] is True


def test_send_lark_direct_propagates_failure():
    """An unsuccessful Lark response must surface ok=False with the error."""
    fake_resp = mock.Mock()
    fake_resp.success = lambda: False
    fake_resp.code = 230001
    fake_resp.msg = "invalid receive_id"
    fake_resp.data = None

    fake_client = mock.Mock()
    fake_client.im.v1.message.create.return_value = fake_resp
    fake_adapter = mock.Mock(_client=fake_client)

    class _FakeBody:
        @classmethod
        def builder(cls):
            b = mock.Mock()
            b.receive_id = lambda v: b
            b.msg_type = lambda v: b
            b.content = lambda v: b
            b.build = lambda: "B"
            return b

    class _FakeReq:
        @classmethod
        def builder(cls):
            r = mock.Mock()
            r.receive_id_type = lambda v: r
            r.request_body = lambda v: r
            r.build = lambda: "R"
            return r

    fake_module = mock.Mock()
    fake_module.CreateMessageRequestBody = _FakeBody
    fake_module.CreateMessageRequest = _FakeReq

    with mock.patch.dict(sys.modules, {"lark_oapi.api.im.v1": fake_module}):
        out = hermes_io._send_lark_direct(fake_adapter, "4ed67983", "x")

    assert out["ok"] is False
    assert "230001" in out["error"]


# ---------------------------------------------------------------------------
# Retry policy tests
# ---------------------------------------------------------------------------


def test_call_llm_retries_on_503_then_succeeds(tmp_path, monkeypatch):
    """A single 503 must trigger a retry that succeeds — not raise."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "model:\n"
        "  default: m\n"
        "  base_url: https://x\n"
        "  api_key: k\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(hermes_io, "HERMES_CONFIG_PATH", cfg)

    sleep_calls: list[float] = []
    monkeypatch.setattr(hermes_io, "_RETRY_BACKOFFS_S", (0.0, 0.0, 0.0))

    bad = mock.Mock()
    bad.status_code = 503
    bad.text = "service unavailable"

    good = mock.Mock()
    good.status_code = 200
    good.json.return_value = {"choices": [{"message": {"content": "ok"}}]}

    posts = mock.Mock(side_effect=[bad, good])
    monkeypatch.setattr(hermes_io.httpx, "post", posts)

    out = hermes_io.call_llm("hi")
    assert out == "ok"
    assert posts.call_count == 2  # one retry, then success


def test_call_llm_does_not_retry_on_400(tmp_path, monkeypatch):
    """4xx responses are deterministic (bad key / body) — must NOT retry."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "model:\n  default: m\n  base_url: https://x\n  api_key: k\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(hermes_io, "HERMES_CONFIG_PATH", cfg)

    bad = mock.Mock()
    bad.status_code = 400
    bad.text = "bad request"

    posts = mock.Mock(return_value=bad)
    monkeypatch.setattr(hermes_io.httpx, "post", posts)

    with pytest.raises(hermes_io.LLMCallError):
        hermes_io.call_llm("hi")
    assert posts.call_count == 1  # no retry


def test_call_llm_retries_on_request_error(tmp_path, monkeypatch):
    """httpx.RequestError (network) is retried; final raise after exhaustion."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "model:\n  default: m\n  base_url: https://x\n  api_key: k\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(hermes_io, "HERMES_CONFIG_PATH", cfg)
    monkeypatch.setattr(hermes_io, "_RETRY_BACKOFFS_S", (0.0, 0.0, 0.0))

    err = hermes_io.httpx.ConnectError("dns fail")
    posts = mock.Mock(side_effect=[err, err, err, err])
    monkeypatch.setattr(hermes_io.httpx, "post", posts)

    with pytest.raises(hermes_io.LLMCallError):
        hermes_io.call_llm("hi")
    assert posts.call_count == 4  # 1 initial + 3 retries


# ---------------------------------------------------------------------------
# Standalone Lark client fallback (cron / out-of-process callers can deliver)
# ---------------------------------------------------------------------------


def test_load_lark_env_creds_reads_app_id_secret(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".hermes").mkdir(parents=True)
    (fake_home / ".hermes" / ".env").write_text(
        "# header comment\n"
        "FEISHU_APP_ID=cli_TESTAPP\n"
        "FEISHU_APP_SECRET=  test_secret_quoted_  \n"
        'FEISHU_DOMAIN="lark"\n'
        "OTHER=ignored\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(hermes_io.Path, "home", classmethod(lambda cls: fake_home))
    creds = hermes_io._load_lark_env_creds()
    assert creds == {
        "FEISHU_APP_ID": "cli_TESTAPP",
        "FEISHU_APP_SECRET": "test_secret_quoted_",
        "FEISHU_DOMAIN": "lark",
    }


def test_load_lark_env_creds_returns_none_when_missing(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    monkeypatch.setattr(hermes_io.Path, "home", classmethod(lambda cls: fake_home))
    assert hermes_io._load_lark_env_creds() is None


def test_build_standalone_lark_client_caches_singleton(tmp_path, monkeypatch):
    """Repeat builds return the same client instance (token-cache continuity)."""
    monkeypatch.setattr(
        hermes_io, "_load_lark_env_creds",
        lambda: {"FEISHU_APP_ID": "cli_x", "FEISHU_APP_SECRET": "s", "FEISHU_DOMAIN": "feishu"},
    )
    monkeypatch.setattr(hermes_io, "_STANDALONE_LARK_CLIENT", None)

    fake_built = mock.Mock(name="fake_lark_client")
    fake_lark = mock.Mock()
    fake_builder = mock.Mock()
    fake_builder.app_id = lambda v: fake_builder
    fake_builder.app_secret = lambda v: fake_builder
    fake_builder.domain = lambda v: fake_builder
    fake_builder.build = lambda: fake_built
    fake_lark.Client.builder = lambda: fake_builder

    with mock.patch.dict(sys.modules, {"lark_oapi": fake_lark,
                                       "lark_oapi.core.const": mock.Mock(FEISHU_DOMAIN="d1", LARK_DOMAIN="d2")}):
        c1 = hermes_io._build_standalone_lark_client()
        c2 = hermes_io._build_standalone_lark_client()
    assert c1 is c2 is fake_built


def test_build_standalone_lark_client_raises_without_creds(monkeypatch):
    monkeypatch.setattr(hermes_io, "_load_lark_env_creds", lambda: None)
    monkeypatch.setattr(hermes_io, "_STANDALONE_LARK_CLIENT", None)
    with pytest.raises(hermes_io.SendDmError) as exc:
        hermes_io._build_standalone_lark_client()
    assert "FEISHU_APP_ID" in str(exc.value)


def test_send_dm_falls_back_to_standalone_when_no_gateway(tmp_path, monkeypatch):
    """When _get_gateway_adapter raises (no in-process runner), send_dm
    must try the standalone client path BEFORE giving up to the queue."""
    # No gateway in this process.
    def _no_adapter(_):
        raise hermes_io.SendDmError("no live GatewayRunner")
    monkeypatch.setattr(hermes_io, "_get_gateway_adapter", _no_adapter)

    # Standalone path returns ok.
    monkeypatch.setattr(
        hermes_io, "_send_lark_standalone",
        lambda rid, msg: {"ok": True, "msg_id": "om_STANDALONE", "platform": "feishu",
                          "receive_id_type": "user_id"},
    )

    # Point storage at tmp so we can detect "did NOT queue" by absence of file.
    monkeypatch.setattr(hermes_io, "_enqueue_outbound_fallback",
                        lambda *a, **kw: pytest.fail("should not have queued"))

    out = hermes_io.send_dm("feishu", "4ed67983", "hi")
    assert out["ok"] is True
    assert out["msg_id"] == "om_STANDALONE"
