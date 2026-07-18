"""Hermes-style message-edit rewind: truncate + stash + restore."""
from __future__ import annotations

from harness.conversation import ConversationalSession


def _sess(tmp_path):
    s = ConversationalSession.__new__(ConversationalSession)
    # Minimal init for rewind helpers (avoid full pilot/MCP wiring).
    s._history = [{"role": "system", "content": "sys"}]
    s._display_transcript = []
    s._session_job_ids = []
    s._rewind_stash = None
    s.state_dir = str(tmp_path)
    import threading
    s._busy = threading.Lock()
    return s


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
