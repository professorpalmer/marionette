"""Hermetic tests for live checkpoint hunk attribution (Agent vs External)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest

from harness.checkpoint_hunks import CheckpointHunkTracker, fs_notify, record_agent_write
from harness.checkpoints import CheckpointStore


@pytest.fixture
def temp_git_repo():
    temp_dir = tempfile.mkdtemp()
    try:
        subprocess.run(["git", "init"], cwd=temp_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=temp_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=temp_dir, check=True)
        file1 = os.path.join(temp_dir, "file1.txt")
        with open(file1, "w", encoding="utf-8") as f:
            f.write("line one\nline two\n")
        subprocess.run(["git", "add", "file1.txt"], cwd=temp_dir, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=temp_dir, check=True)
        yield temp_dir
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _tracker(repo: str, session_id: str = "sess-hunks") -> CheckpointHunkTracker:
    store = CheckpointStore(repo, session_id=session_id)
    return store.hunk_tracker(session_id=session_id)


def test_hunk_tracker_baseline_and_agent_attribution(temp_git_repo):
    repo = temp_git_repo
    tracker = _tracker(repo)

    baseline = tracker.ensure_baseline()
    assert baseline

    path = os.path.join(repo, "file1.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("line one\nagent line\n")
    tracker.record_agent_write("file1.txt")

    live = tracker.recompute()
    assert live["ok"] is True
    assert live["baseline_id"] == baseline
    assert live["files"]
    file_row = live["files"][0]
    assert file_row["path"] == "file1.txt"
    assert file_row["hunks"]
    hunk = file_row["hunks"][0]
    assert hunk["source"] == "agent"
    assert hunk["kind"] == "modified"
    assert hunk["status"] == "pending"


def test_hunk_tracker_external_attribution(temp_git_repo):
    repo = temp_git_repo
    tracker = _tracker(repo)
    tracker.ensure_baseline()

    path = os.path.join(repo, "file1.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("line one\nexternal line\n")
    tracker.fs_notify("file1.txt")

    live = tracker.recompute()
    assert live["ok"] is True
    hunk = live["files"][0]["hunks"][0]
    assert hunk["source"] == "external"


def test_record_agent_write_and_fs_notify_helpers(temp_git_repo):
    repo = temp_git_repo
    session = "helper-session"
    store = CheckpointStore(repo, session_id=session)
    store.snapshot(label="pre-helper", trigger="test", session_id=session)

    path = os.path.join(repo, "file1.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("line one\nhelper agent\n")
    record_agent_write(repo, "file1.txt", session_id=session)

    live = store.hunk_tracker(session_id=session).recompute()
    assert live["files"][0]["hunks"][0]["source"] == "agent"

    with open(path, "w", encoding="utf-8") as f:
        f.write("line one\nhelper external\n")
    fs_notify(repo, "file1.txt", session_id=session)
    live2 = store.hunk_tracker(session_id=session).recompute()
    assert live2["files"][0]["hunks"][0]["source"] == "external"


def test_accept_hunk_marks_status_without_file_change(temp_git_repo):
    repo = temp_git_repo
    tracker = _tracker(repo)
    tracker.ensure_baseline()

    path = os.path.join(repo, "file1.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("line one\naccepted line\n")
    tracker.record_agent_write("file1.txt")

    live = tracker.recompute()
    hunk_id = live["files"][0]["hunks"][0]["id"]
    res = tracker.accept_hunk(hunk_id)
    assert res["ok"] is True
    assert res["status"] == "accepted"

    after = tracker.recompute()
    hunk = after["files"][0]["hunks"][0]
    assert hunk["status"] == "accepted"
    with open(path, "r", encoding="utf-8") as f:
        assert "accepted line" in f.read()


def test_revert_hunk_restores_baseline_content(temp_git_repo):
    repo = temp_git_repo
    tracker = _tracker(repo)
    tracker.ensure_baseline()

    path = os.path.join(repo, "file1.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("line one\nagent edit\n")
    tracker.record_agent_write("file1.txt")

    live = tracker.recompute()
    hunk_id = live["files"][0]["hunks"][0]["id"]
    res = tracker.revert_hunk(hunk_id)
    assert res["ok"] is True
    assert res["status"] == "reverted"

    with open(path, "r", encoding="utf-8") as f:
        assert f.read() == "line one\nline two\n"

    after = tracker.recompute()
    assert after["files"] == []


def test_added_file_hunk_revert_deletes_file(temp_git_repo):
    repo = temp_git_repo
    tracker = _tracker(repo)
    tracker.ensure_baseline()

    new_path = os.path.join(repo, "brand_new.txt")
    with open(new_path, "w", encoding="utf-8") as f:
        f.write("fresh\n")
    tracker.record_agent_write("brand_new.txt")

    live = tracker.recompute()
    assert any(f["path"] == "brand_new.txt" for f in live["files"])
    file_row = next(f for f in live["files"] if f["path"] == "brand_new.txt")
    hunk_id = file_row["hunks"][0]["id"]
    assert file_row["hunks"][0]["kind"] == "added"

    res = tracker.revert_hunk(hunk_id)
    assert res["ok"] is True
    assert not os.path.exists(new_path)


def test_removed_file_hunk_is_external_or_agent(temp_git_repo):
    repo = temp_git_repo
    tracker = _tracker(repo)
    tracker.ensure_baseline()

    os.remove(os.path.join(repo, "file1.txt"))
    tracker.fs_notify("file1.txt")

    live = tracker.recompute()
    assert live["ok"] is True
    file_row = next(f for f in live["files"] if f["path"] == "file1.txt")
    hunk = file_row["hunks"][0]
    assert hunk["kind"] == "removed"
    assert hunk["source"] == "external"

    res = tracker.revert_hunk(hunk["id"])
    assert res["ok"] is True
    with open(os.path.join(repo, "file1.txt"), "r", encoding="utf-8") as f:
        assert "line two" in f.read()


def test_api_checkpoints_hunks_endpoints(temp_git_repo):
    from types import SimpleNamespace

    from harness.api.checkpoints import (
        CheckpointServices,
        get_checkpoints_hunks,
        post_checkpoints_hunks_accept,
        post_checkpoints_hunks_revert,
    )

    repo = temp_git_repo
    svc = CheckpointServices(
        cfg=SimpleNamespace(repo=repo),
        get_active_session_id=lambda: "sess-hunks",
    )

    path = os.path.join(repo, "file1.txt")
    tracker = _tracker(repo)
    tracker.ensure_baseline()
    with open(path, "w", encoding="utf-8") as f:
        f.write("line one\napi agent\n")
    record_agent_write(repo, "file1.txt", session_id="sess-hunks")

    code, payload = get_checkpoints_hunks(svc)
    assert code == 200
    assert payload["ok"] is True
    assert payload["files"]
    hunk_id = payload["files"][0]["hunks"][0]["id"]
    assert payload["files"][0]["hunks"][0]["source"] == "agent"

    code, accept = post_checkpoints_hunks_accept({"hunk_id": hunk_id}, svc)
    assert code == 200
    assert accept["status"] == "accepted"

    with open(path, "w", encoding="utf-8") as f:
        f.write("line one\napi external\n")
    fs_notify(repo, "file1.txt", session_id="sess-hunks")
    code, payload2 = get_checkpoints_hunks(svc)
    assert code == 200
    ext_hunk = payload2["files"][0]["hunks"][0]
    assert ext_hunk["source"] == "external"

    code, revert = post_checkpoints_hunks_revert({"hunk_id": ext_hunk["id"]}, svc)
    assert code == 200
    assert revert["status"] == "reverted"

    code, missing = post_checkpoints_hunks_accept({}, svc)
    assert code == 400
