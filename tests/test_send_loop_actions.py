"""Characterization tests for send_loop_actions peel from _send_locked_inner.

Locks the extracted action-spree helper contract and asserts guard / prefetch /
advisor / dispatch fan-out no longer live inline in the turn kernel.
"""

from __future__ import annotations

import ast
import inspect
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from harness.pilot import PilotAction, PilotTurn
from harness.send_loop import SendLoopMixin
from harness.send_loop_actions import execute_turn_actions

ACTION_HELPERS = ("execute_turn_actions",)


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


def test_action_helpers_are_module_level_callables():
    import harness.send_loop_actions as actions

    for name in ACTION_HELPERS:
        fn = getattr(actions, name)
        assert callable(fn)
        assert fn.__module__ == "harness.send_loop_actions"


def test_mixin_still_owns_public_orchestration_surface():
    for name in ("send", "_send_locked", "_send_locked_inner"):
        attr = getattr(SendLoopMixin, name)
        assert attr.__qualname__ == f"SendLoopMixin.{name}"


def test_mixin_calls_execute_turn_actions():
    src = Path(inspect.getsourcefile(SendLoopMixin)).read_text(encoding="utf-8")
    assert "execute_turn_actions(" in src
    assert "from .send_loop_actions import execute_turn_actions" in src


def test_send_locked_inner_no_longer_inlines_action_spree():
    segment = _inner_source()
    assert "Carry swarm-gate redirect progress" not in segment
    assert "Advisor pass (round 6" not in segment
    assert "Kernel-force native Puppetmaster verbs" not in segment
    assert "---- read-only tool-result assembly" not in segment
    assert "---- local tool-result assembly" not in segment
    assert "---- delegate / swarm / memory tool-result assembly" not in segment
    assert "execute_turn_actions(" in segment


def test_send_locked_inner_no_longer_nests_action_helpers():
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
    assert not (nested_names & set(ACTION_HELPERS)), nested_names & set(ACTION_HELPERS)


def test_execute_turn_actions_plan_mode_skips_mutating_tools():
    act = PilotAction(kind="write_file", path="x.py", content="hi")
    turn = PilotTurn(say="", thinking="", actions=[act])
    session = SimpleNamespace(
        _turn_guard_state=None,
        _cancel=threading.Event(),
        _steer_pending=False,
        _history=[],
        _pending_advisor_warnings=[],
        _append_action_result=MagicMock(),
        _check_and_inject_steer=MagicMock(return_value=iter(())),
        _turn_economy=SimpleNamespace(enforce_tool_batch=lambda msgs: None),
        config=SimpleNamespace(repo="/tmp/r", swarm_adapter="local", no_delegation=False),
        pilot=MagicMock(),
    )
    counters = {"action_seq": 0, "swarms": 0, "demo_swarms": 0}
    events = []
    gen = execute_turn_actions(
        session,
        turn=turn,
        user_message="edit it",
        is_native=True,
        plan=True,
        counters=counters,
        step=0,
        turn_findings=[],
    )
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        disposition, changed = stop.value
    assert disposition is None
    assert changed == []
    assert counters["action_seq"] == 1
    kinds = [e.kind for e in events]
    assert "action_start" in kinds
    assert "action_result" in kinds
    result = next(e for e in events if e.kind == "action_result")
    assert "plan mode: skipped write_file" in result.data.get("error", "")
    session._append_action_result.assert_called_once()


def test_execute_turn_actions_plan_mode_skips_mcp_mutate_paths():
    """Plan mode must block call_mcp + manage_mcp (same as write/edit).

    Intentional trust gap (documented only): call_mcp is NOT routed through
    command_policy danger approval — plan-mode skip is the gate that stops
    MCP side effects during plan turns. Do not "fix" that by adding
    command_policy wrapping here.
    """
    actions = [
        PilotAction(kind="call_mcp", tool="fake.echo", arguments={"text": "x"}),
        PilotAction(
            kind="manage_mcp",
            arguments={"action": "add", "name": "x", "url": "http://127.0.0.1:9/mcp"},
        ),
    ]
    turn = PilotTurn(say="", thinking="", actions=actions)
    session = SimpleNamespace(
        _turn_guard_state=None,
        _cancel=threading.Event(),
        _steer_pending=False,
        _history=[],
        _pending_advisor_warnings=[],
        _append_action_result=MagicMock(),
        _check_and_inject_steer=MagicMock(return_value=iter(())),
        _turn_economy=SimpleNamespace(enforce_tool_batch=lambda msgs: None),
        config=SimpleNamespace(repo="/tmp/r", swarm_adapter="local", no_delegation=False),
        pilot=MagicMock(),
        _mcp=MagicMock(),
    )
    events = []
    gen = execute_turn_actions(
        session,
        turn=turn,
        user_message="wire mcp",
        is_native=True,
        plan=True,
        counters={"action_seq": 0, "swarms": 0, "demo_swarms": 0},
        step=0,
        turn_findings=[],
    )
    try:
        while True:
            events.append(next(gen))
    except StopIteration:
        pass
    results = [e for e in events if e.kind == "action_result"]
    assert len(results) == 2
    assert "plan mode: skipped call_mcp" in results[0].data.get("error", "")
    assert "plan mode: skipped manage_mcp" in results[1].data.get("error", "")
    session._mcp.call.assert_not_called()
    session._mcp.manage.assert_not_called()


def test_execute_turn_actions_no_delegation_blocks_swarm():
    act = PilotAction(kind="run_swarm", goal="explore the loop")
    turn = PilotTurn(say="", thinking="", actions=[act])
    session = SimpleNamespace(
        _turn_guard_state=None,
        _cancel=threading.Event(),
        _steer_pending=False,
        _history=[],
        _pending_advisor_warnings=[],
        _append_action_result=MagicMock(),
        _check_and_inject_steer=MagicMock(return_value=iter(())),
        _turn_economy=SimpleNamespace(enforce_tool_batch=lambda msgs: None),
        config=SimpleNamespace(repo="/tmp/r", swarm_adapter="local", no_delegation=True),
        pilot=MagicMock(),
    )
    gen = execute_turn_actions(
        session,
        turn=turn,
        user_message="swarm it",
        is_native=True,
        plan=False,
        counters={"action_seq": 0, "swarms": 0, "demo_swarms": 0},
        step=0,
        turn_findings=[],
    )
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        disposition, _changed = stop.value
    assert disposition is None
    result = next(e for e in events if e.kind == "action_result")
    assert "delegation is disabled" in result.data.get("error", "")
