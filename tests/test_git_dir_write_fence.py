"""Native file tools never write inside a .git directory."""

from __future__ import annotations

import os

import pytest

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.hash_edit import compute_range_hash, split_lines
from harness.paths import is_git_restricted_path, targets_git_dir
from harness.pilot import PilotAction
from harness.send_loop_phases import dispatch_local_action


def _session(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / ".git" / "hooks").mkdir(parents=True)
    (workspace / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (workspace / "src").mkdir()
    (workspace / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=str(state))
    cfg.repo = str(workspace)
    return ConversationalSession(cfg), workspace


GIT_TARGETS = [
    ".git/hooks/pre-commit",
    ".git/config",
    ".GIT/hooks/post-checkout",
    "vendor/sub/.git/hooks/pre-push",
    "./src/../.git/HEAD",
]


@pytest.mark.parametrize("rel", GIT_TARGETS)
def test_targets_git_dir(tmp_path, rel):
    assert targets_git_dir(os.path.join(str(tmp_path), rel), str(tmp_path))


@pytest.mark.parametrize("rel", ["src/a.py", ".gitignore", ".github/workflows/ci.yml", "docs/git/x.md"])
def test_ordinary_paths_are_not_git_dir(tmp_path, rel):
    assert not targets_git_dir(os.path.join(str(tmp_path), rel), str(tmp_path))


def test_symlink_into_git_dir_is_caught(tmp_path):
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    os.symlink(str(tmp_path / ".git" / "hooks"), str(tmp_path / "hooks-link"))
    assert targets_git_dir(str(tmp_path / "hooks-link" / "pre-commit"), str(tmp_path))


def test_ui_file_api_rule_is_case_insensitive():
    assert is_git_restricted_path(".GIT/config")
    assert not is_git_restricted_path(".gitignore")


@pytest.mark.parametrize("rel", GIT_TARGETS[:3])
def test_write_file_refuses_git_dir(tmp_path, rel):
    session, workspace = _session(tmp_path)
    ok, status, msg = session._do_write_file(PilotAction(kind="write_file", path=rel, content="#!/bin/sh\n"))
    assert not ok
    assert status == "path_traversal"
    assert ".git" in msg
    assert not (workspace / ".git" / "hooks" / "pre-commit").exists()
    assert (workspace / ".git" / "config").read_text(encoding="utf-8") == "[core]\n"


def test_edit_file_refuses_git_config(tmp_path):
    session, workspace = _session(tmp_path)
    act = PilotAction(kind="edit_file", path=".git/config", old_str="[core]", new_str="[core]\n\tfsmonitor = x")
    ok, status, _msg = session._do_edit_file(act)
    assert not ok and status == "path_traversal"
    assert (workspace / ".git" / "config").read_text(encoding="utf-8") == "[core]\n"


def test_hash_edit_refuses_git_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_HASH_EDIT", "1")
    session, workspace = _session(tmp_path)
    lines = split_lines("[core]\n")
    act = PilotAction(kind="hash_edit", path=".git/config", arguments={"ops": [{
        "op": "replace", "anchor": compute_range_hash(lines, 1, 1),
        "start_line": 1, "end_line": 1, "text": "[core]\n\thooksPath = x\n",
    }]})
    ok, status, _msg = session._do_hash_edit(act)
    assert not ok and status == "path_traversal"
    assert (workspace / ".git" / "config").read_text(encoding="utf-8") == "[core]\n"


def test_dispatch_write_file_refuses_git_dir(tmp_path):
    session, workspace = _session(tmp_path)
    act = PilotAction(kind="write_file", path=".git/hooks/pre-commit", content="#!/bin/sh\n")
    events = list(dispatch_local_action(session, act, "w1", True, []))
    assert any("Protected path" in str(getattr(e, "data", "")) for e in events)
    assert not (workspace / ".git" / "hooks" / "pre-commit").exists()


def test_write_file_outside_git_still_works(tmp_path):
    session, workspace = _session(tmp_path)
    ok, _status, _val = session._do_write_file(PilotAction(kind="write_file", path="src/b.py", content="y = 2\n"))
    assert ok
    assert (workspace / "src" / "b.py").read_text(encoding="utf-8") == "y = 2\n"
