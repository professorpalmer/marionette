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

import harness.send_loop_actions as send_loop_actions
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


def test_execute_turn_actions_plan_mode_skips_browser_tools():
    """Plan mode must block browser_* (MCP-sibling external side-effect peel)."""
    actions = [
        PilotAction(kind="browser_navigate", url="https://example.com"),
        PilotAction(kind="browser_click", arguments={"ref": "e1"}),
        PilotAction(kind="browser_screenshot"),
    ]
    turn = PilotTurn(say="", thinking="", actions=actions)
    browser = MagicMock()
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
        _browser=browser,
    )
    events = []
    gen = execute_turn_actions(
        session,
        turn=turn,
        user_message="browse in plan",
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
    assert len(results) == 3
    assert "plan mode: skipped browser_navigate" in results[0].data.get("error", "")
    assert "plan mode: skipped browser_click" in results[1].data.get("error", "")
    assert "plan mode: skipped browser_screenshot" in results[2].data.get("error", "")
    assert browser.mock_calls == []


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


def test_execute_turn_actions_counts_attempted_synchronous_swarm(monkeypatch):
    act = PilotAction(kind="run_swarm", goal="reuse the audit")
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

    def fake_dispatch(*args, **kwargs):
        if False:
            yield None
        return None

    monkeypatch.setattr(send_loop_actions, "dispatch_swarm_action", fake_dispatch)
    counters = {"action_seq": 0, "swarms": 0, "demo_swarms": 0}

    list(execute_turn_actions(
        session,
        turn=turn,
        user_message="reuse it",
        is_native=True,
        plan=False,
        counters=counters,
        step=0,
        turn_findings=[],
    ))

    assert counters["swarms"] == 0
    assert counters["synchronous_swarms"] == 1


_SCREENSHOT_AGENTIC_CLI = (
    'puppetmaster agentic "Add token-level streaming" '
    "--provider opencode-go --model deepseek/deepseek-v4-pro"
)
_SCREENSHOT_WRAPPED_AGENTIC_CLI = (
    "cd /Users/carypalmer/Projects/agent-discord-deepseek && puppetmaster agentic "
    '"Add token-level streaming to puppetmaster backend; parse NDJSON lines '
    'and dispatch swarm workers" --provider opencode-go '
    "--model deepseek/deepseek-v4-pro "
    "--cwd /Users/carypalmer/Projects/agent-discord-deepseek | tail -n 30"
)
_SCREENSHOT_WRAPPED_GOAL = (
    "Add token-level streaming to puppetmaster backend; "
    "parse NDJSON lines and dispatch swarm workers"
)
_GLM53_SWARM_PROMPT = (
    "yeah, puppetmaster swarm glm 5.3 multi-workers via openrouter"
)


def _action_session(*, no_delegation=False):
    return SimpleNamespace(
        _turn_guard_state=None,
        _cancel=threading.Event(),
        _steer_pending=False,
        _history=[],
        _pending_advisor_warnings=[],
        _pending_swarm_mandate=None,
        _pending_swarm_active=False,
        _do_run_command=MagicMock(side_effect=AssertionError("must not shell CLI")),
        _append_action_result=MagicMock(),
        _check_and_inject_steer=MagicMock(return_value=iter(())),
        _turn_economy=SimpleNamespace(enforce_tool_batch=lambda msgs: None),
        config=SimpleNamespace(
            repo="/tmp/r", swarm_adapter="local", no_delegation=no_delegation,
        ),
        pilot=MagicMock(),
    )


def test_execute_translates_screenshot_agentic_cli_to_run_swarm(monkeypatch):
    """A: screenshot-style agentic CLI under explicit swarm becomes run_swarm."""
    captured = []

    def fake_dispatch(session, act, aid, is_native, *, counters, turn_findings):
        captured.append((act, aid, is_native))
        if False:
            yield None
        return None

    monkeypatch.setattr(send_loop_actions, "dispatch_swarm_action", fake_dispatch)
    local = MagicMock(side_effect=AssertionError("must not dispatch_local_action"))
    monkeypatch.setattr(send_loop_actions, "dispatch_local_action", local)

    act = PilotAction(
        kind="run_command",
        command=_SCREENSHOT_AGENTIC_CLI,
        tool_call_id="call_screenshot",
    )
    session = _action_session()
    events = list(execute_turn_actions(
        session,
        turn=PilotTurn(say="", thinking="", actions=[act]),
        user_message=_GLM53_SWARM_PROMPT,
        is_native=True,
        plan=False,
        counters={"action_seq": 0, "swarms": 0, "demo_swarms": 0},
        step=0,
        turn_findings=[],
    ))

    assert len(captured) == 1
    translated, aid, _native = captured[0]
    assert translated.kind == "run_swarm"
    assert translated.goal == "Add token-level streaming"
    assert translated.model == "opencode-go/deepseek/deepseek-v4-pro"
    assert translated.adapter == "agentic"
    assert translated.tool_call_id == "call_screenshot"
    assert aid == "call_screenshot"
    starts = [e for e in events if e.kind == "action_start"]
    assert starts and starts[0].data.get("kind") == "run_swarm"
    assert "Add token-level streaming" in (starts[0].data.get("goal") or "")
    session._do_run_command.assert_not_called()
    local.assert_not_called()
    assert session._pending_swarm_mandate is None


def test_execute_translates_wrapped_screenshot_agentic_cli_to_run_swarm(monkeypatch):
    """Wrapped screenshot CLI under explicit swarm becomes run_swarm, never shells."""
    captured = []

    def fake_dispatch(session, act, aid, is_native, *, counters, turn_findings):
        captured.append((act, aid, is_native))
        if False:
            yield None
        return None

    monkeypatch.setattr(send_loop_actions, "dispatch_swarm_action", fake_dispatch)
    local = MagicMock(side_effect=AssertionError("must not dispatch_local_action"))
    monkeypatch.setattr(send_loop_actions, "dispatch_local_action", local)

    act = PilotAction(
        kind="run_command",
        command=_SCREENSHOT_WRAPPED_AGENTIC_CLI,
        tool_call_id="call_wrapped_screenshot",
    )
    session = _action_session()
    events = list(execute_turn_actions(
        session,
        turn=PilotTurn(say="", thinking="", actions=[act]),
        user_message=_GLM53_SWARM_PROMPT,
        is_native=True,
        plan=False,
        counters={"action_seq": 0, "swarms": 0, "demo_swarms": 0},
        step=0,
        turn_findings=[],
    ))

    assert len(captured) == 1
    translated, aid, _native = captured[0]
    assert translated.kind == "run_swarm"
    assert translated.goal == _SCREENSHOT_WRAPPED_GOAL
    assert translated.model == "opencode-go/deepseek/deepseek-v4-pro"
    assert translated.adapter == "agentic"
    assert translated.tool_call_id == "call_wrapped_screenshot"
    assert aid == "call_wrapped_screenshot"
    starts = [e for e in events if e.kind == "action_start"]
    assert starts and starts[0].data.get("kind") == "run_swarm"
    assert _SCREENSHOT_WRAPPED_GOAL in (starts[0].data.get("goal") or "")
    session._do_run_command.assert_not_called()
    local.assert_not_called()


def test_execute_translates_agentic_cli_to_run_implement_off_swarm(monkeypatch):
    """B: same agentic CLI on a non-swarm implement turn maps to run_implement."""
    captured = []

    def fake_implement(session, act, aid, is_native, **kwargs):
        captured.append((act, aid))
        if False:
            yield None
        return None

    monkeypatch.setattr(send_loop_actions, "dispatch_implement_action", fake_implement)
    monkeypatch.setattr(
        send_loop_actions,
        "dispatch_swarm_action",
        MagicMock(side_effect=AssertionError("must not swarm")),
    )
    monkeypatch.setattr(
        send_loop_actions,
        "dispatch_local_action",
        MagicMock(side_effect=AssertionError("must not shell")),
    )

    act = PilotAction(
        kind="run_command",
        command=_SCREENSHOT_AGENTIC_CLI,
        tool_call_id="call_impl",
    )
    session = _action_session()
    list(execute_turn_actions(
        session,
        turn=PilotTurn(say="", thinking="", actions=[act]),
        user_message="implement token-level streaming",
        is_native=True,
        plan=False,
        counters={"action_seq": 0, "swarms": 0, "demo_swarms": 0},
        step=0,
        turn_findings=[],
    ))

    assert len(captured) == 1
    translated, aid = captured[0]
    assert translated.kind == "run_implement"
    assert translated.goal == "Add token-level streaming"
    assert translated.model == "opencode-go/deepseek/deepseek-v4-pro"
    assert translated.adapter == "agentic"
    assert translated.tool_call_id == "call_impl"
    assert aid == "call_impl"
    session._do_run_command.assert_not_called()


def test_execute_does_not_auto_convert_status_or_artifacts(monkeypatch):
    """C: status/artifacts stay history redirects and never run the shell."""
    swarm = MagicMock(side_effect=AssertionError("must not swarm"))
    implement = MagicMock(side_effect=AssertionError("must not implement"))
    monkeypatch.setattr(send_loop_actions, "dispatch_swarm_action", swarm)
    monkeypatch.setattr(send_loop_actions, "dispatch_implement_action", implement)
    monkeypatch.setattr(
        send_loop_actions,
        "dispatch_local_action",
        MagicMock(side_effect=AssertionError("must not shell")),
    )

    session = _action_session()
    events = list(execute_turn_actions(
        session,
        turn=PilotTurn(say="", thinking="", actions=[
            PilotAction(kind="run_command", command="puppetmaster status"),
            PilotAction(kind="run_command", command="python -m puppetmaster artifacts"),
        ]),
        user_message=_GLM53_SWARM_PROMPT,
        is_native=True,
        plan=False,
        counters={"action_seq": 0, "swarms": 0, "demo_swarms": 0},
        step=0,
        turn_findings=[],
    ))
    errors = [e.data.get("error", "") for e in events if e.kind == "action_result"]
    assert len(errors) == 2
    assert all("REDIRECT" in (err or "") for err in errors)
    assert all("search_state" in (err or "") or "action_result" in (err or "") for err in errors)
    session._do_run_command.assert_not_called()
    swarm.assert_not_called()
    implement.assert_not_called()
