"""Hermetic tests for BedrockDriver ConverseStream + streaming callbacks.

Mocks the AWS eventstream body — no live Bedrock / network. Python 3.9 safe.
"""
from __future__ import annotations

import io
import json
from types import SimpleNamespace
from unittest.mock import patch

from pmharness.drivers.bedrock import (
    BedrockDriver,
    encode_eventstream_message,
    iter_eventstream_messages,
)


def _event(event_type: str, body: dict) -> bytes:
    headers = {
        ":message-type": "event",
        ":event-type": event_type,
        ":content-type": "application/json",
    }
    return encode_eventstream_message(headers, json.dumps(body).encode("utf-8"))


def _stream(events) -> io.BytesIO:
    blob = b"".join(_event(etype, body) for etype, body in events)
    return io.BytesIO(blob)


def _driver() -> BedrockDriver:
    return BedrockDriver("bedrock:claude", "anthropic.claude-sonnet-4-20250514-v1:0")


def _force_local_converse_stream():
    """Hide PM ``bedrock_chat_stream`` so the in-driver ConverseStream path runs."""
    return patch("puppetmaster.bedrock.bedrock_chat_stream", None)


def test_supports_streaming_flag():
    d = _driver()
    assert d.supports_streaming is True
    assert callable(d.chat_stream)


def test_eventstream_roundtrip():
    payload = json.dumps({"delta": {"text": "Hi"}}).encode("utf-8")
    msg = encode_eventstream_message(
        {
            ":message-type": "event",
            ":event-type": "contentBlockDelta",
            ":content-type": "application/json",
        },
        payload,
    )
    msgs = list(iter_eventstream_messages(io.BytesIO(msg)))
    assert len(msgs) == 1
    headers, raw = msgs[0]
    assert headers[":event-type"] == "contentBlockDelta"
    assert json.loads(raw.decode("utf-8"))["delta"]["text"] == "Hi"


def test_chat_stream_emits_text_deltas(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    d = _driver()

    events = [
        ("messageStart", {"role": "assistant"}),
        ("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "Hello"}}),
        ("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": ", world"}}),
        ("messageStop", {"stopReason": "end_turn"}),
        (
            "metadata",
            {
                "usage": {
                    "inputTokens": 10,
                    "outputTokens": 5,
                    "cacheReadInputTokens": 100,
                    "cacheWriteInputTokens": 20,
                }
            },
        ),
    ]
    captured = []
    with _force_local_converse_stream():
        with patch("urllib.request.urlopen", return_value=_stream(events)):
            with patch(
                "puppetmaster.bedrock._auth_headers_for_request",
                return_value={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer x",
                },
            ):
                with patch(
                    "puppetmaster.bedrock._resolve_call_credentials",
                    return_value=SimpleNamespace(
                        kind="bearer",
                        bearer_token="x",
                        access_key_id=None,
                        secret_access_key=None,
                        session_token=None,
                    ),
                ):
                    with patch(
                        "puppetmaster.bedrock.resolve_bedrock_credentials",
                        return_value=SimpleNamespace(kind="bearer"),
                    ):
                        resp = d.chat_stream(
                            [{"role": "user", "content": "hi"}],
                            on_delta=lambda t: captured.append(t),
                        )

    assert captured == ["Hello", ", world"]
    assert resp.text == "Hello, world"
    assert resp.error is None
    assert resp.tokens_out == 5
    # inputTokens 10 + cache read 100 + cache write 20
    assert resp.tokens_in == 130
    assert resp.meta["cache_read_tokens"] == 100
    assert resp.meta["cache_write_tokens"] == 20
    assert resp.meta["stream_started"] is True
    assert resp.meta.get("stream_fallback") is not True


def test_chat_stream_reasoning_and_tool_hint(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    d = _driver()

    events = [
        ("messageStart", {"role": "assistant"}),
        (
            "contentBlockDelta",
            {
                "contentBlockIndex": 0,
                "delta": {"reasoningContent": {"text": "think-a"}},
            },
        ),
        (
            "contentBlockDelta",
            {
                "contentBlockIndex": 0,
                "delta": {"reasoningContent": {"text": "think-b"}},
            },
        ),
        (
            "contentBlockStart",
            {
                "contentBlockIndex": 1,
                "start": {
                    "toolUse": {"toolUseId": "tu_1", "name": "run_command"},
                },
            },
        ),
        (
            "contentBlockDelta",
            {
                "contentBlockIndex": 1,
                "delta": {"toolUse": {"input": '{"command":'}},
            },
        ),
        (
            "contentBlockDelta",
            {
                "contentBlockIndex": 1,
                "delta": {"toolUse": {"input": ' "ls"}'}},
            },
        ),
        (
            "contentBlockDelta",
            {"contentBlockIndex": 2, "delta": {"text": "Running"}},
        ),
        ("messageStop", {"stopReason": "tool_use"}),
        ("metadata", {"usage": {"inputTokens": 3, "outputTokens": 8}}),
    ]
    deltas = []
    reasoning = []
    hints = []
    with _force_local_converse_stream():
        with patch("urllib.request.urlopen", return_value=_stream(events)):
            with patch(
                "puppetmaster.bedrock._resolve_call_credentials",
                return_value=SimpleNamespace(
                    kind="bearer",
                    bearer_token="x",
                    access_key_id=None,
                    secret_access_key=None,
                    session_token=None,
                ),
            ):
                with patch(
                    "puppetmaster.bedrock.resolve_bedrock_credentials",
                    return_value=SimpleNamespace(kind="bearer"),
                ):
                    resp = d.chat_stream(
                        [{"role": "user", "content": "list"}],
                        tools=[
                            {
                                "type": "function",
                                "function": {
                                    "name": "run_command",
                                    "description": "run",
                                    "parameters": {
                                        "type": "object",
                                        "properties": {},
                                    },
                                },
                            }
                        ],
                        on_delta=lambda t: deltas.append(t),
                        on_reasoning_delta=lambda t: reasoning.append(t),
                        on_tool_hint=lambda n: hints.append(n),
                    )

    assert reasoning == ["think-a", "think-b"]
    assert hints == ["run_command"]
    assert deltas == ["Running"]
    tcs = resp.meta["tool_calls"]
    assert len(tcs) == 1
    assert tcs[0]["function"]["name"] == "run_command"
    assert json.loads(tcs[0]["function"]["arguments"]) == {"command": "ls"}
    assert resp.meta["finish_reason"] == "tool_use"


def test_chat_stream_falls_back_on_stream_failure(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    d = _driver()

    fake_turn = SimpleNamespace(
        text="fallback text",
        tool_calls=[{"id": "c1", "name": "read_file", "arguments": {"path": "a"}}],
        finish_reason="end_turn",
        usage={
            "prompt_tokens": 11,
            "completion_tokens": 4,
            "cached_tokens": 7,
            "cache_write_tokens": 2,
        },
    )
    deltas = []
    hints = []

    with patch(
        "puppetmaster.bedrock.resolve_bedrock_credentials",
        return_value=SimpleNamespace(kind="bearer"),
    ):
        with patch(
            "puppetmaster.bedrock.bedrock_chat_stream",
            side_effect=OSError("boom"),
        ):
            with patch("urllib.request.urlopen", side_effect=OSError("boom")):
                with patch(
                    "puppetmaster.bedrock.bedrock_chat", return_value=fake_turn
                ) as chat_mock:
                    resp = d.chat_stream(
                        [{"role": "user", "content": "hi"}],
                        on_delta=lambda t: deltas.append(t),
                        on_reasoning_delta=lambda t: None,
                        on_tool_hint=lambda n: hints.append(n),
                    )

    assert chat_mock.called
    assert deltas == ["fallback text"]
    assert hints == ["read_file"]
    assert resp.text == "fallback text"
    assert resp.error is None
    assert resp.meta["stream_fallback"] is True
    assert resp.meta["cache_read_tokens"] == 7
    assert resp.meta["cache_write_tokens"] == 2


def test_prefers_bedrock_chat_stream_when_present(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    d = _driver()

    captured = []
    reasoning = []

    def fake_stream(**kwargs):
        on_delta = kwargs.get("on_delta")
        if on_delta:
            on_delta("reasoning", "think")
            on_delta("text", "from-pm")
        return SimpleNamespace(
            text="from-pm",
            tool_calls=[],
            finish_reason="end_turn",
            usage={
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
            },
        )

    with patch(
        "puppetmaster.bedrock.resolve_bedrock_credentials",
        return_value=SimpleNamespace(kind="bearer"),
    ):
        with patch(
            "puppetmaster.bedrock.bedrock_chat_stream",
            side_effect=fake_stream,
        ) as stream_mock:
            with patch("urllib.request.urlopen") as urlopen_mock:
                resp = d.chat_stream(
                    [{"role": "user", "content": "hi"}],
                    on_delta=lambda t: captured.append(t),
                    on_reasoning_delta=lambda t: reasoning.append(t),
                )

    assert stream_mock.called
    assert not urlopen_mock.called
    assert captured == ["from-pm"]
    assert reasoning == ["think"]
    assert resp.text == "from-pm"
    assert resp.meta["stream_started"] is True


def test_pm_stream_routes_kind_and_preserves_cache(monkeypatch):
    """When PM stream exists, kind-tagged deltas + cache fields map correctly."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    d = _driver()
    deltas = []
    hints = []

    def fake_stream(**kwargs):
        on_delta = kwargs.get("on_delta")
        if on_delta:
            on_delta("text", "Hi")
        return SimpleNamespace(
            text="Hi",
            tool_calls=[{"id": "t1", "name": "read_file", "arguments": {}}],
            finish_reason="tool_use",
            usage={
                "prompt_tokens": 50,
                "completion_tokens": 3,
                "cached_tokens": 40,
                "cache_write_tokens": 5,
            },
        )

    with patch(
        "puppetmaster.bedrock.resolve_bedrock_credentials",
        return_value=SimpleNamespace(kind="bearer"),
    ):
        with patch(
            "puppetmaster.bedrock.bedrock_chat_stream",
            side_effect=fake_stream,
        ):
            resp = d.chat_stream(
                [{"role": "user", "content": "hi"}],
                on_delta=lambda t: deltas.append(t),
                on_reasoning_delta=lambda t: None,
                on_tool_hint=lambda n: hints.append(n),
            )

    assert deltas == ["Hi"]
    assert hints == ["read_file"]
    assert resp.meta["cache_read_tokens"] == 40
    assert resp.meta["cache_write_tokens"] == 5
    assert resp.tokens_in == 50


def test_module_docstring_no_longer_claims_non_sse_only():
    import pmharness.drivers.bedrock as mod

    doc = mod.__doc__ or ""
    assert "non-SSE" not in doc
    assert "ConverseStream" in doc or "converse-stream" in doc
