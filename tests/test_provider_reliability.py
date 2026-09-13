"""Bounded provider recovery and Stop precedence, without network access."""
import json
import io
import queue
import threading
import urllib.error
from types import SimpleNamespace

import pytest

from pmharness.drivers.base import DriverResponse
from harness.send_loop_phases import run_stream, settle_provider_step_terminal
from harness.stream_performance_store import copy_stream_performance
from test_openai_compat_stream_terminals import _driver, _data, _SseResp


def _response(text="Finished", error=None, **meta):
    return DriverResponse(text=text, error=error, meta=meta)


def _go_exchange(monkeypatch, lines, second):
    calls = []

    class JsonResponse(_SseResp):
        def read(self):
            if isinstance(second, Exception):
                raise second
            return json.dumps(second).encode()

    def urlopen(req, **kwargs):
        body = json.loads(req.data)
        calls.append(body)
        assert len(calls) <= 2, "recovery exceeded one request"
        return _SseResp(lines) if len(calls) == 1 else JsonResponse([])

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return calls


@pytest.mark.parametrize("channel", ["content", "reasoning_content"])
@pytest.mark.parametrize("failure", [False, True])
def test_go_terminal_less_recovery(monkeypatch, channel, failure):
    lines = [_data({"choices": [{"delta": {channel: "Started"}}]})]
    second = TimeoutError("The read operation timed out") if failure else {
        "choices": [{"message": {"content": "Started and finished"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }
    calls = _go_exchange(monkeypatch, lines, second)
    seen = []
    resp = _driver("https://opencode.ai/zen/go/v1").chat_stream(
        [{"role": "user", "content": "hi"}], on_delta=seen.append,
    )
    assert len(calls) == 2
    assert seen == (["Started"] if channel == "content" else [])
    assert bool(resp.error) == failure
    if not failure:
        assert resp.text == "Started and finished"
    perf = copy_stream_performance(resp.meta.get("stream_performance"))
    assert perf["recovery_attempt_count"] == 1
    assert perf["recovery_success_count"] == int(not failure)
    assert perf["recovery_failure_count"] == int(failure)


@pytest.mark.parametrize("second_terminal", ["terminal_less", "empty_tools"])
def test_go_timeout_recovery_does_not_chain_another_recovery(
    monkeypatch, second_terminal,
):
    calls = []

    class SecondResponse(_SseResp):
        def __iter__(self):
            finish = "tool_calls" if second_terminal == "empty_tools" else None
            yield _data({"choices": [{
                "delta": {"content": "Partial"},
                "finish_reason": finish,
            }]})

    def urlopen(req, **kwargs):
        calls.append(json.loads(req.data))
        if len(calls) == 1:
            raise TimeoutError("The read operation timed out")
        return SecondResponse([])

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    tools = [{"type": "function", "function": {
        "name": "run_parallel", "parameters": {"type": "object"},
    }}]
    resp = _driver("https://opencode.ai/zen/go/v1").chat_stream(
        [{"role": "user", "content": "hi"}], tools=tools,
        on_delta=lambda _: None,
    )

    assert len(calls) == 2
    assert resp.error
    assert resp.meta["recovery_attempted"] is True
    assert resp.meta["stream_performance"]["recovery_attempt_count"] == 1


def test_go_timeout_recovery_does_not_chain_empty_400_fallback(monkeypatch):
    calls = []

    def urlopen(req, **kwargs):
        calls.append(json.loads(req.data))
        if len(calls) == 1:
            raise TimeoutError("The read operation timed out")
        raise urllib.error.HTTPError(
            req.full_url, 400, "Bad Request", {},
            io.BytesIO(b'{"choices":[{"message":{"content":""}}]}'),
        )

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    resp = _driver("https://opencode.ai/zen/go/v1").chat_stream(
        [{"role": "user", "content": "hi"}], on_delta=lambda _: None,
    )

    assert len(calls) == 2
    assert resp.error
    assert resp.meta["retry_attempts"] == 2
    assert resp.meta["stream_performance"] == {
        "recovery_attempt_count": 1,
        "recovery_success_count": 0,
        "recovery_failure_count": 1,
    }


@pytest.mark.parametrize("stream", [False, True])
def test_timeout_retry_does_not_chain_reasoning_fallback(monkeypatch, stream):
    calls = []

    def urlopen(req, **kwargs):
        calls.append(json.loads(req.data))
        if len(calls) == 1:
            raise TimeoutError("The read operation timed out")
        raise urllib.error.HTTPError(
            req.full_url, 400, "Bad Request", {},
            io.BytesIO(b'{"error":{"message":"Unknown parameter: reasoning"}}'),
        )

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    driver = _driver("https://opencode.ai/zen/go/v1")
    driver.enable_reasoning = True
    messages = [{"role": "user", "content": "hi"}]
    if stream:
        resp = driver.chat_stream(messages, on_delta=lambda _: None)
    else:
        resp = driver.chat(messages)

    assert len(calls) == 2
    assert resp.error
    assert resp.meta["retry_attempts"] == 2


@pytest.mark.parametrize("stream", [False, True])
def test_reasoning_fallback_timeout_does_not_chain_transport_retry(
    monkeypatch, stream,
):
    calls = []

    def urlopen(req, **kwargs):
        calls.append(json.loads(req.data))
        if len(calls) == 1:
            raise urllib.error.HTTPError(
                req.full_url, 400, "Bad Request", {},
                io.BytesIO(b'{"error":{"message":"Unknown parameter: reasoning"}}'),
            )
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    driver = _driver("https://opencode.ai/zen/go/v1")
    driver.enable_reasoning = True
    messages = [{"role": "user", "content": "hi"}]
    if stream:
        resp = driver.chat_stream(messages, on_delta=lambda _: None)
    else:
        resp = driver.chat(messages)

    assert len(calls) == 2
    assert resp.error
    assert resp.meta["retry_attempts"] == 2
    assert resp.meta["recovery_attempted"] is True


def test_go_read_timeout_recovers_once_without_reemitting_text(monkeypatch):
    calls = []
    seen = []

    class TimeoutStream(_SseResp):
        def __iter__(self):
            yield _data({"choices": [{"delta": {"content": "Started"}}]})
            raise TimeoutError("The read operation timed out")

    class RecoveryResponse(_SseResp):
        def read(self):
            return json.dumps({
                "choices": [{
                    "message": {"content": "Started and finished"},
                    "finish_reason": "stop",
                }],
            }).encode()

    def urlopen(req, **kwargs):
        calls.append(json.loads(req.data))
        return TimeoutStream([]) if len(calls) == 1 else RecoveryResponse([])

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    resp = _driver("https://opencode.ai/zen/go/v1").chat_stream(
        [{"role": "user", "content": "hi"}], on_delta=seen.append,
    )
    assert len(calls) == 2
    assert seen == ["Started"]
    assert resp.error is None
    assert resp.text == "Started and finished"
    assert resp.meta["retry_attempts"] == 2
    assert resp.meta["stream_performance"]["recovery_attempt_count"] == 1


def test_go_waits_through_keepalives_for_real_finish(monkeypatch):
    lines = [_data({"choices": [{"delta": {"reasoning_content": "Thinking"}}]})]
    lines += [b": keepalive\n"] * 12
    lines += [_data({"choices": [{"delta": {"content": "Finished"}, "finish_reason": "stop"}]}), b"data: [DONE]\n"]
    calls = _go_exchange(monkeypatch, lines, {})
    armed = []
    monkeypatch.setattr("pmharness.drivers.codex_responses._arm_post_answer_idle_timeout", lambda *a: armed.append(a))
    resp = _driver("https://opencode.ai/zen/go/v1").chat_stream([], on_delta=lambda _: None)
    assert resp.error is None
    assert resp.text == "Finished"
    assert len(calls) == 1
    assert armed == []


def _session(stream, chat):
    return SimpleNamespace(
        pilot=SimpleNamespace(chat_stream=stream, chat=chat),
        _cancel=threading.Event(),
        _messages_for_provider=lambda: [{"role": "user", "content": "hi"}],
    )


@pytest.mark.parametrize("terminal", ["incomplete", "error", "provider_eof"])
def test_stop_wins_over_provider_failure(terminal):
    cancel = threading.Event()
    cancel.set()
    session = SimpleNamespace(_cancel=cancel, _submit_housekeeping=lambda *a: None, _maybe_ingest=lambda *a: None)
    events = list(settle_provider_step_terminal(
        session, _response("Partial", "late failure", stream_terminal=terminal),
        user_message="hi", step=0, swarms=[], turn_prose=[], turn_findings=[],
    ))
    assert not [e for e in events if e.kind == "error"]
    assert [e.data["stop_cause"] for e in events if e.kind == "assistant_done"] == ["cancelled"]


def test_local_cutoff_provenance_survives_receipt_copy():
    assert copy_stream_performance({"local_idle_cutoff_count": 1, "local_keepalive_cutoff_count": 1}) == {
        "local_idle_cutoff_count": 1, "local_keepalive_cutoff_count": 1,
    }


@pytest.mark.parametrize("finish", ["content_filter", "unknown"])
def test_explicit_terminal_never_recovers(monkeypatch, finish):
    calls = _go_exchange(monkeypatch, [_data({"choices": [{
        "delta": {"content": "Partial"}, "finish_reason": finish,
    }]})], {})
    resp = _driver("https://opencode.ai/zen/go/v1").chat_stream([], on_delta=lambda _: None)
    assert len(calls) == 1
    assert resp.error
    assert resp.meta["finish_reason"] == finish


@pytest.mark.parametrize("error,meta", [
    ("HTTP 401: TimeoutError", {}),
    ("HTTP 403: read timed out", {}),
    ("HTTP 400: configuration timeout invalid", {}),
    ("ValueError('invalid timeout configuration')", {}),
    ("SSLError('certificate verify failed')", {}),
    ("TimeoutError('read timed out')", {"recovery_attempted": True}),
    ("TimeoutError('read timed out')", {"retry_attempts": 2}),
    ("TimeoutError('read timed out')", {"finish_reason": "length"}),
])
def test_nonretryable_or_exhausted_request_never_recovers(error, meta):
    from pmharness.drivers.retry import is_transport_timeout
    assert not is_transport_timeout(_response(error=error, **meta))


def test_stop_during_go_request_suppresses_recovery(monkeypatch):
    cancel = threading.Event()
    calls = []
    class StoppedStream(_SseResp):
        def __iter__(self):
            yield _data({"choices": [{"delta": {"content": "Partial"}}]})
            cancel.set()
            raise TimeoutError("The read operation timed out")
    def urlopen(*a, **kw):
        calls.append(1)
        return StoppedStream([])
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    resp = _driver("https://opencode.ai/zen/go/v1").chat_stream(
        [], on_delta=lambda _: None, is_cancelled=cancel.is_set,
    )
    assert calls == [1]
    assert resp.error
    assert not resp.meta.get("recovery_attempted")


def test_timeout_after_partial_tool_call_fails_closed_without_retry(monkeypatch):
    from harness.terminal_cause import provider_tools_are_executable
    calls = []

    class PartialToolStream(_SseResp):
        def __iter__(self):
            yield _data({"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "bad", "type": "function",
                "function": {
                    "name": "run_command", "arguments": '{"command":',
                },
            }]}}]})
            raise TimeoutError("The read operation timed out")

    def urlopen(req, **kwargs):
        calls.append(json.loads(req.data))
        return PartialToolStream([])

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    resp = _driver("https://opencode.ai/zen/go/v1").chat_stream([], on_delta=lambda _: None)
    assert len(calls) == 1
    assert resp.error
    assert not provider_tools_are_executable(resp)
    assert not resp.meta.get("recovery_attempted")


@pytest.mark.parametrize("cutoff", ["idle", "keepalive"])
def test_actual_local_cutoff_receipt(monkeypatch, cutoff):
    class LocalStream(_SseResp):
        def __iter__(self):
            yield _data({"choices": [{"delta": {"content": "Working"}}]})
            if cutoff == "idle":
                raise TimeoutError("The read operation timed out")
            yield from [b": keepalive\n"] * 10
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: LocalStream([]))
    monkeypatch.setattr("pmharness.drivers.codex_responses._arm_post_answer_idle_timeout", lambda *a: True)
    driver = _driver("http://127.0.0.1:8080/v1")
    driver.vendor = "llama.cpp"
    resp = driver.chat_stream([], on_delta=lambda _: None)
    perf = copy_stream_performance(resp.meta.get("stream_performance"))
    assert perf[f"local_{cutoff}_cutoff_count"] == 1


def test_go_recovery_uses_frozen_nonstream_wire_body(monkeypatch):
    from harness.request_snapshot import FrozenRequest
    driver = _driver("https://opencode.ai/zen/go/v1")
    calls = _go_exchange(monkeypatch, [_data({"choices": [{"delta": {"content": "Started"}}]})], {
        "choices": [{"message": {"content": "Started and finished"}, "finish_reason": "stop"}],
    })
    request = FrozenRequest.capture(driver.chat_stream, [{"role": "user", "content": "hi"}], {"tools": [], "system": "sys"})
    driver.model = "changed-after-freeze"
    messages, kwargs = request.materialize()
    resp = request.method(messages, on_delta=lambda _: None, **kwargs)
    assert len(calls) == 2
    assert calls[0]["stream"] is True
    assert "stream" not in calls[1]
    assert calls[1]["messages"] == calls[0]["messages"]
    assert calls[1]["model"] == calls[0]["model"] == "gpt-4o"
    assert resp.error is None
    receipt = request.wire_receipt()
    assert receipt["wire_status"] == "verified"
    assert receipt["recovery_wire"]["wire_status"] == "verified"
    assert receipt["recovery_wire"]["wire_attempts"] == 1


def test_frozen_stream_allows_only_length_continuation_body(monkeypatch):
    from harness.request_snapshot import FrozenRequest

    driver = _driver("https://opencode.ai/zen/go/v1")
    calls = []
    exchanges = [
        [
            _data({"choices": [{"delta": {"content": "Part one"}, "finish_reason": "length"}]}),
            b"data: [DONE]\n",
        ],
        [
            _data({"choices": [{"delta": {"content": " part two"}, "finish_reason": "stop"}]}),
            b"data: [DONE]\n",
        ],
    ]

    def urlopen(req, **kwargs):
        calls.append(json.loads(req.data))
        return _SseResp(exchanges[len(calls) - 1])

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    request = FrozenRequest.capture(
        driver.chat_stream,
        [{"role": "user", "content": "hi"}],
        {"tools": [], "system": "sys"},
    )
    driver.model = "changed-after-freeze"
    messages, kwargs = request.materialize()
    resp = request.method(messages, on_delta=lambda _: None, **kwargs)
    assert resp.error is None
    assert resp.text == "Part onepart two"
    assert len(calls) == 2
    assert calls[0]["model"] == calls[1]["model"] == "gpt-4o"
    assert calls[1]["messages"][:2] == calls[0]["messages"]
    assert calls[1]["messages"][-2]["role"] == "assistant"
    assert calls[1]["messages"][-1]["role"] == "user"
    receipt = request.wire_receipt()
    assert receipt["wire_status"] == "verified"
    assert receipt["continuation_wire"][0]["wire_status"] == "verified"


def test_stop_after_length_terminal_prevents_continuation_request(monkeypatch):
    cancel = threading.Event()
    calls = []

    class LengthStream(_SseResp):
        def __iter__(self):
            yield _data({"choices": [{"delta": {"content": "Part one"}, "finish_reason": "length"}]})
            cancel.set()
            yield b"data: [DONE]\n"

    def urlopen(req, **kwargs):
        calls.append(json.loads(req.data))
        return LengthStream([])

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    resp = _driver("https://opencode.ai/zen/go/v1").chat_stream(
        [], on_delta=lambda _: None, is_cancelled=cancel.is_set,
    )
    assert len(calls) == 1
    assert resp.text == "Part one"
    assert resp.meta["length_continues"] == 0


@pytest.mark.parametrize("finish", ["length", "content_filter"])
def test_timeout_recovery_preserves_explicit_retry_terminal(monkeypatch, finish):
    calls = []

    class TimeoutStream(_SseResp):
        def __iter__(self):
            yield _data({"choices": [{"delta": {"content": "Partial"}}]})
            raise TimeoutError("The read operation timed out")

    class RecoveryResponse(_SseResp):
        def read(self):
            return json.dumps({
                "choices": [{
                    "message": {"content": "Partial recovered"},
                    "finish_reason": finish,
                }],
            }).encode()

    def urlopen(req, **kwargs):
        calls.append(json.loads(req.data))
        if len(calls) == 1:
            return TimeoutStream([])
        if len(calls) == 2:
            return RecoveryResponse([])
        return _SseResp([
            _data({"choices": [{"delta": {"content": " done"}, "finish_reason": "stop"}]}),
            b"data: [DONE]\n",
        ])

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    resp = _driver("https://opencode.ai/zen/go/v1").chat_stream(
        [], on_delta=lambda _: None,
    )
    if finish == "length":
        assert len(calls) == 3
        assert resp.meta["stream_terminal"] == "stop"
        assert resp.text == "Partial recovereddone"
    else:
        assert len(calls) == 2
        assert resp.meta["stream_terminal"] == "content_filter"
        assert resp.meta["finish_reason"] == "content_filter"
        assert resp.text == "Partial recovered"


def test_length_continuation_sums_cost_and_cache_accounting():
    from pmharness.drivers.openai_compat import _length_attempt_meta

    first = _response(
        "Part one", stream_terminal="length", finish_reason="length",
        provider_cost_usd=0.1, cache_read_tokens=8,
    )
    second = _response(
        "Part two", stream_terminal="stop", finish_reason="stop",
        provider_cost_usd=0.2, cache_read_tokens=8,
    )
    meta = _length_attempt_meta([first, second], second)
    assert meta["provider_cost_usd"] == pytest.approx(0.3)
    assert meta["cache_read_tokens"] == 16


def test_explicit_continuation_failure_replaces_prior_length_terminal(monkeypatch):
    calls = []
    exchanges = [
        [_data({"choices": [{"delta": {"content": "Part"}, "finish_reason": "length"}]}), b"data: [DONE]\n"],
        [_data({"choices": [{"delta": {}, "finish_reason": "content_filter"}]}), b"data: [DONE]\n"],
    ]
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: calls.append(1) or _SseResp(exchanges[len(calls) - 1]))
    resp = _driver().chat_stream([], on_delta=lambda _: None)
    assert calls == [1, 1]
    assert resp.meta["finish_reason"] == "content_filter"
    assert resp.meta["stream_terminal"] == "content_filter"
    assert "content_filter" in resp.error


def test_frozen_continuation_nonstream_fallback_keeps_continuation_messages(monkeypatch):
    from harness.request_snapshot import FrozenRequest

    monkeypatch.setenv("HARNESS_PROMPT_CACHE", "1")
    driver = _driver("https://openrouter.ai/api/v1")
    driver.model = "anthropic/claude-sonnet-4"
    driver.enable_reasoning = True
    bodies = []

    class JsonResponse(_SseResp):
        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "Part two"}, "finish_reason": "stop"}],
            }).encode()

    def urlopen(req, **kwargs):
        bodies.append(json.loads(req.data))
        if len(bodies) == 1:
            return _SseResp([
                _data({"choices": [{"delta": {"content": "Part one"}, "finish_reason": "length"}]}),
                b"data: [DONE]\n",
            ])
        if len(bodies) == 2:
            raise urllib.error.HTTPError(
                req.full_url, 400, "bad", {}, io.BytesIO(b"Unknown parameter reasoning"),
            )
        return JsonResponse([])

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    request = FrozenRequest.capture(
        driver.chat_stream, [{"role": "user", "content": "hi"}],
        {"tools": [], "system": "sys"},
    )
    messages, kwargs = request.materialize()
    resp = request.method(messages, on_delta=lambda _: None, **kwargs)
    assert resp.error is None
    assert resp.text == "Part onePart two"
    assert len(bodies) == 3
    assert bodies[2]["messages"] == bodies[1]["messages"]
    assert bodies[2]["messages"] != bodies[0]["messages"]
    assert request.wire_receipt()["wire_status"] == "verified"


@pytest.mark.parametrize("failure", ["timeout", "terminal_less", "empty_tools"])
def test_length_continuation_recovery_keeps_continuation_messages(monkeypatch, failure):
    from harness.request_snapshot import FrozenRequest

    driver = _driver("https://opencode.ai/zen/go/v1")
    bodies = []

    class ContinuationResponse(_SseResp):
        def __iter__(self):
            finish = "tool_calls" if failure == "empty_tools" else None
            yield _data({"choices": [{
                "delta": {"content": "Part two"},
                "finish_reason": finish,
            }]})
            if failure == "timeout":
                raise TimeoutError("The read operation timed out")

    class RecoveryResponse(_SseResp):
        def read(self):
            message = {"content": "Part two complete"}
            finish = "stop"
            if failure == "empty_tools":
                message = {"content": "", "tool_calls": [{
                    "id": "call_recovered",
                    "type": "function",
                    "function": {"name": "run_parallel", "arguments": "{}"},
                }]}
                finish = "tool_calls"
            return json.dumps({
                "choices": [{"message": message, "finish_reason": finish}],
            }).encode()

    def urlopen(req, **kwargs):
        bodies.append(json.loads(req.data))
        if len(bodies) == 1:
            return _SseResp([
                _data({"choices": [{
                    "delta": {"content": "Part one"},
                    "finish_reason": "length",
                }]}),
                b"data: [DONE]\n",
            ])
        if len(bodies) == 2:
            return ContinuationResponse([])
        return RecoveryResponse([])

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    request = FrozenRequest.capture(
        driver.chat_stream, [{"role": "user", "content": "hi"}],
        {"tools": [{"type": "function", "function": {
            "name": "run_parallel", "parameters": {"type": "object"},
        }}]},
    )
    messages, kwargs = request.materialize()
    resp = request.method(messages, on_delta=lambda _: None, **kwargs)

    assert resp.error is None
    assert [len(body["messages"]) for body in bodies] == [1, 3, 3]
    if failure == "empty_tools":
        assert resp.meta["tool_calls"][0]["function"]["name"] == "run_parallel"
    assert request.wire_receipt()["wire_status"] == "verified"


def test_frozen_initial_reasoning_fallback_removes_only_reasoning(monkeypatch):
    from harness.request_snapshot import FrozenRequest

    driver = _driver("https://opencode.ai/zen/go/v1")
    driver.enable_reasoning = True
    bodies = []

    class JsonResponse(_SseResp):
        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "Finished"}, "finish_reason": "stop"}],
            }).encode()

    def urlopen(req, **kwargs):
        bodies.append(json.loads(req.data))
        if len(bodies) == 1:
            raise urllib.error.HTTPError(
                req.full_url, 400, "bad", {}, io.BytesIO(b"Unknown parameter reasoning"),
            )
        return JsonResponse([])

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    request = FrozenRequest.capture(
        driver.chat_stream, [{"role": "user", "content": "hi"}],
        {"tools": [], "system": "sys"},
    )
    messages, kwargs = request.materialize()
    resp = request.method(messages, on_delta=lambda _: None, **kwargs)
    assert resp.error is None
    assert len(bodies) == 2
    assert "reasoning" in bodies[0] and "reasoning" not in bodies[1]
    assert {key: value for key, value in bodies[0].items() if key not in {"reasoning", "stream"}} == {
        key: value for key, value in bodies[1].items() if key != "stream_options"
    }
    receipt = request.wire_receipt()
    assert receipt["wire_status"] == "verified"
    assert receipt["fallback_wire"][0]["wire_status"] == "verified"


def test_sync_reasoning_fallback_honors_stop_before_second_request(monkeypatch):
    cancel = threading.Event()
    driver = _driver("https://opencode.ai/zen/go/v1")
    driver.enable_reasoning = True
    calls = []

    def urlopen(req, **kwargs):
        calls.append(1)
        cancel.set()
        raise urllib.error.HTTPError(
            req.full_url, 400, "bad", {}, io.BytesIO(b"Unknown parameter reasoning"),
        )

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    resp = driver.chat([], is_cancelled=cancel.is_set)
    assert calls == [1]
    assert resp.error == "cancelled before provider fallback"


def test_sync_dispatch_stop_prevents_retry_request(monkeypatch):
    from harness.send_loop_phases import dispatch_sync_pilot_chat

    cancel = threading.Event()
    calls = []
    driver = _driver("https://opencode.ai/zen/go/v1")

    def urlopen(*args, **kwargs):
        calls.append(1)
        cancel.set()
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    session = SimpleNamespace(
        pilot=driver,
        _cancel=cancel,
        _messages_for_provider=lambda: [{"role": "user", "content": "hi"}],
    )
    resp = dispatch_sync_pilot_chat(session, [], "sys")
    assert calls == [1]
    assert resp.error


def test_native_executor_timeout_is_not_replayed():
    calls = []
    def stream(messages, **kwargs):
        return _response(error="TimeoutError('read timed out')")
    def chat(*a, **kw):
        calls.append(1)
        return _response(finish_reason="stop")
    session = _session(stream, chat)
    session.pilot.apply_host_mode = lambda **kw: "agent"
    q = queue.Queue()
    run_stream(session, q, [], "sys")
    assert calls == []


@pytest.mark.parametrize("failure", ["incomplete", "error", "raise"])
def test_stop_set_in_provider_request_settles_cancelled(monkeypatch, tmp_path, failure):
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession
    from test_post_swarm_synthesis import _SequencePilot
    session = ConversationalSession(HarnessConfig(driver="stub-oracle-v2", state_dir=str(tmp_path)))
    def stopped(**kwargs):
        session._cancel.set()
        if failure == "raise":
            raise RuntimeError("late transport failure")
        return _response("Partial", "late failure", stream_terminal=failure)
    pilot = _SequencePilot([stopped], streaming=True)
    session.pilot = pilot
    session._build_visible_tools_schema = lambda: []
    session._maybe_compact_history = lambda *a, **kw: iter(())
    session._submit_housekeeping = lambda *a, **kw: None
    session._turn_budget_exhausted = lambda: False
    events = list(session._send_locked_inner("hello"))
    assert len(pilot.calls) == 1
    assert not [e for e in events if e.kind == "error"]
    assert [e.data["stop_cause"] for e in events if e.kind == "assistant_done"] == ["cancelled"]


@pytest.mark.parametrize("failure", [False, True])
def test_sync_timeout_retry_budget_and_receipts(failure):
    from pmharness.drivers.retry import with_retry
    calls = []
    def call():
        calls.append(1)
        if len(calls) == 1 or failure:
            return _response(error="TimeoutError('The read operation timed out')")
        return _response(finish_reason="stop")
    resp = with_retry(call, sleep=lambda _: None)
    assert len(calls) == 2
    assert bool(resp.error) == failure
    perf = copy_stream_performance(resp.meta.get("stream_performance"))
    assert perf["recovery_attempt_count"] == 1
    assert perf["recovery_failure_count"] == int(failure)


def test_retry_stops_before_a_second_request_when_cancelled():
    from pmharness.drivers.retry import with_retry
    calls = []
    cancelled = [False]
    resp = with_retry(
        lambda: calls.append(1) or _response(error="TimeoutError('read timed out')"),
        sleep=lambda _: cancelled.__setitem__(0, True),
        is_cancelled=lambda: cancelled[0],
    )
    assert calls == [1]
    assert resp.error


def test_recovery_merges_provider_cost_and_cache_usage():
    from pmharness.drivers.retry import merge_recovery_response
    first = DriverResponse(
        text="a", tokens_in=10,
        meta={"provider_cost_usd": 0.1, "cache_read_tokens": 8},
    )
    retry = DriverResponse(
        text="ab", tokens_in=20,
        meta={"provider_cost_usd": 0.2, "cache_read_tokens": 16},
    )
    merged = merge_recovery_response(first, retry, recovered=True)
    assert merged.tokens_in == 30
    assert merged.meta["provider_cost_usd"] == pytest.approx(0.3)
    assert merged.meta["cache_read_tokens"] == 24


def test_sync_timeout_recovery_uses_frozen_model(monkeypatch):
    from harness.request_snapshot import FrozenRequest
    from harness.send_loop_phases import _recover_transport_timeout
    driver = _driver("https://opencode.ai/zen/go/v1")
    request = FrozenRequest.capture(
        driver.chat, [{"role": "user", "content": "hi"}],
        {"tools": [], "system": "sys"},
    )
    driver.model = "changed-after-freeze"
    bodies = []

    class Response(_SseResp):
        def read(self):
            return json.dumps({
                "choices": [{
                    "message": {"content": "ok"}, "finish_reason": "stop",
                }],
            }).encode()

    def urlopen(req, **kwargs):
        bodies.append(json.loads(req.data))
        return Response([])

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    recovered = _recover_transport_timeout(
        SimpleNamespace(pilot=driver, _cancel=threading.Event()),
        _response(error="TimeoutError('read timed out')"), request,
    )
    assert recovered.error is None
    assert bodies[0]["model"] == "gpt-4o"
    assert request.wire_receipt()["wire_status"] == "verified"
