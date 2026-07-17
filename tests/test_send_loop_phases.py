"""Characterization tests for send_loop_phases peel from _send_locked_inner.

Locks the extracted helper contracts (queue kinds, prefetch dispatch, stdout
job_id scrape) and asserts the nested closures no longer live inside the
mixin turn kernel — so unit tests can target the helpers directly.
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
    read_stdout_thread,
    run_prefetch,
    run_stream,
    stream_swarm,
)

PHASE_HELPERS = (
    "run_stream",
    "run_prefetch",
    "stream_swarm",
    "read_stdout_thread",
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
