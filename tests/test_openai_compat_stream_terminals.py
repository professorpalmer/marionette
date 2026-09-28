"""Fail-closed OpenAI chat Completions stream terminals.

Shared parser contract for Ox Alpha / OpenRouter / OpenCode Go hosts.
Hermetic except for owned loopback-socket deadline tests; no external network.
"""
from __future__ import annotations

from contextlib import contextmanager
import http.client
import io
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

import pmharness.drivers.openai_compat as openai_compat
from pmharness.drivers.openai_compat import (
    OpenAICompatDriver,
    _consume_openai_chat_sse,
)


def _driver(base_url="https://api.openai.com/v1", model="gpt-4o"):
    d = OpenAICompatDriver(
        name="t",
        model=model,
        base_url=base_url,
        api_key_env="TEST_OAI_KEY",
    )
    d._key = lambda: "fake-key"
    return d


class _SseResp:
    def __init__(self, lines):
        self._lines = list(lines)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self._lines)


class _OwnedReadSocket:
    def __init__(self, released=None):
        self.released = released
        self.shutdown_calls = 0

    def settimeout(self, _seconds):
        return None

    def shutdown(self, _how):
        self.shutdown_calls += 1
        if self.released is not None:
            self.released.set()


def _data(payload) -> bytes:
    return ("data: " + json.dumps(payload) + "\n").encode("utf-8")


def _run_stream(monkeypatch, driver, lines, **kwargs):
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: _SseResp(lines),
    )
    return driver.chat_stream(
        [{"role": "user", "content": "hi"}],
        on_delta=kwargs.get("on_delta") or (lambda _t: None),
        on_reasoning_delta=kwargs.get("on_reasoning_delta"),
        on_tool_hint=kwargs.get("on_tool_hint"),
        tools=kwargs.get("tools"),
    )


def test_clean_stop_succeeds(monkeypatch):
    lines = [
        _data({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}),
        b"data: [DONE]\n",
    ]
    resp = _run_stream(monkeypatch, _driver(), lines)
    assert resp.error is None
    assert resp.text == "ok"
    assert resp.meta["finish_reason"] == "stop"
    assert resp.meta["stream_terminal"] == "stop"
    assert resp.meta["malformed_sse_chunks"] == 0


@pytest.mark.parametrize("answer,with_tool", [("", True), ("Answer", False), ("", False)])
def test_deepseek_sse_reasoning_never_becomes_spoken_answer(monkeypatch, answer, with_tool):
    from harness.send_loop_phases import resolve_emit_say_texts

    reasoning = "fixture private planning"
    delta = {"content": answer}
    if with_tool:
        delta["tool_calls"] = [{"index": 0, "id": "call-1", "type": "function", "function": {
            "name": "read_file", "arguments": '{"path":"fixture.txt"}',
        }}]
    seen = []
    resp = _run_stream(monkeypatch, _driver(model="deepseek-flash"), [
        _data({"choices": [{"delta": {"reasoning_content": reasoning}}]}),
        _data({"choices": [{"delta": delta, "finish_reason": "tool_calls" if with_tool else "stop"}]}),
        b"data: [DONE]\n",
    ], on_reasoning_delta=seen.append)
    assert "".join(seen) == reasoning
    assert resolve_emit_say_texts(cleaned_say_text=resp.text or "", resp=resp) == (answer, "", "")
    if with_tool:
        assert resp.meta["tool_calls"][0]["function"]["name"] == "read_file"


def test_duplicate_content_snapshot_does_not_double_text(monkeypatch):
    phrase = "Received—single response."
    seen = []
    lines = [
        _data({"choices": [{"delta": {"content": phrase}}]}),
        _data({"choices": [{"delta": {"content": f"\n\n{phrase}"}, "finish_reason": "stop"}]}),
        b"data: [DONE]\n",
    ]
    resp = _run_stream(monkeypatch, _driver(), lines, on_delta=seen.append)
    assert resp.error is None
    assert resp.text == phrase
    assert "".join(seen) == phrase


def test_single_reasoning_frame_internal_duplicate_collapses(monkeypatch):
    phrase = (
        "The user is greeting me again. This is a simple greeting — "
        "no tool calls needed."
    )
    lines = [
        _data({"choices": [{"delta": {"reasoning_content": f"{phrase}\n\n{phrase}"}}]}),
        _data({"choices": [{"delta": {"content": "Hey!"}, "finish_reason": "stop"}]}),
        b"data: [DONE]\n",
    ]
    seen = []
    resp = _run_stream(
        monkeypatch,
        _driver(),
        lines,
        on_reasoning_delta=seen.append,
    )
    assert "".join(seen) == phrase
    assert resp.meta.get("reasoning") == phrase
    assert resp.meta.get("reasoning_content") == phrase


def test_duplicate_reasoning_snapshot_does_not_double_text(monkeypatch):
    phrase = "A hashmap stores key-value pairs."
    seen = []
    lines = [
        _data({"choices": [{"delta": {"reasoning_content": phrase}}]}),
        _data({"choices": [{"delta": {
            "reasoning_content": f"\n\n{phrase}",
            "content": "done",
        }, "finish_reason": "stop"}]}),
        b"data: [DONE]\n",
    ]
    resp = _run_stream(monkeypatch, _driver(), lines, on_reasoning_delta=seen.append)
    assert resp.error is None
    assert "".join(seen) == phrase
    assert resp.meta.get("reasoning") == phrase
    assert resp.meta.get("reasoning_content") == phrase


def test_incremental_reasoning_then_full_snapshot_does_not_double(monkeypatch):
    first = "A hashmap stores "
    rest = "key-value pairs with O(1) lookup."
    full = first + rest
    seen = []
    lines = [
        _data({"choices": [{"delta": {"reasoning_content": first}}]}),
        _data({"choices": [{"delta": {"reasoning_content": rest}}]}),
        _data({"choices": [{"delta": {
            "reasoning_content": full,
            "content": "ok",
        }, "finish_reason": "stop"}]}),
        b"data: [DONE]\n",
    ]
    resp = _run_stream(monkeypatch, _driver(), lines, on_reasoning_delta=seen.append)
    assert resp.error is None
    assert "".join(seen) == full
    assert resp.meta.get("reasoning") == full
    assert resp.meta.get("reasoning_content") == full


def test_finish_reason_length_is_explicit_incomplete(monkeypatch):
    lines = [
        _data({"choices": [{"delta": {"content": "partial "}}]}),
        _data({
            "choices": [{"delta": {"content": "cut"}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 4},
        }),
        b"data: [DONE]\n",
    ]
    calls = {"n": 0}

    def urlopen(*_a, **_k):
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("no continue fixture")
        return _SseResp(lines)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    resp = _driver().chat_stream(
        [{"role": "user", "content": "hi"}],
        on_delta=lambda _t: None,
    )
    assert resp.error
    assert "length" in resp.error
    assert resp.text == "partial cut"
    assert resp.meta["finish_reason"] == "length"
    assert resp.meta["stream_terminal"] == "length"
    assert resp.meta["stream_started"] is True
    assert resp.tokens_in == 8
    assert resp.tokens_out == 4
    assert resp.meta["tool_calls"] == []


def test_finish_reason_length_continues_then_stops(monkeypatch):
    first = [
        _data({"choices": [{"delta": {"content": "hello"}, "finish_reason": "length"}]}),
        b"data: [DONE]\n",
    ]
    second = [
        _data({"choices": [{"delta": {"content": " world"}, "finish_reason": "stop"}]}),
        b"data: [DONE]\n",
    ]
    queue = [first, second]

    def urlopen(*_a, **_k):
        return _SseResp(queue.pop(0))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    resp = _driver().chat_stream(
        [{"role": "user", "content": "hi"}],
        on_delta=lambda _t: None,
    )
    assert resp.error is None
    assert resp.text == "helloworld"
    assert resp.meta["finish_reason"] == "stop"
    assert resp.meta.get("length_continues") == 1


def test_build_chat_body_omits_nonpositive_max_tokens():
    driver = _driver()
    driver.max_tokens = None
    body = driver._build_chat_body([{"role": "user", "content": "hi"}])
    assert "max_tokens" not in body
    assert "max_completion_tokens" not in body
    driver.max_tokens = 0
    body = driver._build_chat_body([{"role": "user", "content": "hi"}])
    assert "max_tokens" not in body
    driver.max_tokens = 8000
    body = driver._build_chat_body([{"role": "user", "content": "hi"}])
    field = driver._output_token_limit_field()
    assert body[field] == 8000


def test_partial_eof_without_done_or_finish_is_incomplete(monkeypatch):
    lines = [
        _data({"choices": [{"delta": {"content": "partial"}}]}),
    ]
    resp = _run_stream(monkeypatch, _driver(), lines)
    assert resp.error
    assert resp.text == "partial"
    assert resp.meta["stream_terminal"] == "incomplete"
    assert resp.meta["finish_reason"] == ""
    assert resp.meta["stream_started"] is True


def test_empty_stream_is_not_success(monkeypatch):
    resp = _run_stream(monkeypatch, _driver(), [b"data: [DONE]\n"])
    assert resp.error
    assert resp.text == ""
    assert resp.meta["stream_terminal"] == "empty"
    assert resp.meta["stream_started"] is False


def test_malformed_sse_is_not_silent_success(monkeypatch):
    lines = [
        b"data: not-json{{{{\n",
        b"data: [1, 2, 3]\n",
        b"data: [DONE]\n",
    ]
    resp = _run_stream(monkeypatch, _driver(), lines)
    assert resp.error
    assert resp.text == ""
    assert resp.meta["malformed_sse_chunks"] == 2
    assert resp.meta["stream_terminal"] == "empty"


def test_partial_text_then_http_failure_preserves_progress(monkeypatch):
    class _PartialThenHttp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            yield _data({
                "choices": [{"delta": {"content": "hello"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            })
            raise urllib.error.HTTPError(
                "https://api.openai.com/v1", 502, "bad", {},
                io.BytesIO(b'{"error":{"message":"upstream"}}'),
            )

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: _PartialThenHttp(),
    )
    resp = _driver().chat_stream(
        [{"role": "user", "content": "hi"}],
        on_delta=lambda _t: None,
    )
    assert resp.error and "502" in resp.error
    assert resp.text == "hello"
    assert resp.tokens_in == 3
    assert resp.tokens_out == 1
    assert resp.meta["raw_usage"] == {"prompt_tokens": 3, "completion_tokens": 1}
    assert resp.meta["stream_started"] is True
    assert resp.meta["stream_terminal"] == "error"
    assert resp.meta["tool_calls"] == []


def test_truncated_tool_arguments_cannot_dispatch(monkeypatch):
    lines = [
        _data({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path":',
                        },
                    }],
                },
            }],
        }),
        _data({"choices": [{"delta": {}, "finish_reason": "length"}]}),
        b"data: [DONE]\n",
    ]
    resp = _run_stream(monkeypatch, _driver(), lines)
    assert resp.meta["tool_calls"] == []
    assert resp.meta.get("incomplete_tool_calls")
    assert resp.meta["incomplete_tool_calls"][0]["function"]["arguments"] == '{"path":'
    assert resp.meta["finish_reason"] == "length"
    assert resp.error


def test_truncated_args_with_tool_calls_finish_still_withheld(monkeypatch):
    lines = [
        _data({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_1",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "x',
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        }),
        b"data: [DONE]\n",
    ]
    resp = _run_stream(monkeypatch, _driver(), lines)
    assert resp.error
    assert "truncated" in resp.error.lower() or "tool" in resp.error.lower()
    assert resp.meta["finish_reason"] == "tool_calls"
    assert resp.meta["stream_terminal"] == "incomplete"
    assert resp.meta["tool_calls"] == []
    assert resp.meta["incomplete_tool_calls"][0]["function"]["name"] == "read_file"


def test_opencode_go_retries_incomplete_tool_stream_without_reemitting_text(monkeypatch):
    """A Go relay cutoff may emit text/name, then no complete tool JSON.

    The retry is silent so the first visible announcement is not duplicated;
    only the complete second response may reach the action parser.
    """
    first = [
        _data({"choices": [{"delta": {"content": "Fanning out..."}}]}),
        _data({"choices": [{"delta": {"tool_calls": [{
            "index": 0,
            "id": "call_partial",
            "type": "function",
            "function": {"name": "run_parallel", "arguments": '{"goals": ['},
        }]}, "finish_reason": "tool_calls"}]}),
        b"data: [DONE]\n",
    ]
    second = {
        "choices": [{
            "message": {"content": "", "tool_calls": [{
                "id": "call_recovered",
                "type": "function",
                "function": {"name": "run_parallel", "arguments": '{"goals": []}'},
            }]},
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 4, "completion_tokens": 3},
    }

    class _JsonResp:
        def __init__(self, payload):
            self._data = json.dumps(payload).encode("utf-8")

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    calls = []

    def urlopen(req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        calls.append(body)
        if body.get("stream"):
            return _SseResp(first)
        return _JsonResp(second)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    seen = []
    resp = _driver(
        base_url="https://opencode.ai/zen/go/v1", model="deepseek-flash",
    ).chat_stream(
        [{"role": "user", "content": "do the protocol test"}],
        tools=[{"type": "function", "function": {
            "name": "run_parallel", "parameters": {"type": "object"},
        }}],
        on_delta=seen.append,
    )

    assert len(calls) == 2
    assert calls[0]["stream"] is True
    assert "stream" not in calls[1]
    assert seen == ["Fanning out..."]
    assert resp.error is None
    assert resp.text == ""
    assert resp.meta["tool_calls"][0]["function"]["name"] == "run_parallel"
    assert resp.meta["incomplete_retry_recovered"] is True


def test_opencode_go_retries_tool_finish_without_assembled_call(monkeypatch):
    """A relay can report tool_calls after dropping every tool delta."""
    first = [
        _data({"choices": [{"delta": {"content": "Working"}}]}),
        _data({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
        b"data: [DONE]\n",
    ]
    second = {
        "choices": [{
            "message": {"content": "", "tool_calls": [{
                "id": "call_recovered",
                "type": "function",
                "function": {"name": "run_parallel", "arguments": "{}"},
            }]},
            "finish_reason": "tool_calls",
        }],
    }

    class _JsonResp:
        def read(self):
            return json.dumps(second).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    calls = []

    def urlopen(req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        calls.append(body)
        return _SseResp(first) if body.get("stream") else _JsonResp()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    resp = _driver(
        base_url="https://opencode.ai/zen/go/v1", model="deepseek-flash",
    ).chat_stream(
        [{"role": "user", "content": "do the protocol test"}],
        tools=[{"type": "function", "function": {
            "name": "run_parallel", "parameters": {"type": "object"},
        }}],
        on_delta=lambda _text: None,
    )

    assert len(calls) == 2
    assert resp.error is None
    assert resp.meta["incomplete_retry_recovered"] is True
    assert resp.meta["tool_calls"][0]["function"]["name"] == "run_parallel"


def test_opencode_go_incomplete_tool_retry_preserves_explicit_filter(monkeypatch):
    first = [
        _data({"choices": [{
            "delta": {"content": "Working"},
            "finish_reason": "tool_calls",
        }]}),
        b"data: [DONE]\n",
    ]
    second = {
        "choices": [{
            "message": {"content": ""},
            "finish_reason": "content_filter",
        }],
    }

    class _JsonResp:
        def read(self):
            return json.dumps(second).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    calls = []

    def urlopen(req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        calls.append(body)
        return _SseResp(first) if body.get("stream") else _JsonResp()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    resp = _driver(
        base_url="https://opencode.ai/zen/go/v1", model="deepseek-flash",
    ).chat_stream(
        [{"role": "user", "content": "do the protocol test"}],
        tools=[{"type": "function", "function": {
            "name": "run_parallel", "parameters": {"type": "object"},
        }}],
        on_delta=lambda _text: None,
    )

    assert len(calls) == 2
    assert resp.error and "content_filter" in resp.error
    assert resp.meta["finish_reason"] == "content_filter"
    assert resp.meta["stream_terminal"] == "content_filter"
    assert resp.meta["incomplete_retry_recovered"] is False


def test_reasoning_content_presence_survives_stream_parser(monkeypatch):
    """DeepSeek's native field must remain distinct from normalized reasoning."""
    lines = [
        _data({"choices": [{"delta": {"reasoning_content": "plan"}}]}),
        _data({"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]}),
        b"data: [DONE]\n",
    ]
    resp = _run_stream(monkeypatch, _driver(model="deepseek-flash"), lines)

    assert resp.meta["reasoning"] == "plan"
    assert resp.meta["reasoning_content"] == "plan"


def test_empty_reasoning_content_field_is_preserved_in_sync_response(monkeypatch):
    class _JsonResp:
        def read(self):
            return json.dumps({
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "ok",
                        "reasoning_content": "",
                    },
                    "finish_reason": "stop",
                }],
            }).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _JsonResp())
    resp = _driver(model="deepseek-flash").chat(
        [{"role": "user", "content": "hi"}],
    )

    assert resp.meta["reasoning"] == ""
    assert resp.meta["reasoning_content"] == ""


def test_duplicate_and_out_of_order_index_assembly(monkeypatch):
    lines = [
        _data({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 1,
                        "id": "call_b",
                        "function": {"name": "beta", "arguments": ""},
                    }],
                },
            }],
        }),
        _data({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_a",
                        "function": {"name": "alpha", "arguments": ""},
                    }],
                },
            }],
        }),
        _data({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 1,
                        "function": {"arguments": '{"b":2}'},
                    }],
                },
            }],
        }),
        _data({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "function": {"arguments": '{"a":1}'},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        }),
        b"data: [DONE]\n",
    ]
    resp = _run_stream(monkeypatch, _driver(), lines)
    assert resp.error is None
    tool_calls = resp.meta["tool_calls"]
    assert [tc["id"] for tc in tool_calls] == ["call_a", "call_b"]
    assert tool_calls[0]["function"]["arguments"] == '{"a":1}'
    assert tool_calls[1]["function"]["arguments"] == '{"b":2}'


def test_missing_tool_call_ids_stay_empty_for_canonicalization(monkeypatch):
    lines = [
        _data({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path":"a"}',
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        }),
        b"data: [DONE]\n",
    ]
    resp = _run_stream(monkeypatch, _driver(), lines)
    assert resp.error is None
    assert resp.meta["tool_calls"][0]["id"] == ""
    assert resp.meta["tool_calls"][0]["function"]["name"] == "read_file"


@pytest.mark.parametrize("base_url,model", [
    ("https://openrouter.ai/api/v1", "stealth/ox-alpha"),
    ("https://opencode.ai/zen/go/v1", "ox-alpha-free"),
    ("https://opencode.ai/zen/v1", "x-preview-f-free"),
])
def test_host_cases_use_shared_parser_contract(base_url, model):
    lines = [
        _data({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}),
        b"data: [DONE]\n",
    ]
    parsed = _consume_openai_chat_sse(lines)
    assert parsed["error"] is None
    assert parsed["text"] == "ok"
    assert parsed["stream_terminal"] == "stop"
    assert parsed["finish_reason"] == "stop"
    assert parsed["malformed_sse_chunks"] == 0
    assert parsed["tool_calls"] == []

    length_lines = [
        _data({"choices": [{"delta": {"content": "cut"}, "finish_reason": "length"}]}),
        b"data: [DONE]\n",
    ]
    length = _consume_openai_chat_sse(length_lines)
    assert length["stream_terminal"] == "length"
    assert length["error"]
    assert length["text"] == "cut"
    # Host overlays stay on the request; the parser is host-agnostic.
    d = _driver(base_url=base_url, model=model)
    if d._is_opencode_go_host():
        assert d._is_openrouter_host() is False
    if d._is_openrouter_host():
        assert d._is_opencode_go_host() is False


def test_content_filter_is_classified_truthfully(monkeypatch):
    lines = [
        _data({"choices": [{"delta": {"content": "nope"}, "finish_reason": "content_filter"}]}),
        b"data: [DONE]\n",
    ]
    resp = _run_stream(monkeypatch, _driver(), lines)
    assert resp.error
    assert "content_filter" in resp.error
    assert "without a finish_reason" not in resp.error
    assert resp.meta["finish_reason"] == "content_filter"
    assert resp.meta["stream_terminal"] == "content_filter"
    assert resp.text == "nope"
    assert resp.meta["tool_calls"] == []


def test_legacy_function_call_is_executable_when_complete(monkeypatch):
    lines = [
        _data({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_1",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path":"a"}',
                        },
                    }],
                },
                "finish_reason": "function_call",
            }],
        }),
        b"data: [DONE]\n",
    ]
    resp = _run_stream(monkeypatch, _driver(), lines)
    assert resp.error is None
    assert resp.meta["stream_terminal"] == "tool_calls"
    assert resp.meta["finish_reason"] == "function_call"
    assert resp.meta["tool_calls"][0]["function"]["name"] == "read_file"


def test_unknown_finish_reason_fails_closed_and_names_the_reason(monkeypatch):
    lines = [
        _data({"choices": [{"delta": {"content": "ok"}, "finish_reason": "mystery_code"}]}),
        b"data: [DONE]\n",
    ]
    resp = _run_stream(monkeypatch, _driver(), lines)
    assert resp.error
    assert "mystery_code" in resp.error
    assert "without a finish_reason" not in resp.error
    assert resp.meta["stream_terminal"] == "incomplete"
    assert resp.text == "ok"


def test_finish_reason_error_is_known_terminal(monkeypatch):
    lines = [
        _data({"choices": [{"delta": {"content": "hi"}}]}),
        _data({"choices": [{"delta": {"content": "!"}, "finish_reason": "error"}]}),
        b"data: [DONE]\n",
    ]
    resp = _run_stream(monkeypatch, _driver(), lines)
    assert resp.error
    assert "unrecognized" not in resp.error
    assert "finish_reason=error" in resp.error
    assert resp.text == "hi!"
    assert resp.meta["finish_reason"] == "error"
    assert resp.meta["stream_terminal"] in ("incomplete", "provider_eof")
    assert resp.meta.get("tool_calls") == []
    assert resp.tokens_in == 0
    assert resp.tokens_out == 0


def _run_chat(monkeypatch, driver, payload):
    class _JsonResp:
        def __init__(self, body):
            self._data = json.dumps(body).encode("utf-8")

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: _JsonResp(payload),
    )
    return driver.chat([{"role": "user", "content": "hi"}])


def test_chat_finish_reason_stop_succeeds(monkeypatch):
    resp = _run_chat(monkeypatch, _driver(), {
        "choices": [{
            "message": {"content": "hello", "tool_calls": []},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1},
    })
    assert resp.error is None
    assert resp.text == "hello"
    assert resp.meta["finish_reason"] == "stop"
    assert resp.meta["stream_terminal"] == "stop"
    assert resp.tokens_in == 2


def test_chat_finish_reason_length_fails_closed(monkeypatch):
    resp = _run_chat(monkeypatch, _driver(), {
        "choices": [{
            "message": {"content": "partial cut"},
            "finish_reason": "length",
        }],
        "usage": {"prompt_tokens": 4, "completion_tokens": 3},
    })
    assert resp.error
    assert "length" in resp.error
    assert resp.text == "partial cut"
    assert resp.meta["finish_reason"] == "length"
    assert resp.meta["stream_terminal"] == "length"
    assert resp.tokens_out == 3


def test_chat_blank_finish_fails_closed(monkeypatch):
    resp = _run_chat(monkeypatch, _driver(), {
        "choices": [{
            "message": {"content": "sync text"},
            "finish_reason": None,
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    })
    assert resp.error
    assert resp.meta["stream_terminal"] in ("incomplete", "empty")
    assert resp.text == "sync text"


def test_chat_unknown_finish_fails_closed(monkeypatch):
    resp = _run_chat(monkeypatch, _driver(), {
        "choices": [{
            "message": {"content": "x"},
            "finish_reason": "mystery_code",
        }],
        "usage": {},
    })
    assert resp.error
    assert "mystery_code" in resp.error
    assert "without a finish_reason" not in resp.error


def test_chat_content_filter_fails_closed(monkeypatch):
    resp = _run_chat(monkeypatch, _driver(), {
        "choices": [{
            "message": {"content": "blocked"},
            "finish_reason": "content_filter",
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    })
    assert resp.error
    assert resp.meta["stream_terminal"] == "content_filter"
    assert resp.text == "blocked"


def test_chat_tool_calls_truncated_is_error(monkeypatch):
    resp = _run_chat(monkeypatch, _driver(), {
        "choices": [{
            "message": {
                "content": "",
                "tool_calls": [{
                    "id": "c1",
                    "function": {"name": "read_file", "arguments": '{"path":'},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {},
    })
    assert resp.error
    assert resp.meta["tool_calls"] == []
    assert resp.meta["incomplete_tool_calls"]
    assert resp.meta["stream_terminal"] == "incomplete"


def test_chat_legacy_function_call_is_executable(monkeypatch):
    resp = _run_chat(monkeypatch, _driver(), {
        "choices": [{
            "message": {
                "content": "",
                "function_call": {"name": "read_file", "arguments": "{}"},
            },
            "finish_reason": "function_call",
        }],
        "usage": {},
    })
    assert resp.error is None
    assert resp.meta["stream_terminal"] == "tool_calls"
    assert resp.meta["tool_calls"][0]["function"]["name"] == "read_file"


def test_host_overlays_remain_on_the_request(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured[req.full_url] = json.loads(req.data.decode("utf-8"))
        return _SseResp([
            _data({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}),
            b"data: [DONE]\n",
        ])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    tools = [{"type": "function", "function": {"name": "run_command", "parameters": {}}}]

    go = _driver("https://opencode.ai/zen/go/v1", "ox-alpha-free")
    go.chat_stream([{"role": "user", "content": "hi"}], tools=tools, on_delta=lambda _t: None)
    go_body = captured[f"{go.base_url}/chat/completions"]
    assert "stream_options" not in go_body
    assert "parallel_tool_calls" not in go_body

    router = _driver("https://openrouter.ai/api/v1", "stealth/ox-alpha")
    router.chat_stream(
        [{"role": "user", "content": "hi"}], tools=tools, on_delta=lambda _t: None,
    )
    or_body = captured[f"{router.base_url}/chat/completions"]
    assert or_body["parallel_tool_calls"] is True
    assert or_body["stream_options"] == {"include_usage": True}


@pytest.mark.parametrize(
    "finish,terminal",
    [
        ("STOP", "stop"),
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("MAX_TOKENS", "length"),
        ("max_output_tokens", "length"),
        ("length", "length"),
        ("CONTENT_FILTER", "content_filter"),
    ],
)
def test_chat_finish_aliases_are_case_insensitive(monkeypatch, finish, terminal):
    resp = _run_chat(monkeypatch, _driver(), {
        "choices": [{
            "message": {"content": "alias text", "tool_calls": []},
            "finish_reason": finish,
        }],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    })
    assert resp.meta["finish_reason"] == finish
    assert resp.meta["stream_terminal"] == terminal
    assert "without a finish_reason" not in str(resp.error or "")
    if terminal == "stop":
        assert resp.error is None
    else:
        assert resp.error
        assert finish in resp.error


def test_chat_tool_calls_alias_validates_json(monkeypatch):
    ok = _run_chat(monkeypatch, _driver(), {
        "choices": [{
            "message": {
                "content": "",
                "tool_calls": [{
                    "id": "c1",
                    "function": {"name": "read_file", "arguments": "{}"},
                }],
            },
            "finish_reason": "TOOL_CALLS",
        }],
        "usage": {},
    })
    assert ok.error is None
    assert ok.meta["stream_terminal"] == "tool_calls"
    assert ok.meta["finish_reason"] == "TOOL_CALLS"

    truncated = _run_chat(monkeypatch, _driver(), {
        "choices": [{
            "message": {
                "content": "",
                "tool_calls": [{
                    "id": "c1",
                    "function": {"name": "read_file", "arguments": '{"path":'},
                }],
            },
            "finish_reason": "function_call",
        }],
        "usage": {},
    })
    assert truncated.error
    assert truncated.meta["stream_terminal"] == "incomplete"
    assert "without a finish_reason" not in truncated.error


def test_openai_compat_requires_explicit_terminal():
    assert _driver().requires_explicit_terminal is True


@pytest.mark.parametrize(
    "finish,terminal",
    [
        ("STOP", "stop"),
        ("MAX_TOKENS", "length"),
        ("CONTENT_FILTER", "content_filter"),
        ("end_turn", "stop"),
    ],
)
def test_stream_finish_aliases_are_case_insensitive(monkeypatch, finish, terminal):
    lines = [
        _data({"choices": [{"delta": {"content": "ok"}, "finish_reason": finish}]}),
        b"data: [DONE]\n",
    ]
    resp = _run_stream(monkeypatch, _driver(), lines)
    assert resp.meta["finish_reason"] == finish
    assert resp.meta["stream_terminal"] == terminal
    assert "without a finish_reason" not in str(resp.error or "")


def test_openrouter_keepalives_do_not_cut_visible_answer(monkeypatch):
    """OpenRouter DeepSeek pauses after reasoning; comment keepalives must not seal."""
    lines = [
        _data({"choices": [{"delta": {"reasoning_content": "drafting summary"}}]}),
    ]
    lines.extend([b": keepalive\n"] * 10)
    lines.extend(
        [
            _data({"choices": [{"delta": {"content": "here is the map"}, "finish_reason": "stop"}]}),
            b"data: [DONE]\n",
        ]
    )
    resp = _run_stream(
        monkeypatch,
        _driver(base_url="https://openrouter.ai/api/v1", model="deepseek/deepseek-v4.1-flash"),
        lines,
    )
    assert resp.error is None
    assert resp.text == "here is the map"
    assert resp.meta["finish_reason"] == "stop"


def test_finish_reason_without_done_settles_when_end_on_finish():
    """llama.cpp can send finish_reason=stop then keepalives with no [DONE]."""
    def _lines():
        yield _data({"choices": [{"delta": {"content": "pong"}, "finish_reason": "stop"}]})
        n = 0
        while True:
            n += 1
            if n > 50:
                raise AssertionError("parser waited for [DONE] after finish_reason")
            yield b": keepalive\n"

    parsed = _consume_openai_chat_sse(_lines(), end_on_finish=True)
    assert parsed["error"] is None
    assert parsed["text"] == "pong"
    assert parsed["finish_reason"] == "stop"
    assert parsed["stream_terminal"] == "stop"
    assert parsed["saw_done"] is False
    assert parsed["stream_started"] is True


def test_llama_cpp_requests_and_drains_trailing_usage(monkeypatch):
    captured = {}

    class _LazySse:
        def __init__(self):
            self._sock = _OwnedReadSocket()
            self._tail = io.BytesIO(
                b"\n"
                + _data({
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 3741,
                        "completion_tokens": 32768,
                        "prompt_tokens_details": {"cached_tokens": 2622},
                    },
                })
                + b"\ndata: [DONE]\n\n"
            )

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            yield _data({
                "choices": [{"delta": {"content": "pong"}, "finish_reason": "stop"}],
            })

        def settimeout(self, _seconds):
            return None

        def read1(self, size):
            return self._tail.read(size)

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _LazySse()

    monkeypatch.setenv("LLAMA_CPP_API_KEY", "local-test-key")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    driver = OpenAICompatDriver(
        name="llama-cpp:qwen38-cyber",
        model="qwen38-cyber",
        base_url="http://127.0.0.1:8080/v1",
        api_key_env="LLAMA_CPP_API_KEY",
        extra_body={"stream_options": {"include_usage": True, "future": "ok"}},
    )
    resp = driver.chat_stream(
        [{"role": "user", "content": "ping"}],
        on_delta=lambda _t: None,
    )
    assert captured["body"]["stream_options"] == {
        "include_usage": True,
        "future": "ok",
    }
    assert resp.error is None
    assert resp.text == "pong"
    assert resp.tokens_in == 3741
    assert resp.tokens_out == 32768
    assert resp.meta["cache_read_tokens"] == 2622
    assert resp.meta["raw_usage"]["completion_tokens"] == 32768
    assert resp.meta["finish_reason"] == "stop"
    assert resp.meta["stream_terminal"] == "stop"
    assert resp.meta["saw_done"] is False


def test_llama_cpp_usage_on_finish_does_not_read_a_tail(monkeypatch):
    class _UsageOnFinish:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            yield _data({
                "choices": [{"delta": {"content": "pong"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            })

        def read1(self, _size):
            raise AssertionError("usage on the terminal event must not drain a tail")

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _UsageOnFinish())
    driver = OpenAICompatDriver(
        name="llama-cpp:qwen",
        model="qwen",
        base_url="http://127.0.0.1:8080/v1",
        api_key_env="UNUSED",
    )
    resp = driver.chat_stream(
        [{"role": "user", "content": "ping"}], on_delta=lambda _t: None,
    )
    assert resp.error is None
    assert (resp.tokens_in, resp.tokens_out) == (3, 1)


def test_llama_cpp_done_without_finish_does_not_read_a_tail(monkeypatch):
    class _DoneWithoutFinish:
        _sock = _OwnedReadSocket()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            yield _data({"choices": [{"delta": {"content": "partial"}}]})
            yield b"data: [DONE]\n"

        def read1(self, _size):
            raise AssertionError("[DONE] without a finish_reason must not drain")

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _DoneWithoutFinish())
    driver = OpenAICompatDriver(
        name="llama-cpp:qwen",
        model="qwen",
        base_url="http://127.0.0.1:8080/v1",
        api_key_env="UNUSED",
    )
    resp = driver.chat_stream(
        [{"role": "user", "content": "ping"}], on_delta=lambda _t: None,
    )
    assert resp.error
    assert resp.text == "partial"
    assert resp.meta["finish_reason"] == ""


def test_llama_cpp_missing_terminal_does_not_read_a_tail(monkeypatch):
    class _MissingTerminal:
        _sock = _OwnedReadSocket()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            yield _data({"choices": [{"delta": {"content": "partial"}}]})

        def read1(self, _size):
            raise AssertionError("a stream without a terminal must not drain")

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _MissingTerminal())
    driver = OpenAICompatDriver(
        name="llama-cpp:qwen",
        model="qwen",
        base_url="http://127.0.0.1:8080/v1",
        api_key_env="UNUSED",
    )
    resp = driver.chat_stream(
        [{"role": "user", "content": "ping"}], on_delta=lambda _t: None,
    )
    assert resp.error
    assert resp.text == "partial"
    assert resp.meta["stream_terminal"] == "incomplete"


def test_llama_cpp_tail_without_owned_socket_is_unavailable(monkeypatch):
    class _NoInterruptSeam:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            yield _data({
                "choices": [{"delta": {"content": "pong"}, "finish_reason": "stop"}],
            })

        def read1(self, _size):
            raise AssertionError("an uninterruptible tail must not be read")

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _NoInterruptSeam())
    driver = OpenAICompatDriver(
        name="llama-cpp:qwen",
        model="qwen",
        base_url="http://127.0.0.1:8080/v1",
        api_key_env="UNUSED",
    )
    resp = driver.chat_stream(
        [{"role": "user", "content": "ping"}], on_delta=lambda _t: None,
    )
    assert resp.error is None
    assert resp.text == "pong"
    assert resp.tokens_in == 0
    assert resp.tokens_out == 0


@pytest.mark.parametrize("tail", [b"", b"\ndata: [DONE]\n\n", b"\ndata: not-json\n\n"])
def test_llama_cpp_missing_or_malformed_usage_preserves_finish(monkeypatch, tail):
    class _Tail:
        def __init__(self):
            self._sock = _OwnedReadSocket()
            self.tail = io.BytesIO(tail)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            yield _data({
                "choices": [{"delta": {"content": "partial"}, "finish_reason": "length"}],
            })

        def settimeout(self, _seconds):
            return None

        def read1(self, size):
            return self.tail.read(size)

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Tail())
    driver = OpenAICompatDriver(
        name="llama-cpp:qwen",
        model="qwen",
        base_url="http://127.0.0.1:8080/v1",
        api_key_env="UNUSED",
        max_tokens=0,
    )
    resp = driver.chat_stream(
        [{"role": "user", "content": "ping"}], on_delta=lambda _t: None,
    )
    assert resp.meta["finish_reason"] == "length"
    assert resp.meta["stream_terminal"] == "length"
    assert resp.error
    assert resp.tokens_in == 0
    assert resp.tokens_out == 0


def test_llama_cpp_tail_never_appends_text_tools_or_later_finish(monkeypatch):
    trailing_junk = _data({
        "choices": [{
            "delta": {
                "content": "must not appear",
                "tool_calls": [{
                    "index": 0,
                    "id": "late",
                    "function": {"name": "read_file", "arguments": "{}"},
                }],
            },
            "finish_reason": "tool_calls",
        }],
    })

    class _Tail:
        def __init__(self):
            self._sock = _OwnedReadSocket()
            self.tail = io.BytesIO(b"\n" + trailing_junk + b"\ndata: [DONE]\n\n")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            yield _data({
                "choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}],
            })

        def settimeout(self, _seconds):
            return None

        def read1(self, size):
            return self.tail.read(size)

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Tail())
    driver = OpenAICompatDriver(
        name="llama-cpp:qwen",
        model="qwen",
        base_url="http://127.0.0.1:8080/v1",
        api_key_env="UNUSED",
    )
    resp = driver.chat_stream(
        [{"role": "user", "content": "ping"}], on_delta=lambda _t: None,
    )
    assert resp.error is None
    assert resp.text == "answer"
    assert resp.meta["finish_reason"] == "stop"
    assert resp.meta["tool_calls"] == []


def test_llama_cpp_without_usage_request_keeps_early_finish(monkeypatch):
    class _NoUsageDriver(OpenAICompatDriver):
        def _build_chat_body(self, *args, **kwargs):
            body = super()._build_chat_body(*args, **kwargs)
            body.pop("stream_options", None)
            return body

    class _FinishThenFail:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            yield _data({
                "choices": [{"delta": {"content": "pong"}, "finish_reason": "stop"}],
            })
            raise AssertionError("no-usage request read beyond the finish event")

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _FinishThenFail())
    driver = _NoUsageDriver(
        name="llama-cpp:qwen",
        model="qwen",
        base_url="http://127.0.0.1:8080/v1",
        api_key_env="UNUSED",
    )
    resp = driver.chat_stream(
        [{"role": "user", "content": "ping"}], on_delta=lambda _t: None,
    )
    assert resp.error is None
    assert resp.text == "pong"


def test_llama_cpp_oversized_tail_stops_without_changing_finish(monkeypatch):
    class _OversizedTail:
        def __init__(self):
            self._sock = _OwnedReadSocket()
            self.remaining = openai_compat._LLAMA_CPP_USAGE_TAIL_BYTES + 4096
            self.read_bytes = 0

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            yield _data({
                "choices": [{"delta": {"content": "pong"}, "finish_reason": "stop"}],
            })

        def settimeout(self, _seconds):
            return None

        def read1(self, size):
            chunk = b"x" * min(size, self.remaining)
            self.remaining -= len(chunk)
            self.read_bytes += len(chunk)
            return chunk

    response = _OversizedTail()
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: response)
    driver = OpenAICompatDriver(
        name="llama-cpp:qwen",
        model="qwen",
        base_url="http://127.0.0.1:8080/v1",
        api_key_env="UNUSED",
    )
    resp = driver.chat_stream(
        [{"role": "user", "content": "ping"}], on_delta=lambda _t: None,
    )
    assert resp.error is None
    assert resp.meta["finish_reason"] == "stop"
    assert response.read_bytes == openai_compat._LLAMA_CPP_USAGE_TAIL_BYTES


def test_llama_cpp_length_with_complete_then_partial_calls_stays_withheld(monkeypatch):
    calls = {"count": 0}

    class _Tail:
        def __init__(self):
            self._sock = _OwnedReadSocket()
            self.tail = io.BytesIO(b"\n" + _data({
                "choices": [],
                "usage": {"prompt_tokens": 8, "completion_tokens": 4},
            }))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            yield _data({
                "choices": [{
                    "delta": {"tool_calls": [{
                        "index": 0,
                        "id": "complete",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }, {
                        "index": 1,
                        "id": "partial",
                        "function": {"name": "read_file", "arguments": '{"path":'},
                    }]},
                    "finish_reason": "length",
                }],
            })

        def settimeout(self, _seconds):
            return None

        def read1(self, size):
            return self.tail.read(size)

    def fake_urlopen(*_args, **_kwargs):
        calls["count"] += 1
        return _Tail()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    driver = OpenAICompatDriver(
        name="vendor-qwen",
        model="qwen",
        base_url="http://127.0.0.1:8080/v1",
        api_key_env="UNUSED",
        vendor="llamacpp",
    )
    resp = driver.chat_stream(
        [{"role": "user", "content": "ping"}],
        tools=[{"type": "function", "function": {"name": "read_file"}}],
        on_delta=lambda _t: None,
    )
    assert calls["count"] == 1
    assert resp.meta["finish_reason"] == "length"
    assert resp.meta["stream_terminal"] == "length"
    assert resp.meta["tool_calls"] == []
    assert len(resp.meta["incomplete_tool_calls"]) == 2
    assert (resp.tokens_in, resp.tokens_out) == (8, 4)


def test_llama_cpp_tail_watchdog_interrupts_and_joins(monkeypatch):
    released = threading.Event()

    class _BlockedTail:
        def __init__(self):
            self._sock = _OwnedReadSocket(released)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            yield _data({
                "choices": [{"delta": {"content": "pong"}, "finish_reason": "stop"}],
            })

        def read1(self, _size):
            assert released.wait(1.0)
            return b""

    response = _BlockedTail()
    monkeypatch.setattr(openai_compat, "_LLAMA_CPP_USAGE_TAIL_SECONDS", 0.02)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: response)
    driver = OpenAICompatDriver(
        name="llama-cpp:qwen",
        model="qwen",
        base_url="http://127.0.0.1:8080/v1",
        api_key_env="UNUSED",
    )
    started = time.monotonic()
    resp = driver.chat_stream(
        [{"role": "user", "content": "ping"}], on_delta=lambda _t: None,
    )
    assert time.monotonic() - started < 0.2
    assert response._sock.shutdown_calls == 1
    assert not any(
        thread.name == "llama-cpp-usage-tail-deadline"
        for thread in threading.enumerate()
    )
    assert resp.error is None
    assert resp.text == "pong"


def test_llama_cpp_httpresponse_chunk_header_is_absolutely_bounded(monkeypatch):
    client, server = socket.socketpair()
    stop = threading.Event()

    terminal = _data({
        "choices": [{"delta": {"content": "pong"}, "finish_reason": "stop"}],
    }) + b"\n"

    def serve():
        with server:
            try:
                server.sendall(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: text/event-stream\r\n"
                    b"Transfer-Encoding: chunked\r\n"
                    b"Connection: close\r\n\r\n"
                    + f"{len(terminal):x}\r\n".encode("ascii")
                    + terminal
                    + b"\r\n"
                )
                for _ in range(100):
                    if stop.wait(0.01):
                        return
                    server.sendall(b"1")
            except OSError:
                return

    server_thread = threading.Thread(target=serve, daemon=True)
    server_thread.start()
    raw_response = http.client.HTTPResponse(client)
    raw_response.begin()
    monkeypatch.setattr(openai_compat, "_LLAMA_CPP_USAGE_TAIL_SECONDS", 0.05)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: raw_response)
    driver = OpenAICompatDriver(
        name="llama-cpp:qwen",
        model="qwen",
        base_url="http://127.0.0.1:8080/v1",
        api_key_env="UNUSED",
    )
    started = time.monotonic()
    try:
        resp = driver.chat_stream(
            [{"role": "user", "content": "ping"}], on_delta=lambda _t: None,
        )
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        client.close()
        server_thread.join(timeout=2.0)

    assert not server_thread.is_alive()
    assert elapsed < 0.3
    assert resp.error is None
    assert resp.text == "pong"
    assert resp.meta["finish_reason"] == "stop"
    assert not any(
        thread.name == "llama-cpp-usage-tail-deadline"
        for thread in threading.enumerate()
    )


@pytest.mark.parametrize("rejected_param", [
    "stream_options",
    "stream_options.include_usage",
    "include_usage",
])
def test_llama_cpp_stream_options_400_falls_back_once_and_stays_omitted(
    monkeypatch, rejected_param,
):
    bodies = []

    class _JsonResponse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({
                "choices": [{
                    "message": {"content": "fallback", "tool_calls": []},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 4, "completion_tokens": 1},
            }).encode("utf-8")

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        bodies.append(body)
        if len(bodies) == 1:
            detail = json.dumps({
                "error": {
                    "param": rejected_param,
                    "message": "unsupported stream_options value secret-value",
                },
            }).encode("utf-8")
            raise urllib.error.HTTPError(
                req.full_url, 400, "bad", {}, io.BytesIO(detail),
            )
        if body.get("stream") is True:
            return _SseResp([
                _data({
                    "choices": [{
                        "delta": {"content": "next"},
                        "finish_reason": "stop",
                    }],
                }),
            ])
        return _JsonResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    driver = OpenAICompatDriver(
        name="llama-cpp:qwen",
        model="qwen",
        base_url="http://127.0.0.1:8080/v1",
        api_key_env="UNUSED",
    )
    first = driver.chat_stream(
        [{"role": "user", "content": "ping"}], on_delta=lambda _t: None,
    )
    second = driver.chat_stream(
        [{"role": "user", "content": "again"}], on_delta=lambda _t: None,
    )

    assert len(bodies) == 3
    assert bodies[0]["stream_options"]["include_usage"] is True
    assert bodies[1].get("stream") is not True
    assert "stream_options" not in bodies[1]
    assert bodies[2]["stream"] is True
    assert "stream_options" not in bodies[2]
    assert first.text == "fallback"
    assert first.meta["recovery_attempted"] is True
    assert first.meta["retry_attempts"] == 2
    assert first.meta["omitted_provider_controls"] == ["stream_options"]
    assert second.text == "next"
    assert second.meta["omitted_provider_controls"] == ["stream_options"]
    assert "secret-value" not in json.dumps(first.meta)
    fresh_driver = OpenAICompatDriver(
        name="llama-cpp:fresh",
        model="qwen",
        base_url="http://127.0.0.1:8080/v1",
        api_key_env="UNUSED",
    )
    assert fresh_driver._build_chat_body([], stream=True)["stream_options"] == {
        "include_usage": True,
    }


def test_llama_cpp_stream_options_rejection_after_activity_does_not_fallback(monkeypatch):
    calls = []

    class _PartialThenReject:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            yield _data({"choices": [{"delta": {"content": "partial"}}]})
            raise urllib.error.HTTPError(
                "http://127.0.0.1:8080/v1/chat/completions",
                400,
                "bad",
                {},
                io.BytesIO(json.dumps({
                    "error": {
                        "param": "stream_options",
                        "message": "unsupported stream_options",
                    },
                }).encode("utf-8")),
            )

    def fake_urlopen(req, timeout=None):
        calls.append(json.loads(req.data.decode("utf-8")))
        return _PartialThenReject()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    driver = OpenAICompatDriver(
        name="llama-cpp:qwen",
        model="qwen",
        base_url="http://127.0.0.1:8080/v1",
        api_key_env="UNUSED",
    )
    resp = driver.chat_stream(
        [{"role": "user", "content": "ping"}], on_delta=lambda _t: None,
    )

    assert len(calls) == 1
    assert calls[0]["stream_options"]["include_usage"] is True
    assert resp.text == "partial"
    assert resp.error and "HTTP 400" in resp.error
    assert not resp.meta.get("recovery_attempted")
    assert "stream_options" not in resp.meta.get("omitted_provider_controls", [])


def test_generic_stream_options_rejection_is_not_llama_cpp_compat(monkeypatch):
    bodies = []

    def reject(req, timeout=None):
        bodies.append(json.loads(req.data.decode("utf-8")))
        raise urllib.error.HTTPError(
            req.full_url,
            400,
            "bad",
            {},
            io.BytesIO(json.dumps({
                "error": {
                    "param": "stream_options",
                    "message": "unsupported stream_options",
                },
            }).encode("utf-8")),
        )

    monkeypatch.setattr(urllib.request, "urlopen", reject)
    driver = OpenAICompatDriver(
        name="generic",
        model="model",
        base_url="https://example.test/v1",
        api_key_env="UNUSED",
    )
    driver._key = lambda: "test"
    resp = driver.chat_stream(
        [{"role": "user", "content": "ping"}], on_delta=lambda _t: None,
    )

    assert len(bodies) == 1
    assert bodies[0]["stream_options"]["include_usage"] is True
    assert resp.error and "HTTP 400" in resp.error
    assert not resp.meta.get("recovery_attempted")


def test_generic_explicit_stream_options_rejection_preserves_default_request(monkeypatch):
    bodies = []

    def transport(req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        bodies.append(body)
        if len(bodies) == 1:
            detail = json.dumps({"error": {"param": "stream_options"}}).encode()
            raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(detail))
        if not body.get("stream"):
            return io.BytesIO(json.dumps({"choices": [{
                "message": {"content": "fallback"}, "finish_reason": "stop",
            }]}).encode())
        return _SseResp([_data({"choices": [{
            "delta": {"content": "next"}, "finish_reason": "stop",
        }]}), b"data: [DONE]\n\n"])

    monkeypatch.setattr(urllib.request, "urlopen", transport)
    driver = _driver(base_url="https://example.test/v1")
    driver.extra_body = {"stream_options": {"include_usage": True}}
    first = driver.chat_stream([], on_delta=lambda _text: None)
    second = driver.chat_stream([], on_delta=lambda _text: None)

    assert len(bodies) == 3
    assert not first.error and not second.error
    assert first.meta["recovery_attempted"] is True
    assert bodies[2]["stream_options"] == {"include_usage": True}
    assert "stream_options" not in second.meta.get("omitted_provider_controls", [])


@contextmanager
def _real_llama_tail_server(mode):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    stop = threading.Event()
    accepted = {}

    def serve():
        try:
            conn, _ = listener.accept()
            accepted["conn"] = conn
            with conn:
                request = b""
                while b"\r\n\r\n" not in request:
                    part = conn.recv(4096)
                    if not part:
                        return
                    request += part
                terminal = _data({
                    "choices": [{
                        "delta": {"content": "pong"},
                        "finish_reason": "stop",
                    }],
                }) + b"\n"
                if mode in {"chunk_header", "chunk_body"}:
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: text/event-stream\r\n"
                        b"Transfer-Encoding: chunked\r\n"
                        b"Connection: close\r\n\r\n"
                        + f"{len(terminal):x}\r\n".encode("ascii")
                        + terminal
                        + b"\r\n"
                    )
                    if mode == "chunk_body":
                        conn.sendall(b"10000\r\n")
                    for _ in range(100):
                        if stop.wait(0.01):
                            return
                        conn.sendall(b"1")
                    return
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: text/event-stream\r\n"
                    b"Connection: close\r\n\r\n"
                    + terminal
                )
                if mode == "silent":
                    stop.wait(3.0)
                    return
                payload = b": keepalive\n\n" if mode == "keepalive" else b"x"
                while not stop.wait(0.01):
                    try:
                        conn.sendall(payload)
                    except OSError:
                        return
        except OSError:
            return

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}/v1"
    finally:
        stop.set()
        conn = accepted.get("conn")
        if conn is not None:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        listener.close()
        thread.join(timeout=2.0)
        assert not thread.is_alive()


@pytest.mark.parametrize(
    "mode", ["silent", "keepalive", "trickle", "chunk_header", "chunk_body"],
)
def test_llama_cpp_real_socket_tail_is_absolutely_bounded(monkeypatch, mode):
    monkeypatch.setattr(openai_compat, "_LLAMA_CPP_USAGE_TAIL_SECONDS", 0.2)
    with _real_llama_tail_server(mode) as base_url:
        driver = OpenAICompatDriver(
            name="llama-cpp:qwen",
            model="qwen",
            base_url=base_url,
            api_key_env="UNUSED",
            timeout=3,
        )
        started = time.monotonic()
        resp = driver.chat_stream(
            [{"role": "user", "content": "ping"}], on_delta=lambda _t: None,
        )
        elapsed = time.monotonic() - started
    assert elapsed < 0.7
    assert resp.error is None
    assert resp.text == "pong"
    assert resp.meta["finish_reason"] == "stop"
    assert not any(
        thread.name == "llama-cpp-usage-tail-deadline"
        for thread in threading.enumerate()
    )


def test_stop_finish_with_complete_tool_calls_is_executable():
    """llama.cpp/Qwen often emit delta.tool_calls then finish_reason=stop."""
    def _lines():
        yield _data({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_wiki",
                        "type": "function",
                        "function": {
                            "name": "query_wiki",
                            "arguments": '{"question":"prior findings on example service"}',
                        },
                    }, {
                        "index": 1,
                        "id": "call_fetch",
                        "type": "function",
                        "function": {
                            "name": "web_fetch",
                            "arguments": '{"url":"https://example.com/docs"}',
                        },
                    }],
                },
                "finish_reason": "stop",
            }],
        })
        n = 0
        while True:
            n += 1
            if n > 50:
                raise AssertionError("parser waited for [DONE] after stop+tools")
            yield b": keepalive\n"

    parsed = _consume_openai_chat_sse(_lines(), end_on_finish=True)
    assert parsed["error"] is None
    assert parsed["finish_reason"] == "stop"
    assert parsed["stream_terminal"] == "tool_calls"
    names = [tc["function"]["name"] for tc in parsed["tool_calls"]]
    assert names == ["query_wiki", "web_fetch"]
    assert parsed["saw_done"] is False


def test_stop_finish_with_truncated_tool_args_is_incomplete():
    lines = [
        _data({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_1",
                        "function": {
                            "name": "query_wiki",
                            "arguments": '{"question":',
                        },
                    }],
                },
                "finish_reason": "stop",
            }],
        }),
        b"data: [DONE]\n",
    ]
    parsed = _consume_openai_chat_sse(lines)
    assert parsed["error"]
    assert parsed["stream_terminal"] == "incomplete"
    assert parsed["tool_calls"] == []
    assert parsed["incomplete_tool_calls"][0]["function"]["name"] == "query_wiki"


def test_ollama_loopback_keeps_stream_options_and_is_not_llama_cpp(monkeypatch):
    captured = {}

    class _DoneSse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            yield _data({
                "choices": [{"delta": {"content": "pong"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            })
            yield b"data: [DONE]\n"

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _DoneSse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    driver = OpenAICompatDriver(
        name="local:ollama-127-0-0-1-11434/llama3",
        model="llama3",
        base_url="http://127.0.0.1:11434/v1",
        api_key_env="LOCAL_OLLAMA_11434_API_KEY",
        vendor="ollama",
        allow_keyless=True,
    )
    assert driver._is_llama_cpp_host() is False
    assert driver._is_loopback_base() is True
    resp = driver.chat_stream(
        [{"role": "user", "content": "ping"}],
        on_delta=lambda _t: None,
    )
    assert captured["body"]["stream_options"] == {"include_usage": True}
    assert resp.error is None
    assert resp.meta.get("raw_usage") is not None or resp.tokens_out or resp.tokens_in


def test_public_llama_cpp_driver_key_fails_closed(monkeypatch):
    monkeypatch.delenv("PUBLIC_LLAMA_KEY", raising=False)
    driver = OpenAICompatDriver(
        name="llama-cpp:qwen",
        model="qwen",
        base_url="https://proxy.runpod.net/v1",
        api_key_env="PUBLIC_LLAMA_KEY",
        vendor="llama.cpp",
        allow_keyless=False,
    )
    with pytest.raises(RuntimeError, match="missing API key"):
        driver._key()


def test_loopback_llama_cpp_driver_synthesizes_local_key(monkeypatch):
    monkeypatch.delenv("LOCAL_LLAMA_KEY", raising=False)
    driver = OpenAICompatDriver(
        name="llama-cpp:qwen",
        model="qwen",
        base_url="http://127.0.0.1:8080/v1",
        api_key_env="LOCAL_LLAMA_KEY",
        vendor="llama.cpp",
        allow_keyless=False,
    )
    assert driver._key() == "local"
