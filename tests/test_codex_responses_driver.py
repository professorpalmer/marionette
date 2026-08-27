"""Codex Responses driver: SSE stream required + pool resolve (mocked HTTP)."""

from __future__ import annotations

import json
import time
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
    answer_looks_incomplete,
    responses_input_has_tool_results,
    responses_stream_error,
    responses_stream_label,
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


def test_response_from_raw_stamps_known_phase_and_skips_legacy():
    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5.6-luna")
    phased = driver._response_from_raw(
        {
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
                    "content": [{"type": "output_text", "text": "Done."}],
                },
            ],
        },
        t0=time.time(),
    )
    assert phased.text == "Done."
    assert phased.assistant_phase == "final_answer"

    legacy = driver._response_from_raw(
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "legacy answer"}],
                },
            ],
        },
        t0=time.time(),
    )
    assert legacy.text == "legacy answer"
    assert legacy.assistant_phase is None


def test_response_from_raw_preserves_commentary_before_tool_call():
    """OpenAI Agents JS #1513 follow-up: replay commentary before the tool."""
    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5.6-luna")
    resp = driver._response_from_raw(
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "phase": "commentary",
                    "content": [{"type": "output_text", "text": "Checking. "}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "read_file",
                    "arguments": "{}",
                },
            ],
        },
        t0=time.time(),
    )

    assert resp.text == "Checking. "
    assert resp.assistant_phase == "commentary"


def test_response_from_raw_analysis_before_tool_is_not_promoted():
    """Analysis stays off DriverResponse.text even when a tool call follows."""
    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5.6-luna")
    resp = driver._response_from_raw(
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "phase": "analysis",
                    "content": [{"type": "output_text", "text": "thinking aloud "}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "read_file",
                    "arguments": "{}",
                },
            ],
        },
        t0=time.time(),
    )

    assert resp.text == ""
    assert resp.assistant_phase is None
    assert resp.meta["tool_calls"][0]["id"] == "call_1"


def test_response_from_raw_commentary_without_tool_stays_empty():
    """Commentary-only still streams via progress; do not stamp a ledger row."""
    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5.6-luna")
    resp = driver._response_from_raw(
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "phase": "commentary",
                    "content": [{"type": "output_text", "text": "Checking. "}],
                },
            ],
        },
        t0=time.time(),
    )

    assert resp.text == ""
    assert resp.assistant_phase is None


def test_messages_to_input_replays_commentary_on_tool_row():
    """Same assistant row carries commentary then function_call then the tool result."""
    inp = _messages_to_responses_input([
        {"role": "user", "content": "inspect"},
        {
            "role": "assistant",
            "content": "Checking. ",
            "phase": "commentary",
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
    ])
    assert inp[0]["role"] == "user"
    assert inp[1]["type"] == "message"
    assert inp[1]["phase"] == "commentary"
    assert inp[1]["content"][0]["text"] == "Checking. "
    assert inp[2]["type"] == "function_call"
    assert inp[2]["call_id"] == "call_1"
    assert inp[3]["type"] == "function_call_output"
    assert inp[3]["call_id"] == "call_1"


def test_response_from_raw_empty_final_does_not_fallback_to_commentary():
    """Extract already refuses commentary fallback; DriverResponse must too."""
    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5.6-luna")
    resp = driver._response_from_raw(
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "phase": "commentary",
                    "content": [{"type": "output_text", "text": "I need to critique…"}],
                },
                {
                    "type": "message",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": ""}],
                },
            ],
            "output_text": "I need to critique…",
        },
        t0=time.time(),
    )
    assert resp.text == ""
    assert resp.assistant_phase == "final_answer"


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


def test_messages_to_input_preserves_known_assistant_phase():
    """Issue #228: replay assistant messages with OpenAI's known phase."""
    inp = _messages_to_responses_input([
        {"role": "user", "content": "inspect the sql"},
        {
            "role": "assistant",
            "content": "I'll inspect the SQL first.",
            "phase": "commentary",
        },
        {
            "role": "assistant",
            "content": "The employee filter explains it.",
            "phase": "final_answer",
        },
        {
            "role": "assistant",
            "content": "calling",
            "phase": "final_answer",
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }],
        },
    ])
    by_text = {
        part["text"]: item
        for item in inp
        if item.get("type") == "message"
        for part in item.get("content") or []
        if isinstance(part, dict) and part.get("type") == "output_text"
    }
    assert by_text["I'll inspect the SQL first."]["phase"] == "commentary"
    assert by_text["The employee filter explains it."]["phase"] == "final_answer"
    assert by_text["calling"]["phase"] == "final_answer"
    assert inp[-1]["type"] == "function_call"


def test_messages_to_input_omits_absent_invalid_and_non_assistant_phase():
    """Do not infer final_answer; never forward phase on user/tool rows."""
    inp = _messages_to_responses_input([
        {"role": "user", "content": "hi", "phase": "final_answer"},
        {"role": "assistant", "content": "legacy answer"},
        {"role": "assistant", "content": "nope", "phase": "analysis"},
        {"role": "assistant", "content": "also-nope", "phase": "FINAL_ANSWER "},
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "ok",
            "phase": "commentary",
        },
    ])
    assert "phase" not in inp[0]
    assert inp[1]["role"] == "assistant" and "phase" not in inp[1]
    assert inp[2]["role"] == "assistant" and "phase" not in inp[2]
    assert inp[3]["phase"] == "final_answer"
    assert inp[4]["type"] == "function_call_output"
    assert "phase" not in inp[4]


def test_messages_to_input_emits_input_image_for_multimodal_list():
    """Native vision history must become Responses input_image, not JSON text."""
    data_url = "data:image/png;base64,aaa"
    inp = _messages_to_responses_input([
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe this"},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        },
    ])
    assert len(inp) == 1
    parts = inp[0]["content"]
    assert {"type": "input_text", "text": "describe this"} in parts
    assert {"type": "input_image", "image_url": data_url} in parts
    assert not any(
        isinstance(p.get("text"), str) and "image_url" in p.get("text", "")
        for p in parts
        if p.get("type") == "input_text"
    )


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


def test_chat_preserves_reasoning_only_response_for_terminal_synthesis(pool_dir, monkeypatch):
    cp.add_oauth_entry(
        "openai-codex",
        access_token="eyJhbGciOiJub25lIn0.eyJodHRwczovL2FwaS5vcGVuYWkuY29tL2F1dGgiOnsiY2hhdGdwdF9hY2NvdW50X2lkIjoiYWNjMSJ9fQ.",
        label="codex-1",
    )
    driver = CodexResponsesDriver(name="codex", model="gpt-5.5")

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def __iter__(self):
            return iter([
                b'data: {"type":"response.output_item.added","item":{"type":"reasoning","id":"rs_1"}}\n',
                b'data: {"type":"response.reasoning_summary_text.delta","item_id":"rs_1","delta":"The audit found one issue."}\n',
                b'data: {"type":"response.output_item.done","item":{"type":"reasoning","id":"rs_1"}}\n',
                b'data: {"type":"response.completed","response":{"status":"completed","usage":{}}}\n',
            ])

    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: _Resp())

    response = driver.chat([{"role": "user", "content": "summarize"}])

    assert response.error is None
    assert response.text == ""
    assert response.meta["reasoning"] == "The audit found one issue."


def test_consume_sse_answer_start_seals_reasoning():
    """A final_answer item must seal an open reasoning stream (not just commentary)."""
    item_done = []
    lines = [
        b'data: {"type":"response.output_item.added","item":{"type":"reasoning","id":"rs_1"}}\n',
        b'data: {"type":"response.reasoning_summary_text.delta","item_id":"rs_1","delta":"planning"}\n',
        b'data: {"type":"response.output_item.added","item":{"type":"message","phase":"final_answer","id":"msg_f"}}\n',
        b'data: {"type":"response.output_text.delta","item_id":"msg_f","delta":"Test received."}\n',
        b'data: {"type":"response.completed","response":{"status":"completed","usage":{}}}\n',
    ]
    raw = _consume_codex_sse(
        lines,
        on_stream_item_done=item_done.append,
    )
    assert raw["output_text"] == "Test received."
    assert any(
        isinstance(d, dict) and d.get("stream_id") == "rs_1" for d in item_done
    )


def test_consume_sse_answer_done_timeout_completes():
    """Idle timeout after a tool-free answer must not hang the Electron turn."""

    def lines():
        yield b'data: {"type":"response.output_item.added","item":{"type":"message","phase":"final_answer","id":"msg_f"}}\n'
        yield b'data: {"type":"response.output_text.delta","item_id":"msg_f","delta":"Test received."}\n'
        yield b'data: {"type":"response.output_item.done","item":{"type":"message","phase":"final_answer","id":"msg_f"}}\n'
        raise TimeoutError("timed out")

    raw = _consume_codex_sse(lines())
    assert raw["status"] == "completed"
    assert raw["output_text"] == "Test received."
    assert not raw.get("error")


def test_consume_sse_hanging_colon_timeout_stays_incomplete():
    """A mid-clause stop like 'Cloudflare's:' must not forge completed."""

    def lines():
        yield b'data: {"type":"response.output_item.added","item":{"type":"message","phase":"final_answer","id":"msg_f"}}\n'
        yield b'data: {"type":"response.output_text.delta","item_id":"msg_f","delta":"Cloudflare\'s:"}\n'
        yield b'data: {"type":"response.output_item.done","item":{"type":"message","phase":"final_answer","id":"msg_f"}}\n'
        raise TimeoutError("timed out")

    raw = _consume_codex_sse(lines())
    assert raw["status"] == "incomplete"
    assert raw["output_text"].endswith(":")


def test_consume_sse_tool_followup_timeout_does_not_forge_completed():
    """Post-swarm synthesis is a tool-result follow-up; idle-drain must not seal it."""

    def lines():
        yield b'data: {"type":"response.output_item.added","item":{"type":"message","phase":"final_answer","id":"msg_f"}}\n'
        yield b'data: {"type":"response.output_text.delta","item_id":"msg_f","delta":"Workers launched Chrome."}\n'
        yield b'data: {"type":"response.output_item.done","item":{"type":"message","phase":"final_answer","id":"msg_f"}}\n'
        raise TimeoutError("timed out")

    raw = _consume_codex_sse(lines(), allow_post_answer_idle=False)
    assert raw["status"] == "incomplete"
    assert "Chrome" in raw["output_text"]


def test_answer_looks_incomplete_and_tool_result_input():
    assert answer_looks_incomplete("Cloudflare's:") is True
    assert answer_looks_incomplete("Test received.") is False
    assert responses_input_has_tool_results({
        "input": [{"type": "function_call_output", "call_id": "c1", "output": "ok"}],
    }) is True
    assert responses_input_has_tool_results({
        "input": [{"type": "message", "role": "user", "content": []}],
    }) is False


def test_consume_sse_keepalives_after_answer_complete():
    """Codex SSE comments after a tool-free answer must not hold Still working."""
    item_done = []

    def lines():
        yield b'data: {"type":"response.output_item.added","item":{"type":"message","phase":"final_answer","id":"msg_f"}}\n'
        yield b'data: {"type":"response.output_text.delta","item_id":"msg_f","delta":"Test received."}\n'
        yield b'data: {"type":"response.output_item.done","item":{"type":"message","phase":"final_answer","id":"msg_f"}}\n'
        yield b": keepalive\n"
        yield b": keepalive\n"
        yield b": keepalive\n"
        yield b'data: {"type":"response.completed","response":{"status":"completed","usage":{}}}\n'

    raw = _consume_codex_sse(lines(), on_stream_item_done=item_done.append)
    assert raw["status"] == "completed"
    assert raw["output_text"] == "Test received."
    assert not raw.get("error")
    # Must finish on keepalives — never reach the late completed event.
    assert any(
        isinstance(d, dict) and d.get("stream_id") == "msg_f" for d in item_done
    )


def test_consume_sse_in_progress_after_answer_text_completes(monkeypatch):
    """Luna keeps the SSE open with JSON in_progress after the answer paints."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "pmharness.drivers.codex_responses.time.monotonic",
        lambda: clock["now"],
    )

    def lines():
        yield b'data: {"type":"response.output_item.added","item":{"type":"message","phase":"final_answer","id":"msg_f"}}\n'
        yield b'data: {"type":"response.output_text.delta","item_id":"msg_f","delta":"Test received."}\n'
        yield b'data: {"type":"response.in_progress"}\n'
        clock["now"] += 2.5
        yield b'data: {"type":"response.in_progress"}\n'
        yield b'data: {"type":"response.completed","response":{"status":"completed","usage":{}}}\n'
        raise AssertionError("must settle on in_progress, not wait for completed")

    raw = _consume_codex_sse(lines())
    assert raw["status"] == "completed"
    assert raw["output_text"] == "Test received."
    assert not raw.get("error")


def test_consume_sse_trailing_summary_then_timeout():
    """In-flight reasoning after the answer is kept; then the turn settles."""
    reasoning = []

    def lines():
        yield b'data: {"type":"response.output_item.added","item":{"type":"message","phase":"final_answer","id":"msg_f"}}\n'
        yield b'data: {"type":"response.output_text.delta","item_id":"msg_f","delta":"Test received."}\n'
        yield b'data: {"type":"response.output_item.done","item":{"type":"message","phase":"final_answer","id":"msg_f"}}\n'
        yield b'data: {"type":"response.reasoning_summary_text.delta","item_id":"rs_1","delta":"Short ping, no tools."}\n'
        raise TimeoutError("timed out")

    raw = _consume_codex_sse(lines(), on_reasoning_delta=reasoning.append)
    assert raw["status"] == "completed"
    assert raw["output_text"] == "Test received."
    assert raw["reasoning"] == "Short ping, no tools."
    assert reasoning and reasoning[0]["text"] == "Short ping, no tools."


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


def test_chatgpt_build_body_omits_max_output_tokens():
    d = CodexResponsesDriver(name="codex", model="gpt-5.5", max_tokens=2048)
    body = d._build_body([{"role": "user", "content": "hi"}])
    assert "max_output_tokens" not in body


def test_non_chatgpt_build_body_sends_max_output_tokens():
    d = CodexResponsesDriver(
        name="muse",
        model="muse-spark-1.2-contributor",
        base_url="https://opencode.ai/zen/go/v1",
        api_key_env="OPENCODE_GO_API_KEY",
        max_tokens=2048,
        chatgpt_backend=False,
    )
    body = d._build_body([{"role": "user", "content": "hi"}])
    assert body["max_output_tokens"] == 2048


def test_non_chatgpt_build_body_omits_nonpositive_max_output_tokens():
    d = CodexResponsesDriver(
        name="muse",
        model="muse-spark-1.2-contributor",
        chatgpt_backend=False,
        max_tokens=0,
    )
    body = d._build_body([{"role": "user", "content": "hi"}])
    assert "max_output_tokens" not in body


def test_responses_stream_label_names_host_not_driver_class():
    assert responses_stream_label(chatgpt_backend=True) == "Codex Responses"
    assert responses_stream_label(
        chatgpt_backend=False,
        base_url="https://opencode.ai/zen/go/v1",
    ) == "OpenCode Responses"
    assert responses_stream_label(
        chatgpt_backend=False,
        base_url="https://api.openai.com/v1",
    ) == "OpenAI Responses"
    err = responses_stream_error(
        "OpenCode Responses",
        "no_terminal",
        "response.output_item.added",
    )
    assert err.startswith("OpenCode Responses")
    assert "codex" not in err.lower()
    assert "response.output_item.added" in err


def test_consume_sse_non_chatgpt_eof_without_terminal_is_incomplete():
    lines = [
        b'data: {"type":"response.output_text.delta","delta":"partial"}\n',
        b"data: [DONE]\n",
    ]
    raw = _consume_codex_sse(lines, chatgpt_backend=False)
    assert raw["status"] != "completed"
    assert raw["status"] == "incomplete"
    assert raw["output_text"] == "partial"
    assert raw.get("error")
    assert "openai responses" in str(raw["error"]).lower()
    assert "codex" not in str(raw["error"]).lower()
    text, _, finish = _extract_text_and_tools(raw)
    assert text == "partial"
    assert finish == "incomplete"


def test_consume_sse_opencode_empty_eof_after_item_added_names_host():
    lines = [
        b'data: {"type":"response.output_item.added","item":{"type":"message","id":"msg_x"}}\n',
        b"data: [DONE]\n",
    ]
    raw = _consume_codex_sse(
        lines,
        chatgpt_backend=False,
        stream_label="OpenCode Responses",
    )
    assert raw["status"] == "failed"
    err = str(raw.get("error") or "").lower()
    assert "opencode responses" in err
    assert "did not emit a terminal response" in err
    assert "response.output_item.added" in err
    assert "codex" not in err


def test_consume_sse_chatgpt_still_seals_after_answer_timeout():
    """ChatGPT anti-hang drain must not regress when chatgpt_backend defaults."""

    def lines():
        yield b'data: {"type":"response.output_item.added","item":{"type":"message","phase":"final_answer","id":"msg_f"}}\n'
        yield b'data: {"type":"response.output_text.delta","item_id":"msg_f","delta":"Test received."}\n'
        yield b'data: {"type":"response.output_item.done","item":{"type":"message","phase":"final_answer","id":"msg_f"}}\n'
        raise TimeoutError("timed out")

    raw = _consume_codex_sse(lines())
    assert raw["status"] == "completed"
    assert raw["output_text"] == "Test received."
    assert not raw.get("error")


class _IterResp:
    def __init__(self, lines):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self._lines)


def _muse_responses_driver():
    d = CodexResponsesDriver(
        name="opencode-go:muse-spark-1.2-contributor",
        model="muse-spark-1.2-contributor",
        base_url="https://opencode.ai/zen/go/v1",
        api_key_env="OPENCODE_GO_API_KEY",
        max_tokens=4096,
        chatgpt_backend=False,
    )
    d._key = lambda: "sk-go-test"
    return d


def test_muse_production_driver_is_non_chatgpt_responses():
    from pmharness.drivers.openai_compat import OpenAICompatDriver

    d = _muse_responses_driver()
    assert isinstance(d, CodexResponsesDriver)
    assert not isinstance(d, OpenAICompatDriver)
    assert d.chatgpt_backend is False


def test_muse_slow_answer_deltas_reach_completed(monkeypatch):
    """Slow Muse tokens spanning >2s plus a 400ms pause must not seal early."""
    d = _muse_responses_driver()
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        "pmharness.drivers.codex_responses.time.monotonic",
        lambda: clock["now"],
    )

    def lines():
        yield b'data: {"type":"response.output_item.added","item":{"type":"message","phase":"final_answer","id":"msg_f"}}\n'
        yield b'data: {"type":"response.output_text.delta","item_id":"msg_f","delta":"Hel"}\n'
        clock["now"] += 2.2
        yield b'data: {"type":"response.output_text.delta","item_id":"msg_f","delta":"lo"}\n'
        clock["now"] += 0.4
        yield b'data: {"type":"response.output_text.delta","item_id":"msg_f","delta":"!"}\n'
        yield b'data: {"type":"response.output_item.done","item":{"type":"message","phase":"final_answer","id":"msg_f","content":[{"type":"output_text","text":"Hello!"}]}}\n'
        yield b'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":4,"output_tokens":3}}}\n'

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _IterResp(lines()))
    deltas = []
    resp = d.chat_stream(
        [{"role": "user", "content": "hi"}],
        on_delta=deltas.append,
    )
    assert resp.error is None
    answer = "".join(
        (p["text"] if isinstance(p, dict) else p) for p in deltas
    )
    assert answer == "Hello!"
    assert resp.text == "Hello!"
    assert resp.meta["finish_reason"] == "completed"
    assert resp.tokens_out == 3
    assert resp.meta.get("stream_started") is True
    assert resp.meta.get("stream_terminal")
    assert resp.meta.get("last_provider_event") == "response.completed"


def test_muse_eof_without_terminal_preserves_partial_text(monkeypatch):
    d = _muse_responses_driver()

    def lines():
        yield b'data: {"type":"response.output_text.delta","delta":"partial answer"}\n'
        yield b'data: {"usage":{"input_tokens":2,"output_tokens":1}}\n'
        yield b"data: [DONE]\n"

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _IterResp(lines()))
    resp = d.chat_stream(
        [{"role": "user", "content": "hi"}],
        on_delta=lambda _t: None,
    )
    assert resp.error
    assert "terminal" in resp.error.lower()
    assert "opencode" in resp.error.lower()
    assert "codex" not in resp.error.lower()
    assert resp.text == "partial answer"
    assert resp.meta["finish_reason"] != "completed"
    assert resp.meta.get("stream_started") is True
    assert resp.meta.get("stream_terminal")
    assert resp.meta.get("last_provider_event")


def test_muse_incomplete_max_output_tokens_does_not_continue(monkeypatch):
    """Production-shaped Muse cap: one incomplete stream, no second POST."""
    d = _muse_responses_driver()
    posts = {"n": 0}
    item = {
        "type": "message",
        "id": "msg_cap",
        "phase": "final_answer",
        "content": [{"type": "output_text", "text": "partial muse answer"}],
    }
    terminal = {
        "type": "response.incomplete",
        "response": {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "usage": {
                "input_tokens": 40,
                "output_tokens": 12,
                "cost": 0.02,
            },
            "model": "muse-spark-1.2-contributor",
        },
    }

    def lines():
        yield f'data: {json.dumps({"type":"response.output_item.done","item":item})}\n'.encode()
        yield f'data: {json.dumps(terminal)}\n'.encode()

    def fake_urlopen(*_a, **_k):
        posts["n"] += 1
        return _IterResp(lines())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    resp = d.chat_stream(
        [{"role": "user", "content": "write a long essay"}],
        on_delta=lambda _t: None,
    )
    assert posts["n"] == 1
    assert resp.error
    assert "incomplete" in (resp.error or "").lower()
    assert resp.text == "partial muse answer"
    assert resp.meta["finish_reason"] == "incomplete"
    assert resp.meta["incomplete_reason"] == "max_output_tokens"
    assert resp.meta.get("stream_started") is True
    assert resp.meta.get("stream_terminal")
    assert resp.meta.get("last_provider_event") == "response.incomplete"
    assert resp.tokens_in == 40
    assert resp.tokens_out == 12
    assert resp.meta.get("incomplete_retries") == 0
