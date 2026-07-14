"""Hermetic tests for GeminiDriver.chat_stream (streamGenerateContent SSE)."""
import io
import json
from unittest.mock import patch

from pmharness.drivers.gemini import GeminiDriver


def _sse(chunks):
    """Encode a list of GenerateContent-shaped dicts as Gemini SSE bytes."""
    lines = []
    for chunk in chunks:
        lines.append(f"data: {json.dumps(chunk)}")
        lines.append("")
    return io.BytesIO(("\n".join(lines) + "\n").encode("utf-8"))


def _driver(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-gemini-key")
    return GeminiDriver("gemini-3.5-flash", "gemini-3.5-flash")


def test_supports_streaming_flag():
    d = GeminiDriver("gemini-3.5-flash", "gemini-3.5-flash")
    assert d.supports_streaming is True
    assert callable(d.chat_stream)


def test_chat_stream_hits_stream_endpoint(monkeypatch):
    d = _driver(monkeypatch)
    captured = []

    def mock_urlopen(req, timeout=None):
        captured.append(req)
        return _sse([{
            "candidates": [{
                "content": {"parts": [{"text": "hi"}], "role": "model"},
                "finishReason": "STOP",
            }],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1},
        }])

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        d.chat_stream([{"role": "user", "content": "hey"}], on_delta=lambda t: None)

    assert len(captured) == 1
    url = captured[0].full_url
    assert ":streamGenerateContent" in url
    assert "alt=sse" in url
    assert "key=fake-gemini-key" in url
    body = json.loads(captured[0].data.decode("utf-8"))
    assert body["generationConfig"]["thinkingConfig"] == {"includeThoughts": True}


def test_chat_stream_emits_text_deltas(monkeypatch):
    d = _driver(monkeypatch)
    chunks = [
        {"candidates": [{"content": {"parts": [{"text": "Hello"}]}}]},
        {
            "candidates": [{
                "content": {"parts": [{"text": ", world"}]},
                "finishReason": "STOP",
            }],
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 5,
                "cachedContentTokenCount": 2,
            },
        },
    ]
    captured = []
    with patch("urllib.request.urlopen", return_value=_sse(chunks)):
        resp = d.chat_stream(
            [{"role": "user", "content": "hi"}],
            on_delta=lambda t: captured.append(t),
        )

    assert captured == ["Hello", ", world"]
    assert resp.text == "Hello, world"
    assert resp.error is None
    assert resp.tokens_in == 10
    assert resp.tokens_out == 5
    assert resp.meta["cache_read_tokens"] == 2
    assert resp.meta["finish_reason"] == "STOP"
    assert resp.meta["stream_started"] is True


def test_chat_stream_emits_reasoning_deltas(monkeypatch):
    d = _driver(monkeypatch)
    chunks = [
        {"candidates": [{"content": {"parts": [
            {"text": "Let me think…", "thought": True},
        ]}}]},
        {"candidates": [{"content": {"parts": [
            {"text": " more.", "thought": True},
            {"text": "Answer"},
        ], "role": "model"}, "finishReason": "STOP"}]},
    ]
    text_deltas = []
    reasoning = []
    with patch("urllib.request.urlopen", return_value=_sse(chunks)):
        resp = d.chat_stream(
            [{"role": "user", "content": "q"}],
            on_delta=lambda t: text_deltas.append(t),
            on_reasoning_delta=lambda t: reasoning.append(t),
        )

    assert reasoning == ["Let me think…", " more."]
    assert text_deltas == ["Answer"]
    assert resp.text == "Answer"
    assert resp.meta["reasoning"] == "Let me think… more."


def test_chat_stream_assembles_tool_calls_and_hints(monkeypatch):
    d = _driver(monkeypatch)
    chunks = [
        {"candidates": [{"content": {"parts": [
            {"text": "Calling tool."},
            {
                "thoughtSignature": "SIG_XYZ",
                "functionCall": {
                    "name": "read_file",
                    "args": {"path": "foo.txt"},
                },
            },
        ], "role": "model"}, "finishReason": "STOP"}]},
    ]
    hints = []
    with patch("urllib.request.urlopen", return_value=_sse(chunks)):
        resp = d.chat_stream(
            [{"role": "user", "content": "read foo"}],
            tools=[{
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "read",
                    "parameters": {"type": "object", "properties": {}},
                },
            }],
            on_delta=lambda t: None,
            on_tool_hint=lambda n: hints.append(n),
        )

    assert hints == ["read_file"]
    assert resp.text == "Calling tool."
    tcs = resp.meta["tool_calls"]
    assert len(tcs) == 1
    assert tcs[0]["function"]["name"] == "read_file"
    assert json.loads(tcs[0]["function"]["arguments"]) == {"path": "foo.txt"}
    assert tcs[0]["thought_signature"] == "SIG_XYZ"


def test_chat_stream_http_error(monkeypatch):
    import urllib.error

    d = _driver(monkeypatch)

    class FakeHTTPError(urllib.error.HTTPError):
        def __init__(self):
            # HTTPError wants fp with read(); use BytesIO
            super().__init__(
                "https://example", 503, "busy", hdrs={}, fp=io.BytesIO(b'{"error":"busy"}')
            )

    with patch("urllib.request.urlopen", side_effect=FakeHTTPError()):
        resp = d.chat_stream([{"role": "user", "content": "hi"}], on_delta=lambda t: None)

    assert resp.error and "HTTP 503" in resp.error
    assert resp.meta.get("stream_started") is False
