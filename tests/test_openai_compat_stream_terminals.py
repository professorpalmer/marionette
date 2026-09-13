"""Fail-closed OpenAI chat Completions stream terminals.

Shared parser contract for Ox Alpha / OpenRouter / OpenCode Go hosts.
Hermetic: no network.
"""
from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

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


def test_llama_cpp_omits_stream_options_and_stops_on_finish(monkeypatch):
    captured = {}
    hung = {"n": 0}

    class _LazySse:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            yield _data({
                "choices": [{"delta": {"content": "pong"}, "finish_reason": "stop"}],
            })
            while True:
                hung["n"] += 1
                if hung["n"] > 50:
                    raise AssertionError("llama-cpp parser waited for [DONE]")
                yield b": keepalive\n"

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
    )
    resp = driver.chat_stream(
        [{"role": "user", "content": "ping"}],
        on_delta=lambda _t: None,
    )
    assert "stream_options" not in captured["body"]
    assert resp.error is None
    assert resp.text == "pong"
    assert resp.meta["finish_reason"] == "stop"
    assert resp.meta["stream_terminal"] == "stop"
    assert resp.meta["saw_done"] is False


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
