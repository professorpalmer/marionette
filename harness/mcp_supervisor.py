from __future__ import annotations

"""Parent-death supervisor for stdio MCP servers (POSIX only).

Stdio MCP servers run in their own process groups, so a harness killed with
SIGKILL leaves them running (macOS has no PR_SET_PDEATHSIG). One small child
per harness process holds the registered process groups. It reads
newline-delimited JSON ``{"op": "add"|"del", "pgid": N}`` on stdin; at EOF
(the harness died or closed the pipe) it terminates every registered group.

Run as ``python -m harness.mcp_supervisor``. The harness side is
``register`` / ``unregister`` / ``close``; all are best effort and never raise.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Optional, Set

# Captured at import so a test that patches subprocess.Popen for an MCP
# server does not also capture the supervisor spawn.
_Popen = subprocess.Popen

TERM_GRACE_S = 2.0


# ---- supervisor child -----------------------------------------------------
def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except OSError:
        pass


def terminate_groups(groups: Set[int], grace_s: float = TERM_GRACE_S) -> None:
    for pgid in groups:
        _signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace_s
    alive = {g for g in groups if _group_exists(g)}
    while alive and time.monotonic() < deadline:
        time.sleep(0.05)
        alive = {g for g in alive if _group_exists(g)}
    for pgid in alive:
        _signal_group(pgid, signal.SIGKILL)


def main() -> int:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    own = os.getpgrp()
    groups: Set[int] = set()
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        pgid = msg.get("pgid")
        if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 1 or pgid == own:
            continue
        if msg.get("op") == "add":
            groups.add(pgid)
        elif msg.get("op") == "del":
            groups.discard(pgid)
    terminate_groups(groups)
    return 0


# ---- harness side ---------------------------------------------------------
def _enabled() -> bool:
    if os.name == "nt":
        return False
    return (os.environ.get("HARNESS_MCP_SUPERVISOR") or "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


class _Link:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._groups: Set[int] = set()
        self._warned = False

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _warn_once(self, exc: BaseException) -> None:
        if self._warned:
            return
        self._warned = True
        try:
            from .diag import note as _diag

            _diag("mcp.supervisor", exc)
        except Exception:
            pass

    def _write(self, msg: dict) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        self._proc.stdin.flush()

    def _spawn(self) -> bool:
        """Start a supervisor and replay the current set (lock held)."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(p for p in (root, env.get("PYTHONPATH")) if p)
        try:
            self._proc = _Popen(
                [sys.executable, "-m", "harness.mcp_supervisor"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=root,
                env=env,
                start_new_session=True,
                close_fds=True,
            )
            for pgid in sorted(self._groups):
                self._write({"op": "add", "pgid": pgid})
            return True
        except Exception as exc:
            self._drop()
            self._warn_once(exc)
            return False

    def _drop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            pass

    def register(self, pgid: int) -> None:
        if not _enabled():
            return
        with self._lock:
            self._groups.add(pgid)
            for _ in range(2):
                if not self._alive():
                    self._drop()
                    # A fresh supervisor gets the whole set, this pgid included.
                    self._spawn()
                    return
                try:
                    self._write({"op": "add", "pgid": pgid})
                    return
                except Exception:
                    self._drop()

    def unregister(self, pgid: int) -> None:
        if not _enabled():
            return
        with self._lock:
            self._groups.discard(pgid)
            if not self._alive():
                return
            try:
                self._write({"op": "del", "pgid": pgid})
            except Exception:
                self._drop()

    def close(self) -> None:
        with self._lock:
            self._drop()


_LINK = _Link()


def register(pgid: object) -> None:
    if isinstance(pgid, int) and not isinstance(pgid, bool) and pgid > 1:
        try:
            _LINK.register(pgid)
        except Exception as exc:
            _LINK._warn_once(exc)


def unregister(pgid: object) -> None:
    if isinstance(pgid, int) and not isinstance(pgid, bool) and pgid > 1:
        try:
            _LINK.unregister(pgid)
        except Exception:
            pass


def close() -> None:
    """Close the pipe. Call only after every MCP server is stopped."""
    try:
        _LINK.close()
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
