"""Write fence: analysis/QA roles cannot mutate the workspace."""

from __future__ import annotations

import subprocess

import pytest

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.hash_edit import compute_range_hash, split_lines
from harness.pilot import PilotAction


def _session(tmp_path, *, role="", repo=None):
    workspace = repo if repo is not None else (tmp_path / "workspace")
    workspace.mkdir(exist_ok=True)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=str(state))
    cfg.repo = str(workspace)
    session = ConversationalSession(cfg)
    session.job_role = role
    return session, workspace


def _git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init"], cwd=repo, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"], cwd=repo, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    seed = repo / "seed.txt"
    seed.write_text("before\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "seed.txt"], cwd=repo, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=repo, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return repo


def _hash_edit_action(path, text, replacement):
    lines = split_lines(text)
    anchor = compute_range_hash(lines, 1, 1)
    return PilotAction(
        kind="hash_edit",
        path=path,
        arguments={
            "ops": [{
                "op": "replace",
                "anchor": anchor,
                "start_line": 1,
                "end_line": 1,
                "text": replacement,
            }],
        },
    )


@pytest.mark.parametrize("role", ["analysis", "qa"])
def test_read_only_role_refuses_write_file(tmp_path, role):
    session, workspace = _session(tmp_path, role=role)
    seed = workspace / "seed.txt"
    seed.write_text("before\n", encoding="utf-8")
    act = PilotAction(kind="write_file", path="seed.txt", content="after\n")
    ok, status, msg = session._do_write_file(act, write=True)
    assert ok is False
    assert status == "read_only_role"
    assert "cannot write" in msg
    assert seed.read_text(encoding="utf-8") == "before\n"


@pytest.mark.parametrize("role", ["analysis", "qa"])
def test_read_only_role_refuses_edit_file(tmp_path, role):
    session, workspace = _session(tmp_path, role=role)
    seed = workspace / "seed.txt"
    seed.write_text("before\n", encoding="utf-8")
    act = PilotAction(
        kind="edit_file", path="seed.txt", old_str="before\n", new_str="after\n",
    )
    ok, status, msg = session._do_edit_file(act, write=True)
    assert ok is False
    assert status == "read_only_role"
    assert "cannot write" in msg
    assert seed.read_text(encoding="utf-8") == "before\n"


@pytest.mark.parametrize("role", ["analysis", "qa"])
def test_read_only_role_refuses_hash_edit(tmp_path, role, monkeypatch):
    monkeypatch.setenv("HARNESS_HASH_EDIT", "1")
    session, workspace = _session(tmp_path, role=role)
    seed = workspace / "seed.txt"
    seed.write_text("before\n", encoding="utf-8")
    act = _hash_edit_action("seed.txt", "before\n", "after")
    ok, status, msg = session._do_hash_edit(act, write=True)
    assert ok is False
    assert status == "read_only_role"
    assert "cannot write" in msg
    assert seed.read_text(encoding="utf-8") == "before\n"


@pytest.mark.parametrize("role", ["", "implement"])
def test_writable_role_still_writes(tmp_path, role, monkeypatch):
    monkeypatch.setenv("HARNESS_HASH_EDIT", "1")
    session, workspace = _session(tmp_path, role=role)
    seed = workspace / "seed.txt"
    seed.write_text("before\n", encoding="utf-8")

    ok, status, _val = session._do_write_file(
        PilotAction(kind="write_file", path="seed.txt", content="written\n"),
        write=True,
    )
    assert ok is True and status == "success"
    assert seed.read_text(encoding="utf-8") == "written\n"

    ok, status, _msg = session._do_edit_file(
        PilotAction(
            kind="edit_file", path="seed.txt",
            old_str="written\n", new_str="edited\n",
        ),
        write=True,
    )
    assert ok is True and status == "success"
    assert seed.read_text(encoding="utf-8") == "edited\n"

    act = _hash_edit_action("seed.txt", "edited\n", "hashed")
    ok, status, _msg = session._do_hash_edit(act, write=True)
    assert ok is True and status == "success"
    assert seed.read_text(encoding="utf-8") == "hashed\n"


def test_analysis_dry_run_write_does_not_touch_disk(tmp_path):
    session, workspace = _session(tmp_path, role="analysis")
    act = PilotAction(kind="write_file", path="new.txt", content="ghost\n")
    ok, status, val = session._do_write_file(act, write=False)
    assert ok is True and status == "success"
    assert val == 0
    assert not (workspace / "new.txt").exists()


def test_analysis_can_still_read_file(tmp_path):
    session, workspace = _session(tmp_path, role="analysis")
    (workspace / "seed.txt").write_text("visible\n", encoding="utf-8")
    ok, status, val = session._do_read_file(
        PilotAction(kind="read_file", path="seed.txt"),
    )
    assert ok is True and status == "success"
    assert "visible" in val


def test_active_job_role_fallback_refuses_write(tmp_path):
    session, workspace = _session(tmp_path, role="")
    session._active_job_role = "qa"
    seed = workspace / "seed.txt"
    seed.write_text("before\n", encoding="utf-8")
    ok, status, msg = session._do_write_file(
        PilotAction(kind="write_file", path="seed.txt", content="after\n"),
        write=True,
    )
    assert ok is False
    assert status == "read_only_role"
    assert "cannot write" in msg
    assert seed.read_text(encoding="utf-8") == "before\n"


def test_apply_worker_patch_refuses_analysis_local_job(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    session, _workspace = _session(tmp_path, repo=repo)
    session._local_jobs["local-analysis"] = {
        "id": "local-analysis",
        "status": "running",
        "role": "analysis",
    }

    def boom(*_a, **_k):
        raise AssertionError("git apply must not run for a read-only role")

    monkeypatch.setattr("subprocess.run", boom)

    artifacts = [{
        "type": "patch",
        "payload": {
            "unified_diff": (
                "diff --git a/seed.txt b/seed.txt\n"
                "--- a/seed.txt\n"
                "+++ b/seed.txt\n"
                "@@ -1 +1 @@\n"
                "-before\n"
                "+after\n"
            ),
            "files": ["seed.txt"],
        },
    }]
    applied, files, msg = session._apply_worker_patch(
        artifacts, "local-analysis",
    )
    assert applied is False
    assert files == []
    assert "cannot write" in msg
    assert (repo / "seed.txt").read_text(encoding="utf-8") == "before\n"
