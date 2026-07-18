"""Characterization tests for send_loop_phases peel from _send_locked_inner.

Locks the extracted helper contracts (queue kinds, stream drain, metering,
action-goal labels, prefetch pool, stdout job_id scrape, idle steer/queue
drain, read-only/local tool-result assembly, auto-verify) and asserts those
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
    LOCAL_ACTION_KINDS,
    READ_ONLY_KINDS,
    action_display_goal,
    dispatch_local_action,
    dispatch_readonly_action,
    drain_idle_turn,
    drain_stream_queue,
    meter_pilot_step,
    read_stdout_thread,
    run_auto_verify,
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
    "drain_idle_turn",
    "dispatch_readonly_action",
    "dispatch_local_action",
    "run_auto_verify",
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
    assert "drain_idle_turn(" in src
    assert "run_auto_verify(" in src
    assert "execute_turn_actions(" in src
    # Action-spree fan-out (prefetch / readonly / local) lives in send_loop_actions.
    actions_src = Path("harness/send_loop_actions.py").read_text(encoding="utf-8")
    assert "action_display_goal(" in actions_src
    assert "run_parallel_prefetch(" in actions_src
    assert "dispatch_readonly_action(" in actions_src
    assert "dispatch_local_action(" in actions_src


def test_send_locked_inner_no_longer_inlines_readonly_branches():
    """Read-only tool-result assembly must live in dispatch_readonly_action."""
    src = Path(inspect.getsourcefile(SendLoopMixin)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    inner = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "SendLoopMixin":
            for item in node.body:
                if (
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name == "_send_locked_inner"
                ):
                    inner = item
                    break
    assert inner is not None
    segment = ast.get_source_segment(src, inner) or ""
    assert "---- read_file branch" not in segment
    assert "---- list_dir branch" not in segment
    assert "execute_turn_actions(" in segment
    assert "drain_idle_turn(" in segment
    assert "run_auto_verify(" in segment
    actions_src = Path("harness/send_loop_actions.py").read_text(encoding="utf-8")
    assert "dispatch_readonly_action(" in actions_src


def test_send_locked_inner_no_longer_inlines_local_branches():
    """Local tool-result assembly must live in dispatch_local_action."""
    src = Path(inspect.getsourcefile(SendLoopMixin)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    inner = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "SendLoopMixin":
            for item in node.body:
                if (
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name == "_send_locked_inner"
                ):
                    inner = item
                    break
    assert inner is not None
    segment = ast.get_source_segment(src, inner) or ""
    assert "---- open_project branch" not in segment
    assert "---- write_file branch" not in segment
    assert "---- hash_edit branch" not in segment
    assert "---- MCP tool call branch" not in segment
    assert "execute_turn_actions(" in segment
    actions_src = Path("harness/send_loop_actions.py").read_text(encoding="utf-8")
    assert "dispatch_local_action(" in actions_src
    assert "LOCAL_ACTION_KINDS" in actions_src


def test_run_stream_puts_done_on_success():
    q: queue.Queue = queue.Queue()
    resp = SimpleNamespace(text="ok")

    def chat_stream(messages, **kwargs):
        assert kwargs["tools"] == [{"name": "t"}]
        assert kwargs["system"] == "sys"
        kwargs["on_delta"]("hello")
        return resp

    history = [{"role": "system"}, {"role": "user", "content": "hi"}]
    session = SimpleNamespace(
        pilot=SimpleNamespace(
            chat_stream=chat_stream,
            supports_streaming=True,
        ),
        _history=history,
        _elide_stale_reads=lambda msgs: msgs,
        _messages_for_provider=lambda: history[1:],
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

    history = [{"role": "system"}, {"role": "user", "content": "hi"}]
    session = SimpleNamespace(
        pilot=SimpleNamespace(chat_stream=chat_stream),
        _history=history,
        _elide_stale_reads=lambda msgs: msgs,
        _messages_for_provider=lambda: history[1:],
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


def test_stream_swarm_passes_resolved_git_child_cwd(monkeypatch, tmp_path):
    """stream_swarm must resolve Marionette Home parent before execute_intent."""
    import os
    import shutil
    import subprocess

    if shutil.which("git") is None:
        import pytest
        pytest.skip("git not available")

    from harness.repo_resolve import clear_effective_repo_cache, resolve_effective_repo

    clear_effective_repo_cache()
    home = tmp_path / "home" / ".marionette"
    home.mkdir(parents=True)
    child = home / "marionette"
    child.mkdir()
    subprocess.run(
        ["git", "init"],
        cwd=str(child),
        check=True,
        capture_output=True,
        text=True,
    )
    q: queue.Queue = queue.Queue()
    seen: dict = {}

    def fake_execute(intent, **kwargs):
        seen["cwd"] = kwargs.get("cwd")
        seen["repo"] = kwargs.get("repo")
        return SimpleNamespace(job_id="job_resolved")

    monkeypatch.setattr(
        "harness.send_loop_phases.execute_intent", fake_execute
    )
    session = SimpleNamespace(
        state_dir=str(tmp_path / "state"),
        harness_session_id="sess",
        config=SimpleNamespace(repo=str(home)),
    )
    stream_swarm(session, intent=SimpleNamespace(), delta_q=q)
    assert q.get_nowait()[0] == "done"
    expected = resolve_effective_repo(str(home))
    assert os.path.normcase(os.path.normpath(seen["cwd"])) == os.path.normcase(
        os.path.normpath(expected)
    )
    assert os.path.normcase(os.path.normpath(seen["repo"])) == os.path.normcase(
        os.path.normpath(expected)
    )
    assert os.path.normcase(os.path.normpath(str(home))) != os.path.normcase(
        os.path.normpath(expected)
    )
    clear_effective_repo_cache()


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
    history = [{"role": "system"}, {"role": "user", "content": "hi"}]
    session = SimpleNamespace(
        pilot=SimpleNamespace(
            chat_stream=lambda messages, **kwargs: resp,
        ),
        _history=history,
        _elide_stale_reads=lambda msgs: msgs,
        _messages_for_provider=lambda: history[1:],
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


def test_drain_idle_turn_delivers_steers_and_continues():
    history = [{"role": "assistant", "content": "done"}]
    steers = ["course correct"]

    session = SimpleNamespace(
        drain_steer=lambda: list(steers),
        _history=history,
        _steer_marker=lambda t: f"<steer>{t}</steer>",
        _steer_pending=True,
        _next_queued_needs_driver_swap=lambda: False,
        _pop_next_prompt=lambda: None,
        _submit_housekeeping=MagicMock(),
        _maybe_ingest=MagicMock(),
    )
    events = []
    gen = drain_idle_turn(
        session,
        user_message="orig",
        step=0,
        swarms=0,
        turn_prose=[],
        turn_findings=[],
    )
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        disposition, user_message = stop.value
    assert disposition == "continue"
    assert user_message == "orig"
    assert session._steer_pending is False
    assert events[0].kind == "steer"
    assert history[-1]["content"] == "<steer>course correct</steer>"
    session._submit_housekeeping.assert_not_called()


def test_drain_idle_turn_finalizes_when_idle():
    submitted = []

    session = SimpleNamespace(
        drain_steer=lambda: [],
        _history=[],
        _steer_pending=False,
        _next_queued_needs_driver_swap=lambda: False,
        _pop_next_prompt=lambda: None,
        _submit_housekeeping=lambda fn, *a: submitted.append((fn, a)),
        _maybe_ingest="ingest",
    )
    events = []
    gen = drain_idle_turn(
        session,
        user_message="hi",
        step=2,
        swarms=1,
        turn_prose=["p"],
        turn_findings=["f"],
    )
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        disposition, user_message = stop.value
    assert disposition == "return"
    assert user_message == "hi"
    assert events[0].kind == "assistant_done"
    assert events[0].data == {"turns": 3, "swarms": 1}
    assert submitted == [("ingest", ("hi", ["p"], ["f"]))]


def test_drain_idle_turn_breaks_on_driver_swap():
    session = SimpleNamespace(
        drain_steer=lambda: [],
        _next_queued_needs_driver_swap=lambda: True,
        _pop_next_prompt=MagicMock(),
        _submit_housekeeping=MagicMock(),
    )
    gen = drain_idle_turn(
        session,
        user_message="hi",
        step=0,
        swarms=0,
        turn_prose=[],
        turn_findings=[],
    )
    try:
        next(gen)
        raise AssertionError("expected immediate return")
    except StopIteration as stop:
        disposition, user_message = stop.value
    assert disposition == "break"
    assert user_message == "hi"
    session._pop_next_prompt.assert_not_called()


def test_dispatch_readonly_action_read_file_success():
    act = PilotAction(kind="read_file", path="a.py")
    appended = []
    session = SimpleNamespace(
        _do_read_file=MagicMock(return_value=(True, "ok", "body")),
        _append_action_result=lambda *a, **k: appended.append((a, k)),
    )
    events = list(dispatch_readonly_action(session, act, 0, "a1", {}, True))
    assert events[0].kind == "action_result"
    assert events[0].data["types"] == ["file"]
    session._do_read_file.assert_called_once_with(act)
    assert appended and "returned" in appended[0][0][2]


def test_dispatch_readonly_action_uses_prefetch_hit():
    act = PilotAction(kind="list_dir", path=".")
    session = SimpleNamespace(
        _do_list_dir=MagicMock(),
        _append_action_result=MagicMock(),
    )
    prefetch = {2: (True, "ok", (3, "a\nb\nc"))}
    events = list(dispatch_readonly_action(session, act, 2, "a2", prefetch, False))
    assert events[0].data["types"] == ["dir"]
    session._do_list_dir.assert_not_called()
    session._append_action_result.assert_called_once()


def test_dispatch_readonly_action_read_file_error():
    act = PilotAction(kind="read_file", path="missing.py")
    session = SimpleNamespace(
        _do_read_file=MagicMock(return_value=(False, "repo_not_open", "no repo")),
        _append_action_result=MagicMock(),
    )
    events = list(dispatch_readonly_action(session, act, 0, "a1", {}, True))
    assert events[0].data.get("error") == "no repo"
    session._append_action_result.assert_called_once()


def test_local_action_kinds_covers_workspace_mutate_browse_mcp():
    assert "open_project" in LOCAL_ACTION_KINDS
    assert "write_file" in LOCAL_ACTION_KINDS
    assert "run_command" in LOCAL_ACTION_KINDS
    assert "call_mcp" in LOCAL_ACTION_KINDS
    assert "manage_mcp" in LOCAL_ACTION_KINDS
    assert "browser_navigate" in LOCAL_ACTION_KINDS
    # Delegation / swarm live in send_loop_dispatch, not local kinds.
    assert "run_swarm" not in LOCAL_ACTION_KINDS
    assert "run_implement" not in LOCAL_ACTION_KINDS
    assert "read_file" not in LOCAL_ACTION_KINDS


def test_dispatch_local_action_open_project_requires_path():
    act = PilotAction(kind="open_project", path="")
    session = SimpleNamespace(
        config=SimpleNamespace(repo=None),
        _append_action_result=MagicMock(),
    )
    events = list(dispatch_local_action(session, act, "a1", True, []))
    assert events[0].data.get("error")
    assert "path is required" in events[0].data["error"]
    session._append_action_result.assert_called_once()


def test_dispatch_local_action_write_file_requires_repo():
    act = PilotAction(kind="write_file", path="a.py", content="x")
    session = SimpleNamespace(
        config=SimpleNamespace(repo=""),
        _append_action_result=MagicMock(),
        _do_write_file=MagicMock(),
    )
    changed: list = []
    events = list(dispatch_local_action(session, act, "a1", False, changed))
    assert "No workspace directory" in events[0].data.get("error", "")
    session._do_write_file.assert_not_called()
    assert changed == []


def test_dispatch_local_action_write_file_success_tracks_changed(tmp_path):
    target = tmp_path / "a.py"
    act = PilotAction(kind="write_file", path="a.py", content="hello")
    session = SimpleNamespace(
        config=SimpleNamespace(repo=str(tmp_path)),
        harness_session_id="sess",
        _checkpoints=SimpleNamespace(snapshot=MagicMock(return_value=None)),
        _do_write_file=MagicMock(
            side_effect=[(True, "ok", None), (True, "ok", 5)]
        ),
        _append_action_result=MagicMock(),
    )
    changed: list = []
    events = list(dispatch_local_action(session, act, "a9", True, changed))
    assert events[0].kind == "action_result"
    assert events[0].data["types"] == ["file"]
    assert changed == [str(target)]
    assert session._do_write_file.call_count == 2


def test_dispatch_local_action_run_command_blocked():
    act = PilotAction(kind="run_command", command="rm -rf /")
    session = SimpleNamespace(
        config=SimpleNamespace(repo="/repo"),
        _do_run_command=MagicMock(
            return_value=(
                False,
                "blocked",
                {
                    "message": "blocked destructive",
                    "category": "destructive",
                    "command_hash": "a" * 64,
                },
            )
        ),
        register_pending_command_approval=MagicMock(return_value={
            "session_id": "session-a",
            "workspace_root": "/repo",
        }),
        _append_action_result=MagicMock(),
    )
    events = list(dispatch_local_action(session, act, "a3", True, []))
    assert events[0].kind == "command_approval_pending"
    assert events[0].data["command"] == "rm -rf /"
    assert events[0].data["command_hash"] == "a" * 64
    session.register_pending_command_approval.assert_called_once()
    session._append_action_result.assert_called_once()


def test_dispatch_local_action_call_mcp_unavailable():
    act = PilotAction(kind="call_mcp", tool="x.y", arguments={})
    session = SimpleNamespace(
        _mcp=None,
        _append_action_result=MagicMock(),
    )
    events = list(dispatch_local_action(session, act, "a4", False, []))
    assert events[0].data.get("error") == "MCP not available"


def test_dispatch_local_action_search_tools_success():
    act = PilotAction(kind="search_tools", query="browser")
    session = SimpleNamespace(
        _do_search_tools=MagicMock(return_value=(True, "ok", "found browser_*")),
        _append_action_result=MagicMock(),
    )
    events = list(dispatch_local_action(session, act, "a5", True, []))
    assert events[0].data["types"] == ["search_tools"]
    session._append_action_result.assert_called_once()


def test_run_auto_verify_skips_when_no_changed_files():
    session = SimpleNamespace(
        config=SimpleNamespace(auto_verify=True, repo="/repo", verify_command=""),
        _cancel=threading.Event(),
        _history=[],
    )
    gen = run_auto_verify(
        session,
        turn_changed_files=[],
        auto_verify_iters=0,
        auto_verify_cap=2,
        plan=False,
    )
    try:
        next(gen)
        raise AssertionError("expected silent return")
    except StopIteration as stop:
        iters, again = stop.value
    assert iters == 0
    assert again is False


def test_run_auto_verify_retries_on_failure(monkeypatch):
    import harness.verify as verify_mod

    cancel = threading.Event()
    session = SimpleNamespace(
        config=SimpleNamespace(
            auto_verify=True, repo="/repo", verify_command="pytest -q"
        ),
        _cancel=cancel,
        _history=[],
    )
    monkeypatch.setattr(
        verify_mod,
        "run_verify",
        lambda *a, **k: (False, "FAILED tests/test_x.py"),
    )
    monkeypatch.setattr(
        verify_mod, "_command_display", lambda cmd: str(cmd), raising=False
    )

    events = []
    gen = run_auto_verify(
        session,
        turn_changed_files=["a.py"],
        auto_verify_iters=0,
        auto_verify_cap=2,
        plan=False,
    )
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        iters, again = stop.value
    assert again is True
    assert iters == 1
    assert [e.kind for e in events] == ["verifying", "auto_verify"]
    assert events[1].data["passed"] is False
    assert session._history[-1]["role"] == "user"
    assert "[auto-verify]" in session._history[-1]["content"]

