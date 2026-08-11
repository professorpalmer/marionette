"""Hermes-style message-edit rewind: truncate + stash + restore."""
from __future__ import annotations

import os
import subprocess
import types

from harness.checkpoints import CheckpointStore
from harness.conversation import ConversationalSession


def _sess(tmp_path, *, repo: str | None = None, session_id: str = "sess-rewind"):
    s = ConversationalSession.__new__(ConversationalSession)
    # Minimal init for rewind helpers (avoid full pilot/MCP wiring).
    s._history = [{"role": "system", "content": "sys"}]
    s._display_transcript = []
    s._session_job_ids = []
    s._rewind_stash = None
    s.state_dir = str(tmp_path)
    s.harness_session_id = session_id
    s.config = types.SimpleNamespace(repo=repo)
    s._checkpoints = CheckpointStore(repo, session_id=session_id) if repo else None
    import threading
    s._busy = threading.Lock()
    return s


def _init_git_repo(path: str) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    tracked = os.path.join(path, "tracked.txt")
    with open(tracked, "w", encoding="utf-8") as f:
        f.write("base\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=path,
        check=True,
        capture_output=True,
    )


def test_rewind_to_user_ordinal_truncates_and_prefills(tmp_path):
    s = _sess(tmp_path)
    s._display_transcript = [
        {"type": "message", "role": "user", "text": "one"},
        {"type": "message", "role": "assistant", "text": "a1"},
        {"type": "message", "role": "user", "text": "two"},
        {"type": "message", "role": "assistant", "text": "a2"},
    ]
    s._history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "a2"},
    ]

    res = s.rewind_to_user_ordinal(1)
    assert res["ok"] is True
    assert res["prefill"] == "two"
    assert len(s._display_transcript) == 2
    assert s._display_transcript[-1]["text"] == "a1"
    assert [m["role"] for m in s._history[1:]] == ["user", "assistant"]
    assert s._rewind_stash is not None
    assert res.get("workspace_restored") is False
    assert "workspace files were not restored" in (res.get("notice") or "")


def test_restore_rewind_stash(tmp_path):
    s = _sess(tmp_path)
    s._display_transcript = [
        {"type": "message", "role": "user", "text": "one"},
        {"type": "message", "role": "assistant", "text": "a1"},
        {"type": "message", "role": "user", "text": "two"},
    ]
    s._history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "two"},
    ]
    assert s.rewind_to_user_ordinal(1)["ok"]
    assert len(s._display_transcript) == 2
    restored = s.restore_rewind_stash()
    assert restored["ok"] is True
    assert len(s._display_transcript) == 3
    assert s._display_transcript[-1]["text"] == "two"
    assert s._rewind_stash is None


def test_restore_rewind_stash_installs_command_approval_lock(tmp_path):
    """Minimal/legacy sessions lack ``_command_approval_lock`` until restore."""
    s = _sess(tmp_path)
    assert not hasattr(s, "_command_approval_lock")
    s._display_transcript = [
        {"type": "message", "role": "user", "text": "one"},
        {"type": "message", "role": "assistant", "text": "a1"},
        {"type": "message", "role": "user", "text": "two"},
    ]
    s._history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "two"},
    ]
    assert s.rewind_to_user_ordinal(1)["ok"]
    restored = s.restore_rewind_stash()
    assert restored["ok"] is True
    assert hasattr(s, "_command_approval_lock")
    assert s._command_approval_lock is not None
    # Guard must keep locking semantics (real lock, not a no-op).
    with s._command_approval_lock_guard():
        s._pending_command_approvals = {}
        s._approved_commands = set()


def test_command_approval_lock_guard_concurrent_first_touch_is_thread_safe(tmp_path):
    """Concurrent lazy install must publish one shared lock and containers."""
    import threading

    s = _sess(tmp_path)
    assert not hasattr(s, "_command_approval_lock")
    barrier = threading.Barrier(8)
    locks: list = []
    pendings: list = []
    approveds: list = []
    errors: list = []

    def touch() -> None:
        try:
            barrier.wait(timeout=5)
            lock = s._command_approval_lock_guard()
            locks.append(lock)
            pendings.append(s._pending_command_approvals)
            approveds.append(s._approved_commands)
            with lock:
                s._approved_commands.add("x")
                s._approved_commands.discard("x")
        except Exception as exc:  # pragma: no cover - failure surfaces below
            errors.append(exc)

    threads = [threading.Thread(target=touch) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert errors == []
    assert len(locks) == 8
    assert all(lock is locks[0] for lock in locks)
    assert all(p is pendings[0] for p in pendings)
    assert all(a is approveds[0] for a in approveds)
    assert locks[0] is s._command_approval_lock
    assert isinstance(pendings[0], dict)
    assert isinstance(approveds[0], set)
    assert locks[0].acquire(blocking=False)
    locks[0].release()


def test_rewind_blocked_while_busy(tmp_path):
    s = _sess(tmp_path)
    s._display_transcript = [{"type": "message", "role": "user", "text": "x"}]
    s._history = [{"role": "system", "content": "sys"}, {"role": "user", "content": "x"}]
    assert s._busy.acquire(blocking=False)
    try:
        res = s.rewind_to_user_ordinal(0)
        assert res["ok"] is False
        assert res.get("code") == "busy"
    finally:
        s._busy.release()


def test_rewind_with_checkpoint_restores_written_file(tmp_path):
    """Edit-prior rewind restores disk from the cut turn's pre-mutation checkpoint."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(str(repo))
    target = repo / "agent.txt"
    target.write_text("before\n", encoding="utf-8")

    s = _sess(tmp_path, repo=str(repo), session_id="sess-ws-restore")
    s._display_transcript = [
        {"type": "message", "role": "user", "text": "one"},
        {"type": "message", "role": "assistant", "text": "a1"},
        {"type": "message", "role": "user", "text": "two"},
        {"type": "message", "role": "assistant", "text": "a2"},
    ]
    s._history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "a2"},
    ]

    # Pre-mutation checkpoint for turn 1, then agent overwrites the file.
    cp_id = s._checkpoints.snapshot(
        label="Before writing agent.txt",
        trigger="write_file",
        session_id="sess-ws-restore",
        user_ordinal=1,
    )
    assert cp_id
    target.write_text("after agent edit\n", encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "after agent edit\n"

    res = s.rewind_to_user_ordinal(1)
    assert res["ok"] is True
    assert res["workspace_restored"] is True
    assert res["checkpoint_id"] == cp_id
    assert "workspace restored" in (res.get("notice") or "")
    assert target.read_text(encoding="utf-8") == "before\n"
    assert s._rewind_stash.get("workspace_auto_snapshot_id")

    # Cancel/Revert puts the post-edit disk state back.
    restored = s.restore_rewind_stash()
    assert restored["ok"] is True
    assert restored.get("workspace_restored") is True
    assert target.read_text(encoding="utf-8") == "after agent edit\n"


def test_rewind_without_checkpoint_does_not_claim_disk_restore(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(str(repo))
    target = repo / "agent.txt"
    target.write_text("stale agent edit\n", encoding="utf-8")

    s = _sess(tmp_path, repo=str(repo), session_id="sess-no-cp")
    s._display_transcript = [
        {"type": "message", "role": "user", "text": "one"},
        {"type": "message", "role": "assistant", "text": "a1"},
        {"type": "message", "role": "user", "text": "two"},
    ]
    s._history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "two"},
    ]

    res = s.rewind_to_user_ordinal(1)
    assert res["ok"] is True
    assert res["workspace_restored"] is False
    assert res.get("checkpoint_id") is None
    notice = res.get("notice") or ""
    assert "workspace files were not restored" in notice
    assert "workspace restored to that turn" not in notice
    # Disk left unchanged — do not pretend.
    assert target.read_text(encoding="utf-8") == "stale agent edit\n"


def test_find_rewind_checkpoint_prefers_exact_then_later_ordinal(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(str(repo))
    store = CheckpointStore(str(repo), session_id="sess-find")
    earlier = store.snapshot("t0", "write_file", user_ordinal=0)
    later = store.snapshot("t2", "write_file", user_ordinal=2)
    assert earlier and later
    # Cut at ordinal 1: no exact match → earliest later-turn checkpoint (2).
    picked = store.find_rewind_checkpoint(1, session_id="sess-find")
    assert picked is not None
    assert picked["id"] == later
    exact = store.find_rewind_checkpoint(0, session_id="sess-find")
    assert exact is not None
    assert exact["id"] == earlier
    assert store.find_rewind_checkpoint(3, session_id="sess-find") is None
