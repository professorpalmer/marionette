from __future__ import annotations

"""Session-scoped persistent Python REPL for the ``run_ipython`` pilot tool.

Steals Prime Agent's *persistent kernel* insight without IPython monoculture:
native file/shell/swarm tools stay first-class; this is one extra tool for
stateful probes (variables survive across turns in the same session).

Each kernel is a child process (``harness/ipython_child.py``). A cell used to
run in a backend thread: it changed the cwd and stdout of the whole backend,
and a cell that passed its time limit kept the kernel lock for ever. Now a
time limit interrupts the cell and the namespace stays. A cell that ignores
the interrupt ends the child, and the next cell starts a new one. The child
gets ``model_child_env``, so it does not see provider keys.

The child uses IPython's InteractiveShell when it is installed, else a stdlib
exec loop, so Marionette stays stdlib-first.
"""

import itertools
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import weakref
from dataclasses import dataclass
from typing import Any, Optional

DEFAULT_TIMEOUT_SEC = 60.0
DEFAULT_OUTPUT_CAP = 64 * 1024
MAX_IPYTHON_DEPTH = 2
_ipython_depth = threading.local()

_CHILD_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ipython_child.py")
_START_TIMEOUT_SEC = 30.0
# Time a cell gets to stop after the interrupt before the child is ended.
_INTERRUPT_GRACE_SEC = 3.0


@dataclass
class KernelResult:
    ok: bool
    output: str
    error: str = ""
    backend: str = "stdlib"
    timed_out: bool = False


def _clamp(text: str, cap: int = DEFAULT_OUTPUT_CAP) -> str:
    if not text:
        return ""
    if len(text) <= cap:
        return text
    omitted = len(text) - cap
    return text[:cap] + f"\n… [truncated {omitted} chars]"


def _end_child(proc: "subprocess.Popen[str]") -> None:
    if proc.poll() is not None:
        return
    try:
        proc.kill()
        proc.wait(timeout=5)
    except Exception:
        pass


class _Child:
    """One kernel process and the thread that reads its replies."""

    def __init__(self, cwd: str) -> None:
        from .child_env import model_child_env

        self.proc = subprocess.Popen(
            [sys.executable, "-I", _CHILD_SCRIPT],
            cwd=cwd,
            env=model_child_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.replies: "queue.Queue[Optional[dict]]" = queue.Queue()
        threading.Thread(target=self._read, name="marionette-ipython-reader", daemon=True).start()
        self._finalizer = weakref.finalize(self, _end_child, self.proc)
        ready = self.next_reply(_START_TIMEOUT_SEC)
        if not ready or not ready.get("ready"):
            self.end()
            raise RuntimeError("python kernel did not start")
        self.backend = str(ready.get("backend") or "stdlib")

    def _read(self) -> None:
        for line in self.proc.stdout:
            try:
                self.replies.put(json.loads(line))
            except ValueError:
                continue
        self.replies.put(None)

    def next_reply(self, timeout: float) -> Optional[dict]:
        try:
            return self.replies.get(timeout=max(0.1, timeout))
        except queue.Empty:
            return None

    def send(self, request: dict) -> None:
        self.proc.stdin.write(json.dumps(request) + "\n")
        self.proc.stdin.flush()

    def interrupt(self) -> bool:
        if os.name == "nt" or self.proc.poll() is not None:
            return False
        try:
            self.proc.send_signal(signal.SIGINT)
            return True
        except OSError:
            return False

    def alive(self) -> bool:
        return self.proc.poll() is None

    def end(self) -> None:
        self._finalizer()


class PersistentPythonKernel:
    """One persistent user namespace per ConversationalSession."""

    def __init__(self, cwd: str) -> None:
        self.cwd = os.path.realpath(cwd) if cwd else os.getcwd()
        self._lock = threading.Lock()
        self._child: Optional[_Child] = None
        self._backend = "stdlib"
        self._ids = itertools.count(1)
        self._closed = False

    @property
    def backend(self) -> str:
        return self._backend

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._child is not None:
                self._child.end()
                self._child = None

    def execute(
        self,
        code: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SEC,
        output_cap: int = DEFAULT_OUTPUT_CAP,
    ) -> KernelResult:
        if self._closed:
            return KernelResult(
                ok=False, output="", error="kernel closed", backend=self._backend,
            )
        source = (code or "").strip()
        if not source:
            return KernelResult(
                ok=False, output="", error="empty code", backend=self._backend,
            )
        with self._lock:
            return self._execute_locked(source, float(timeout), output_cap)

    def _execute_locked(self, source: str, timeout: float, cap: int) -> KernelResult:
        try:
            child = self._live_child()
        except Exception as exc:
            return KernelResult(
                ok=False, output="", error=f"python kernel could not start: {exc}",
                backend=self._backend,
            )
        request_id = next(self._ids)
        try:
            child.send({"id": request_id, "code": source, "cwd": self.cwd, "cap": cap})
        except (OSError, ValueError):
            self._drop_child()
            return KernelResult(
                ok=False, output="", error="python kernel exited; run the cell again",
                backend=self._backend,
            )
        reply = self._reply_for(child, request_id, timeout)
        if reply is None and child.alive():
            interrupted = child.interrupt()
            reply = self._reply_for(child, request_id, _INTERRUPT_GRACE_SEC) if interrupted else None
            if reply is None:
                self._drop_child()
                return KernelResult(
                    ok=False, output="",
                    error=(
                        f"Stopped at the {timeout:g} s limit. The cell did not stop on "
                        "the interrupt, so the kernel restarted and its variables are gone."
                    ),
                    backend=self._backend, timed_out=True,
                )
            return KernelResult(
                ok=False, output=_clamp(reply.get("output") or "", cap),
                error=f"Interrupted at the {timeout:g} s limit. Variables are kept.",
                backend=self._backend, timed_out=True,
            )
        if reply is None:
            self._drop_child()
            return KernelResult(
                ok=False, output="",
                error="python kernel exited during the cell; variables are gone",
                backend=self._backend,
            )
        if reply.get("interrupted"):
            return KernelResult(
                ok=False, output=_clamp(reply.get("output") or "", cap),
                error="interrupted", backend=self._backend,
            )
        return KernelResult(
            ok=bool(reply.get("ok")),
            output=_clamp(reply.get("output") or "", cap),
            error=_clamp(reply.get("error") or "", cap),
            backend=self._backend,
        )

    def _reply_for(self, child: _Child, request_id: int, timeout: float) -> Optional[dict]:
        deadline = time.monotonic() + timeout
        while True:
            reply = child.next_reply(deadline - time.monotonic())
            if reply is None or reply.get("id") == request_id:
                return reply
            if time.monotonic() >= deadline:
                return None

    def _live_child(self) -> _Child:
        if self._child is None or not self._child.alive():
            self._child = _Child(self.cwd)
            self._backend = self._child.backend
        return self._child

    def _drop_child(self) -> None:
        if self._child is not None:
            self._child.end()
            self._child = None


def get_or_create_kernel(session: Any) -> PersistentPythonKernel:
    """Lazy session attribute ``_ipython_kernel``."""
    existing = getattr(session, "_ipython_kernel", None)
    if isinstance(existing, PersistentPythonKernel) and not existing._closed:
        # Retarget cwd if workspace moved.
        config = getattr(session, "config", None)
        cwd = (
            getattr(config, "repo", None)
            or getattr(config, "state_dir", None)
            or os.getcwd()
        )
        if cwd and os.path.realpath(str(cwd)) != existing.cwd:
            existing.cwd = os.path.realpath(str(cwd))
        return existing
    config = getattr(session, "config", None)
    cwd = (
        getattr(config, "repo", None)
        or getattr(config, "state_dir", None)
        or os.getcwd()
    )
    kernel = PersistentPythonKernel(str(cwd))
    try:
        setattr(session, "_ipython_kernel", kernel)
    except Exception:
        pass
    return kernel
