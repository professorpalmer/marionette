"""Tests for 'full gas' pilot orchestration capabilities: run_implement, run_parallel, and route_task."""
import pytest
from unittest.mock import MagicMock, patch
import json
import tempfile
import sys

from harness.pilot import (
    build_tools_schema,
    parse_tool_calls,
    parse_inline_tool_calls,
    _coerce_actions,
    PilotAction,
    PilotTurn,
    parse_pilot_turn
)
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession, ConvEvent


def test_build_tools_schema_has_new_tools():
    schemas = build_tools_schema()
    names = [s["function"]["name"] for s in schemas if s.get("type") == "function"]
    
    assert "run_implement" in names
    assert "run_parallel" in names
    assert "route_task" in names
    
    # Assert schemas details
    impl_schema = next(s for s in schemas if s["function"]["name"] == "run_implement")
    assert "goal" in impl_schema["function"]["parameters"]["required"]
    
    parallel_schema = next(s for s in schemas if s["function"]["name"] == "run_parallel")
    assert "goals" in parallel_schema["function"]["parameters"]["required"]
    
    route_schema = next(s for s in schemas if s["function"]["name"] == "route_task")
    assert "instruction" in route_schema["function"]["parameters"]["required"]


def test_parse_tool_calls_new_tools():
    # 1. run_implement
    tc_impl = [{
        "id": "tc_1",
        "type": "function",
        "function": {
            "name": "run_implement",
            "arguments": json.dumps({"goal": "Add feature X", "adapter": "hermes"})
        }
    }]
    actions_impl = parse_tool_calls(tc_impl)
    assert len(actions_impl) == 1
    assert actions_impl[0].kind == "run_implement"
    assert actions_impl[0].goal == "Add feature X"
    assert actions_impl[0].adapter == "hermes"

    # 2. run_parallel
    tc_parallel = [{
        "id": "tc_2",
        "type": "function",
        "function": {
            "name": "run_parallel",
            "arguments": json.dumps({"goals": ["Add tests", "Fix lint"], "adapter": "cursor", "mode": "implement"})
        }
    }]
    actions_parallel = parse_tool_calls(tc_parallel)
    assert len(actions_parallel) == 1
    assert actions_parallel[0].kind == "run_parallel"
    assert actions_parallel[0].goals == ["Add tests", "Fix lint"]
    assert actions_parallel[0].adapter == "cursor"
    assert actions_parallel[0].mode == "implement"

    # 3. route_task
    tc_route = [{
        "id": "tc_3",
        "type": "function",
        "function": {
            "name": "route_task",
            "arguments": json.dumps({"instruction": "write tests", "role": "explore"})
        }
    }]
    actions_route = parse_tool_calls(tc_route)
    assert len(actions_route) == 1
    assert actions_route[0].kind == "route_task"
    assert actions_route[0].instruction == "write tests"
    assert actions_route[0].arguments.get("role") == "explore"


def test_parse_inline_tool_calls_new_tools():
    content = (
        "Let's implement this:\n"
        "<tool_call>{\"name\": \"run_implement\", \"arguments\": {\"goal\": \"Refactor billing\"}}</tool_call>"
    )
    acts = parse_inline_tool_calls(content)
    assert len(acts) == 1
    assert acts[0].kind == "run_implement"
    assert acts[0].goal == "Refactor billing"


def test_run_swarm_remains_read_only():
    tc = [{
        "id": "tc_swarm",
        "type": "function",
        "function": {
            "name": "run_swarm",
            "arguments": json.dumps({"goal": "investigate memory leak", "roles": ["explore"]})
        }
    }]
    acts = parse_tool_calls(tc)
    assert len(acts) == 1
    assert acts[0].kind == "run_swarm"
    assert acts[0].goal == "investigate memory leak"
    assert acts[0].roles == ["explore"]


@patch("subprocess.Popen")
@patch("subprocess.run")
def test_executor_smoke_run_implement(mock_run, mock_popen):
    # Set up config
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    cfg.repo = "/mock/repo"
    
    # Mock subprocess.Popen for start command
    mock_p = MagicMock()
    mock_p.stdout = ["Started job job_1234567890ab"]
    mock_popen.return_value = mock_p
    
    # Mock subprocess.run for await and artifacts
    mock_res_await = MagicMock()
    mock_res_await.return_value = MagicMock(returncode=0)
    
    mock_res_art = MagicMock()
    mock_res_art.stdout = json.dumps([
        {
            "job_id": "job_1234567890ab",
            "type": "patch",
            "payload": {
                "files": ["src/main.py"],
                "unified_diff": "diff src/main.py..."
            }
        }
    ])
    mock_run.side_effect = [mock_res_await, mock_res_art]
    
    session = ConversationalSession(cfg)
    
    # We inject our detect function mock to always return "hermes"
    session._detect_default_implement_adapter = MagicMock(return_value="hermes")
    
    # Send a prompt triggering a pilot action
    from harness.pilot import PilotAction
    action = PilotAction(kind="run_implement", goal="Add print statement")
    
    # Directly invoke our send logic or trigger actions processing
    # Let's mock a pilot turn that returns this action
    class FakePilot:
        name = "fake"
        def __init__(self):
            self.calls = 0
        def complete(self, prompt, *, system=None):
            from pmharness.drivers.openai_compat import DriverResponse
            self.calls += 1
            if self.calls == 1:
                txt = '{"say": "Starting implement worker.", "actions": [{"kind": "run_implement", "goal": "Add print statement"}]}'
            else:
                txt = '{"say": "Done.", "actions": []}'
            return DriverResponse(text=txt, tokens_out=10, latency_ms=1.0)
            
    session.pilot = FakePilot()
    events = list(session.send("Implement something!"))
    
    # Verify events
    kinds = [e.kind for e in events]
    assert "action_start" in kinds
    assert "action_result" in kinds
    
    # Verify subprocess was called with correct arguments
    mock_popen.assert_called_once()
    args, kwargs = mock_popen.call_args
    cmd = args[0]
    assert "puppetmaster" in cmd
    assert "hermes" in cmd
    assert "Add print statement" in cmd
    assert "--cwd" in cmd
    assert "/mock/repo" in cmd
    assert "--mode" in cmd
    assert "implement" in cmd


@patch("subprocess.Popen")
@patch("subprocess.run")
def test_executor_smoke_run_parallel(mock_run, mock_popen):
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    cfg.repo = "/mock/repo"
    
    mock_p = MagicMock()
    mock_p.stdout = ["Started job job_abcdef123456"]
    mock_popen.return_value = mock_p
    
    mock_res_await = MagicMock()
    mock_res_await.return_value = MagicMock(returncode=0)
    
    mock_res_art = MagicMock()
    mock_res_art.stdout = json.dumps([
        {
            "job_id": "job_abcdef123456",
            "type": "finding",
            "payload": {
                "report": "Analysis results"
            }
        }
    ])
    def _run_side(*a, **k):
        # await calls return rc=0; artifacts calls return the json; any extra
        # call (e.g. platform status) returns a benign empty result.
        argv = a[0] if a else k.get("args", [])
        if isinstance(argv, (list, tuple)) and "artifacts" in argv:
            return mock_res_art
        return mock_res_await
    mock_run.side_effect = _run_side
    
    session = ConversationalSession(cfg)
    session._detect_default_implement_adapter = MagicMock(return_value="hermes")
    
    class FakeParallelPilot:
        name = "fake"
        def __init__(self):
            self.calls = 0
        def complete(self, prompt, *, system=None):
            from pmharness.drivers.openai_compat import DriverResponse
            self.calls += 1
            if self.calls == 1:
                txt = '{"say": "Running in parallel.", "actions": [{"kind": "run_parallel", "goals": ["Audit auth", "Audit cache"], "mode": "analysis"}]}'
            else:
                txt = '{"say": "Done.", "actions": []}'
            return DriverResponse(text=txt, tokens_out=10, latency_ms=1.0)
            
    session.pilot = FakeParallelPilot()
    events = list(session.send("Run parallel checks!"))
    
    # Let's verify our processes were fanned out
    assert mock_popen.call_count == 2
    # Verify aggregate result is returned
    kinds = [e.kind for e in events]
    assert "action_result" in kinds
