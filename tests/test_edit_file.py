from __future__ import annotations
import os
import json
import tempfile
import subprocess
import pytest
from typing import Optional, Any

from harness.pilot import build_tools_schema, parse_tool_calls, PilotAction
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession, ConvEvent


class FakeMcpTool:
    def __init__(self, server: str, name: str, description: str, input_schema: dict):
        self.server = server
        self.name = name
        self.description = description
        self.input_schema = input_schema


def test_edit_file_schema_present():
    # (a) build_tools_schema() and build_tools_schema(no_delegation=True) BOTH include edit_file
    schemas = build_tools_schema()
    names = [s["function"]["name"] for s in schemas]
    assert "edit_file" in names

    schemas_no_delegation = build_tools_schema(no_delegation=True)
    names_no_delegation = [s["function"]["name"] for s in schemas_no_delegation]
    assert "edit_file" in names_no_delegation


def create_temp_git_repo():
    repo_dir = tempfile.mkdtemp()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)
    
    # create initial commit so HEAD exists
    initial_file = os.path.join(repo_dir, "init.txt")
    with open(initial_file, "w") as f:
        f.write("initial")
    subprocess.run(["git", "add", "init.txt"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo_dir, check=True)
    return repo_dir


class _FakeEditPilot:
    name = "fake-edit-pilot"
    
    def __init__(self, action: PilotAction):
        self.action = action
        self.calls = 0

    def complete(self, task_prompt: str, *, system: Optional[str] = None) -> Any:
        from pmharness.drivers.openai_compat import DriverResponse
        return DriverResponse(text="")

    def chat(self, messages: list, *, tools: list | None = None, system: str | None = None) -> Any:
        from pmharness.drivers.openai_compat import DriverResponse
        self.calls += 1
        if self.calls == 1:
            tool_calls = [
                {
                    "id": "tc_edit_1",
                    "type": "function",
                    "function": {
                        "name": "edit_file",
                        "arguments": json.dumps({
                            "path": self.action.path,
                            "old_str": self.action.old_str,
                            "new_str": self.action.new_str
                        })
                    }
                }
            ]
            return DriverResponse(
                text="",
                tokens_out=15,
                latency_ms=1.0,
                meta={
                    "tool_calls": tool_calls,
                    "reasoning": "Performing surgical edit.",
                    "finish_reason": "tool_calls"
                }
            )
        else:
            return DriverResponse(
                text="surgical edit completed.",
                tokens_out=20,
                latency_ms=1.0,
                meta={
                    "tool_calls": [],
                    "reasoning": "All done.",
                    "finish_reason": "stop"
                }
            )


def test_edit_file_success():
    # (b) a unit test that drives an edit_file action through the session on a tiny temp git repo:
    # create a file, edit_file replaces a unique substring, assert the file content changed correctly
    # and a checkpoint was taken
    repo_dir = create_temp_git_repo()
    state_dir = tempfile.mkdtemp()
    
    target_file = os.path.join(repo_dir, "test.txt")
    with open(target_file, "w") as f:
        f.write("hello world\nthis is a line\nend of file")

    subprocess.run(["git", "add", "test.txt"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "add test.txt"], cwd=repo_dir, check=True)

    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=state_dir, repo=repo_dir)
    s = ConversationalSession(cfg)
    
    assert len(s._checkpoints.list()) == 0

    act = PilotAction(kind="edit_file", path="test.txt", old_str="this is a line", new_str="this is a surgically replaced line")
    s.pilot = _FakeEditPilot(act)

    events = list(s.send("surgical edit please"))
    kinds = [e.kind for e in events]
    
    assert "action_start" in kinds
    assert "checkpoint" in kinds
    assert "action_result" in kinds
    
    with open(target_file, "r") as f:
        updated_content = f.read()
    assert updated_content == "hello world\nthis is a surgically replaced line\nend of file"

    checkpoints = s._checkpoints.list()
    assert len(checkpoints) > 0
    assert "Before editing test.txt" in checkpoints[0]["label"]


def test_edit_file_old_str_not_found():
    # (c) edit_file with old_str not present -> error result, file unchanged
    repo_dir = create_temp_git_repo()
    state_dir = tempfile.mkdtemp()
    
    target_file = os.path.join(repo_dir, "test.txt")
    with open(target_file, "w") as f:
        f.write("hello world\nthis is a line\nend of file")

    subprocess.run(["git", "add", "test.txt"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "add test.txt"], cwd=repo_dir, check=True)

    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=state_dir, repo=repo_dir)
    s = ConversationalSession(cfg)

    act = PilotAction(kind="edit_file", path="test.txt", old_str="nonexistent substring", new_str="replacement")
    s.pilot = _FakeEditPilot(act)

    events = list(s.send("surgical edit with missing old_str"))
    
    action_result_events = [e for e in events if e.kind == "action_result"]
    assert len(action_result_events) == 1
    assert "error" in action_result_events[0].data
    assert "old_str not found" in action_result_events[0].data["error"]

    with open(target_file, "r") as f:
        content = f.read()
    assert content == "hello world\nthis is a line\nend of file"


def test_edit_file_old_str_duplicate():
    # (d) edit_file with old_str matching twice -> error result, file unchanged
    repo_dir = create_temp_git_repo()
    state_dir = tempfile.mkdtemp()
    
    target_file = os.path.join(repo_dir, "test.txt")
    with open(target_file, "w") as f:
        f.write("apple apple apple")

    subprocess.run(["git", "add", "test.txt"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "add test.txt"], cwd=repo_dir, check=True)

    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=state_dir, repo=repo_dir)
    s = ConversationalSession(cfg)

    act = PilotAction(kind="edit_file", path="test.txt", old_str="apple", new_str="orange")
    s.pilot = _FakeEditPilot(act)

    events = list(s.send("surgical edit with duplicate old_str"))
    
    action_result_events = [e for e in events if e.kind == "action_result"]
    assert len(action_result_events) == 1
    assert "error" in action_result_events[0].data
    assert "old_str matched 3 times" in action_result_events[0].data["error"]

    with open(target_file, "r") as f:
        content = f.read()
    assert content == "apple apple apple"


def test_edit_file_outside_root():
    # (e) edit_file on a path outside the repo root -> confinement error, nothing written
    repo_dir = create_temp_git_repo()
    state_dir = tempfile.mkdtemp()
    
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=state_dir, repo=repo_dir)
    s = ConversationalSession(cfg)

    act = PilotAction(kind="edit_file", path="../outside.txt", old_str="something", new_str="replacement")
    s.pilot = _FakeEditPilot(act)

    events = list(s.send("surgical edit outside root"))
    
    action_result_events = [e for e in events if e.kind == "action_result"]
    assert len(action_result_events) == 1
    assert "error" in action_result_events[0].data
    assert "Path traversal attempt rejected" in action_result_events[0].data["error"]


def test_parse_tool_calls_truncation():
    tc_truncated = [
        {
            "id": "tc_trunc_1",
            "type": "function",
            "function": {
                "name": "write_file",
                "arguments": '{"path": "truncated.py", "content": "print('
            }
        }
    ]
    actions = parse_tool_calls(tc_truncated)
    assert len(actions) == 1
    assert actions[0].kind == "__invalid__"
    assert "TRUNCATED" in actions[0].content
    assert "edit_file" in actions[0].content
