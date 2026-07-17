"""Characterization tests for send_loop_phases peel from _send_locked_inner.

Locks the extracted helper contracts (queue kinds, stream drain, metering,
action-goal labels, prefetch pool, stdout job_id scrape) and asserts those
helpers are module-level callables invoked from the mixin turn kernel — so
unit tests can target them directly without nested closures.
"""

from __future__ import annotations

import ast
import inspect
import queue
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from harness.pilot import PilotAction
from harness.send_loop import SendLoopMixin
from harness.send_loop_phases import (
    READ_ONLY_KINDS,
    action_display_goal,
    drain_stream_queue,
    meter_pilot_step,
    read_stdout_thread,
    run_parallel_prefetch,
    run_prefetch,
    run_stream,
    stream_swarm,
)

PHASE_HELPERS = (
    "run_stream",
    "run_prefetch",
    "run_parallel_prefetch",
    "stream_swarm",
    "read_stdout_thread",
    "action_display_goal",
    "drain_stream_queue",
    "meter_pilot_step",
)


def test_phase_helpers_are_module_level_callables():
    import harness.send_loop_phases as phases

    for name in PHASE_HELPERS:
        fn = getattr(phases, name)
        assert callable(fn)
        assert fn.__module__ == "harness.send_loop_phases"


def test_send_locked_inner_no_longer_nests_phase_helpers():
    """The peel must remove the named nested defs from the turn kernel AST."""
    src = Path(inspect.getsourcefile(SendLoopMixin)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    nested_names: set[str] = set()

    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != "SendLoopMixin":
            continue
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if item.name != "_send_locked_inner":
                continue
            for child in ast.walk(item):
                if (
                    isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and child is not item
                ):
                    nested_names.add(child.name)

    # Historical nested name was `_stream_swarm`; extracted as `stream_swarm`.
    forbidden = set(PHASE_HELPERS) | {"_stream_swarm"}
    assert not (nested_names & forbidden), nested_names & forbidden


def test_mixin_still_owns_public_orchestration_surface():
    for name in ("send", "_send_locked", "_send_locked_inner"):
        attr = getattr(SendLoopMixin, name)
        assert attr.__qualname__ == f"SendLoopMixin.{name}"


def test_mixin_calls_new_phase_helpers():
    src = Path(inspect.getsourcefile(SendLoopMixin)).read_text(encoding="utf-8")
    assert "drain_stream_queue(" in src
    assert "meter_pilot_step(" in src
    assert "action_display_goal(" in src
    assert "run_parallel_prefetch(" in src



def test_run_stream_puts_done_on_success():
    q: queue.Queue = queue.Queue()
    resp = SimpleNamespace(text="ok")

    def chat_stream(messages, **kwargs):
        assert kwargs["tools"] == [{"name": "t"}]
        assert kwargs["system"] == "sys"
        kwargs["on_delta"]("hello")
        return resp

    session = SimpleNamespace(
        pilot=SimpleNamespace(
            chat_stream=chat_stream,
            supports_streaming=True,
        ),
        _history=[{"role": "system"}, {"role": "user", "content": "hi"}],
        _elide_stale_reads=lambda msgs: msgs,
    )
    run_stream(session, q, [{"name": "t"}], "sys")
    kinds = []
    while not q.empty():
        kinds.append(q.get_nowait()[0])
    assert kinds == ["delta", "done"]


def test_run_stream_puts_error_on_failure():
    q: queue.Queue = queue.Queue()

    def chat_stream(messages, **kwargs):
        raise RuntimeError("boom")

    session = SimpleNamespace(
        pilot=SimpleNamespace(chat_stream=chat_stream),
        _history=[{"role": "system"}, {"role": "user", "content": "hi"}],
        _elide_stale_reads=lambda msgs: msgs,
    )
    run_stream(session, q, [], "sys")
    kind, val = q.get_nowait()
    assert kind == "error"
    assert isinstance(val, RuntimeError)


def test_run_prefetch_dispatches_read_file():
    act = PilotAction(kind="read_file", path="a.py")
    session = MagicMock()
    session._do_read_file.return_value = (True, "ok", "body")
    idx, res = run_prefetch(session, (3, act))
    assert idx == 3
    assert res == (True, "ok", "body")
    session._do_read_file.assert_called_once_with(act)


def test_run_prefetch_unknown_kind_returns_exception_tuple():
    act = PilotAction(kind="not_a_prefetch_tool", goal="x")
    idx, res = run_prefetch(SimpleNamespace(), (1, act))
    assert idx == 1
    assert res[0] is False
    assert res[1] == "exception"
    assert "Unknown prefetch kind" in res[2]


def test_run_prefetch_handler_exception_surfaces_as_tuple():
    act = PilotAction(kind="list_dir", path=".")
    session = MagicMock()
    session._do_list_dir.side_effect = ValueError("nope")
    idx, res = run_prefetch(session, (0, act))
    assert idx == 0
    assert res == (False, "exception", "nope")


def test_stream_swarm_puts_done_and_forwards_deltas(monkeypatch):
    q: queue.Queue = queue.Queue()
    result = SimpleNamespace(job_id="job_abc123def456")

    def fake_execute(intent, **kwargs):
        kwargs["on_delta"]("w1", "text", "hi")
        return result

    monkeypatch.setattr(
        "harness.send_loop_phases.execute_intent", fake_execute
    )
    session = SimpleNamespace(
        state_dir="/tmp/state",
        harness_session_id="sess",
        config=SimpleNamespace(repo="/repo"),
    )
    stream_swarm(session, intent=SimpleNamespace(), delta_q=q)
    assert q.get_nowait() == ("delta", ("w1", "text", "hi"))
    assert q.get_nowait() == ("done", result)


def test_stream_swarm_puts_error(monkeypatch):
    q: queue.Queue = queue.Queue()

    def boom(*a, **k):
        raise RuntimeError("swarm failed")

    monkeypatch.setattr("harness.send_loop_phases.execute_intent", boom)
    session = SimpleNamespace(
        state_dir="/tmp/state",
        harness_session_id="",
        config=SimpleNamespace(repo=None),
    )
    stream_swarm(session, intent=SimpleNamespace(), delta_q=q)
    kind, val = q.get_nowait()
    assert kind == "error"
    assert "swarm failed" in str(val)


def test_read_stdout_thread_captures_job_id():
    class _Stdout:
        def __iter__(self):
            return iter(
                [
                    "starting…\n",
                    "created job_deadbeef0012 for worker\n",
                    "more\n",
                ]
            )

    p_info = {
        "proc": SimpleNamespace(stdout=_Stdout()),
        "lines": [],
        "job_id": None,
    }
    read_stdout_thread(p_info)
    assert p_info["job_id"] == "job_deadbeef0012"
    assert len(p_info["lines"]) == 3


def test_read_stdout_thread_tolerates_stdout_errors():
    class _BadStdout:
        def __iter__(self):
            raise OSError("pipe closed")

    p_info = {
        "proc": SimpleNamespace(stdout=_BadStdout()),
        "lines": [],
        "job_id": None,
    }
    read_stdout_thread(p_info)  # must not raise
    assert p_info["job_id"] is None


def test_run_stream_usable_as_thread_target():
    q: queue.Queue = queue.Queue()
    resp = SimpleNamespace(text="ok")
    session = SimpleNamespace(
        pilot=SimpleNamespace(
            chat_stream=lambda messages, **kwargs: resp,
        ),
        _history=[{"role": "system"}, {"role": "user", "content": "hi"}],
        _elide_stale_reads=lambda msgs: msgs,
    )
    t = threading.Thread(
        target=run_stream, args=(session, q, [], "sys"), daemon=True
    )
    t.start()
    t.join(timeout=2)
    assert not t.is_alive()
    assert q.get(timeout=1)[0] == "done"


def test_read_only_kinds_covers_prefetchable_tools():
    assert "read_file" in READ_ONLY_KINDS
    assert "lsp" in READ_ONLY_KINDS
    assert "run_command" not in READ_ONLY_KINDS
    assert "write_file" not in READ_ONLY_KINDS


def test_action_display_goal_by_kind():
    assert action_display_goal(PilotAction(kind="read_file", path="a.py")) == "a.py"
    assert action_display_goal(
        PilotAction(kind="run_command", command="pytest")
    ) == "pytest"
    assert action_display_goal(
        PilotAction(kind="web_search", query="foo")
    ) == "foo"
    assert action_display_goal(
        PilotAction(kind="manage_mcp", arguments={"action": "list", "name": "x"})
    ) == "list x"
    assert action_display_goal(
        PilotAction(kind="browser_navigate", arguments={"url": "https://ex"})
    ) == "https://ex"
    assert action_display_goal(
        PilotAction(kind="relocate_session", arguments={"workspace_root": "/w"})
    ) == "/w"
    # Unknown kinds keep the PilotAction.goal default.
    bare = PilotAction(kind="run_swarm", goal="ship it")
    assert action_display_goal(bare) == "ship it"


def test_drain_stream_queue_yields_deltas_and_returns_resp():
    q: queue.Queue = queue.Queue()
    resp = SimpleNamespace(text="done")
    # Minimal JSON say-envelope so StreamingSayExtractor emits prose.
    q.put(("delta", '{"say": "Hello'))
    q.put(("delta", ' world"}'))
    q.put(("reasoning", "think"))
    q.put(("tool_hint", "read_file"))
    q.put(("tool_hint", {"name": "edit_file", "goal": "fix", "id": "c1"}))
    q.put(("wait", "still working"))
    q.put(("done", resp))

    events = []
    gen = drain_stream_queue(q)
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        streamed, got = stop.value

    kinds = [e.kind for e in events]
    assert "message_delta" in kinds
    assert ("thinking", {"text": "think", "delta": True}) in [
        (e.kind, e.data) for e in events
    ]
    assert any(e.kind == "tool_prep" and e.data.get("name") == "read_file" for e in events)
    assert any(
        e.kind == "tool_prep"
        and e.data.get("name") == "edit_file"
        and e.data.get("id") == "c1"
        for e in events
    )
    assert any(e.kind == "notice" and e.data.get("kind") == "wait" for e in events)
    assert got is resp
    assert "Hello" in streamed


def test_drain_stream_queue_raises_on_error():
    q: queue.Queue = queue.Queue()
    q.put(("error", RuntimeError("stream broke")))
    gen = drain_stream_queue(q)
    try:
        next(gen)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "stream broke" in str(exc)


def test_drain_stream_queue_usable_via_yield_from():
    q: queue.Queue = queue.Queue()
    resp = SimpleNamespace(text="ok")
    q.put(("done", resp))

    def _consume():
        prose, r = yield from drain_stream_queue(q)
        return prose, r

    gen = _consume()
    try:
        next(gen)
        raise AssertionError("expected StopIteration with return value")
    except StopIteration as stop:
        prose, r = stop.value
    assert prose == ""
    assert r is resp


def test_run_parallel_prefetch_skips_singleton():
    session = MagicMock()
    act = PilotAction(kind="read_file", path="a.py")
    assert run_parallel_prefetch(session, [(0, act)]) == {}
    session._do_read_file.assert_not_called()


def test_run_parallel_prefetch_maps_multiple(monkeypatch):
    session = MagicMock()
    session._do_read_file.side_effect = lambda act: (True, "ok", act.path)
    session._do_list_dir.side_effect = lambda act: (True, "ok", ["x"])
    targets = [
        (0, PilotAction(kind="read_file", path="a.py")),
        (1, PilotAction(kind="list_dir", path=".")),
    ]
    out = run_parallel_prefetch(session, targets)
    assert out[0] == (True, "ok", "a.py")
    assert out[1] == (True, "ok", ["x"])


def test_meter_pilot_step_accumulates_tokens_and_provider_cost(monkeypatch):
    meters = {}

    def accumulate(**kwargs):
        meters.update(kwargs)

    session = SimpleNamespace(
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
        _accumulate_session_meters=accumulate,
    )
    resp = SimpleNamespace(
        tokens_out=10,
        tokens_in=100,
        meta={
            "cache_read_tokens": 40,
            "cache_write_tokens": 5,
            "provider_cost_usd": 0.0123,
        },
    )
    meter_pilot_step(session, resp, prompt="x" * 400)
    assert session._tokens_out == 10
    assert session._tokens_in == 100
    assert session._tokens_used == 110
    assert session._tokens_cached == 40
    assert session._tokens_cache_write == 5
    assert session._last_prompt_tokens == 100
    assert session._provider_cost_usd == 0.0123
    assert meters["estimated_cost_usd"] == 0.0123
    assert meters["input_tokens"] == 100
    assert meters["output_tokens"] == 10


def test_meter_pilot_step_estimates_tokens_in_from_prompt():
    meters = {}
    session = SimpleNamespace(
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
        config=SimpleNamespace(driver="unknown/driver"),
        _accumulate_session_meters=lambda **kw: meters.update(kw),
    )
    resp = SimpleNamespace(tokens_out=4, tokens_in=0, meta={})
    prompt = "abcd" * 25  # 100 chars → tokens_in fallback 25
    meter_pilot_step(session, resp, prompt=prompt)
    assert session._tokens_in == 25
    assert session._last_prompt_tokens == 25

