"""Tests for the built-in terminal PTY manager (stdlib-only)."""
import os
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from harness import pty_manager
from harness.pty_manager import (
    PTY_AVAILABLE,
    PtyManager,
    PtySession,
    _append_to_buffer,
    _unix_shell,
    _windows_shell,
    _windows_shell_command,
)

pytestmark_unix = pytest.mark.skipif(os.name == "nt", reason="Unix PTY integration test")
pytestmark_win_e2e = pytest.mark.skipif(os.name != "nt", reason="Windows ConPTY e2e test")
pytestmark_pty = pytest.mark.skipif(not PTY_AVAILABLE, reason="PTY not available on this system")


# ---------------------------------------------------------------------------
# Cross-platform unit tests (no real PTY required)
# ---------------------------------------------------------------------------


def test_append_to_buffer_caps_at_256kb():
    buf = bytearray()
    lock = threading.Lock()
    chunk = b"x" * 100_000
    for _ in range(4):
        _append_to_buffer(buf, lock, chunk)
    assert len(buf) == 262144


def test_read_since_offset_clamping():
    s = object.__new__(PtySession)
    s._buffer = bytearray(b"hello")
    s._lock = threading.Lock()
    data, off = s.read_since(-5)
    assert data == b"hello" and off == 5
    data2, off2 = s.read_since(999)
    assert data2 == b"hello" and off2 == 5
    data3, off3 = s.read_since(2)
    assert data3 == b"llo" and off3 == 5


def test_windows_shell_prefers_pwsh(monkeypatch):
    calls = []

    def fake_which(name):
        calls.append(name)
        if name == "pwsh":
            return r"C:\Program Files\PowerShell\7\pwsh.exe"
        return None

    monkeypatch.setattr(pty_manager.shutil, "which", fake_which)
    assert _windows_shell() == r"C:\Program Files\PowerShell\7\pwsh.exe"
    assert calls[0] == "pwsh"


def test_windows_shell_falls_back_to_comspec(monkeypatch):
    monkeypatch.setattr(pty_manager.shutil, "which", lambda _n: None)
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    monkeypatch.setattr(pty_manager.os.path, "isabs", lambda p: p.startswith("C:\\"))
    monkeypatch.setattr(pty_manager.os.path, "isfile", lambda p: p.endswith("cmd.exe"))
    monkeypatch.setattr(pty_manager.os, "access", lambda _p, _m: True)
    assert _windows_shell() == r"C:\Windows\System32\cmd.exe"


@pytest.mark.skipif(os.name != "nt", reason="Windows COMSPEC validation")
def test_windows_shell_rejects_bogus_comspec(monkeypatch):
    monkeypatch.setattr(pty_manager.shutil, "which", lambda _n: None)
    monkeypatch.setenv("COMSPEC", "not-a-shell")
    notes = []
    monkeypatch.setattr(pty_manager, "_diag_note", lambda where, **kw: notes.append((where, kw)))
    assert _windows_shell() == r"C:\Windows\System32\cmd.exe"
    assert notes
    assert notes[0][0] == "pty_manager._windows_shell"


@pytest.mark.skipif(os.name == "nt", reason="Unix SHELL validation")
def test_unix_shell_rejects_bogus_shell(monkeypatch):
    monkeypatch.setenv("SHELL", "bash")
    notes = []
    monkeypatch.setattr(pty_manager, "_diag_note", lambda where, **kw: notes.append((where, kw)))
    assert _unix_shell() == "/bin/sh"
    assert notes
    assert notes[0][0] == "pty_manager._unix_shell"


@pytest.mark.skipif(os.name == "nt", reason="Unix SHELL validation")
def test_unix_shell_uses_valid_absolute_shell(monkeypatch, tmp_path):
    shell = tmp_path / "myshell"
    shell.write_text("")
    shell.chmod(0o755)
    monkeypatch.setenv("SHELL", str(shell))
    assert _unix_shell() == str(shell)


def test_windows_shell_command_powershell():
    cmd = _windows_shell_command(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    assert cmd == r'"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo'


def test_pty_unavailable_raises_clear_error(monkeypatch):
    monkeypatch.setattr(pty_manager, "PTY_AVAILABLE", False)
    with pytest.raises(RuntimeError, match="not available"):
        PtySession()


def _bootstrap_conpty_test_symbols(monkeypatch):
    """Inject minimal ConPTY symbols so kernel32 mocks work on non-Windows CI."""
    import ctypes
    from ctypes import wintypes

    if hasattr(pty_manager, "kernel32") and hasattr(pty_manager, "HPCON"):
        return

    monkeypatch.setattr(pty_manager, "ctypes", ctypes, raising=False)
    monkeypatch.setattr(pty_manager, "wintypes", wintypes, raising=False)
    monkeypatch.setattr(pty_manager, "kernel32", MagicMock(), raising=False)
    monkeypatch.setattr(pty_manager, "INVALID_HANDLE_VALUE", wintypes.HANDLE(-1).value, raising=False)
    monkeypatch.setattr(pty_manager, "PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE", 0x00020016, raising=False)
    monkeypatch.setattr(pty_manager, "EXTENDED_STARTUPINFO_PRESENT", 0x00080000, raising=False)
    monkeypatch.setattr(pty_manager, "CREATE_UNICODE_ENVIRONMENT", 0x00000400, raising=False)
    monkeypatch.setattr(pty_manager, "STILL_ACTIVE", 259, raising=False)
    monkeypatch.setattr(pty_manager, "ERROR_BROKEN_PIPE", 109, raising=False)
    monkeypatch.setattr(pty_manager, "HANDLE_FLAG_INHERIT", 0x00000001, raising=False)
    monkeypatch.setattr(pty_manager, "STARTF_USESTDHANDLES", 0x00000100, raising=False)

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
    monkeypatch.setattr(pty_manager, "COORD", COORD, raising=False)
    monkeypatch.setattr(pty_manager, "SECURITY_ATTRIBUTES", SECURITY_ATTRIBUTES, raising=False)
    monkeypatch.setattr(pty_manager, "STARTUPINFOW", STARTUPINFOW, raising=False)
    monkeypatch.setattr(pty_manager, "STARTUPINFOEXW", STARTUPINFOEXW, raising=False)
    monkeypatch.setattr(pty_manager, "PROCESS_INFORMATION", PROCESS_INFORMATION, raising=False)
    monkeypatch.setattr(pty_manager, "HPCON", HPCON, raising=False)


def test_conpty_init_calls_kernel32_plumbing(monkeypatch):
    """Mock kernel32 so ConPTY setup can be exercised on any CI runner."""
    _bootstrap_conpty_test_symbols(monkeypatch)
    monkeypatch.setattr(pty_manager.os, "name", "nt")
    monkeypatch.setattr(pty_manager, "PTY_AVAILABLE", True)

    fake_k32 = MagicMock()
    fake_k32.CreatePipe.return_value = True

    def fake_create_pseudo_console(_size, _hin, _hout, _flags, phpc):
        target = getattr(phpc, "_obj", getattr(phpc, "contents", phpc))
        target.value = 0xDEADBEEF
        return 0

    fake_k32.CreatePseudoConsole.side_effect = fake_create_pseudo_console
    fake_k32.InitializeProcThreadAttributeList.return_value = True
    fake_k32.UpdateProcThreadAttribute.return_value = True
    fake_k32.CreateProcessW.return_value = True
    fake_k32.DeleteProcThreadAttributeList.return_value = None
    fake_k32.CloseHandle.return_value = True
    fake_k32.ClosePseudoConsole.return_value = None
    fake_k32.GetLastError.return_value = 0

    read_calls = {"n": 0, "wait": True}

    def _set_dword(arg, value):
        obj = getattr(arg, "contents", None) or getattr(arg, "_obj", arg)
        obj.value = value

    def fake_readfile(_h, buf, _size, nread, _ov):
        if read_calls["n"] == 0:
            data = b"mock output"
            pty_manager.ctypes.memmove(buf, data, len(data))
            _set_dword(nread, len(data))
            read_calls["n"] += 1
            return True
        while read_calls["wait"]:
            time.sleep(0.01)
        _set_dword(nread, 0)
        return False

    fake_k32.ReadFile.side_effect = fake_readfile
    fake_k32.WriteFile.return_value = True

    def fake_exit_code(_h, code):
        _set_dword(code, pty_manager.STILL_ACTIVE)
        return True

    fake_k32.GetExitCodeProcess.side_effect = fake_exit_code
    fake_k32.TerminateProcess.return_value = True
    fake_k32.ResizePseudoConsole.return_value = 0

    def capture_create_process(*args, **_kw):
        pi_ref = args[-1]
        pi = getattr(pi_ref, "contents", None) or getattr(pi_ref, "_obj", pi_ref)
        pi.hProcess = 111
        pi.hThread = 222
        pi.dwProcessId = 4242
        return True

    fake_k32.CreateProcessW.side_effect = capture_create_process
    monkeypatch.setattr(pty_manager, "kernel32", fake_k32)
    monkeypatch.setattr(pty_manager, "_windows_shell", lambda: r"C:\Windows\System32\cmd.exe")

    s = PtySession(cwd=os.getcwd(), cols=80, rows=24)
    assert s.pid == 4242
    assert fake_k32.CreatePipe.call_count == 2
    fake_k32.CreatePseudoConsole.assert_called_once()
    fake_k32.CreateProcessW.assert_called_once()
    fake_k32.UpdateProcThreadAttribute.assert_called_once()

    time.sleep(0.2)
    data, _off = s.read_since(0)
    assert b"mock output" in data

    s.write("hi")
    fake_k32.WriteFile.assert_called()

    s.resize(30, 100)
    assert s.rows == 30 and s.cols == 100
    fake_k32.ResizePseudoConsole.assert_called_once()

    read_calls["wait"] = False
    s.kill()
    fake_k32.ClosePseudoConsole.assert_called()
    fake_k32.TerminateProcess.assert_called()


# ---------------------------------------------------------------------------
# Unix integration tests
# ---------------------------------------------------------------------------


@pytestmark_unix
@pytestmark_pty
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
    s.write("printf done\n")
    time.sleep(0.4)
    data2, off2 = s.read_since(off)
    assert off2 >= off
    m.kill(s.id)
    time.sleep(0.2)
    assert not s.alive()
    assert m.get(s.id) is None


@pytestmark_unix
@pytestmark_pty
def test_pty_resize_does_not_crash():
    m = PtyManager()
    s = m.create(cwd="/tmp")
    s.resize(40, 120)
    assert s.cols == 120 and s.rows == 40
    m.kill(s.id)


@pytestmark_pty
def test_pty_get_missing_returns_none():
    m = PtyManager()
    assert m.get("nonexistent") is None


@pytestmark_unix
@pytestmark_pty
def test_pty_reap_removes_dead_sessions():
    m = PtyManager()
    s = m.create()
    sid = s.id
    assert m.get(sid) is not None
    s.kill()
    time.sleep(0.1)
    m.reap()
    assert m.get(sid) is None


@pytestmark_unix
@pytestmark_pty
def test_pty_kill_is_idempotent():
    m = PtyManager()
    s = m.create()
    sid = s.id
    m.kill(sid)
    m.kill(sid)
    m.kill("nonexistent-id")
    assert m.get(sid) is None


@pytestmark_unix
@pytestmark_pty
def test_pty_write_after_kill_is_safe():
    m = PtyManager()
    s = m.create()
    s.kill()
    time.sleep(0.1)
    s.write("echo hi\n")
    assert s.alive() is False


# ---------------------------------------------------------------------------
# Windows ConPTY end-to-end
# ---------------------------------------------------------------------------


@pytestmark_win_e2e
@pytestmark_pty
def test_conpty_create_write_read_resize_kill():
    m = PtyManager()
    cwd = os.getcwd()
    s = m.create(cwd=cwd, cols=80, rows=24)
    assert s.id
    assert s.alive()

    deadline = time.time() + 5.0
    offset = 0
    while time.time() < deadline:
        time.sleep(0.2)
        if s.alive():
            break
    assert s.alive()

    s.write("echo hello\r\n")
    found = False
    deadline = time.time() + 10.0
    while time.time() < deadline:
        data, offset = s.read_since(offset)
        if b"hello" in data.lower():
            found = True
            break
        time.sleep(0.15)
    assert found, "expected 'hello' in ConPTY output"

    s.resize(40, 120)
    assert s.cols == 120 and s.rows == 40

    m.kill(s.id)
    time.sleep(0.3)
    assert not s.alive()
    assert m.get(s.id) is None
