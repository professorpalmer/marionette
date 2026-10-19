"""Shut the backend down when the desktop app that spawned it is gone.

Electron spawns the backend in its own process group so a quit can signal the
whole tree. A crash or force-quit of Electron sends no signal, so without this
watch the backend, its dashboard and its workers live on under launchd.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable, Optional

PARENT_ENV = "HARNESS_PARENT_PID"
_POLL_S = 2.0


def _watched_pid(env) -> int:
    try:
        pid = int(str(env.get(PARENT_ENV) or "").strip())
    except ValueError:
        return 0
    # Only a pid that is our real parent: a relaunch under another
    # supervisor must not inherit a stale claim.
    return pid if pid > 1 and pid == os.getppid() else 0


def _wait_windows(pid: int) -> Optional[Callable[[], None]]:
    """Return a blocking wait on the parent's process handle, or None."""
    import ctypes

    synchronize = 0x00100000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return None

    def wait() -> None:
        try:
            kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
        finally:
            kernel32.CloseHandle(handle)

    return wait


def start_parent_watch(
    on_gone: Callable[[], None],
    *,
    env=None,
    poll_s: float = _POLL_S,
) -> Optional[threading.Thread]:
    """Call ``on_gone`` once when the parent in ``HARNESS_PARENT_PID`` exits.

    POSIX reparents an orphan, so ``getppid`` changes. Windows does not
    reparent, so the thread waits on a handle opened while the parent lives.
    """
    pid = _watched_pid(os.environ if env is None else env)
    if not pid:
        return None
    if os.name == "nt":
        wait = _wait_windows(pid)
        if wait is None:
            return None
    else:
        def wait() -> None:
            while os.getppid() == pid:
                time.sleep(poll_s)

    def run() -> None:
        wait()
        on_gone()

    thread = threading.Thread(target=run, name="parent-watch", daemon=True)
    thread.start()
    return thread
