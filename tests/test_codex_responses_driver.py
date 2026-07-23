"""Codex Responses driver: SSE stream required + pool resolve (mocked HTTP)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from harness import credential_pool as cp
from pmharness.drivers.codex_responses import (
    CodexResponsesDriver,
    _codex_tool_hint_goal,
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


def test_codex_tool_hint_goal_from_arguments():
    assert _codex_tool_hint_goal('{"command":"git status"}', "run_command") == "git status"
    assert _codex_tool_hint_goal(
        '{"goal":"prefer marionette child"}', "run_implement",
    ) == "prefer marionette child"
    assert _codex_tool_hint_goal('{"path":"harness/x.py"}', "read_file") == "harness/x.py"
    assert _codex_tool_hint_goal("{}", "run_command") == ""


def test_extract_excludes_commentary_from_final_text():
    """Completed commentary must not contaminate DriverResponse.text."""
    raw = {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "phase": "commentary",
                "status": "completed",
                "content": [{"type": "output_text", "text": "Checking. "}],
            },
            {
                "type": "message",
                "phase": "final_answer",
                "status": "completed",
                "content": [{"type": "output_text", "text": "Done."}],
            },
        ],
    }
    text, tools, finish = _extract_text_and_tools(raw)
    assert text == "Done."
    assert tools == []
    assert finish == "completed"


def test_extract_excludes_analysis_and_keeps_phaseless_legacy():
    """Analysis excluded; phase-less legacy messages still count as answer."""
    raw = {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "phase": "analysis",
                "content": [{"type": "output_text", "text": "thinking aloud "}],
            },
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "legacy answer"}],
            },
        ],
    }
    text, _, _ = _extract_text_and_tools(raw)
    assert text == "legacy answer"


def test_extract_empty_final_does_not_fallback_to_commentary():
    """Empty final_answer must not fall back to commentary prose."""
    raw = {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "phase": "commentary",
                "content": [{"type": "output_text", "text": "Checking. "}],
            },
            {
                "type": "message",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": ""}],
            },
        ],
        "output_text": "Checking. ",
    }
    text, _, _ = _extract_text_and_tools(raw)
    assert text == ""


def test_commentary_streams_via_delta_but_extract_is_final_only():
    """Commentary arrives on progress/on_delta; extract text is final_answer."""
    progress = []
    answers = []

    def on_delta(payload):
        if isinstance(payload, dict) and payload.get("channel") == "progress":
            progress.append(payload)
        else:
            answers.append(payload)

    lines = [
        b'data: {"type":"response.output_item.added","item":{"type":"message","phase":"commentary","id":"msg_c"}}\n',
        b'data: {"type":"response.output_text.delta","item_id":"msg_c","delta":"Checking. "}\n',
        b'data: {"type":"response.output_item.done","item":{"type":"message","phase":"commentary","id":"msg_c","content":[{"type":"output_text","text":"Checking. "}]}}\n',
        b'data: {"type":"response.output_item.added","item":{"type":"message","phase":"final_answer","id":"msg_f"}}\n',
        b'data: {"type":"response.output_text.delta","item_id":"msg_f","delta":"Done."}\n',
        b'data: {"type":"response.output_item.done","item":{"type":"message","phase":"final_answer","id":"msg_f","content":[{"type":"output_text","text":"Done."}]}}\n',
        b'data: {"type":"response.completed","response":{"status":"completed","usage":{}}}\n',
    ]
    raw = _consume_codex_sse(lines, on_delta=on_delta)
    assert "".join(p["text"] for p in progress) == "Checking. "
    answer_text = "".join(
        (a["text"] if isinstance(a, dict) else a) for a in answers
    )
    assert answer_text == "Done."
    text, _, _ = _extract_text_and_tools(raw)
    assert text == "Done."
    assert raw["output_text"] == "Done."


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
        b'data: {"type":"response.completed","response":{"status":"completed","model":"gpt-5.6-luna","usage":{"input_tokens":2,"output_tokens":1}}}\n',
    ]
    raw = _consume_codex_sse(lines)
    assert raw["status"] == "completed"
    assert raw["output_text"] == "hello"
    assert raw["usage"]["input_tokens"] == 2
    assert raw["model"] == "gpt-5.6-luna"
    text, _, _ = _extract_text_and_tools(raw)
    assert text == "hello"


def test_consume_sse_routes_commentary_to_progress():
    """Commentary is visible progress — never the reasoning/thinking stream."""
    reasoning = []
    text = []
    lines = [
        b'data: {"type":"response.output_item.added","item":{"type":"message","phase":"commentary","id":"msg_c"}}\n',
        b'data: {"type":"response.output_text.delta","item_id":"msg_c","delta":"Scanning..."}\n',
        b'data: {"type":"response.output_item.added","item":{"type":"message","phase":"final_answer","id":"msg_f"}}\n',
        b'data: {"type":"response.output_text.delta","item_id":"msg_f","delta":"answer"}\n',
        b'data: {"type":"response.completed","response":{"status":"completed","usage":{}}}\n',
    ]
    raw = _consume_codex_sse(
        lines,
        on_delta=text.append,
        on_reasoning_delta=reasoning.append,
    )
    assert reasoning == []
    assert len(text) == 2
    assert text[0]["text"] == "Scanning..."
    assert text[0]["channel"] == "progress"
    assert text[0]["stream_id"] == "msg_c"
    assert text[1]["text"] == "answer"
    assert text[1]["channel"] == "answer"
    assert raw["output_text"] == "answer"


def test_consume_sse_interleaved_channels_use_item_identity():
    """Arrival order must never reassign another item's channel."""
    progress = []
    reasoning = []
    answers = []
    item_done = []

    def on_delta(payload):
        if isinstance(payload, dict) and payload.get("channel") == "progress":
            progress.append(payload)
        else:
            answers.append(payload)

    lines = [
        b'data: {"type":"response.output_item.added","output_index":0,"item":{"type":"reasoning","id":"rs_0"}}\n',
        b'data: {"type":"response.reasoning_summary_text.delta","item_id":"rs_0","output_index":0,"delta":"Planning "}\n',
        b'data: {"type":"response.output_item.added","output_index":1,"item":{"type":"message","phase":"commentary","id":"msg_1"}}\n',
        b'data: {"type":"response.output_text.delta","item_id":"msg_1","output_index":1,"delta":"I\'ll inspect "}\n',
        b'data: {"type":"response.reasoning_summary_text.delta","item_id":"rs_0","output_index":0,"delta":"the parser "}\n',
        b'data: {"type":"response.output_text.delta","item_id":"msg_1","output_index":1,"delta":"the stream."}\n',
        b'data: {"type":"response.output_item.added","output_index":2,"item":{"type":"function_call","id":"fc_2","name":"read_file","arguments":"{}"}}\n',
        b'data: {"type":"response.output_item.done","output_index":2,"item":{"type":"function_call","id":"fc_2","name":"read_file","arguments":"{}"}}\n',
        b'data: {"type":"response.output_item.added","output_index":3,"item":{"type":"message","phase":"final_answer","id":"msg_3"}}\n',
        b'data: {"type":"response.output_text.delta","item_id":"msg_3","output_index":3,"delta":"Found it."}\n',
        b'data: {"type":"response.output_item.done","output_index":0,"item":{"type":"reasoning","id":"rs_0"}}\n',
        b'data: {"type":"response.output_item.done","output_index":1,"item":{"type":"message","phase":"commentary","id":"msg_1"}}\n',
        b'data: {"type":"response.output_item.done","output_index":3,"item":{"type":"message","phase":"final_answer","id":"msg_3"}}\n',
        b'data: {"type":"response.completed","response":{"status":"completed","usage":{}}}\n',
    ]
    raw = _consume_codex_sse(
        lines,
        on_delta=on_delta,
        on_reasoning_delta=reasoning.append,
        on_stream_item_done=item_done.append,
    )
    progress_text = "".join(p["text"] for p in progress)
    reasoning_text = "".join(
        (r["text"] if isinstance(r, dict) else r) for r in reasoning
    )
    answer_text = "".join(
        (a["text"] if isinstance(a, dict) else a) for a in answers
    )
    assert progress_text == "I'll inspect the stream."
    assert reasoning_text == "Planning the parser "
    assert answer_text == "Found it."
    assert raw["output_text"] == "Found it."
    # Function-call boundaries must not steal another item's phase.
    assert all(p["stream_id"] == "msg_1" for p in progress)
    assert all(
        (r["stream_id"] if isinstance(r, dict) else "rs_0") == "rs_0"
        for r in reasoning
    )
    assert any(d.get("stream_id") == "fc_2" for d in item_done if isinstance(d, dict))
    assert any(d.get("stream_id") == "msg_1" for d in item_done if isinstance(d, dict))


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
