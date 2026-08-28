"""Deterministic TTFT / provider-output timing for the interactive pilot stream."""

from __future__ import annotations

import queue
import threading
from types import SimpleNamespace

from harness.send_loop_phases import (
    _finish_and_attach_timing,
    dispatch_pilot_provider_call,
    dispatch_sync_pilot_chat,
    drain_stream_queue,
    meter_pilot_step,
    run_stream,
)
from harness.stream_performance import (
    BACKEND_READY_TOTAL_MS,
    FIRST_ANSWER_CALLBACK_MS,
    FIRST_CONTENT_CALLBACK_MS,
    FIRST_VISIBLE_ANSWER_MS,
    PROVIDER_CALL_TOTAL_MS,
    PROVIDER_OUTPUT_TPS,
    STREAM_PERFORMANCE_KEY,
    THROUGHPUT_BASIS,
    THROUGHPUT_BASIS_KEY,
    StreamTimingAccumulator,
    attach_stream_performance,
    call_timed_phase,
    classify_stream_event,
    make_stream_timing_accumulator,
    reset_provider_step_timing,
    reset_timing_before_step,
    timed_phase,
    yield_timed_phase,
)
from pmharness.drivers.base import DriverResponse


class FakeClock:
    """Monotonic clock in seconds, advanced by exact millisecond ticks."""

    def __init__(self, start: float = 1000.0) -> None:
        self._ms = int(round(start * 1000.0))

    def __call__(self) -> float:
        return self._ms / 1000.0

    def advance(self, seconds: float) -> None:
        self._ms += int(round(seconds * 1000.0))


def _stream_session(chat_stream):
    history = [{"role": "system"}, {"role": "user", "content": "hi"}]
    return SimpleNamespace(
        pilot=SimpleNamespace(
            chat_stream=chat_stream,
            supports_streaming=True,
        ),
        _history=history,
        _elide_stale_reads=lambda msgs: msgs,
        _messages_for_provider=lambda: history[1:],
    )


def _meter_session():
    meters = {}
    return SimpleNamespace(
        _tokens_used=0,
        _tokens_out=0,
        _turn_output_tokens=0,
        _tokens_in=0,
        _last_prompt_tokens=0,
        _tokens_cached=0,
        _tokens_cache_write=0,
        _tokens_cache_write_5m=0,
        _tokens_cache_write_1h=0,
        _plan_billing=False,
        _price_source="",
        _provider_cost_usd=0.0,
        _provider_billed_tokens_in=0,
        _provider_billed_tokens_out=0,
        _provider_billed_tokens_cached=0,
        _provider_billed_tokens_cache_write=0,
        _provider_billed_tokens_cache_write_5m=0,
        _provider_billed_tokens_cache_write_1h=0,
        config=SimpleNamespace(driver="openai/gpt-test"),
        _accumulate_session_meters=lambda **kw: meters.update(kw),
        meters=meters,
    )


def test_classify_stream_event_table():
    cases = (
        ("delta", "hello", "answer"),
        ("delta", "  hello  ", "answer"),
        ("delta", {"text": "hi", "channel": "answer"}, "answer"),
        ("delta", {"delta": "hi", "stream_id": "a"}, "answer"),
        ("reasoning", "think", "reasoning"),
        ("thinking", {"text": "think", "channel": "reasoning"}, "reasoning"),
        ("delta", {"text": "think", "channel": "reasoning"}, "reasoning"),
        ("delta", "", None),
        ("delta", "   ", None),
        ("delta", None, None),
        ("delta", {"text": ""}, None),
        ("delta", {"text": "ok", "channel": "progress"}, None),
        ("progress", "still working", None),
        ("tool", "read_file", None),
        ("tool_call", "read_file", None),
        ("delta", {"text": "ok", "channel": "tool"}, None),
        ("delta", {"text": "ok", "channel": "tool_call"}, None),
        ("tool_hint", "read_file", None),
        ("wait", "still working", None),
        ("item_done", {"stream_id": "a"}, None),
        ("delta", {"type": "usage", "usage": {"output_tokens": 3}}, None),
        ("delta", {"type": "header"}, None),
        ("delta", {"type": "keepalive"}, None),
        ("delta", {"event": "ping"}, None),
        ("delta", {"kind": "completion"}, None),
        ("usage", {"output_tokens": 4}, None),
        ("done", "ok", None),
    )
    for kind, payload, expected in cases:
        assert classify_stream_event(kind, payload) == expected, (kind, payload)


def test_empty_stream_omits_ttft_and_tps():
    clock = FakeClock()
    acc = StreamTimingAccumulator(clock=clock)
    clock.advance(0.012)
    acc.finish()
    snap = acc.snapshot(tokens_out=0)
    assert snap["content_delta_count"] == 0
    assert snap["provider_call_total_ms"] == 12.0
    assert BACKEND_READY_TOTAL_MS not in snap
    assert "request_to_first_content_callback_ms" not in snap
    assert "request_to_first_answer_callback_ms" not in snap
    assert "decode_window_ms" not in snap
    assert "max_inter_delta_ms" not in snap
    assert "provider_output_tokens_per_second" not in snap


def test_one_answer_delta_has_ttft_but_no_tps():
    clock = FakeClock()
    acc = StreamTimingAccumulator(clock=clock)
    clock.advance(0.080)
    acc.note("delta", "Hello")
    clock.advance(0.020)
    acc.finish()
    snap = acc.snapshot(tokens_out=9)
    assert snap["content_delta_count"] == 1
    assert snap["request_to_first_content_callback_ms"] == 80.0
    assert snap["request_to_first_answer_callback_ms"] == 80.0
    assert snap["provider_call_total_ms"] == 100.0
    assert "decode_window_ms" not in snap
    assert "max_inter_delta_ms" not in snap
    assert "provider_output_tokens_per_second" not in snap


def test_multiple_deltas_exact_max_gap_and_tps():
    clock = FakeClock()
    acc = StreamTimingAccumulator(clock=clock)
    clock.advance(0.100)
    acc.note("delta", "A")
    clock.advance(0.100)
    acc.note("delta", "B")
    clock.advance(0.300)
    acc.note("delta", "C")
    clock.advance(0.100)
    acc.finish()
    snap = acc.snapshot(tokens_out=12)
    assert snap["content_delta_count"] == 3
    assert snap["request_to_first_content_callback_ms"] == 100.0
    assert snap["request_to_first_answer_callback_ms"] == 100.0
    assert snap["decode_window_ms"] == 400.0
    assert snap["max_inter_delta_ms"] == 300.0
    assert snap["provider_call_total_ms"] == 600.0
    # 12 provider tokens / 0.400s first-content→last-content, not / 0.600s total.
    assert snap["provider_output_tokens_per_second"] == 30.0
    assert snap[THROUGHPUT_BASIS_KEY] == THROUGHPUT_BASIS


def test_reasoning_before_answer_distinct_ttfts():
    clock = FakeClock()
    acc = StreamTimingAccumulator(clock=clock)
    clock.advance(0.050)
    acc.note("reasoning", "thinking")
    clock.advance(0.150)
    acc.note("delta", "visible")
    acc.finish()
    snap = acc.snapshot(tokens_out=4)
    assert snap["request_to_first_content_callback_ms"] == 50.0
    assert snap["request_to_first_answer_callback_ms"] == 200.0
    assert snap["content_delta_count"] == 2
    assert snap["decode_window_ms"] == 150.0
    assert snap["provider_output_tokens_per_second"] == 26.666667
    assert snap[THROUGHPUT_BASIS_KEY] == THROUGHPUT_BASIS


def test_empty_tool_header_usage_keepalive_excluded():
    clock = FakeClock()
    acc = StreamTimingAccumulator(clock=clock)
    clock.advance(0.010)
    acc.note("delta", "")
    acc.note("delta", "   ")
    acc.note("tool_hint", "read_file")
    acc.note("wait", "still working")
    acc.note("item_done", {"stream_id": "a"})
    acc.note("delta", {"type": "header"})
    acc.note("delta", {"type": "usage", "usage": {"output_tokens": 2}})
    acc.note("delta", {"type": "keepalive"})
    acc.note("delta", {"text": "progress", "channel": "progress"})
    clock.advance(0.040)
    acc.note("delta", "token")
    acc.finish()
    snap = acc.snapshot(tokens_out=8)
    assert snap["content_delta_count"] == 1
    assert snap["request_to_first_content_callback_ms"] == 50.0
    assert snap["request_to_first_answer_callback_ms"] == 50.0
    assert "provider_output_tokens_per_second" not in snap


def test_tps_omitted_without_tokens_or_window():
    clock = FakeClock()
    acc = StreamTimingAccumulator(clock=clock)
    acc.note("delta", "A")
    clock.advance(0.200)
    acc.note("delta", "B")
    acc.finish()
    assert "provider_output_tokens_per_second" not in acc.snapshot(tokens_out=0)
    assert "provider_output_tokens_per_second" not in acc.snapshot(tokens_out=None)


def test_same_instant_content_omits_decode_window_and_tps():
    """Two content callbacks at one fake-clock tick: no window, no TPS."""
    clock = FakeClock()
    acc = StreamTimingAccumulator(clock=clock)
    clock.advance(0.080)
    acc.note("delta", "A")
    acc.note("delta", "B")
    acc.finish()
    snap = acc.snapshot(tokens_out=9)
    assert snap["content_delta_count"] == 2
    assert snap["request_to_first_content_callback_ms"] == 80.0
    assert snap["request_to_first_answer_callback_ms"] == 80.0
    assert "decode_window_ms" not in snap
    assert "provider_output_tokens_per_second" not in snap


def test_attach_preserves_existing_meta_and_latency():
    resp = DriverResponse(
        text="ok",
        tokens_out=3,
        latency_ms=42.5,
        meta={"cache_read_tokens": 7, "custom": "keep"},
    )
    attach_stream_performance(resp, {"content_delta_count": 1, "provider_call_total_ms": 10.0})
    assert resp.latency_ms == 42.5
    assert resp.meta["cache_read_tokens"] == 7
    assert resp.meta["custom"] == "keep"
    assert resp.meta[STREAM_PERFORMANCE_KEY]["content_delta_count"] == 1


def test_attach_does_not_overwrite_existing_stream_performance():
    resp = SimpleNamespace(
        latency_ms=9.0,
        meta={STREAM_PERFORMANCE_KEY: {"content_delta_count": 99}},
    )
    attach_stream_performance(resp, {"content_delta_count": 1})
    assert resp.meta[STREAM_PERFORMANCE_KEY]["content_delta_count"] == 99
    assert resp.latency_ms == 9.0


def test_run_stream_queue_shape_unchanged_and_attaches_performance():
    q: queue.Queue = queue.Queue()
    clock = FakeClock()
    resp = DriverResponse(
        text="ok",
        tokens_out=6,
        latency_ms=17.0,
        meta={"foo": "bar"},
    )

    def chat_stream(messages, **kwargs):
        kwargs["on_reasoning_delta"]("think")
        kwargs["on_delta"]("")
        kwargs["on_tool_hint"]("read_file")
        clock.advance(0.200)
        kwargs["on_delta"]("hello")
        return resp

    run_stream(_stream_session(chat_stream), q, [{"name": "t"}], "sys", clock=clock)
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    kinds = [kind for kind, _val in events]
    assert kinds == ["reasoning", "delta", "tool_hint", "delta", "done"]
    assert events[1][1] == ""
    assert events[2][1] == "read_file"
    assert events[3][1] == "hello"
    done = events[-1][1]
    assert done is resp
    assert done.latency_ms == 17.0
    assert done.meta["foo"] == "bar"
    perf = done.meta[STREAM_PERFORMANCE_KEY]
    assert perf["content_delta_count"] == 2
    assert perf["request_to_first_content_callback_ms"] == 0.0
    assert perf["request_to_first_answer_callback_ms"] == 200.0
    assert perf["decode_window_ms"] == 200.0
    assert perf["provider_output_tokens_per_second"] == 30.0
    assert perf[THROUGHPUT_BASIS_KEY] == THROUGHPUT_BASIS


def test_run_stream_empty_stream_still_puts_done():
    q: queue.Queue = queue.Queue()
    clock = FakeClock()
    resp = DriverResponse(text="", tokens_out=0, latency_ms=5.0, meta={})

    def chat_stream(messages, **kwargs):
        clock.advance(0.025)
        return resp

    run_stream(_stream_session(chat_stream), q, [], "sys", clock=clock)
    kind, val = q.get_nowait()
    assert kind == "done"
    perf = val.meta[STREAM_PERFORMANCE_KEY]
    assert perf["content_delta_count"] == 0
    assert perf["provider_call_total_ms"] == 25.0
    assert BACKEND_READY_TOTAL_MS not in perf
    assert FIRST_VISIBLE_ANSWER_MS not in perf
    assert "provider_output_tokens_per_second" not in perf


def test_run_stream_timing_failure_does_not_break_chat():
    q: queue.Queue = queue.Queue()
    resp = DriverResponse(text="ok", latency_ms=1.0)

    def boom_clock():
        raise RuntimeError("clock broke")

    def chat_stream(messages, **kwargs):
        kwargs["on_delta"]("hi")
        return resp

    run_stream(_stream_session(chat_stream), q, [], "sys", clock=boom_clock)
    kinds = []
    while not q.empty():
        kinds.append(q.get_nowait()[0])
    assert kinds == ["delta", "done"]


def test_meter_pilot_step_preserves_stream_performance_and_accounting(monkeypatch):
    session = _meter_session()
    perf = {
        "content_delta_count": 3,
        "request_to_first_content_callback_ms": 40.0,
        "request_to_first_answer_callback_ms": 80.0,
        "decode_window_ms": 200.0,
        "provider_output_tokens_per_second": 50.0,
    }
    resp = SimpleNamespace(
        tokens_out=10,
        tokens_in=100,
        latency_ms=33.0,
        meta={
            "cache_read_tokens": 40,
            "cache_write_tokens": 5,
            "provider_cost_usd": 0.0123,
            STREAM_PERFORMANCE_KEY: perf,
        },
    )
    meter_pilot_step(session, resp, prompt="x" * 400)
    assert resp.latency_ms == 33.0
    assert resp.meta[STREAM_PERFORMANCE_KEY] == perf
    assert resp.meta["tokens_in_basis"] == "provider"
    assert session._tokens_out == 10
    assert session._tokens_in == 100
    assert session._tokens_used == 110
    assert session._tokens_cached == 40
    assert session._provider_cost_usd == 0.0123
    assert session.meters["estimated_cost_usd"] == 0.0123
    assert session.meters["output_tokens"] == 10
    assert not hasattr(session, STREAM_PERFORMANCE_KEY)
    assert not hasattr(session, "_last_stream_performance")


def test_meter_pilot_step_malformed_tokens_out_is_honest():
    session = _meter_session()
    resp = SimpleNamespace(
        tokens_out="nope",
        tokens_in=40,
        latency_ms=5.0,
        meta={STREAM_PERFORMANCE_KEY: {"content_delta_count": 1}},
    )
    meter_pilot_step(session, resp, prompt="x" * 80)
    assert session._tokens_out == 0
    assert session._turn_output_tokens == 0
    assert session._tokens_in == 40
    assert session._tokens_used == 40
    assert resp.meta[STREAM_PERFORMANCE_KEY]["content_delta_count"] == 1
    assert resp.latency_ms == 5.0


def test_phase_durations_and_pre_request_total():
    clock = FakeClock()
    acc = StreamTimingAccumulator(clock=clock)
    acc.begin_phase("image_prep")
    clock.advance(0.050)
    acc.end_phase("image_prep")
    acc.begin_phase("prompt_tools")
    clock.advance(0.030)
    acc.end_phase("prompt_tools")
    clock.advance(0.020)
    acc.mark_request_start()
    acc.finish()
    snap = acc.snapshot()
    assert snap["image_prep_ms"] == 50.0
    assert snap["prompt_tools_ms"] == 30.0
    assert snap["pre_request_total_ms"] == 100.0
    assert "task_profile_ms" not in snap
    assert "step_wiki_ms" not in snap
    assert "request_to_first_content_callback_ms" not in snap


def test_skipped_and_unknown_phases_omitted():
    clock = FakeClock()
    acc = StreamTimingAccumulator(clock=clock)
    acc.begin_phase("not_a_real_phase")
    clock.advance(0.040)
    acc.end_phase("not_a_real_phase")
    acc.begin_phase("step_codegraph")
    clock.advance(0.010)
    # never closed
    acc.mark_request_start()
    snap = acc.snapshot()
    assert "not_a_real_phase_ms" not in snap
    assert "step_codegraph_ms" not in snap
    assert snap["pre_request_total_ms"] == 50.0


def test_ttft_stays_request_relative_after_long_pre_request():
    clock = FakeClock()
    acc = StreamTimingAccumulator(clock=clock)
    acc.begin_phase("advisory_compaction")
    clock.advance(0.400)
    acc.end_phase("advisory_compaction")
    acc.mark_request_start()
    clock.advance(0.080)
    acc.note("delta", "Hello")
    clock.advance(0.020)
    acc.finish()
    snap = acc.snapshot(tokens_out=9)
    assert snap["advisory_compaction_ms"] == 400.0
    assert snap["pre_request_total_ms"] == 400.0
    assert snap["request_to_first_content_callback_ms"] == 80.0
    assert snap["request_to_first_answer_callback_ms"] == 80.0
    assert snap["provider_call_total_ms"] == 100.0
    assert "provider_output_tokens_per_second" not in snap


def test_attach_fills_missing_keys_without_overwrite():
    resp = SimpleNamespace(
        latency_ms=9.0,
        meta={
            STREAM_PERFORMANCE_KEY: {
                "image_prep_ms": 12.0,
                "content_delta_count": 0,
            },
            "custom": "keep",
        },
    )
    attach_stream_performance(resp, {
        "content_delta_count": 3,
        "request_to_first_content_callback_ms": 40.0,
        "image_prep_ms": 99.0,
        "provider_call_total_ms": 25.0,
    })
    perf = resp.meta[STREAM_PERFORMANCE_KEY]
    assert perf["image_prep_ms"] == 12.0
    assert perf["content_delta_count"] == 0
    assert perf["request_to_first_content_callback_ms"] == 40.0
    assert perf["provider_call_total_ms"] == 25.0
    assert resp.latency_ms == 9.0
    assert resp.meta["custom"] == "keep"


def test_sync_chat_attaches_pre_request_without_ttft():
    clock = FakeClock()
    acc = StreamTimingAccumulator(clock=clock)
    acc.begin_phase("prompt_tools")
    clock.advance(0.040)
    acc.end_phase("prompt_tools")
    resp = DriverResponse(text="ok", tokens_out=4, latency_ms=25.0, meta={"foo": "bar"})

    def chat(messages, **kwargs):
        clock.advance(0.025)
        return resp

    history = [{"role": "system"}, {"role": "user", "content": "hi"}]
    session = SimpleNamespace(
        pilot=SimpleNamespace(chat=chat),
        _history=history,
        _elide_stale_reads=lambda msgs: msgs,
        _messages_for_provider=lambda: history[1:],
        harness_session_id=None,
    )
    out = dispatch_sync_pilot_chat(session, [], "sys", accumulator=acc)
    assert out is resp
    assert resp.latency_ms == 25.0
    assert resp.meta["foo"] == "bar"
    perf = resp.meta[STREAM_PERFORMANCE_KEY]
    assert perf["prompt_tools_ms"] == 40.0
    assert perf["pre_request_total_ms"] == 40.0
    assert perf["provider_call_total_ms"] == 25.0
    assert BACKEND_READY_TOTAL_MS not in perf
    assert FIRST_VISIBLE_ANSWER_MS not in perf
    assert perf["content_delta_count"] == 0
    assert "request_to_first_content_callback_ms" not in perf
    assert "request_to_first_answer_callback_ms" not in perf
    assert "decode_window_ms" not in perf
    assert "provider_output_tokens_per_second" not in perf


def test_empty_stream_omits_ttft_even_with_pre_request():
    clock = FakeClock()
    acc = StreamTimingAccumulator(clock=clock)
    acc.begin_phase("user_append")
    clock.advance(0.015)
    acc.end_phase("user_append")
    q: queue.Queue = queue.Queue()
    resp = DriverResponse(text="", tokens_out=0, latency_ms=5.0, meta={})

    def chat_stream(messages, **kwargs):
        clock.advance(0.025)
        return resp

    run_stream(
        _stream_session(chat_stream), q, [], "sys",
        clock=clock, accumulator=acc,
    )
    kind, val = q.get_nowait()
    assert kind == "done"
    perf = val.meta[STREAM_PERFORMANCE_KEY]
    assert perf["user_append_ms"] == 15.0
    assert perf["pre_request_total_ms"] == 15.0
    assert perf["provider_call_total_ms"] == 25.0
    assert perf["content_delta_count"] == 0
    assert "request_to_first_content_callback_ms" not in perf
    assert "provider_output_tokens_per_second" not in perf


def test_timed_phase_helpers_survive_boom_clock():
    def boom_clock():
        raise RuntimeError("clock broke")

    acc = make_stream_timing_accumulator(clock=boom_clock)
    assert acc is None

    def gen():
        yield "vision"
        return ("ok", [])

    def runner():
        result = yield from yield_timed_phase(None, "image_prep", gen())
        assert result == ("ok", [])

    assert list(runner()) == ["vision"]
    assert call_timed_phase(None, "prompt_tools", lambda: 7) == 7
    with timed_phase(None, "step_wiki"):
        seen = True
    assert seen


def test_yield_timed_phase_preserves_events_and_return():
    clock = FakeClock()
    acc = StreamTimingAccumulator(clock=clock)

    def gen():
        yield {"kind": "task_profile", "profile": "STANDARD"}
        clock.advance(0.012)
        return "classified"

    def runner():
        result = yield from yield_timed_phase(acc, "task_profile", gen())
        assert result == "classified"

    assert list(runner()) == [{"kind": "task_profile", "profile": "STANDARD"}]
    snap = acc.snapshot()
    assert snap["task_profile_ms"] == 12.0


def test_reset_drops_turn_once_phases_and_rebases_origin():
    clock = FakeClock()
    acc = StreamTimingAccumulator(clock=clock)
    acc.begin_phase("image_prep")
    clock.advance(0.100)
    acc.end_phase("image_prep")
    acc.mark_request_start()
    clock.advance(0.050)
    acc.note("delta", "a")
    acc.finish()
    first = acc.snapshot(tokens_out=1)
    assert first["image_prep_ms"] == 100.0
    assert first["request_to_first_content_callback_ms"] == 50.0

    acc.reset_for_next_provider_call()
    clock.advance(0.020)
    acc.begin_phase("step_codegraph")
    clock.advance(0.030)
    acc.end_phase("step_codegraph")
    acc.mark_request_start()
    second = acc.snapshot()
    assert "image_prep_ms" not in second
    assert second["step_codegraph_ms"] == 30.0
    assert second["pre_request_total_ms"] == 50.0
    assert second["content_delta_count"] == 0
    assert "request_to_first_content_callback_ms" not in second


def test_run_stream_shared_accumulator_keeps_pre_request_and_request_relative_ttft():
    clock = FakeClock()
    acc = StreamTimingAccumulator(clock=clock)
    acc.begin_phase("image_prep")
    clock.advance(0.050)
    acc.end_phase("image_prep")
    acc.begin_phase("thread_start")
    clock.advance(0.004)
    q: queue.Queue = queue.Queue()
    resp = DriverResponse(text="ok", tokens_out=6, latency_ms=17.0, meta={})

    def chat_stream(messages, **kwargs):
        clock.advance(0.080)
        kwargs["on_delta"]("hello")
        return resp

    run_stream(
        _stream_session(chat_stream), q, [], "sys",
        clock=clock, accumulator=acc,
    )
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    assert [kind for kind, _val in events] == ["delta", "done"]
    perf = events[-1][1].meta[STREAM_PERFORMANCE_KEY]
    assert perf["image_prep_ms"] == 50.0
    assert perf["thread_start_ms"] == 4.0
    assert perf["pre_request_total_ms"] == 54.0
    assert perf["request_to_first_content_callback_ms"] == 80.0
    assert perf["request_to_first_answer_callback_ms"] == 80.0
    assert events[-1][1].latency_ms == 17.0


def test_dispatch_complete_attaches_without_ttft():
    clock = FakeClock()
    acc = StreamTimingAccumulator(clock=clock)
    acc.begin_phase("prompt_tools")
    clock.advance(0.018)
    acc.end_phase("prompt_tools")
    resp = DriverResponse(text="done", tokens_out=2, latency_ms=11.0, meta={})

    def complete(prompt, **kwargs):
        clock.advance(0.011)
        return resp

    session = SimpleNamespace(
        pilot=SimpleNamespace(complete=complete),
        config=SimpleNamespace(no_delegation=False),
        harness_session_id=None,
    )

    def run():
        return (yield from dispatch_pilot_provider_call(
            session,
            plan=False,
            sys_prompt="sys",
            prompt="ping",
            synthesis_nudge_active=False,
            accumulator=acc,
        ))

    streamed, out = _exhaust_dispatch(run())
    assert streamed == ""
    assert out is resp
    perf = resp.meta[STREAM_PERFORMANCE_KEY]
    assert perf["prompt_tools_ms"] == 18.0
    assert perf["pre_request_total_ms"] == 18.0
    assert perf["provider_call_total_ms"] == 11.0
    assert BACKEND_READY_TOTAL_MS not in perf
    assert FIRST_VISIBLE_ANSWER_MS not in perf
    assert "request_to_first_content_callback_ms" not in perf
    assert "provider_output_tokens_per_second" not in perf
    assert resp.latency_ms == 11.0


def _exhaust_dispatch(gen):
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value


def test_yield_timed_phase_excludes_consumer_suspension():
    """Fake-clock time while the consumer is suspended must not enter phase_ms."""
    clock = FakeClock()
    acc = StreamTimingAccumulator(clock=clock)

    def gen():
        clock.advance(0.010)
        yield {"kind": "task_profile", "profile": "STANDARD"}
        clock.advance(0.005)
        return "classified"

    def runner():
        result = yield from yield_timed_phase(acc, "task_profile", gen())
        assert result == "classified"

    it = runner()
    ev = next(it)
    assert ev["profile"] == "STANDARD"
    clock.advance(0.200)
    try:
        while True:
            next(it)
    except StopIteration:
        pass
    snap = acc.snapshot()
    assert snap["task_profile_ms"] == 15.0
    assert "request_to_first_content_callback_ms" not in snap


def test_yield_timed_phase_image_prep_excludes_yielded_event_wait():
    clock = FakeClock()
    acc = StreamTimingAccumulator(clock=clock)

    def gen():
        clock.advance(0.008)
        yield {"kind": "vision", "status": "native"}
        clock.advance(0.004)
        return ("ok", [])

    def runner():
        result = yield from yield_timed_phase(acc, "image_prep", gen())
        assert result == ("ok", [])

    it = runner()
    next(it)
    clock.advance(0.500)
    try:
        while True:
            next(it)
    except StopIteration:
        pass
    assert acc.snapshot()["image_prep_ms"] == 12.0


def test_malformed_tokens_out_does_not_drop_receipt_or_raise():
    clock = FakeClock()
    acc = StreamTimingAccumulator(clock=clock)
    clock.advance(0.080)
    acc.note("delta", "Hello")
    clock.advance(0.020)
    acc.finish()
    for bad in ("nope", True, object(), [], {}):
        snap = acc.snapshot(tokens_out=bad)
        assert snap["content_delta_count"] == 1
        assert snap[FIRST_CONTENT_CALLBACK_MS] == 80.0
        assert snap[FIRST_ANSWER_CALLBACK_MS] == 80.0
        assert PROVIDER_OUTPUT_TPS not in snap
        assert THROUGHPUT_BASIS_KEY not in snap

    resp = DriverResponse(text="ok", tokens_out="nope", latency_ms=5.0, meta={"foo": "bar"})
    _finish_and_attach_timing(acc, resp)
    assert resp.latency_ms == 5.0
    assert resp.meta["foo"] == "bar"
    perf = resp.meta[STREAM_PERFORMANCE_KEY]
    assert perf[FIRST_CONTENT_CALLBACK_MS] == 80.0
    assert PROVIDER_OUTPUT_TPS not in perf


def test_run_stream_malformed_tokens_out_still_attaches():
    q: queue.Queue = queue.Queue()
    clock = FakeClock()
    resp = DriverResponse(text="ok", tokens_out="bad", latency_ms=9.0, meta={})

    def chat_stream(messages, **kwargs):
        kwargs["on_delta"]("hello")
        return resp

    run_stream(_stream_session(chat_stream), q, [], "sys", clock=clock)
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    assert [kind for kind, _val in events] == ["delta", "done"]
    perf = events[-1][1].meta[STREAM_PERFORMANCE_KEY]
    assert perf["content_delta_count"] == 1
    assert FIRST_CONTENT_CALLBACK_MS in perf
    assert PROVIDER_OUTPUT_TPS not in perf


def test_visible_answer_is_say_extractor_not_raw_callback():
    clock = FakeClock()
    acc = StreamTimingAccumulator(clock=clock)
    acc.mark_request_start()
    acc.note("delta", {"text": '{"say": "', "stream_id": "a1", "channel": "answer"})
    clock.advance(0.050)
    acc.note("delta", {"text": 'Hi"}', "stream_id": "a1", "channel": "answer"})
    q: queue.Queue = queue.Queue()
    resp = DriverResponse(text="ok", tokens_out=4, latency_ms=8.0, meta={})
    q.put(("delta", {"text": '{"say": "', "stream_id": "a1", "channel": "answer"}))
    q.put(("delta", {"text": 'Hi"}', "stream_id": "a1", "channel": "answer"}))
    q.put(("done", resp))

    events = []
    gen = drain_stream_queue(q, accumulator=acc)
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        streamed, got = stop.value

    deltas = [e for e in events if e.kind == "message_delta"]
    assert [d.data["text"] for d in deltas] == ["Hi"]
    assert streamed == "Hi"
    acc.finish()
    attach_stream_performance(got, acc.snapshot(tokens_out=4))
    perf = got.meta[STREAM_PERFORMANCE_KEY]
    assert perf[FIRST_CONTENT_CALLBACK_MS] == 0.0
    assert perf[FIRST_ANSWER_CALLBACK_MS] == 0.0
    assert perf[FIRST_VISIBLE_ANSWER_MS] == 50.0
    assert got.latency_ms == 8.0


def test_reset_timing_before_step_preserves_then_drops_turn_once():
    clock = FakeClock()
    acc = StreamTimingAccumulator(clock=clock)
    acc.begin_phase("image_prep")
    clock.advance(0.100)
    acc.end_phase("image_prep")
    acc.begin_phase("user_append")
    clock.advance(0.020)
    acc.end_phase("user_append")
    reset_timing_before_step(acc, 0)
    first = acc.snapshot()
    assert first["image_prep_ms"] == 100.0
    assert first["user_append_ms"] == 20.0

    reset_timing_before_step(acc, 1)
    second = acc.snapshot()
    assert "image_prep_ms" not in second
    assert "user_append_ms" not in second
    assert second["content_delta_count"] == 0


def test_reset_timing_before_step_past_zero_drops_prior_receipt():
    """Later tool-loop steps still clear prior phases and stream marks."""
    clock = FakeClock()
    acc = StreamTimingAccumulator(clock=clock)
    acc.begin_phase("prompt_tools")
    clock.advance(0.015)
    acc.end_phase("prompt_tools")
    acc.mark_request_start()
    clock.advance(0.040)
    acc.note("delta", "partial")
    acc.finish()
    prior = acc.snapshot(tokens_out=2)
    assert prior["prompt_tools_ms"] == 15.0
    assert prior[FIRST_CONTENT_CALLBACK_MS] == 40.0

    reset_timing_before_step(acc, 1)
    acc.begin_phase("advisory_compaction")
    clock.advance(0.025)
    acc.end_phase("advisory_compaction")
    retry = acc.snapshot()
    assert "prompt_tools_ms" not in retry
    assert FIRST_CONTENT_CALLBACK_MS not in retry
    assert retry["advisory_compaction_ms"] == 25.0


def test_reset_provider_step_timing_keeps_closed_phases_clears_attempt_marks():
    """Step-0 overflow retry keeps reusable phases; drops per-attempt clocks."""
    clock = FakeClock()
    acc = StreamTimingAccumulator(clock=clock)
    acc.begin_phase("image_prep")
    clock.advance(0.100)
    acc.end_phase("image_prep")
    acc.begin_phase("task_profile")
    clock.advance(0.020)
    acc.end_phase("task_profile")
    acc.begin_phase("user_append")
    clock.advance(0.015)
    acc.end_phase("user_append")
    acc.begin_phase("auto_codegraph")
    clock.advance(0.010)
    acc.end_phase("auto_codegraph")
    acc.begin_phase("advisory_compaction")
    clock.advance(0.008)
    acc.end_phase("advisory_compaction")
    acc.begin_phase("step_codegraph")
    clock.advance(0.006)
    acc.end_phase("step_codegraph")
    acc.begin_phase("step_wiki")
    clock.advance(0.004)
    acc.end_phase("step_wiki")
    acc.begin_phase("prompt_tools")
    clock.advance(0.012)
    acc.end_phase("prompt_tools")
    acc.begin_phase("thread_start")
    clock.advance(0.003)
    acc.end_phase("thread_start")
    acc.mark_request_start()
    clock.advance(0.040)
    acc.note("delta", "partial")
    acc.mark_first_visible_answer()
    acc.finish()
    acc.mark_backend_ready()
    prior = acc.snapshot(tokens_out=2)
    assert prior["image_prep_ms"] == 100.0
    assert prior["prompt_tools_ms"] == 12.0
    assert prior["thread_start_ms"] == 3.0
    assert prior["pre_request_total_ms"] == 178.0
    assert prior[FIRST_CONTENT_CALLBACK_MS] == 40.0
    assert prior[FIRST_VISIBLE_ANSWER_MS] == 40.0
    assert prior[PROVIDER_CALL_TOTAL_MS] == 40.0
    assert prior[BACKEND_READY_TOTAL_MS] == 40.0

    reset_provider_step_timing(acc)
    cleared = acc.snapshot()
    assert cleared["image_prep_ms"] == 100.0
    assert cleared["task_profile_ms"] == 20.0
    assert cleared["user_append_ms"] == 15.0
    assert cleared["auto_codegraph_ms"] == 10.0
    assert cleared["advisory_compaction_ms"] == 8.0
    assert cleared["step_codegraph_ms"] == 6.0
    assert cleared["step_wiki_ms"] == 4.0
    assert "prompt_tools_ms" not in cleared
    assert "thread_start_ms" not in cleared
    assert "pre_request_total_ms" not in cleared
    assert FIRST_CONTENT_CALLBACK_MS not in cleared
    assert FIRST_ANSWER_CALLBACK_MS not in cleared
    assert FIRST_VISIBLE_ANSWER_MS not in cleared
    assert PROVIDER_CALL_TOTAL_MS not in cleared
    assert BACKEND_READY_TOTAL_MS not in cleared
    assert "decode_window_ms" not in cleared
    assert "max_inter_delta_ms" not in cleared
    assert cleared["content_delta_count"] == 0

    acc.begin_phase("advisory_compaction")
    clock.advance(0.025)
    acc.end_phase("advisory_compaction")
    acc.begin_phase("prompt_tools")
    clock.advance(0.009)
    acc.end_phase("prompt_tools")
    acc.begin_phase("thread_start")
    clock.advance(0.002)
    acc.end_phase("thread_start")
    acc.mark_request_start()
    retry = acc.snapshot()
    assert retry["image_prep_ms"] == 100.0
    assert retry["task_profile_ms"] == 20.0
    assert retry["user_append_ms"] == 15.0
    assert retry["auto_codegraph_ms"] == 10.0
    assert retry["step_codegraph_ms"] == 6.0
    assert retry["step_wiki_ms"] == 4.0
    assert retry["advisory_compaction_ms"] == 33.0
    assert retry["prompt_tools_ms"] == 9.0
    assert retry["thread_start_ms"] == 2.0
    assert retry["pre_request_total_ms"] == 36.0
    assert FIRST_CONTENT_CALLBACK_MS not in retry
    assert FIRST_VISIBLE_ANSWER_MS not in retry
    assert PROVIDER_CALL_TOTAL_MS not in retry
    assert BACKEND_READY_TOTAL_MS not in retry


def test_reset_provider_attempt_marks_drops_open_per_attempt_phase():
    """Open prompt_tools / thread_start must not leak across the overflow reset."""
    clock = FakeClock()
    acc = StreamTimingAccumulator(clock=clock)
    acc.begin_phase("user_append")
    clock.advance(0.015)
    acc.end_phase("user_append")
    acc.begin_phase("prompt_tools")
    clock.advance(0.010)
    acc.begin_phase("thread_start")
    clock.advance(0.004)
    acc.reset_provider_attempt_marks()
    clock.advance(0.050)
    acc.end_phase("prompt_tools")
    acc.end_phase("thread_start")
    acc.begin_phase("prompt_tools")
    clock.advance(0.007)
    acc.end_phase("prompt_tools")
    acc.mark_request_start()
    snap = acc.snapshot()
    assert snap["user_append_ms"] == 15.0
    assert snap["prompt_tools_ms"] == 7.0
    assert "thread_start_ms" not in snap
    assert snap["pre_request_total_ms"] == 57.0


def test_reset_provider_step_timing_never_raises():
    reset_provider_step_timing(None)
    reset_provider_step_timing(object())

    class Boom:
        def reset_provider_attempt_marks(self):
            raise RuntimeError("nope")

    reset_provider_step_timing(Boom())


def test_threaded_dispatch_visible_mark_and_per_identity_first_frame():
    """Send-thread begin, stream-thread dispatch, callback, drain, attach."""
    clock = FakeClock()
    acc = StreamTimingAccumulator(clock=clock)
    acc.begin_phase("image_prep")
    clock.advance(0.040)
    acc.end_phase("image_prep")

    resp = DriverResponse(text="ok", tokens_out=8, latency_ms=20.0, meta={"keep": 1})
    stream_thread_name = {"name": ""}

    def chat_stream(messages, **kwargs):
        stream_thread_name["name"] = threading.current_thread().name
        kwargs["on_delta"]({"text": "Hi", "stream_id": "a1", "channel": "answer"})
        clock.advance(0.050)
        kwargs["on_delta"]({"text": " there", "stream_id": "a1", "channel": "answer"})
        kwargs["on_tool_hint"]("read_file")
        kwargs["on_delta"]({"text": "Next", "stream_id": "a2", "channel": "answer"})
        return resp

    history = [{"role": "system"}, {"role": "user", "content": "hi"}]
    session = SimpleNamespace(
        pilot=SimpleNamespace(
            chat=lambda *a, **k: resp,
            chat_stream=chat_stream,
            supports_streaming=True,
        ),
        config=SimpleNamespace(no_delegation=False),
        _history=history,
        _elide_stale_reads=lambda msgs: msgs,
        _messages_for_provider=lambda: history[1:],
        harness_session_id=None,
        _build_visible_tools_schema=lambda: [],
    )

    send_thread_name = threading.current_thread().name
    events = []

    def run():
        return (yield from dispatch_pilot_provider_call(
            session,
            plan=False,
            sys_prompt="sys",
            prompt="ping",
            synthesis_nudge_active=False,
            accumulator=acc,
        ))

    streamed, out = _exhaust_dispatch_events(run(), events)
    assert streamed == "Hi thereNext"
    assert out is resp
    assert out.latency_ms == 20.0
    assert out.meta["keep"] == 1
    assert stream_thread_name["name"]
    assert stream_thread_name["name"] != send_thread_name

    deltas = [e for e in events if getattr(e, "kind", None) == "message_delta"]
    assert deltas[0].data["text"] == "Hi"
    assert deltas[0].data.get("stream_id") == "a1"
    assert any(
        e.data.get("stream_id") == "a2" and e.data.get("text") == "Next"
        for e in deltas
    )
    assert any(getattr(e, "kind", None) == "tool_prep" for e in events)

    perf = out.meta[STREAM_PERFORMANCE_KEY]
    assert perf["image_prep_ms"] == 40.0
    assert FIRST_CONTENT_CALLBACK_MS in perf
    assert FIRST_ANSWER_CALLBACK_MS in perf
    assert FIRST_VISIBLE_ANSWER_MS in perf
    assert perf[FIRST_VISIBLE_ANSWER_MS] >= perf[FIRST_CONTENT_CALLBACK_MS]
    assert "thread_start_ms" in perf
    assert PROVIDER_CALL_TOTAL_MS in perf
    assert BACKEND_READY_TOTAL_MS in perf
    assert perf[BACKEND_READY_TOTAL_MS] >= perf[FIRST_VISIBLE_ANSWER_MS]


def _exhaust_dispatch_events(gen, events):
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        return stop.value


def test_yield_timed_phase_send_forwards_and_preserves_return():
    clock = FakeClock()
    acc = StreamTimingAccumulator(clock=clock)

    def gen():
        clock.advance(0.005)
        got = yield "ask"
        clock.advance(0.007)
        return got

    def runner():
        result = yield from yield_timed_phase(acc, "task_profile", gen())
        assert result == "pong"

    it = runner()
    assert next(it) == "ask"
    clock.advance(0.200)
    try:
        it.send("pong")
    except StopIteration:
        pass
    assert acc.snapshot()["task_profile_ms"] == 12.0


def test_yield_timed_phase_throw_forwards_and_runs_inner_finally():
    clock = FakeClock()
    acc = StreamTimingAccumulator(clock=clock)
    seen = []

    def gen():
        try:
            clock.advance(0.004)
            yield "x"
            yield "should-not-run"
        except ValueError as exc:
            seen.append(str(exc))
            clock.advance(0.006)
            yield "handled"
        finally:
            seen.append("finally")

    def runner():
        yield from yield_timed_phase(acc, "task_profile", gen())

    it = runner()
    assert next(it) == "x"
    clock.advance(0.300)
    assert it.throw(ValueError("boom")) == "handled"
    try:
        next(it)
    except StopIteration:
        pass
    assert seen == ["boom", "finally"]
    assert acc.snapshot()["task_profile_ms"] == 10.0


def test_yield_timed_phase_throw_uncaught_runs_inner_finally():
    seen = []

    def gen():
        try:
            yield "x"
            yield "y"
        finally:
            seen.append("finally")

    def runner():
        yield from yield_timed_phase(None, "task_profile", gen())

    it = runner()
    assert next(it) == "x"
    try:
        it.throw(ValueError("nope"))
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "nope" in str(exc)
    assert seen == ["finally"]


def test_yield_timed_phase_close_runs_inner_finally():
    clock = FakeClock()
    acc = StreamTimingAccumulator(clock=clock)
    seen = []

    def gen():
        try:
            clock.advance(0.004)
            yield "x"
            clock.advance(0.050)
            yield "y"
        finally:
            clock.advance(0.006)
            seen.append("finally")

    def runner():
        yield from yield_timed_phase(acc, "image_prep", gen())

    it = runner()
    next(it)
    clock.advance(0.300)
    it.close()
    assert seen == ["finally"]
    assert acc.snapshot()["image_prep_ms"] == 10.0


def test_note_out_of_order_clock_uses_minima_maxima_skips_negative_gap():
    class ReversibleClock:
        def __init__(self) -> None:
            self.t = 1000.0

        def __call__(self) -> float:
            return self.t

    clock = ReversibleClock()
    acc = StreamTimingAccumulator(clock=clock)
    acc.mark_request_start()
    clock.t = 1000.100
    acc.note("delta", "late-first")
    clock.t = 1000.020
    acc.note("delta", "early-second")
    clock.t = 1000.015
    acc.mark_first_visible_answer()
    clock.t = 1000.080
    acc.mark_first_visible_answer()
    clock.t = 1000.250
    acc.note("delta", "last")
    acc.finish()
    snap = acc.snapshot(tokens_out=6)
    assert snap[FIRST_CONTENT_CALLBACK_MS] == 20.0
    assert snap[FIRST_ANSWER_CALLBACK_MS] == 20.0
    assert snap[FIRST_VISIBLE_ANSWER_MS] == 15.0
    assert snap["decode_window_ms"] == 230.0
    assert snap["max_inter_delta_ms"] == 150.0
    assert snap["max_inter_delta_ms"] >= 0
    assert snap[PROVIDER_CALL_TOTAL_MS] == 250.0
    assert BACKEND_READY_TOTAL_MS not in snap


def test_concurrent_notes_minima_maxima_never_negative_gap():
    class ThreadClock:
        def __init__(self) -> None:
            self.default = 1000.0
            self.tls = threading.local()

        def __call__(self) -> float:
            return getattr(self.tls, "t", self.default)

    clock = ThreadClock()
    acc = StreamTimingAccumulator(clock=clock)
    acc.mark_request_start()
    barrier = threading.Barrier(2)

    def worker(when: float, text: str) -> None:
        clock.tls.t = when
        barrier.wait()
        acc.note("delta", text)

    threads = [
        threading.Thread(target=worker, args=(1000.080, "late")),
        threading.Thread(target=worker, args=(1000.010, "early")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    snap = acc.snapshot(tokens_out=4)
    assert snap[FIRST_CONTENT_CALLBACK_MS] == 10.0
    assert snap["decode_window_ms"] == 70.0
    if "max_inter_delta_ms" in snap:
        assert snap["max_inter_delta_ms"] >= 0


def test_visible_after_provider_return_uses_backend_ready_boundary():
    """Provider return before drain is valid; visible may exceed provider total."""
    clock = FakeClock()
    acc = StreamTimingAccumulator(clock=clock)
    acc.mark_request_start()
    clock.advance(0.030)
    acc.note("delta", {"text": '{"say": "Hi"}', "stream_id": "a1", "channel": "answer"})
    clock.advance(0.020)
    acc.finish()
    mid = acc.snapshot()
    assert mid[PROVIDER_CALL_TOTAL_MS] == 50.0
    assert FIRST_VISIBLE_ANSWER_MS not in mid
    assert BACKEND_READY_TOTAL_MS not in mid

    clock.advance(0.040)
    q: queue.Queue = queue.Queue()
    resp = DriverResponse(text="ok", tokens_out=2, latency_ms=8.0, meta={})
    q.put(("delta", {"text": '{"say": "Hi"}', "stream_id": "a1", "channel": "answer"}))
    q.put(("done", resp))
    events = []
    gen = drain_stream_queue(q, accumulator=acc)
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        streamed, got = stop.value

    assert streamed == "Hi"
    perf = got.meta[STREAM_PERFORMANCE_KEY]
    assert perf[FIRST_CONTENT_CALLBACK_MS] == 30.0
    assert perf[FIRST_VISIBLE_ANSWER_MS] == 90.0
    assert perf[PROVIDER_CALL_TOTAL_MS] == 50.0
    assert perf[BACKEND_READY_TOTAL_MS] == 90.0
    assert perf[FIRST_VISIBLE_ANSWER_MS] > perf[PROVIDER_CALL_TOTAL_MS]
    assert perf[BACKEND_READY_TOTAL_MS] >= perf[FIRST_VISIBLE_ANSWER_MS]
    assert got.latency_ms == 8.0


def test_step0_overflow_retry_keeps_turn_once_phases(monkeypatch, tmp_path):
    """First-step CONTEXT_OVERFLOW keeps turn-once phases and emergency compact."""
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession

    clock = FakeClock()
    acc = StreamTimingAccumulator(clock=clock)
    orig_begin = acc.begin_phase
    orig_end = acc.end_phase

    def begin(name):
        clock.advance(0.001)
        return orig_begin(name)

    def end(name):
        clock.advance(0.010)
        return orig_end(name)

    acc.begin_phase = begin
    acc.end_phase = end
    monkeypatch.setattr(
        "harness.send_loop.make_stream_timing_accumulator",
        lambda clock=None: acc,
    )
    monkeypatch.setattr(
        "harness.send_loop.profile_skips_auto_inject",
        lambda session: (False, False),
    )

    snaps = []
    orig_attach = attach_stream_performance

    def spy_attach(resp, snap):
        snaps.append(dict(snap))
        return orig_attach(resp, snap)

    monkeypatch.setattr(
        "harness.send_loop_phases.attach_stream_performance", spy_attach,
    )

    compact_calls = []
    _pad = {"role": "user", "content": "X" * 1000}

    cfg = HarnessConfig(
        driver="stub-oracle-v2",
        state_dir=str(tmp_path),
        repo=str(tmp_path),
    )
    session = ConversationalSession(cfg)
    monkeypatch.setattr(session, "_resolve_append_only", lambda: False)
    session._get_codegraph_context = lambda msg: ""
    session._history.append(_pad)

    def _spy_compact(force=False, emergency=False):
        compact_calls.append({"force": force, "emergency": emergency})
        # One-shot byte drop so overflow retry scores progress (413 is bytes).
        if emergency and _pad in session._history:
            session._history.remove(_pad)
        if False:
            yield None

    monkeypatch.setattr(session, "_maybe_compact_history", _spy_compact)

    class OverflowThenOk:
        name = "overflow-then-ok"

        def __init__(self):
            self.calls = 0

        def chat(self, messages, tools=None, system=None):
            self.calls += 1
            clock.advance(0.040 if self.calls == 1 else 0.020)
            if self.calls == 1:
                return DriverResponse(
                    text="",
                    error="HTTP 400: maximum context length exceeded",
                    tokens_out=1,
                    latency_ms=1.0,
                )
            return DriverResponse(
                text='{"say": "recovered", "actions": []}',
                tokens_out=2,
                latency_ms=1.0,
            )

        def complete(self, prompt, system=None):
            return DriverResponse(text="summary", tokens_out=1, latency_ms=1.0)

    session.pilot = OverflowThenOk()
    events = list(session.send("audit the overflow retry path"))
    assert not any(getattr(e, "kind", None) == "error" for e in events)
    assert session.pilot.calls == 2
    assert any(c.get("emergency") for c in compact_calls)
    assert len(snaps) >= 2
    first, retry = snaps[0], snaps[-1]
    for key in (
        "image_prep_ms",
        "task_profile_ms",
        "user_append_ms",
        "auto_codegraph_ms",
    ):
        assert key in first, key
        assert key in retry, key
        assert retry[key] == first[key]
    if "step_codegraph_ms" in first:
        assert retry["step_codegraph_ms"] == first["step_codegraph_ms"]
    if "step_wiki_ms" in first:
        assert retry["step_wiki_ms"] == first["step_wiki_ms"]
    assert "advisory_compaction_ms" in retry
    assert retry["advisory_compaction_ms"] > first.get("advisory_compaction_ms", 0)
    assert "prompt_tools_ms" in first
    assert "prompt_tools_ms" in retry
    assert retry["prompt_tools_ms"] <= first["prompt_tools_ms"]
    assert "thread_start_ms" not in retry
    assert FIRST_CONTENT_CALLBACK_MS not in retry
    assert FIRST_VISIBLE_ANSWER_MS not in retry
    assert BACKEND_READY_TOTAL_MS not in retry
    assert retry[PROVIDER_CALL_TOTAL_MS] == 20.0
    assert first[PROVIDER_CALL_TOTAL_MS] == 40.0
    assert "pre_request_total_ms" in first
    assert "pre_request_total_ms" in retry
    assert retry["pre_request_total_ms"] < first["pre_request_total_ms"]
    emergency_added = (
        retry["advisory_compaction_ms"] - first.get("advisory_compaction_ms", 0)
    )
    retry_prep = emergency_added + retry["prompt_tools_ms"]
    assert retry["pre_request_total_ms"] >= retry_prep
    # Slack is wrapper gaps only — the failed provider window is not included.
    assert retry["pre_request_total_ms"] - retry_prep < first[PROVIDER_CALL_TOTAL_MS]


def test_step_gt0_clears_turn_once_phases(monkeypatch, tmp_path):
    """Normal tool-loop step > 0 still drops turn-once phases."""
    import json

    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession

    clock = FakeClock()
    acc = StreamTimingAccumulator(clock=clock)
    orig_begin = acc.begin_phase
    orig_end = acc.end_phase

    def begin(name):
        clock.advance(0.001)
        return orig_begin(name)

    def end(name):
        clock.advance(0.010)
        return orig_end(name)

    acc.begin_phase = begin
    acc.end_phase = end
    monkeypatch.setattr(
        "harness.send_loop.make_stream_timing_accumulator",
        lambda clock=None: acc,
    )

    snaps = []
    orig_attach = attach_stream_performance

    def spy_attach(resp, snap):
        snaps.append(dict(snap))
        return orig_attach(resp, snap)

    monkeypatch.setattr(
        "harness.send_loop_phases.attach_stream_performance", spy_attach,
    )

    (tmp_path / "spy.txt").write_text("hello", encoding="utf-8")
    cfg = HarnessConfig(
        driver="stub-oracle-v2",
        state_dir=str(tmp_path),
        repo=str(tmp_path),
    )
    session = ConversationalSession(cfg)

    class TwoStep:
        name = "two-step-timing"

        def __init__(self):
            self.calls = 0

        def chat(self, messages, tools=None, system=None):
            self.calls += 1
            if self.calls == 1:
                return DriverResponse(
                    text="",
                    tokens_out=5,
                    latency_ms=1.0,
                    meta={
                        "tool_calls": [
                            {
                                "id": "call_spy_1",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": json.dumps({"path": "spy.txt"}),
                                },
                            }
                        ],
                        "finish_reason": "tool_calls",
                    },
                )
            return DriverResponse(
                text='{"say": "done after tool", "actions": []}',
                tokens_out=5,
                latency_ms=1.0,
                meta={"tool_calls": [], "finish_reason": "stop"},
            )

        def complete(self, prompt, system=None):
            return DriverResponse(text="summary", tokens_out=1, latency_ms=1.0)

    session.pilot = TwoStep()
    list(session.send("read spy.txt then finish"))
    assert session.pilot.calls >= 2
    assert len(snaps) >= 2
    assert "image_prep_ms" in snaps[0]
    assert "user_append_ms" in snaps[0]
    assert "image_prep_ms" not in snaps[-1]
    assert "user_append_ms" not in snaps[-1]
    assert "task_profile_ms" not in snaps[-1]
