"""Codex Responses driver: SSE stream required + pool resolve (mocked HTTP)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from harness import credential_pool as cp
from pmharness.drivers.codex_responses import (
    CodexResponsesDriver,
    _consume_codex_sse,
    _extract_text_and_tools,
    _messages_to_responses_input,
)


@pytest.fixture
def pool_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("OPENAI_CODEX_TOKEN", raising=False)
    cp.clear_pools_for_tests()
    yield tmp_path
    cp.clear_pools_for_tests()


def test_extract_text_and_tools():
    raw = {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "hello"}],
            },
            {
                "type": "function_call",
                "call_id": "c1",
                "name": "read_file",
                "arguments": "{\"path\":\"a.py\"}",
            },
        ],
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }
    text, tools, finish = _extract_text_and_tools(raw)
    assert text == "hello"
    assert tools[0]["function"]["name"] == "read_file"
    assert finish == "completed"


def test_messages_to_input_skips_system():
    inp = _messages_to_responses_input([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ])
    assert len(inp) == 1
    assert inp[0]["role"] == "user"


def test_consume_sse_assembles_text_and_usage():
    lines = [
        b'data: {"type":"response.output_text.delta","delta":"hel"}\n',
        b'data: {"type":"response.output_text.delta","delta":"lo"}\n',
        b'data: {"type":"response.output_item.done","item":{"type":"message","content":[{"type":"output_text","text":"hello"}]}}\n',
        b'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":2,"output_tokens":1}}}\n',
    ]
    raw = _consume_codex_sse(lines)
    assert raw["status"] == "completed"
    assert raw["output_text"] == "hello"
    assert raw["usage"]["input_tokens"] == 2
    text, _, _ = _extract_text_and_tools(raw)
    assert text == "hello"


def test_consume_sse_routes_commentary_to_reasoning():
    reasoning = []
    text = []
    lines = [
        b'data: {"type":"response.output_item.added","item":{"type":"message","phase":"commentary"}}\n',
        b'data: {"type":"response.output_text.delta","delta":"thinking..."}\n',
        b'data: {"type":"response.output_item.added","item":{"type":"message","phase":"final_answer"}}\n',
        b'data: {"type":"response.output_text.delta","delta":"answer"}\n',
        b'data: {"type":"response.completed","response":{"status":"completed","usage":{}}}\n',
    ]
    raw = _consume_codex_sse(
        lines,
        on_delta=text.append,
        on_reasoning_delta=reasoning.append,
    )
    assert reasoning == ["thinking..."]
    assert text == ["answer"]
    assert raw["output_text"] == "answer"


def test_driver_complete_sends_stream_true(pool_dir, monkeypatch):
    cp.add_oauth_entry(
        "openai-codex",
        access_token="eyJhbGciOiJub25lIn0.eyJodHRwczovL2FwaS5vcGVuYWkuY29tL2F1dGgiOnsiY2hhdGdwdF9hY2NvdW50X2lkIjoiYWNjMSJ9fQ.",
        label="codex-1",
    )
    monkeypatch.setenv("HARNESS_CODEX_REASONING_EFFORT", "low")
    d = CodexResponsesDriver(name="codex", model="gpt-5.5")
    assert d.supports_streaming is True

    class _Resp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def __iter__(self):
            return iter([
                b'data: {"type":"response.output_text.delta","delta":"ok"}\n',
                b'data: {"type":"response.output_item.done","item":{"type":"message","content":[{"type":"output_text","text":"ok"}]}}\n',
                b'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":1,"output_tokens":1}}}\n',
            ])

    captured = {}

    def fake_urlopen(req, timeout=None):
        assert "chatgpt.com" in req.full_url or "backend-api/codex" in req.full_url
        body = json.loads(req.data.decode("utf-8"))
        captured["body"] = body
        assert body.get("stream") is True
        assert "max_output_tokens" not in body
        assert body.get("reasoning", {}).get("effort") == "low"
        assert body.get("reasoning", {}).get("summary") == "auto"
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    resp = d.complete("ping")
    assert resp.error is None
    assert resp.text == "ok"
    assert captured["body"]["model"] == "gpt-5.5"


def test_chat_stream_emits_deltas(pool_dir, monkeypatch):
    cp.add_oauth_entry(
        "openai-codex",
        access_token="eyJhbGciOiJub25lIn0.eyJodHRwczovL2FwaS5vcGVuYWkuY29tL2F1dGgiOnsiY2hhdGdwdF9hY2NvdW50X2lkIjoiYWNjMSJ9fQ.",
        label="codex-1",
    )
    d = CodexResponsesDriver(name="codex", model="gpt-5.5")
    deltas = []

    class _Resp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def __iter__(self):
            return iter([
                b'data: {"type":"response.output_text.delta","delta":"a"}\n',
                b'data: {"type":"response.output_text.delta","delta":"b"}\n',
                b'data: {"type":"response.completed","response":{"status":"completed","usage":{}}}\n',
            ])

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())
    resp = d.chat_stream(
        [{"role": "user", "content": "hi"}],
        on_delta=deltas.append,
    )
    assert resp.error is None
    assert deltas == ["a", "b"]
    assert resp.text == "ab"


@pytest.mark.parametrize("effort,api_effort", [
    ("medium", "medium"),
    ("high", "high"),
    ("xhigh", "xhigh"),
    ("max", "max"),
])
def test_driver_build_body_reasoning_effort(pool_dir, monkeypatch, effort, api_effort):
    cp.add_oauth_entry(
        "openai-codex",
        access_token="eyJhbGciOiJub25lIn0.eyJodHRwczovL2FwaS5vcGVuYWkuY29tL2F1dGgiOnsiY2hhdGdwdF9hY2NvdW50X2lkIjoiYWNjMSJ9fQ.",
        label="codex-1",
    )
    monkeypatch.setenv("HARNESS_CODEX_REASONING_EFFORT", effort)
    d = CodexResponsesDriver(name="codex", model="gpt-5.5")
    captured = {}

    class _Resp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def __iter__(self):
            return iter([
                b'data: {"type":"response.completed","response":{"status":"completed","usage":{}}}\n',
            ])

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    d.complete("ping")
    assert captured["body"]["reasoning"]["effort"] == api_effort


def test_driver_none_omits_reasoning_block(pool_dir, monkeypatch):
    cp.add_oauth_entry(
        "openai-codex",
        access_token="eyJhbGciOiJub25lIn0.eyJodHRwczovL2FwaS5vcGVuYWkuY29tL2F1dGgiOnsiY2hhdGdwdF9hY2NvdW50X2lkIjoiYWNjMSJ9fQ.",
        label="codex-1",
    )
    monkeypatch.setenv("HARNESS_CODEX_REASONING_EFFORT", "none")
    d = CodexResponsesDriver(name="codex", model="gpt-5.5")
    captured = {}

    class _Resp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def __iter__(self):
            return iter([
                b'data: {"type":"response.completed","response":{"status":"completed","usage":{}}}\n',
            ])

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    d.complete("ping")
    assert "reasoning" not in captured["body"]
