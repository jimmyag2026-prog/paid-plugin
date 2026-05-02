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

    fake = _fake_response(status=500, content="internal server error")
    with mock.patch.object(hermes_io.httpx, "post", return_value=fake):
        with pytest.raises(hermes_io.LLMCallError) as exc:
            hermes_io.call_llm("hi")
    assert "500" in str(exc.value)


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
