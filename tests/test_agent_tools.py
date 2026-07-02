"""Tests for real pilot agent tools (read_file, write_file, run_command, list_dir)."""
import json
import os
import tempfile
from dataclasses import dataclass
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession, is_safe_path


@dataclass
class FakeResponse:
    text: str
    error: str = ""
    tokens_out: int = 0
    tokens_in: int = 0


def test_is_safe_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        real_tmp = os.path.realpath(tmpdir)
        # Inside workspace
        assert is_safe_path(os.path.join(real_tmp, "foo.py"), real_tmp) is True
        assert is_safe_path(os.path.join(real_tmp, "sub/bar.py"), real_tmp) is True
        # Workspace itself
        assert is_safe_path(real_tmp, real_tmp) is True
        # Outside workspace
        assert is_safe_path(os.path.join(real_tmp, "../outside.py"), real_tmp) is False
        assert is_safe_path("/etc/passwd", real_tmp) is False


def test_agent_tools_execution():
    with tempfile.TemporaryDirectory() as tmpdir:
        real_tmp = os.path.realpath(tmpdir)
        cfg = HarnessConfig(repo=real_tmp, swarm_adapter="demo")
        session = ConversationalSession(cfg)

        class FakePilot:
            def __init__(self):
                self.calls = 0
            def complete(self, prompt, system=None):
                self.calls += 1
                if self.calls == 1:
                    return FakeResponse(text=json.dumps({
                        "say": "Writing file now",
                        "actions": [
                            {"kind": "write_file", "path": "hello.txt", "content": "hello world"}
                        ]
                    }))
                elif self.calls == 2:
                    return FakeResponse(text=json.dumps({
                        "say": "Reading file now",
                        "actions": [
                            {"kind": "read_file", "path": "hello.txt"}
                        ]
                    }))
                elif self.calls == 3:
                    return FakeResponse(text=json.dumps({
                        "say": "Running command now",
                        "actions": [
                            {"kind": "run_command", "command": "echo hi"}
                        ]
                    }))
                elif self.calls == 4:
                    return FakeResponse(text=json.dumps({
                        "say": "Listing dir now",
                        "actions": [
                            {"kind": "list_dir", "path": ""}
                        ]
                    }))
                else:
                    return FakeResponse(text=json.dumps({
                        "say": "Done",
                        "actions": []
                    }))

        session.pilot = FakePilot()
        events = list(session.send("start"))

        # Verify that hello.txt was created and has correct content
        target_file = os.path.join(real_tmp, "hello.txt")
        assert os.path.exists(target_file)
        with open(target_file, "r") as f:
            assert f.read() == "hello world"

        # Check that events have action_start and action_result for all kinds
        kinds_started = [e.data.get("kind") for e in events if e.kind == "action_start"]
        assert "write_file" in kinds_started
        assert "read_file" in kinds_started
        assert "run_command" in kinds_started
        assert "list_dir" in kinds_started

        # Verify confinement rejection
        class TraversalPilot:
            def complete(self, prompt, system=None):
                return FakeResponse(text=json.dumps({
                    "say": "Trying traversal",
                    "actions": [
                        {"kind": "read_file", "path": "../../etc/passwd"}
                    ]
                }))

        session_traversal = ConversationalSession(cfg)
        session_traversal.pilot = TraversalPilot()
        trav_events = list(session_traversal.send("start"))
        
        # Verify traversal was blocked
        results = [e.data for e in trav_events if e.kind == "action_result"]
        assert len(results) > 0
        assert "rejected" in results[0].get("error", "").lower() or "traversal" in results[0].get("error", "").lower()


def test_run_command_survives_cancel_poisoned_after_action_start():
    """Regression for the "every shell command dies but reads work" bug.

    In autopilot the action_start SSE write can detect a transient client
    disconnect and call _pilot.cancel(), which sets the shared _cancel flag. The
    very next run_command then launched with that flag already set and was killed
    on the spot -- exit 130, "[interrupted by user]" -- even though nothing was
    stopping THIS command. read_file/list_dir never consult the flag, which is why
    they kept working while every run_command died.

    We reproduce it deterministically: consume the generator and set _cancel the
    instant the run_command action_start is emitted (i.e. right before the runner
    launches). With edge-triggered cancellation the command must still complete."""
    with tempfile.TemporaryDirectory() as tmpdir:
        real_tmp = os.path.realpath(tmpdir)
        cfg = HarnessConfig(repo=real_tmp, swarm_adapter="demo")
        session = ConversationalSession(cfg)

        class CmdPilot:
            def __init__(self):
                self.calls = 0

            def complete(self, prompt, system=None):
                self.calls += 1
                if self.calls == 1:
                    return FakeResponse(text=json.dumps({
                        "say": "Running command now",
                        "actions": [{"kind": "run_command", "command": "sleep 0.3; echo alive_marker_42"}],
                    }))
                return FakeResponse(text=json.dumps({"say": "Done", "actions": []}))

        session.pilot = CmdPilot()

        cmd_result = None
        for ev in session.send("start"):
            if ev.kind == "action_start" and ev.data.get("kind") == "run_command":
                # Simulate the sibling-stream / disconnect cancel landing between
                # action_start and the runner launch -- exactly the poison window.
                session._cancel.set()
            if ev.kind == "action_result" and "command" in (ev.data.get("types") or []):
                cmd_result = ev.data

        assert cmd_result is not None, "run_command produced no result -- it was killed before launch"
        headline = cmd_result["artifacts"][0]["headline"]
        assert headline == "Command exited with 0", f"command was wrongly cancelled: {headline!r}"


@dataclass
class _ReadAct:
    path: str
    kind: str = "read_file"
    start_line: object = None
    limit: object = None


def test_read_file_can_read_spilled_result_outside_repo():
    """Regression: oversized tool output is persisted to
    {state_dir}/pmharness-results/<id>.txt and the model is told to read it back
    with read_file. That dir lives outside the workspace, so read_file must allow
    it -- otherwise the pilot is told to read a file it is then refused (the
    "reads and sandbox problems" deadlock)."""
    with tempfile.TemporaryDirectory() as repo, tempfile.TemporaryDirectory() as state:
        cfg = HarnessConfig(repo=os.path.realpath(repo), swarm_adapter="demo",
                            state_dir=os.path.realpath(state))
        session = ConversationalSession(cfg)

        spill_dir = os.path.join(os.path.realpath(state), "pmharness-results")
        os.makedirs(spill_dir, exist_ok=True)
        spill_file = os.path.join(spill_dir, "web_fetch_abc.txt")
        with open(spill_file, "w") as f:
            f.write("SPILLED CONTENT\nline two\n")

        ok, status, val = session._do_read_file(_ReadAct(path=spill_file))
        assert ok, f"reading spilled result should be allowed, got {status}: {val}"
        assert "SPILLED CONTENT" in val

        # A path outside both the repo and the spill dir is still rejected.
        bad = session._do_read_file(_ReadAct(path="/etc/passwd"))
        assert bad[0] is False and bad[1] == "path_traversal"
