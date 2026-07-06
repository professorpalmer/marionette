"""Built-in terminal: a stdlib-only PTY manager.

Spawns the user's shell in a pseudo-terminal (os.openpty), runs it in the workspace
repo, and exposes read/write/resize. No native deps (no node-pty) -- pure Python stdlib,
matching the harness's portable ethos. The HTTP server streams output over SSE and feeds
input via POST. Sessions are keyed by id; output is buffered so a late SSE subscriber
still catches up.
"""
import os
import select
import signal
import struct
import threading
import time
import uuid

# pty/termios/fcntl are Unix-only. On Windows they don't exist; importing this
# module must still succeed so the backend (and every non-terminal feature)
# boots. PTY_AVAILABLE gates session creation instead.
try:
    import pty
    import fcntl
    import termios
    PTY_AVAILABLE = True
except ImportError:
    pty = fcntl = termios = None
    PTY_AVAILABLE = False


class PtySession:
    def __init__(self, cwd: str = None, cols: int = 80, rows: int = 24):
        if not PTY_AVAILABLE:
            raise RuntimeError(
                "The built-in terminal requires a Unix PTY and is not yet "
                "supported on Windows."
            )
        self.id = uuid.uuid4().hex[:12]
        self.cols = cols
        self.rows = rows
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._alive = True
        self._cwd = cwd if (cwd and os.path.isdir(cwd)) else os.path.expanduser("~")

        shell = os.environ.get("SHELL", "/bin/bash")
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            # child: exec the shell in the workspace dir
            try:
                os.chdir(self._cwd)
            except Exception:
                pass
            env = dict(os.environ)
            env["TERM"] = "xterm-256color"
            try:
                os.execvpe(shell, [shell, "-l"], env)
            except Exception:
                os._exit(1)
        else:
            # parent: set initial window size, start reader thread
            self._set_winsize(rows, cols)
            self._reader = threading.Thread(target=self._read_loop, daemon=True)
            self._reader.start()

    def _set_winsize(self, rows: int, cols: int):
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, winsize)
        except Exception:
            pass

    def _read_loop(self):
        while self._alive:
            try:
                r, _, _ = select.select([self.fd], [], [], 0.2)
                if self.fd in r:
                    data = os.read(self.fd, 65536)
                    if not data:
                        break
                    with self._lock:
                        self._buffer.extend(data)
                        # cap buffer at ~256KB so long sessions don't grow unbounded
                        if len(self._buffer) > 262144:
                            del self._buffer[: len(self._buffer) - 262144]
            except (OSError, ValueError):
                break
        self._alive = False

    def read_since(self, offset: int) -> tuple:
        """Return (new_bytes, new_offset) for output produced since `offset`."""
        with self._lock:
            total = len(self._buffer)
            if offset < 0 or offset > total:
                offset = 0
            return bytes(self._buffer[offset:]), total

    def write(self, data: str):
        if not self._alive:
            return
        try:
            os.write(self.fd, data.encode("utf-8", "replace"))
        except OSError:
            self._alive = False

    def resize(self, rows: int, cols: int):
        self.rows, self.cols = rows, cols
        self._set_winsize(rows, cols)

    def alive(self) -> bool:
        if not self._alive:
            return False
        try:
            pid, _ = os.waitpid(self.pid, os.WNOHANG)
            if pid == self.pid:
                self._alive = False
        except OSError:
            self._alive = False
        return self._alive

    def kill(self):
        self._alive = False
        try:
            os.kill(self.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass


class PtyManager:
    def __init__(self):
        self._sessions = {}
        self._lock = threading.Lock()

    def create(self, cwd: str = None, cols: int = 80, rows: int = 24) -> PtySession:
        s = PtySession(cwd=cwd, cols=cols, rows=rows)
        with self._lock:
            self._sessions[s.id] = s
        return s

    def get(self, sid: str):
        with self._lock:
            return self._sessions.get(sid)

    def kill(self, sid: str):
        with self._lock:
            s = self._sessions.pop(sid, None)
        if s:
            s.kill()

    def reap(self):
        with self._lock:
            dead = [sid for sid, s in self._sessions.items() if not s.alive()]
            for sid in dead:
                self._sessions.pop(sid, None)
