"""OpenRouter gets parallel_tool_calls; OpenCode Go and unknown relays do not.

Hermetic: capture the JSON body from chat and chat_stream. No network.
"""
from __future__ import annotations

import json
import urllib.request

import pytest

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


_ABSENT = object()
_OPENROUTER = "https://openrouter.ai/api/v1"
_OPENCODE_GO = "https://opencode.ai/zen/go/v1"
_USAGE = {"include_usage": True}


@pytest.mark.parametrize("base_url, stream, expected", [
    pytest.param(
        _OPENROUTER, False, {"parallel_tool_calls": True, "tools": _TOOLS},
        id="openrouter_chat_sends",
    ),
    pytest.param(
        _OPENROUTER, True, {"parallel_tool_calls": True, "stream_options": _USAGE},
        id="openrouter_chat_stream_sends",
    ),
    pytest.param(
        _OPENCODE_GO, False, {"parallel_tool_calls": _ABSENT},
        id="opencode_go_chat_omits",
    ),
    pytest.param(
        _OPENCODE_GO, True, {"parallel_tool_calls": _ABSENT, "stream_options": _ABSENT},
        id="opencode_go_chat_stream_omits",
    ),
    pytest.param(
        "https://api.openai.com/v1", True,
        {"stream_options": _USAGE, "parallel_tool_calls": _ABSENT},
        id="openai_chat_stream_keeps_stream_options",
    ),
    pytest.param(
        "http://x/v1", False, {"parallel_tool_calls": _ABSENT},
        id="unknown_relay_omits",
    ),
])
def test_parallel_tool_calls_by_relay(monkeypatch, base_url, stream, expected):
    captured = _capture_chat(monkeypatch, _driver(base_url), stream=stream)
    for key, want in expected.items():
        if want is _ABSENT:
            assert key not in captured
        else:
            assert captured[key] == want
            assert type(captured[key]) is type(want)


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
