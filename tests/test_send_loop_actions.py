"""Characterization tests for the send_loop_actions action-spree helper."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import harness.send_loop_actions as send_loop_actions
from harness.pilot import PilotAction, PilotTurn
from harness.send_loop_actions import execute_turn_actions


def _turn_session(*, no_delegation=False, **extra):
    return SimpleNamespace(
        _turn_guard_state=None,
        _cancel=threading.Event(),
        _steer_pending=False,
        _history=[],
        _pending_advisor_warnings=[],
        _append_action_result=MagicMock(),
        _check_and_inject_steer=MagicMock(return_value=iter(())),
        _turn_economy=SimpleNamespace(enforce_tool_batch=lambda msgs: None),
        config=SimpleNamespace(repo="/tmp/r", swarm_adapter="local", no_delegation=no_delegation),
        pilot=MagicMock(),
        **extra,
    )


def _run_actions(session, actions, *, plan, user_message, counters=None):
    gen = execute_turn_actions(
        session,
        turn=PilotTurn(say="", thinking="", actions=actions),
        user_message=user_message,
        is_native=True,
        plan=plan,
        counters=counters if counters is not None else {"action_seq": 0, "swarms": 0, "demo_swarms": 0},
        step=0,
        turn_findings=[],
    )
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration as stop:
        return events, stop.value


def test_execute_turn_actions_plan_mode_skips_mutating_tools():
    act = PilotAction(kind="write_file", path="x.py", content="hi")
    session = _turn_session()
    counters = {"action_seq": 0, "swarms": 0, "demo_swarms": 0}
    events, (disposition, changed) = _run_actions(
        session, [act], plan=True, user_message="edit it", counters=counters,
    )
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
    session = _turn_session(_mcp=MagicMock())
    events, _ = _run_actions(session, actions, plan=True, user_message="wire mcp")
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
    browser = MagicMock()
    session = _turn_session(_browser=browser)
    events, _ = _run_actions(session, actions, plan=True, user_message="browse in plan")
    results = [e for e in events if e.kind == "action_result"]
    assert len(results) == 3
    assert "plan mode: skipped browser_navigate" in results[0].data.get("error", "")
    assert "plan mode: skipped browser_click" in results[1].data.get("error", "")
    assert "plan mode: skipped browser_screenshot" in results[2].data.get("error", "")
    assert browser.mock_calls == []


def test_execute_turn_actions_no_delegation_blocks_swarm():
    act = PilotAction(kind="run_swarm", goal="explore the loop")
    session = _turn_session(no_delegation=True)
    events, (disposition, _changed) = _run_actions(
        session, [act], plan=False, user_message="swarm it",
    )
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


def _action_session(*, no_delegation=False, worker_available=True):
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
        _worker_delegation_available=lambda: worker_available,
        pilot=MagicMock(),
    )


def test_unavailable_worker_route_refuses_dispatch_without_leaf_identity(monkeypatch):
    dispatched = MagicMock(side_effect=AssertionError("must not dispatch"))
    monkeypatch.setattr(send_loop_actions, "dispatch_swarm_action", dispatched)
    session = _action_session(no_delegation=False, worker_available=False)
    events = list(execute_turn_actions(
        session,
        turn=PilotTurn(
            say="",
            thinking="",
            actions=[PilotAction(kind="run_swarm", goal="review the module")],
        ),
        user_message="review the module",
        is_native=True,
        plan=False,
        counters={"action_seq": 0, "swarms": 0, "demo_swarms": 0},
        step=0,
        turn_findings=[],
    ))
    result = next(event for event in events if event.kind == "action_result")
    assert "no working worker route" in result.data.get("error", "")
    assert session.config.no_delegation is False
    dispatched.assert_not_called()


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
