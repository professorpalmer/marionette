"""Tests for AnthropicDriver streaming + native tool-calling parity.

Validates that chat_stream emits text deltas incrementally and assembles the
same text + tool_calls that chat() would, using Anthropic's SSE event protocol."""
import io
import json
from unittest.mock import patch

from pmharness.drivers.anthropic import AnthropicDriver


def _sse(events):
    """Encode a list of (event_type, data_dict) as an Anthropic SSE byte stream."""
    lines = []
    for _etype, data in events:
        lines.append(f"data: {json.dumps(data)}")
        lines.append("")
    return io.BytesIO(("\n".join(lines) + "\n").encode("utf-8"))


def test_supports_streaming_flag():
    d = AnthropicDriver("anthropic:claude-opus-4-8", "claude-opus-4-8")
    assert d.supports_streaming is True
    assert callable(d.chat_stream)


def test_chat_stream_emits_text_deltas(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    d = AnthropicDriver("anthropic:claude-opus-4-8", "claude-opus-4-8")

    events = [
        ("message_start", {"type": "message_start",
                            "message": {"usage": {"input_tokens": 10}}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "Hello"}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": ", world"}}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                           "usage": {"output_tokens": 5}}),
    ]

    captured = []
    with patch("urllib.request.urlopen", return_value=_sse(events)):
        resp = d.chat_stream([{"role": "user", "content": "hi"}],
                             on_delta=lambda t: captured.append(t))

    assert captured == ["Hello", ", world"]
    assert resp.text == "Hello, world"
    assert resp.error is None
    assert resp.tokens_out == 5


def test_chat_stream_assembles_tool_calls(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    d = AnthropicDriver("anthropic:claude-opus-4-8", "claude-opus-4-8")

    events = [
        ("message_start", {"type": "message_start", "message": {"usage": {}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "tool_use", "id": "tu_1",
                                                   "name": "run_command"}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "input_json_delta",
                                           "partial_json": '{"command":'}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "input_json_delta",
                                           "partial_json": ' "ls -la"}'}}),
        ("message_delta", {"type": "message_delta",
                           "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 8}}),
    ]

    with patch("urllib.request.urlopen", return_value=_sse(events)):
        resp = d.chat_stream([{"role": "user", "content": "list files"}],
                             tools=[{"type": "function", "function": {
                                 "name": "run_command", "description": "run",
                                 "parameters": {"type": "object", "properties": {}}}}])

    tcs = resp.meta["tool_calls"]
    assert len(tcs) == 1
    assert tcs[0]["function"]["name"] == "run_command"
    assert json.loads(tcs[0]["function"]["arguments"]) == {"command": "ls -la"}


def test_chat_stream_handles_error_event(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    d = AnthropicDriver("anthropic:claude-opus-4-8", "claude-opus-4-8")
    events = [("error", {"type": "error",
                         "error": {"type": "overloaded_error", "message": "busy"}})]
    with patch("urllib.request.urlopen", return_value=_sse(events)):
        resp = d.chat_stream([{"role": "user", "content": "hi"}])
    assert resp.error and "overloaded_error" in resp.error
