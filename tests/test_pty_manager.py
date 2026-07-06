"""Tests for the built-in terminal PTY manager (stdlib-only)."""
import time

import pytest

from harness.pty_manager import PTY_AVAILABLE, PtyManager

# PTY sessions require Unix pty/termios; on Windows PtyManager.create raises
# RuntimeError by design (import still succeeds so the backend boots).
pytestmark = pytest.mark.skipif(
    not PTY_AVAILABLE, reason="no Unix PTY on this platform (Windows)"
)


def test_pty_create_write_read_kill():
    m = PtyManager()
    s = m.create(cwd="/tmp", cols=80, rows=24)
    assert s.id
    assert s.alive()
    time.sleep(0.5)  # shell init
    s.write("echo PTY_TEST_$((6*7))\n")
    time.sleep(0.7)
    data, off = s.read_since(0)
    out = data.decode("utf-8", "replace")
    assert "PTY_TEST_42" in out
    # incremental read returns only new bytes
    s.write("printf done\n")
    time.sleep(0.4)
    data2, off2 = s.read_since(off)
    assert off2 >= off
    m.kill(s.id)
    time.sleep(0.2)
    assert not s.alive()
    assert m.get(s.id) is None


def test_pty_resize_does_not_crash():
    m = PtyManager()
    s = m.create(cwd="/tmp")
    s.resize(40, 120)
    assert s.cols == 120 and s.rows == 40
    m.kill(s.id)


def test_pty_get_missing_returns_none():
    m = PtyManager()
    assert m.get("nonexistent") is None


def test_pty_reap_removes_dead_sessions():
    """A killed PTY session must be reaped out of the manager so dead/exited
    terminals do not pile up (the Restart button relies on this cleanup)."""
    import time
    from harness.pty_manager import PtyManager
    m = PtyManager()
    s = m.create()
    sid = s.id
    assert m.get(sid) is not None
    # Kill the underlying shell and reap.
    s.kill()
    time.sleep(0.1)
    m.reap()
    assert m.get(sid) is None


def test_pty_kill_is_idempotent():
    """Killing the same session twice (or a missing id) must not raise."""
    from harness.pty_manager import PtyManager
    m = PtyManager()
    s = m.create()
    sid = s.id
    m.kill(sid)
    m.kill(sid)        # already gone -> no error
    m.kill("nonexistent-id")  # never existed -> no error
    assert m.get(sid) is None


def test_pty_write_after_kill_is_safe():
    """Writing to a killed session must not raise (frontend may send a stray
    keystroke before it notices the exit)."""
    import time
    from harness.pty_manager import PtyManager
    m = PtyManager()
    s = m.create()
    s.kill()
    time.sleep(0.1)
    s.write("echo hi\n")  # must be a no-op, not an exception
    assert s.alive() is False
