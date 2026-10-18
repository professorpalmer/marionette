"""The backend notices when the desktop app that spawned it dies."""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time

from harness.parent_watch import PARENT_ENV, start_parent_watch

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The middle process stands in for Electron: it spawns the "backend" with its
# own pid in HARNESS_PARENT_PID, prints the backend pid, then idles until killed.
_PARENT = textwrap.dedent("""
    import os, subprocess, sys
    child = subprocess.Popen(
        [sys.executable, "-c", sys.argv[1], sys.argv[2]],
        env={**os.environ, "HARNESS_PARENT_PID": str(os.getpid())},
    )
    print(child.pid, flush=True)
    child.wait()
""")

_BACKEND = textwrap.dedent("""
    import os, sys, threading
    from harness.parent_watch import start_parent_watch
    gone = threading.Event()
    started = start_parent_watch(gone.set, poll_s=0.05)
    open(sys.argv[1] + ".started", "w").write("yes" if started else "no")
    gone.wait(30)
    open(sys.argv[1], "w").write("gone")
""")


def _wait_for(predicate, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
            capture_output=True, text=True, check=False,
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        waited, _ = os.waitpid(pid, os.WNOHANG)
        return waited == 0
    except ChildProcessError:
        return True


def test_backend_exits_when_its_parent_is_killed(tmp_path):
    marker = str(tmp_path / "gone")
    env = {**os.environ, "PYTHONPATH": _REPO}
    parent = subprocess.Popen(
        [sys.executable, "-c", _PARENT, _BACKEND, marker],
        env=env,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        backend_pid = int(parent.stdout.readline().strip())
        assert _wait_for(lambda: os.path.exists(marker + ".started"), 20)
        with open(marker + ".started") as fh:
            assert fh.read() == "yes"
        assert not os.path.exists(marker)

        parent.kill()  # SIGKILL / TerminateProcess: no chance to clean up
        parent.wait(10)

        assert _wait_for(lambda: os.path.exists(marker), 10), "backend never noticed"
        assert _wait_for(lambda: not _alive(backend_pid), 10), "backend still running"
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.stdout.close()


def test_no_watch_without_our_real_parent_pid():
    calls = []
    assert start_parent_watch(lambda: calls.append(1), env={}) is None
    assert start_parent_watch(lambda: calls.append(1), env={PARENT_ENV: "junk"}) is None
    # A stale pid from another launcher is not our parent.
    stale = os.getppid() + 100000
    assert start_parent_watch(lambda: calls.append(1), env={PARENT_ENV: str(stale)}) is None
    assert calls == []
