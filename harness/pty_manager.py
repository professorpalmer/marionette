"""Built-in terminal: a stdlib-only PTY manager.

Spawns the user's shell in a pseudo-terminal, runs it in the workspace repo,
and exposes read/write/resize. No native deps (no node-pty) -- pure Python stdlib,
matching the harness's portable ethos. The HTTP server streams output over SSE and feeds
input via POST. Sessions are keyed by id; output is buffered so a late SSE subscriber
still catches up.

On Unix: os.openpty / pty.fork with select/fcntl/termios.
On Windows: ConPTY via ctypes (CreatePseudoConsole, kernel32 pipes/process APIs).
"""
from __future__ import annotations

import os
import ntpath
import shutil
import struct
import threading
import uuid
from typing import Optional

from harness.diag import note as _diag_note

# ---------------------------------------------------------------------------
# Platform availability
# ---------------------------------------------------------------------------

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32

    _CONPTY_AVAILABLE = hasattr(kernel32, "CreatePseudoConsole")
    PTY_AVAILABLE = _CONPTY_AVAILABLE

    if _CONPTY_AVAILABLE:
        # Constants
        INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
        PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
        EXTENDED_STARTUPINFO_PRESENT = 0x00080000
        CREATE_UNICODE_ENVIRONMENT = 0x00000400
        STARTF_USESTDHANDLES = 0x00000100
        STILL_ACTIVE = 259
        ERROR_BROKEN_PIPE = 109

        class COORD(ctypes.Structure):
            _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

        class SECURITY_ATTRIBUTES(ctypes.Structure):
            _fields_ = [
                ("nLength", wintypes.DWORD),
                ("lpSecurityDescriptor", wintypes.LPVOID),
                ("bInheritHandle", wintypes.BOOL),
            ]

        class STARTUPINFOW(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR),
                ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD),
                ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD),
                ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD),
                ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD),
                ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
                ("hStdInput", wintypes.HANDLE),
                ("hStdOutput", wintypes.HANDLE),
                ("hStdError", wintypes.HANDLE),
            ]

        class STARTUPINFOEXW(ctypes.Structure):
            _fields_ = [
                ("StartupInfo", STARTUPINFOW),
                ("lpAttributeList", ctypes.c_void_p),
            ]

        class PROCESS_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("hProcess", wintypes.HANDLE),
                ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD),
                ("dwThreadId", wintypes.DWORD),
            ]

        HPCON = ctypes.c_void_p

        kernel32.CreatePipe.argtypes = [
            ctypes.POINTER(wintypes.HANDLE),
            ctypes.POINTER(wintypes.HANDLE),
            ctypes.POINTER(SECURITY_ATTRIBUTES),
            wintypes.DWORD,
        ]
        kernel32.CreatePipe.restype = wintypes.BOOL

        kernel32.CreatePseudoConsole.argtypes = [
            COORD,
            wintypes.HANDLE,
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(HPCON),
        ]
        kernel32.CreatePseudoConsole.restype = ctypes.c_long

        kernel32.ResizePseudoConsole.argtypes = [HPCON, COORD]
        kernel32.ResizePseudoConsole.restype = ctypes.c_long

        kernel32.ClosePseudoConsole.argtypes = [HPCON]
        kernel32.ClosePseudoConsole.restype = None

        kernel32.InitializeProcThreadAttributeList.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL

        kernel32.UpdateProcThreadAttribute.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL

        kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
        kernel32.DeleteProcThreadAttributeList.restype = None

        kernel32.ReadFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        kernel32.ReadFile.restype = wintypes.BOOL

        kernel32.WriteFile.argtypes = [
            wintypes.HANDLE,
            wintypes.LPCVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        kernel32.WriteFile.restype = wintypes.BOOL

        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL

        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL

        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        kernel32.GetLastError.argtypes = []
        kernel32.GetLastError.restype = wintypes.DWORD

else:
    import select
    import signal

    try:
        import pty
        import fcntl
        import termios

        PTY_AVAILABLE = True
    except ImportError:
        pty = fcntl = termios = None
        PTY_AVAILABLE = False


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_BUFFER_CAP = 262144


def _resolve_cwd(cwd: Optional[str]) -> str:
    if cwd and os.path.isdir(cwd):
        return cwd
    return os.path.expanduser("~")


def _append_to_buffer(buffer: bytearray, lock: threading.Lock, data: bytes) -> None:
    if not data:
        return
    with lock:
        buffer.extend(data)
        if len(buffer) > _BUFFER_CAP:
            del buffer[: len(buffer) - _BUFFER_CAP]


def _default_shell() -> str:
    if os.name == "nt":
        return r"C:\Windows\System32\cmd.exe"
    return "/bin/sh"


def _shell_path_is_usable(path: str | None) -> bool:
    """True when *path* is an absolute path to an existing executable file."""
    return bool(path) and os.path.isabs(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def _validated_env_shell(env_var: str, *, where: str) -> str | None:
    """Return *env_var*'s value when it is a trusted shell path, else None."""
    candidate = os.environ.get(env_var, "")
    if _shell_path_is_usable(candidate):
        return candidate
    if candidate:
        _diag_note(
            where,
            msg=f"invalid {env_var}={candidate!r}, falling back to platform default",
        )
    return None


def _windows_shell() -> str:
    """Prefer pwsh.exe, then powershell.exe, then cmd.exe / COMSPEC."""
    for name in ("pwsh", "pwsh.exe", "powershell.exe", "cmd.exe"):
        found = shutil.which(name)
        if found:
            return found
    comspec = _validated_env_shell("COMSPEC", where="pty_manager._windows_shell")
    if comspec:
        return comspec
    return _default_shell()


def _unix_shell() -> str:
    """Return a trusted interactive shell for Unix PTY sessions."""
    shell = _validated_env_shell("SHELL", where="pty_manager._unix_shell")
    return shell or _default_shell()


def _windows_shell_command(shell: str) -> str:
    """Build a CreateProcessW command line for an interactive shell."""
    base = ntpath.basename(shell).lower()
    if base in ("pwsh.exe", "powershell.exe"):
        return f'"{shell}" -NoLogo'
    return f'"{shell}"'


def _windows_env_block(env: dict) -> ctypes.Array:
    """Double-null-terminated UTF-16 environment block for CreateProcessW."""
    parts = [f"{key}={value}\0" for key, value in sorted(env.items())]
    blob = "".join(parts) + "\0"
    return ctypes.create_unicode_buffer(blob)


def _close_win_handle(handle) -> None:
    if handle and handle != INVALID_HANDLE_VALUE:
        try:
            kernel32.CloseHandle(handle)
        except Exception:
            pass


def _win_check(result, msg: str) -> None:
    if not result:
        err = kernel32.GetLastError()
        raise OSError(err, msg)


# ---------------------------------------------------------------------------
# PtySession -- single public class, platform-specific internals
# ---------------------------------------------------------------------------


class PtySession:
    """Pseudo-terminal session with a unified API on Unix and Windows."""

    def __init__(self, cwd: str = None, cols: int = 80, rows: int = 24):
        if not PTY_AVAILABLE:
            if os.name == "nt":
                raise RuntimeError(
                    "The built-in terminal requires Windows 10 1809+ ConPTY "
                    "(CreatePseudoConsole) and is not available on this system."
                )
            raise RuntimeError(
                "The built-in terminal requires a Unix PTY and is not available "
                "on this platform."
            )
        self.id = uuid.uuid4().hex[:12]
        self.cols = cols
        self.rows = rows
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._alive = True
        self._cwd = _resolve_cwd(cwd)
        if os.name == "nt":
            self._init_conpty(cols, rows)
        else:
            self._init_unix(cols, rows)

    # ----- Unix -------------------------------------------------------------

    def _init_unix(self, cols: int, rows: int) -> None:
        shell = _unix_shell()
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
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
            self._set_winsize_unix(rows, cols)
            self._reader = threading.Thread(target=self._read_loop_unix, daemon=True)
            self._reader.start()

    def _set_winsize_unix(self, rows: int, cols: int) -> None:
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, winsize)
        except Exception:
            pass

    def _read_loop_unix(self) -> None:
        while self._alive:
            try:
                r, _, _ = select.select([self.fd], [], [], 0.2)
                if self.fd in r:
                    data = os.read(self.fd, 65536)
                    if not data:
                        break
                    _append_to_buffer(self._buffer, self._lock, data)
            except (OSError, ValueError):
                break
        self._alive = False

    def _kill_unix(self) -> None:
        try:
            os.kill(self.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass

    def _alive_unix(self) -> bool:
        try:
            pid, _ = os.waitpid(self.pid, os.WNOHANG)
            if pid == self.pid:
                self._alive = False
        except OSError:
            self._alive = False
        return self._alive

    # ----- Windows ConPTY ---------------------------------------------------

    def _init_conpty(self, cols: int, rows: int) -> None:
        self._hpc = HPCON()
        self._input_write = wintypes.HANDLE()
        self._output_read = wintypes.HANDLE()
        self._input_read = wintypes.HANDLE()
        self._output_write = wintypes.HANDLE()
        self._process_handle = wintypes.HANDLE()
        self._thread_handle = wintypes.HANDLE()
        self.pid = 0
        self.fd = None  # Unix-only; kept for API symmetry
        self._hpc_closed = False

        _win_check(
            kernel32.CreatePipe(
                ctypes.byref(self._input_read),
                ctypes.byref(self._input_write),
                None,
                0,
            ),
            "CreatePipe input failed",
        )
        _win_check(
            kernel32.CreatePipe(
                ctypes.byref(self._output_read),
                ctypes.byref(self._output_write),
                None,
                0,
            ),
            "CreatePipe output failed",
        )

        size = COORD(cols, rows)
        hr = kernel32.CreatePseudoConsole(
            size,
            self._input_read,
            self._output_write,
            0,
            ctypes.byref(self._hpc),
        )
        if hr != 0:
            self._cleanup_conpty_handles()
            raise OSError(hr, "CreatePseudoConsole failed")

        attr_size = ctypes.c_size_t(0)
        kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(attr_size))
        attr_buf = (ctypes.c_byte * attr_size.value)()
        _win_check(
            kernel32.InitializeProcThreadAttributeList(attr_buf, 1, 0, ctypes.byref(attr_size)),
            "InitializeProcThreadAttributeList failed",
        )

        hpcon_val = ctypes.c_void_p(self._hpc.value)
        _win_check(
            kernel32.UpdateProcThreadAttribute(
                attr_buf,
                0,
                PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
                hpcon_val,
                ctypes.sizeof(hpcon_val),
                None,
                None,
            ),
            "UpdateProcThreadAttribute failed",
        )

        si = STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        si.StartupInfo.hStdInput = self._input_read
        si.StartupInfo.hStdOutput = self._output_write
        si.StartupInfo.hStdError = self._output_write
        si.lpAttributeList = ctypes.cast(attr_buf, ctypes.c_void_p)

        shell = _windows_shell()
        cmd = _windows_shell_command(shell)
        cmd_buf = ctypes.create_unicode_buffer(cmd)

        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        env_block = _windows_env_block(env)

        pi = PROCESS_INFORMATION()
        cwd_buf = self._cwd if os.path.isdir(self._cwd) else None
        try:
            _win_check(
                kernel32.CreateProcessW(
                    None,
                    cmd_buf,
                    None,
                    None,
                    False,
                    EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT,
                    env_block,
                    cwd_buf,
                    ctypes.byref(si.StartupInfo),
                    ctypes.byref(pi),
                ),
                "CreateProcessW failed",
            )
        finally:
            kernel32.DeleteProcThreadAttributeList(attr_buf)

        # ConPTY dup'd these ends into the child; close our copies now.
        _close_win_handle(self._input_read)
        _close_win_handle(self._output_write)
        self._input_read = wintypes.HANDLE()
        self._output_write = wintypes.HANDLE()

        self._process_handle = pi.hProcess
        self._thread_handle = pi.hThread
        self.pid = pi.dwProcessId

        self._reader = threading.Thread(target=self._read_loop_conpty, daemon=True)
        self._reader.start()

    def _read_loop_conpty(self) -> None:
        while self._alive:
            buf = ctypes.create_string_buffer(65536)
            nread = wintypes.DWORD(0)
            ok = kernel32.ReadFile(
                self._output_read,
                buf,
                65536,
                ctypes.byref(nread),
                None,
            )
            if not ok or nread.value == 0:
                break
            _append_to_buffer(self._buffer, self._lock, buf.raw[: nread.value])
        self._alive = False

    def _cleanup_conpty_handles(self) -> None:
        _close_win_handle(getattr(self, "_input_read", None))
        _close_win_handle(getattr(self, "_input_write", None))
        _close_win_handle(getattr(self, "_output_read", None))
        _close_win_handle(getattr(self, "_output_write", None))
        _close_win_handle(getattr(self, "_process_handle", None))
        _close_win_handle(getattr(self, "_thread_handle", None))
        if getattr(self, "_hpc_closed", False):
            return
        hpc = getattr(self, "_hpc", None)
        if hpc:
            try:
                kernel32.ClosePseudoConsole(hpc)
            except Exception:
                pass
            self._hpc = HPCON()
            self._hpc_closed = True

    def _kill_conpty(self) -> None:
        if getattr(self, "_hpc", None) and not getattr(self, "_hpc_closed", False):
            try:
                kernel32.ClosePseudoConsole(self._hpc)
            except Exception:
                pass
            self._hpc = HPCON()
            self._hpc_closed = True
        if getattr(self, "_process_handle", None):
            try:
                kernel32.TerminateProcess(self._process_handle, 1)
            except Exception:
                pass
        _close_win_handle(getattr(self, "_input_write", None))
        _close_win_handle(getattr(self, "_output_read", None))
        _close_win_handle(getattr(self, "_process_handle", None))
        _close_win_handle(getattr(self, "_thread_handle", None))
        self._input_write = wintypes.HANDLE()
        self._output_read = wintypes.HANDLE()
        self._process_handle = wintypes.HANDLE()
        self._thread_handle = wintypes.HANDLE()

    def _alive_conpty(self) -> bool:
        if not getattr(self, "_process_handle", None):
            self._alive = False
            return False
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(self._process_handle, ctypes.byref(code)):
            self._alive = False
            return False
        if code.value != STILL_ACTIVE:
            self._alive = False
        return self._alive

    # ----- Public API -------------------------------------------------------

    def read_since(self, offset: int) -> tuple:
        """Return (new_bytes, new_offset) for output produced since `offset`."""
        with self._lock:
            total = len(self._buffer)
            if offset < 0 or offset > total:
                offset = 0
            return bytes(self._buffer[offset:]), total

    def write(self, data: str) -> None:
        if not self._alive:
            return
        if os.name == "nt":
            self._write_conpty(data)
        else:
            self._write_unix(data)

    def _write_unix(self, data: str) -> None:
        try:
            os.write(self.fd, data.encode("utf-8", "replace"))
        except OSError:
            self._alive = False

    def _write_conpty(self, data: str) -> None:
        payload = data.encode("utf-8", "replace")
        written = wintypes.DWORD(0)
        ok = kernel32.WriteFile(
            self._input_write,
            payload,
            len(payload),
            ctypes.byref(written),
            None,
        )
        if not ok:
            self._alive = False

    def resize(self, rows: int, cols: int) -> None:
        self.rows, self.cols = rows, cols
        if os.name == "nt":
            if getattr(self, "_hpc", None) and not getattr(self, "_hpc_closed", False):
                try:
                    kernel32.ResizePseudoConsole(self._hpc, COORD(cols, rows))
                except Exception:
                    pass
        else:
            self._set_winsize_unix(rows, cols)

    def alive(self) -> bool:
        if not self._alive:
            return False
        if os.name == "nt":
            return self._alive_conpty()
        return self._alive_unix()

    def kill(self) -> None:
        self._alive = False
        if os.name == "nt":
            self._kill_conpty()
        else:
            self._kill_unix()


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
