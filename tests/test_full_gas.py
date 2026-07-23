"""Tests for 'full gas' pilot orchestration capabilities: run_implement, run_parallel, and route_task."""
import pytest
import subprocess
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



def _wait_for_swarms(session, timeout=10.0):
    """Wait for background swarm futures to finish, then drain results.
    Under the offload model, run_implement/run_parallel apply the worker patch
    in a background thread; this blocks until that completes so tests can then
    assert the file landed. Returns the list of drained swarm_result events."""
    import time as _t
    deadline = _t.time() + timeout
    while session.has_pending_swarms() and _t.time() < deadline:
        _t.sleep(0.05)
    return list(session.drain_swarm_results())

def test_executor_smoke_run_implement():
    import os
    import shutil
    import subprocess
    from unittest.mock import patch, MagicMock
    
    real_run = subprocess.run
    real_popen = subprocess.Popen
    
    # Set up config with a real temp git repo
    temp_repo = tempfile.mkdtemp()
    try:
        subprocess.run(["git", "init"], cwd=temp_repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=temp_repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=temp_repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        # Commit a base file
        with open(os.path.join(temp_repo, "base.txt"), "w") as f:
            f.write("Line 1")
        subprocess.run(["git", "add", "base.txt"], cwd=temp_repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=temp_repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
        cfg.repo = temp_repo
        
        with patch("subprocess.Popen") as mock_popen, patch("subprocess.run") as mock_run:
            # Mock subprocess.Popen for start command
            mock_p = MagicMock()
            mock_p.stdout = ["Started job job_1234567890ab"]
            
            # Setup conditional Popen side effect
            def popen_side_effect(args, *a, **k):
                is_pm = False
                if isinstance(args, list):
                    is_pm = any("puppetmaster" in str(arg) for arg in args)
                elif isinstance(args, str):
                    is_pm = "puppetmaster" in args
                    
                if is_pm:
                    return mock_p
                return real_popen(args, *a, **k)
                
            mock_popen.side_effect = popen_side_effect
            
            # We need a conditional side effect to allow real git commands
            def side_effect(args, *a, **k):
                is_pm = False
                if isinstance(args, list):
                    is_pm = any("puppetmaster" in str(arg) for arg in args)
                elif isinstance(args, str):
                    is_pm = "puppetmaster" in args
                    
                if is_pm:
                    if "codegraph" in args:
                        return MagicMock(returncode=0, stdout="")
                    if "await" in args:
                        return MagicMock(returncode=0)
                    elif "artifacts" in args:
                        mock_res_art = MagicMock()
                        mock_res_art.stdout = json.dumps([
                            {
                                "job_id": "job_1234567890ab",
                                "type": "patch",
                                "payload": {
                                    "files": ["src/main.py"],
                                    "unified_diff": "diff --git a/src/main.py b/src/main.py\nnew file mode 100644\n--- /dev/null\n+++ b/src/main.py\n@@ -0,0 +1 @@\n+print('hello')\n"
                                }
                            }
                        ])
                        return mock_res_art
                return real_run(args, *a, **k)
                
            mock_run.side_effect = side_effect
            
            session = ConversationalSession(cfg)
            
            # We inject our detect function mock to always return "cursor"
            session._detect_default_implement_adapter = MagicMock(return_value="cursor")
            # This smoke test exercises the EXTERNAL puppetmaster-CLI dispatch path,
            # so force puppetmaster availability and request an external adapter
            # explicitly (the default path is now the provider-native worker, which
            # does not Popen the CLI -- covered by test_run_implement_provider.py).
            import harness.conversation as _convmod
            session_pm_orig = getattr(_convmod, "_puppetmaster_available", None)
            _convmod._puppetmaster_available = lambda: True
            # Force the external CLI adapter to read as available so this test keeps
            # exercising the external dispatch path (the requested CLI is not installed
            # in CI; without this the implement correctly falls back to the
            # provider-native worker, which is covered separately).
            session._external_adapter_available = lambda adapter: True
            
            # Send a prompt triggering a pilot action (explicit external adapter)
            from harness.pilot import PilotAction
            action = PilotAction(kind="run_implement", goal="Add print statement", adapter="cursor")
            
            # Directly invoke our send logic or trigger actions processing
            class FakePilot:
                name = "fake"
                def __init__(self):
                    self.calls = 0
                def complete(self, prompt, *, system=None):
                    from pmharness.drivers.openai_compat import DriverResponse
                    self.calls += 1
                    if self.calls == 1:
                        txt = '{"say": "Starting implement worker.", "actions": [{"kind": "run_implement", "goal": "Add print statement", "adapter": "cursor"}]}'
                    else:
                        txt = '{"say": "Done.", "actions": []}'
                    return DriverResponse(text=txt, tokens_out=10, latency_ms=1.0)
                    
            session.pilot = FakePilot()
            events = list(session.send("Implement something!"))
            
            # Verify events
            kinds = [e.kind for e in events]
            assert "action_start" in kinds
            assert "action_result" in kinds
            # New offload model: the dispatch emits swarm_pending and returns; the
            # patch applies in a background future.
            assert "swarm_pending" in kinds
            
            # Verify mock_popen was called at least once
            assert mock_popen.call_count >= 1
            
            # Wait for the background apply to complete, then assert the file landed.
            _wait_for_swarms(session)
            # The KEY assertion: once the background swarm completes, the file exists.
            target_path = os.path.join(temp_repo, "src/main.py")
            assert os.path.exists(target_path)
            with open(target_path, "r") as f:
                assert f.read() == "print('hello')\n"
            
    finally:
        shutil.rmtree(temp_repo, ignore_errors=True)


def test_executor_smoke_run_parallel():
    import os
    import shutil
    import subprocess
    from unittest.mock import patch, MagicMock
    
    real_run = subprocess.run
    real_popen = subprocess.Popen
    
    temp_repo = tempfile.mkdtemp()
    try:
        subprocess.run(["git", "init"], cwd=temp_repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=temp_repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=temp_repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        with open(os.path.join(temp_repo, "base.txt"), "w") as f:
            f.write("Line 1")
        subprocess.run(["git", "add", "base.txt"], cwd=temp_repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=temp_repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
        cfg.repo = temp_repo
        
        with patch("subprocess.Popen") as mock_popen, patch("subprocess.run") as mock_run:
            mock_p1 = MagicMock()
            mock_p1.stdout = ["Started job job_abcdef111111"]
            mock_p2 = MagicMock()
            mock_p2.stdout = ["Started job job_abcdef222222"]
            
            # setup conditional Popen side effect
            p_calls = []
            def popen_side_effect(args, *a, **k):
                is_pm = False
                if isinstance(args, list):
                    is_pm = any("puppetmaster" in str(arg) for arg in args)
                elif isinstance(args, str):
                    is_pm = "puppetmaster" in args
                    
                if is_pm:
                    p_calls.append(args)
                    if len(p_calls) == 1:
                        return mock_p1
                    return mock_p2
                return real_popen(args, *a, **k)
                
            mock_popen.side_effect = popen_side_effect
            
            def side_effect(args, *a, **k):
                is_pm = False
                if isinstance(args, list):
                    is_pm = any("puppetmaster" in str(arg) for arg in args)
                elif isinstance(args, str):
                    is_pm = "puppetmaster" in args
                    
                if is_pm:
                    if "codegraph" in args:
                        return MagicMock(returncode=0, stdout="")
                    if "await" in args:
                        return MagicMock(returncode=0)
                    elif "artifacts" in args:
                        job_id = None
                        for arg in args:
                            if isinstance(arg, str) and arg.startswith("job_"):
                                job_id = arg
                        
                        mock_res_art = MagicMock()
                        if job_id:
                            filename = f"src/{job_id}.py"
                            mock_res_art.stdout = json.dumps([
                                {
                                    "job_id": job_id,
                                    "type": "patch",
                                    "payload": {
                                        "files": [filename],
                                        "unified_diff": f"diff --git a/{filename} b/{filename}\nnew file mode 100644\n--- /dev/null\n+++ b/{filename}\n@@ -0,0 +1 @@\n+print('{job_id}')\n"
                                    }
                                }
                            ])
                        else:
                            mock_res_art.stdout = "[]"
                        return mock_res_art
                    elif "platform" in args:
                        mock_res_plat = MagicMock()
                        mock_res_plat.stdout = "[on] hermes"
                        return mock_res_plat
                return real_run(args, *a, **k)
                
            mock_run.side_effect = side_effect
            
            session = ConversationalSession(cfg)
            session._detect_default_implement_adapter = MagicMock(return_value="hermes")
            # Keep exercising the external dispatch path even though the requested
            # CLI is not installed in CI (otherwise it falls back to provider-native).
            session._external_adapter_available = lambda adapter: True
            
            class FakeParallelPilot:
                name = "fake"
                def __init__(self):
                    self.calls = 0
                def complete(self, prompt, *, system=None):
                    from pmharness.drivers.openai_compat import DriverResponse
                    self.calls += 1
                    if self.calls == 1:
                        txt = '{"say": "Running in parallel.", "actions": [{"kind": "run_parallel", "adapter": "cursor", "goals": ["Audit auth", "Audit cache"], "mode": "implement"}]}'
                    else:
                        txt = '{"say": "Done.", "actions": []}'
                    return DriverResponse(text=txt, tokens_out=10, latency_ms=1.0)
                    
            session.pilot = FakeParallelPilot()
            events = list(session.send("Run parallel checks!"))
            
            # Let's verify our processes were fanned out
            assert len(p_calls) == 2
            # Verify aggregate result is returned
            kinds = [e.kind for e in events]
            assert "action_result" in kinds
            # New offload model: dispatch emits swarm_pending and returns; applies run in background.
            assert "swarm_pending" in kinds
            
            # Wait for the background applies to complete, then assert the files landed.
            _wait_for_swarms(session)
            # The KEY assertion: once the background swarms complete, the target files exist.
            path1 = os.path.join(temp_repo, "src/job_abcdef111111.py")
            path2 = os.path.join(temp_repo, "src/job_abcdef222222.py")
            assert os.path.exists(path1)
            assert os.path.exists(path2)
            with open(path1, "r") as f:
                assert f.read() == "print('job_abcdef111111')\n"
            with open(path2, "r") as f:
                assert f.read() == "print('job_abcdef222222')\n"
                
    finally:
        shutil.rmtree(temp_repo, ignore_errors=True)


@patch("shutil.which")
@patch("tempfile.mkdtemp")
@patch("shutil.rmtree")
@patch("subprocess.Popen")
@patch("subprocess.run")
def test_run_parallel_state_dir_and_fallback(mock_run, mock_popen, mock_rmtree, mock_mkdtemp, mock_which, tmp_path):
    mock_which.return_value = None
    mock_mkdtemp.side_effect = ["/tmp/pmh-par-1", "/tmp/pmh-par-2"]

    # Marker-only checkout: subprocess.run is mocked below, so a real
    # `git init` would hit the mock. The soft-refuse guard accepts a .git dir.
    repo = tmp_path / "mock-repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir="/tmp/test-state-dir")
    cfg.repo = str(repo)
    
    mock_p1 = MagicMock()
    mock_p1.stdout = ["Started job job_111111111111"]
    
    mock_p2 = MagicMock()
    mock_p2.stdout = ["Success but stdout missed the job_id line"]
    
    mock_popen.side_effect = [mock_p1, mock_p2]
    
    mock_res_last = MagicMock(returncode=0, stdout="job_222222222222")
    mock_res_await = MagicMock(returncode=0)
    mock_res_art = MagicMock()
    mock_res_art.stdout = json.dumps([
        {
            "job_id": "job_111111111111",
            "type": "finding",
            "payload": {"report": "Worker 1 results"}
        }
    ])
    
    def _run_side(*a, **k):
        argv = a[0] if a else k.get("args", [])
        if isinstance(argv, list):
            if "last" in argv:
                return mock_res_last
            elif "artifacts" in argv:
                if "job_111111111111" in argv:
                    return mock_res_art
                else:
                    res2 = MagicMock()
                    res2.stdout = json.dumps([
                        {
                            "job_id": "job_222222222222",
                            "type": "finding",
                            "payload": {"report": "Worker 2 results"}
                        }
                    ])
                    return res2
            elif "await" in argv:
                return mock_res_await
        return mock_res_await
        
    mock_run.side_effect = _run_side
    
    session = ConversationalSession(cfg)
    session._detect_default_implement_adapter = MagicMock(return_value="hermes")
    # Keep exercising the external dispatch path even though the requested CLI is
    # not installed in CI (otherwise it falls back to the provider-native worker).
    session._external_adapter_available = lambda adapter: True
    
    class FakeParallelPilot:
        name = "fake"
        def __init__(self):
            self.calls = 0
        def complete(self, prompt, *, system=None):
            from pmharness.drivers.openai_compat import DriverResponse
            self.calls += 1
            if self.calls == 1:
                txt = '{"say": "Running in parallel.", "actions": [{"kind": "run_parallel", "adapter": "cursor", "goals": ["Goal One", "Goal Two"], "mode": "analysis"}]}'
            else:
                txt = '{"say": "Done.", "actions": []}'
            return DriverResponse(text=txt, tokens_out=10, latency_ms=1.0)
            
    session.pilot = FakeParallelPilot()
    events = list(session.send("Run parallel checks!"))
    # New offload model: dispatch (Popen) is synchronous in-turn, but await/artifacts/last/rmtree
    # run in the background futures -- wait for them before asserting on those calls.
    kinds = [e.kind for e in events]
    assert "swarm_pending" in kinds
    _wait_for_swarms(session)
    
    assert mock_popen.call_count == 2
    args1 = mock_popen.call_args_list[0][0][0]
    args2 = mock_popen.call_args_list[1][0][0]
    
    assert args1[1:5] == ["-m", "puppetmaster", "--state-dir", "/tmp/pmh-par-1"]
    assert args2[1:5] == ["-m", "puppetmaster", "--state-dir", "/tmp/pmh-par-2"]
    
    last_calls = [c[0][0] for c in mock_run.call_args_list if "last" in c[0][0]]
    assert len(last_calls) == 1
    assert last_calls[0] == [sys.executable, "-m", "puppetmaster", "--state-dir", "/tmp/pmh-par-2", "last"]
    
    await_calls = [c[0][0] for c in mock_run.call_args_list if "await" in c[0][0]]
    assert len(await_calls) == 2
    assert ["--state-dir", "/tmp/pmh-par-1"] in [await_calls[0][3:5], await_calls[1][3:5]]
    assert ["--state-dir", "/tmp/pmh-par-2"] in [await_calls[0][3:5], await_calls[1][3:5]]
    
    art_calls = [c[0][0] for c in mock_run.call_args_list if "artifacts" in c[0][0]]
    assert len(art_calls) == 2
    assert ["--state-dir", "/tmp/pmh-par-1"] in [art_calls[0][3:5], art_calls[1][3:5]]
    assert ["--state-dir", "/tmp/pmh-par-2"] in [art_calls[0][3:5], art_calls[1][3:5]]
    
    assert mock_rmtree.call_count == 2
    mock_rmtree.assert_any_call("/tmp/pmh-par-1", ignore_errors=True)
    mock_rmtree.assert_any_call("/tmp/pmh-par-2", ignore_errors=True)

