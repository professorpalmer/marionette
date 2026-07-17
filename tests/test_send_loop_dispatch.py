"""Characterization tests for send_loop_dispatch peel from _send_locked_inner.

Locks the extracted swarm / implement / parallel / route_task / memory helper
contracts and asserts those branches no longer live inline in the turn kernel.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from harness.pilot import PilotAction
from harness.send_loop import SendLoopMixin
from harness.send_loop_dispatch import (
    DISPATCH_ACTION_KINDS,
    dispatch_implement_action,
    dispatch_memory_action,
    dispatch_parallel_action,
    dispatch_route_task_action,
    dispatch_swarm_action,
)

DISPATCH_HELPERS = (
    "dispatch_swarm_action",
    "dispatch_implement_action",
    "dispatch_parallel_action",
    "dispatch_route_task_action",
    "dispatch_memory_action",
)


def _inner_source() -> str:
    src = Path(inspect.getsourcefile(SendLoopMixin)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "SendLoopMixin":
            for item in node.body:
                if (
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name == "_send_locked_inner"
                ):
                    return ast.get_source_segment(src, item) or ""
    raise AssertionError("_send_locked_inner not found")


def test_dispatch_helpers_are_module_level_callables():
    import harness.send_loop_dispatch as dispatch

    for name in DISPATCH_HELPERS:
        fn = getattr(dispatch, name)
        assert callable(fn)
        assert fn.__module__ == "harness.send_loop_dispatch"


def test_dispatch_action_kinds_covers_delegate_surface():
    assert DISPATCH_ACTION_KINDS == frozenset(
        {"run_swarm", "run_implement", "run_parallel", "route_task", "memory"}
    )
    # Read-only / local stay in send_loop_phases.
    assert "read_file" not in DISPATCH_ACTION_KINDS
    assert "write_file" not in DISPATCH_ACTION_KINDS
    assert "run_command" not in DISPATCH_ACTION_KINDS


def test_mixin_still_owns_public_orchestration_surface():
    for name in ("send", "_send_locked", "_send_locked_inner"):
        attr = getattr(SendLoopMixin, name)
        assert attr.__qualname__ == f"SendLoopMixin.{name}"


def test_mixin_calls_dispatch_helpers():
    # Kernel fans out via execute_turn_actions; dispatch helpers are invoked there.
    src = Path(inspect.getsourcefile(SendLoopMixin)).read_text(encoding="utf-8")
    assert "execute_turn_actions(" in src
    actions_src = Path("harness/send_loop_actions.py").read_text(encoding="utf-8")
    for name in DISPATCH_HELPERS:
        assert f"{name}(" in actions_src, name
    assert "DISPATCH_ACTION_KINDS" in actions_src


def test_send_locked_inner_no_longer_inlines_dispatch_branches():
    segment = _inner_source()
    assert "---- swarm branch" not in segment
    assert "---- run_implement branch" not in segment
    assert "---- run_parallel branch" not in segment
    assert "---- route_task branch" not in segment
    assert "---- memory branch" not in segment
    assert "execute_turn_actions(" in segment
    actions_src = Path("harness/send_loop_actions.py").read_text(encoding="utf-8")
    assert "dispatch_swarm_action(" in actions_src
    assert "dispatch_implement_action(" in actions_src
    assert "dispatch_parallel_action(" in actions_src
    assert "dispatch_route_task_action(" in actions_src
    assert "dispatch_memory_action(" in actions_src
    assert "DISPATCH_ACTION_KINDS" in actions_src


def test_send_locked_inner_no_longer_nests_dispatch_helpers():
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
    assert not (nested_names & set(DISPATCH_HELPERS)), nested_names & set(DISPATCH_HELPERS)
    assert "execute_turn_actions" not in nested_names


def test_dispatch_route_task_requires_cli():
    act = PilotAction(kind="route_task", instruction="pick a model", arguments={})
    session = SimpleNamespace(
        _append_action_result=MagicMock(),
    )
    import harness.send_loop_dispatch as dispatch

    original = dispatch._puppetmaster_available
    dispatch._puppetmaster_available = lambda: False
    try:
        events = list(dispatch_route_task_action(session, act, "a1", True))
    finally:
        dispatch._puppetmaster_available = original
    assert events[0].kind == "action_result"
    assert "puppetmaster CLI not available" in events[0].data.get("error", "")
    session._append_action_result.assert_called_once()


def test_dispatch_memory_add_queues_in_interactive():
    act = PilotAction(
        kind="memory",
        memory_action="add",
        memory_content="remember the bake flag",
        memory_category="prefs",
    )
    session = SimpleNamespace(
        _auto_mode=False,
        _turn_memory_queue=[],
        _append_action_result=MagicMock(),
    )
    events = list(dispatch_memory_action(session, act, "a2", True))
    assert events[0].kind == "action_result"
    assert events[0].data["types"] == ["memory"]
    assert len(session._turn_memory_queue) == 1
    assert session._turn_memory_queue[0]["text"] == "remember the bake flag"
    session._append_action_result.assert_called_once()


def test_dispatch_memory_add_refuses_in_autopilot():
    act = PilotAction(
        kind="memory",
        memory_action="add",
        memory_content="should not persist",
        memory_category="prefs",
    )
    session = SimpleNamespace(
        _auto_mode=True,
        _turn_memory_queue=[],
        _append_action_result=MagicMock(),
    )
    events = list(dispatch_memory_action(session, act, "a3", False))
    assert events[0].data["types"] == ["memory"]
    assert session._turn_memory_queue == []
    appended = session._append_action_result.call_args[0][2]
    assert "Autopilot" in appended


def test_dispatch_implement_requires_workspace():
    act = PilotAction(kind="run_implement", goal="add a test")
    session = SimpleNamespace(
        config=SimpleNamespace(repo=""),
        _append_action_result=MagicMock(),
        _validate_target_repo=MagicMock(),
    )
    gen = dispatch_implement_action(
        session,
        act,
        "a4",
        True,
        turn_actions=[act],
        action_idx=0,
        action_seq=1,
        step=0,
        swarms=0,
    )
    events = list(gen)
    assert any(e.kind == "action_result" and "No workspace" in e.data.get("error", "") for e in events)
    session._append_action_result.assert_called_once()


def test_dispatch_parallel_requires_goals():
    act = PilotAction(kind="run_parallel", goals=[])
    session = SimpleNamespace(
        config=SimpleNamespace(repo="/repo"),
        _append_action_result=MagicMock(),
        _validate_target_repo=MagicMock(),
    )
    events = list(
        dispatch_parallel_action(
            session,
            act,
            "a5",
            True,
            turn_actions=[act],
            action_idx=0,
            action_seq=1,
            step=0,
            swarms=0,
        )
    )
    assert "non-empty goals" in events[0].data.get("error", "")
    session._append_action_result.assert_called_once()


def test_dispatch_swarm_registers_and_surfaces_error():
    act = PilotAction(kind="run_swarm", goal="audit the peel", roles=["explore"])
    session = SimpleNamespace(
        config=SimpleNamespace(repo="/repo"),
        _session_job_ids=[],
        _register_local_job=MagicMock(),
        _finish_local_job=MagicMock(),
        _append_action_result=MagicMock(),
        _display_transcript=[],
    )
    import harness.send_loop_dispatch as dispatch

    def boom(session, intent, q):
        q.put(("error", RuntimeError("stream failed")))

    original = dispatch.stream_swarm
    dispatch.stream_swarm = boom
    try:
        counters = {"swarms": 0, "demo_swarms": 0}
        events = list(
            dispatch_swarm_action(
                session,
                act,
                "a6",
                True,
                counters=counters,
                turn_findings=[],
            )
        )
    finally:
        dispatch.stream_swarm = original
    kinds = [e.kind for e in events]
    assert "swarm_pending" in kinds
    assert "action_result" in kinds
    assert counters["swarms"] == 0
    session._finish_local_job.assert_called()
    session._append_action_result.assert_called_once()


# ---------------------------------------------------------------------------
# Swarm quality gate: thin/generic findings must not read as a green success.

def test_substantive_artifact_gate():
    from harness.send_loop_dispatch import _is_substantive_artifact

    # Generic one-liner stubs: not substantive.
    assert not _is_substantive_artifact({"type": "finding", "headline": "Audit complete."})
    assert not _is_substantive_artifact({"type": "finding", "headline": "No issues found in the repository."})

    # Short but file-backed: substantive.
    assert _is_substantive_artifact(
        {"type": "finding", "headline": "Unbounded cache growth in harness/server.py line 210"})
    assert _is_substantive_artifact(
        {"type": "risk", "headline": "webapp/src/lib/api.ts:88 swallows fetch errors silently"})

    # Long prose without a path: substantive (real analysis parked in body).
    assert _is_substantive_artifact({
        "type": "finding",
        "headline": "clip",
        "body": "x" * 250,
    })

    # Malformed payloads never crash the gate closed.
    assert _is_substantive_artifact({"type": "finding", "body": None, "headline": None}) in (True, False)
