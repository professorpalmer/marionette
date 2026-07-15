"""Tests for real pilot agent tools (read_file, write_file, run_command, list_dir)."""
import json
import os
import shutil
import subprocess
import sys
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
    # Manual mkdtemp + best-effort cleanup instead of TemporaryDirectory: on
    # Windows under Python 3.9 a still-closing session subprocess handle makes
    # the context manager's rmtree retry loop recurse to death (RecursionError
    # in shutil). ignore_errors leaves the temp dir for the OS to reap instead
    # of failing the test on teardown.
    tmpdir = tempfile.mkdtemp()
    try:
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
            def __init__(self):
                self.calls = 0
            def complete(self, prompt, system=None):
                self.calls += 1
                if self.calls == 1:
                    return FakeResponse(text=json.dumps({
                        "say": "Trying traversal",
                        "actions": [
                            {"kind": "read_file", "path": "../../etc/passwd"}
                        ]
                    }))
                return FakeResponse(text=json.dumps({
                    "say": "Done",
                    "actions": []
                }))

        session_traversal = ConversationalSession(cfg)
        session_traversal.pilot = TraversalPilot()
        trav_events = list(session_traversal.send("start"))
        
        # Verify traversal was blocked
        results = [e.data for e in trav_events if e.kind == "action_result"]
        assert len(results) > 0
        assert "rejected" in results[0].get("error", "").lower() or "traversal" in results[0].get("error", "").lower()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_run_command_survives_cancel_poisoned_after_action_start():
    """Regression for the "every shell command dies but reads work" bug.

    A shared _cancel flag set mid-turn (e.g. explicit Stop / interrupt, or the
    historical autopilot path that cancelled on SSE disconnect) used to poison
    the next run_command: the runner launched with the flag already set and was
    killed on the spot -- exit 130, "[interrupted by user]" -- even though nothing
    was stopping THIS command. read_file/list_dir never consult the flag, which is
    why they kept working while every run_command died.

    We reproduce it deterministically: consume the generator and set _cancel the
    instant the run_command action_start is emitted (i.e. right before the runner
    launches). With edge-triggered cancellation the command must still complete."""
    # mkdtemp + ignore_errors teardown, not TemporaryDirectory: on Windows the
    # spawned subprocess can hold the dir handle a beat past completion, and
    # TemporaryDirectory's cleanup raises WinError 32 (flaky CI teardown).
    tmpdir = tempfile.mkdtemp()
    try:
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
                        # Python one-liner sleep: portable across /bin/sh and
                        # cmd.exe (POSIX `sleep` and `;` chaining are not).
                        "actions": [{"kind": "run_command", "command":
                            f'"{sys.executable}" -c "import time; time.sleep(0.3); print(\'alive_marker_42\')"'}],
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
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


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


def test_read_file_on_directory_returns_listing():
    """read_file on a real directory should succeed with a listing, not IsADirectoryError."""
    with tempfile.TemporaryDirectory() as repo:
        sub = os.path.join(repo, "frontend", "src")
        os.makedirs(sub)
        with open(os.path.join(sub, "app.ts"), "w", encoding="utf-8") as f:
            f.write("export {}\n")
        nested = os.path.join(sub, "components")
        os.makedirs(nested)

        cfg = HarnessConfig(
            repo=os.path.realpath(repo),
            swarm_adapter="demo",
            state_dir=tempfile.mkdtemp(),
        )
        session = ConversationalSession(cfg)

        ok, status, val = session._do_read_file(_ReadAct(path="frontend/src"))
        assert ok is True, f"expected success, got {status}: {val}"
        assert status == "success"
        assert "path is a directory" in val
        assert "use list_dir next time" in val
        assert "app.ts" in val
        assert "components/" in val
        assert "IsADirectoryError" not in val
        assert "Path is a directory:" not in val

        # Narrow file reads remain unchanged.
        ok_file, status_file, val_file = session._do_read_file(
            _ReadAct(path="frontend/src/app.ts")
        )
        assert ok_file is True and status_file == "success"
        assert "export" in val_file
        assert "path is a directory" not in val_file


def test_nested_workspace_read_file_allows_git_toplevel_parent():
    """Workspace nested under a git clone can read_file the parent README.

    Writes stay confined to the open workspace; paths outside the git toplevel
    remain path_traversal.
    """
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as state:
        root = os.path.realpath(tmp)
        subprocess.run(
            ["git", "init"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        nested = os.path.join(root, "Ashita", "addons", "kotoba")
        os.makedirs(nested)
        readme = os.path.join(root, "README.md")
        with open(readme, "w", encoding="utf-8") as f:
            f.write("# ffxiAddons parent readme\n")
        sibling = os.path.join(root, "Ashita", "addons", "other", "note.txt")
        os.makedirs(os.path.dirname(sibling))
        with open(sibling, "w", encoding="utf-8") as f:
            f.write("sibling\n")

        cfg = HarnessConfig(
            repo=os.path.realpath(nested),
            swarm_adapter="demo",
            state_dir=os.path.realpath(state),
        )
        session = ConversationalSession(cfg)

        ok, status, val = session._do_read_file(_ReadAct(path=readme))
        assert ok, f"parent README under git toplevel should be readable, got {status}: {val}"
        assert "ffxiAddons parent readme" in val

        ok_sib, status_sib, val_sib = session._do_read_file(_ReadAct(path=sibling))
        assert ok_sib, f"sibling under git toplevel should be readable, got {status_sib}: {val_sib}"
        assert "sibling" in val_sib

        # True escape outside the git clone is still rejected.
        outside = os.path.join(os.path.dirname(root), "escape-outside.txt")
        bad = session._do_read_file(_ReadAct(path=outside))
        assert bad[0] is False and bad[1] == "path_traversal"

        # Writes/edits stay confined to the nested workspace (not the git root).
        assert not is_safe_path(readme, cfg.repo)
        assert is_safe_path(os.path.join(cfg.repo, "local.txt"), cfg.repo)
