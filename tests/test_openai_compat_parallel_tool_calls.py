"""OpenRouter gets parallel_tool_calls; OpenCode Go and unknown relays do not.

Hermetic: capture the JSON body from chat and chat_stream. No network.
"""
from __future__ import annotations

import json
import urllib.request

from pmharness.drivers.openai_compat import OpenAICompatDriver


_TOOLS = [{"type": "function", "function": {"name": "run_command", "parameters": {}}}]


class _JsonResp:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _SseResp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        yield (
            b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n'
        )
        yield b"data: [DONE]\n"


def _ok_payload():
    return {
        "choices": [{
            "message": {"content": "ok", "tool_calls": []},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


def _driver(base_url: str) -> OpenAICompatDriver:
    d = OpenAICompatDriver(
        name="t",
        model="some-model",
        base_url=base_url,
        api_key_env="TEST_OAI_KEY",
    )
    d._key = lambda: "fake-key"
    return d


def _capture_chat(monkeypatch, driver, *, stream=False):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured.update(json.loads(req.data.decode("utf-8")))
        return _SseResp() if stream else _JsonResp(_ok_payload())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    if stream:
        resp = driver.chat_stream(
            [{"role": "user", "content": "hi"}],
            tools=_TOOLS,
            on_delta=lambda _t: None,
        )
    else:
        resp = driver.chat([{"role": "user", "content": "hi"}], tools=_TOOLS)
    assert not resp.error
    return captured


def test_openrouter_chat_sends_parallel_tool_calls(monkeypatch):
    captured = _capture_chat(
        monkeypatch, _driver("https://openrouter.ai/api/v1"),
    )
    assert captured["parallel_tool_calls"] is True
    assert captured["tools"] == _TOOLS


def test_openrouter_chat_stream_sends_parallel_tool_calls(monkeypatch):
    captured = _capture_chat(
        monkeypatch, _driver("https://openrouter.ai/api/v1"), stream=True,
    )
    assert captured["parallel_tool_calls"] is True
    assert captured["stream_options"] == {"include_usage": True}


def test_opencode_go_chat_omits_parallel_tool_calls(monkeypatch):
    captured = _capture_chat(
        monkeypatch, _driver("https://opencode.ai/zen/go/v1"),
    )
    assert "parallel_tool_calls" not in captured


def test_opencode_go_chat_stream_omits_parallel_tool_calls(monkeypatch):
    captured = _capture_chat(
        monkeypatch, _driver("https://opencode.ai/zen/go/v1"), stream=True,
    )
    assert "parallel_tool_calls" not in captured
    assert "stream_options" not in captured


def test_openai_chat_stream_keeps_stream_options(monkeypatch):
    captured = _capture_chat(
        monkeypatch, _driver("https://api.openai.com/v1"), stream=True,
    )
    assert captured["stream_options"] == {"include_usage": True}
    assert "parallel_tool_calls" not in captured


def test_unknown_relay_omits_parallel_tool_calls(monkeypatch):
    captured = _capture_chat(monkeypatch, _driver("http://x/v1"))
    assert "parallel_tool_calls" not in captured


def test_openrouter_without_tools_omits_parallel_tool_calls(monkeypatch):
    captured = {}
    driver = _driver("https://openrouter.ai/api/v1")

    def fake_urlopen(req, timeout=None):
        captured.update(json.loads(req.data.decode("utf-8")))
        return _JsonResp(_ok_payload())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    resp = driver.chat([{"role": "user", "content": "hi"}])
    assert not resp.error
    assert "parallel_tool_calls" not in captured
