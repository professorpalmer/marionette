from __future__ import annotations

import os
import re
import sys
import json
import logging
import subprocess
import tempfile
import threading
from typing import Optional

from .paths import path_within
from .secure_files import restrict_to_owner
from .diag import note as _diag

logger = logging.getLogger("pmharness.worktrees")

_managed_lock = threading.Lock()
# Normalized worktree path -> {pid: kind}
_managed_processes: dict[str, dict[int, str]] = {}


def _normalize_worktree_path(path: str) -> str:
    try:
        return os.path.realpath(path)
    except Exception:
        return os.path.abspath(path)


def managed_worktree_root(path: str) -> Optional[str]:
    """Return the worktree root when ``path`` is under ``.pmharness-worktrees``."""
    try:
        real = _normalize_worktree_path(path)
        marker = f".pmharness-worktrees{os.sep}"
        idx = real.find(marker)
        if idx < 0:
            norm = real.replace("\\", "/")
            slash_marker = "/.pmharness-worktrees/"
            slash_idx = norm.find(slash_marker)
            if slash_idx < 0:
                return None
            root = norm[: slash_idx + len(slash_marker)]
            rest = norm[slash_idx + len(slash_marker) :]
            name = rest.split("/", 1)[0]
            if not name:
                return None
            return _normalize_worktree_path(root.replace("/", os.sep) + name)
        root = real[: idx + len(marker)]
        rest = real[idx + len(marker) :]
        name = rest.split(os.sep, 1)[0]
        if not name:
            return None
        return _normalize_worktree_path(os.path.join(root, name))
    except Exception:
        return None


def register_worktree_process(
    worktree_or_cwd: str, pid: int, *, kind: str = "worker"
) -> None:
    """Record a Marionette-spawned process tree root for orphan reaping."""
    if pid <= 1:
        return
    root = managed_worktree_root(worktree_or_cwd)
    if not root:
        return
    key = _normalize_worktree_path(root)
    with _managed_lock:
        bucket = _managed_processes.setdefault(key, {})
        bucket[int(pid)] = kind or "worker"


def unregister_worktree_process(worktree_or_cwd: str, pid: int) -> None:
    root = managed_worktree_root(worktree_or_cwd)
    if not root:
        return
    key = _normalize_worktree_path(root)
    with _managed_lock:
        bucket = _managed_processes.get(key)
        if not bucket:
            return
        bucket.pop(int(pid), None)
        if not bucket:
            _managed_processes.pop(key, None)


def bind_worktree_subprocess(
    worktree_or_cwd: str, proc: object, *, kind: str = "worker"
) -> None:
    """Register a spawned child for registry-only reaping (spawn-site helper)."""
    pid = getattr(proc, "pid", None)
    if pid is None:
        return
    register_worktree_process(worktree_or_cwd, int(pid), kind=kind)


def release_worktree_subprocess(worktree_or_cwd: str, proc: object) -> None:
    """Drop a finished child from the registry so PID reuse cannot kill a stranger."""
    pid = getattr(proc, "pid", None)
    if pid is None:
        return
    unregister_worktree_process(worktree_or_cwd, int(pid))


def clear_worktree_process_registry(path: str) -> None:
    key = _normalize_worktree_path(path)
    with _managed_lock:
        _managed_processes.pop(key, None)


def _registered_pids_for_worktree(path: str) -> list[int]:
    key = _normalize_worktree_path(path)
    with _managed_lock:
        return list(_managed_processes.get(key, {}).keys())


def clear_managed_process_registry_for_tests() -> None:
    """Test helper: drop all registered worktree process provenance."""
    with _managed_lock:
        _managed_processes.clear()

def _git(repo: str, *args: str, timeout: int = 15) -> tuple[int, str, str]:
    if not repo:
        return 1, "", "No repository configured"
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, p.stdout.strip(), p.stderr.strip()

def _is_repo(repo: str) -> bool:
    if not repo:
        return False
    rc, out, _ = _git(repo, "rev-parse", "--is-inside-work-tree")
    return rc == 0 and out == "true"

def _branch_exists(repo: str, branch: str) -> bool:
    rc, out, _ = _git(repo, "branch", "--list", branch)
    return rc == 0 and bool(out.strip())

def list_worktrees(repo: str) -> list[dict]:
    if not _is_repo(repo):
        return []
    rc, out, _ = _git(repo, "worktree", "list", "--porcelain")
    if rc != 0:
        return []
    
    worktrees = []
    current = {}
    
    for line in out.splitlines():
        line = line.strip()
        if not line:
            if current:
                worktrees.append(current)
                current = {}
            continue
        
        parts = line.split(" ", 1)
        key = parts[0]
        val = parts[1] if len(parts) > 1 else ""
        
        if key == "worktree":
            if current:
                worktrees.append(current)
            current = {
                "path": val,
                "branch": "",
                "head": "",
                "is_main": False,
                "locked": False
            }
        elif key == "HEAD" and current:
            current["head"] = val
        elif key == "branch" and current:
            branch_ref = val
            if branch_ref.startswith("refs/heads/"):
                current["branch"] = branch_ref[11:]
            elif branch_ref.startswith("refs/"):
                current["branch"] = branch_ref.split("/")[-1]
            else:
                current["branch"] = branch_ref
        elif key == "locked" and current:
            current["locked"] = True
            
    if current:
        worktrees.append(current)
        
    real_repo = os.path.realpath(repo) if repo else ""
    for wt in worktrees:
        wt_path = os.path.realpath(wt["path"])
        if real_repo and wt_path == real_repo:
            wt["is_main"] = True
            
    if worktrees and not any(wt["is_main"] for wt in worktrees):
        worktrees[0]["is_main"] = True
        
    return worktrees

def _get_managed_dir(repo: str) -> str:
    real_repo = os.path.realpath(repo)
    return os.path.abspath(os.path.join(real_repo, "..", ".pmharness-worktrees"))

def _is_confined(path: str, parent: str) -> bool:
    """True if ``path`` is STRICTLY inside ``parent`` -- a managed worktree must
    be nested within the managed dir, never the managed dir itself. Shares the
    confinement primitive with is_safe_path; see harness.paths (allow_equal is
    the only difference)."""
    return path_within(path, parent, allow_equal=False)

def _safe_branch_name(branch: str) -> str:
    safe = re.sub(r'[^a-zA-Z0-9_\-]', '_', branch)
    return safe.strip('._-')

def add_worktree(repo: str, branch: str, base: str = "HEAD", path: Optional[str] = None) -> dict:
    if not _is_repo(repo):
        raise RuntimeError("No git repository configured or invalid repository")
    
    if branch.startswith("-") or (base and base.startswith("-")):
        raise ValueError("Invalid branch or base name (cannot start with '-')")
        
    managed_dir = _get_managed_dir(repo)
    os.makedirs(managed_dir, exist_ok=True)
    
    if path is None:
        safe_branch = _safe_branch_name(branch)
        path = os.path.join(managed_dir, safe_branch)
    else:
        path = os.path.abspath(path)
        if not _is_confined(path, managed_dir):
            raise ValueError("Path traversal detected or path outside managed directory")
            
    if _branch_exists(repo, branch):
        args = ["worktree", "add", path, branch]
    else:
        args = ["worktree", "add", "-b", branch, path, base]
        
    rc, out, err = _git(repo, *args)
    if rc != 0:
        raise RuntimeError(err or out)
        
    return {"path": path, "branch": branch}

def _cwd_under(cwd: str, worktree: str) -> bool:
    """True only when `cwd` is genuinely inside `worktree`.

    Uses realpath normalization and a trailing-separator prefix check so a
    sibling dir (pmedit-89 vs pmedit-8941d0fa) never matches. Best-effort:
    returns False on any error rather than raising.
    """
    try:
        if not cwd or not worktree:
            return False
        wt = os.path.realpath(worktree)
        cw = os.path.realpath(cwd)
        if cw == wt:
            return True
        return cw.startswith(wt + os.sep)
    except Exception:
        return False


def _parse_lsof_cwd_output(stdout: str) -> list[tuple[int, str]]:
    """Parse ``lsof -Fpn`` cwd lines into (pid, cwd) pairs."""
    out_pairs: list[tuple[int, str]] = []
    cur_pid: Optional[int] = None
    for line in (stdout or "").splitlines():
        if not line:
            continue
        tag, val = line[0], line[1:]
        if tag == "p":
            try:
                cur_pid = int(val)
            except Exception:
                cur_pid = None
        elif tag == "n" and cur_pid is not None:
            out_pairs.append((cur_pid, val))
    return out_pairs


def _worktree_pid_cwds_lsof() -> Optional[list[tuple[int, str]]]:
    """Run lsof for cwd enumeration. None when lsof is missing, slow, or denied."""
    try:
        # -Fn emits n<cwd>, -Fp emits p<pid>; -d cwd restricts to the cwd fd.
        p = subprocess.run(
            ["lsof", "-a", "-d", "cwd", "-Fpn"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
    except Exception:
        return None
    if p.returncode != 0:
        return None
    return _parse_lsof_cwd_output(p.stdout)


def _worktree_pid_cwds_proc_linux() -> list[tuple[int, str]]:
    """Best-effort /proc/<pid>/cwd enumeration on Linux. Returns [] on any error."""
    out_pairs: list[tuple[int, str]] = []
    try:
        proc_root = "/proc"
        if not os.path.isdir(proc_root):
            return out_pairs
        for name in os.listdir(proc_root):
            if not name.isdigit():
                continue
            try:
                pid = int(name)
            except ValueError:
                continue
            cwd_link = os.path.join(proc_root, name, "cwd")
            try:
                cwd = os.readlink(cwd_link)
            except OSError:
                continue
            if cwd:
                out_pairs.append((pid, cwd))
    except Exception:
        return out_pairs
    return out_pairs


def _worktree_pid_cwds_posix() -> list[tuple[int, str]]:
    """Best-effort enumeration of (pid, cwd) on POSIX.

    macOS: lsof only (/proc is absent). Linux: lsof first, then /proc fallback
    when lsof is missing, slow, or denied. Never raises.
    """
    lsof_pairs = _worktree_pid_cwds_lsof()
    if lsof_pairs is not None:
        return lsof_pairs
    if sys.platform == "linux":
        return _worktree_pid_cwds_proc_linux()
    return []


def _worktree_pid_cwds_windows() -> list[tuple[int, str]]:
    """Best-effort enumeration of (pid, cwd) on Windows. Returns [] on any error."""
    out_pairs: list[tuple[int, str]] = []
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_INFORMATION = 0x0400
        PROCESS_VM_READ = 0x0010
        ProcessBasicInformation = 0
        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESS_BASIC_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("Reserved1", wintypes.LPVOID),
                ("PebBaseAddress", wintypes.LPVOID),
                ("Reserved2_0", wintypes.LPVOID),
                ("Reserved2_1", wintypes.LPVOID),
                ("UniqueProcessId", wintypes.LPVOID),
                ("Reserved3", wintypes.LPVOID),
            ]

        class UNICODE_STRING(ctypes.Structure):
            _fields_ = [
                ("Length", wintypes.USHORT),
                ("MaximumLength", wintypes.USHORT),
                ("Buffer", wintypes.LPVOID),
            ]

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel32 = ctypes.windll.kernel32
        ntdll = ctypes.windll.ntdll
        ptr_size = ctypes.sizeof(ctypes.c_void_p)
        params_offset = 0x20 if ptr_size == 8 else 0x10
        cwd_offset = 0x38 if ptr_size == 8 else 0x24
        ptr_fmt = "<Q" if ptr_size == 8 else "<I"
        import struct as _struct

        def _ptr_value(raw) -> int:
            if isinstance(raw, int):
                return raw
            return ctypes.cast(raw, ctypes.c_void_p).value or 0

        def _read_ptr(h, addr: int) -> Optional[int]:
            buf = ctypes.create_string_buffer(ptr_size)
            read = ctypes.c_size_t()
            if not kernel32.ReadProcessMemory(
                h, ctypes.c_void_p(addr), buf, ptr_size, ctypes.byref(read)
            ):
                return None
            return _struct.unpack(ptr_fmt, buf.raw)[0]

        def _process_cwd(pid: int) -> Optional[str]:
            h = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
            if not h:
                return None
            try:
                pbi = PROCESS_BASIC_INFORMATION()
                ret_len = wintypes.ULONG()
                status = ntdll.NtQueryInformationProcess(
                    h,
                    ProcessBasicInformation,
                    ctypes.byref(pbi),
                    ctypes.sizeof(pbi),
                    ctypes.byref(ret_len),
                )
                peb_addr = _ptr_value(pbi.PebBaseAddress)
                if status != 0 or not peb_addr:
                    return None
                proc_params = _read_ptr(h, peb_addr + params_offset)
                if proc_params is None:
                    return None
                us = UNICODE_STRING()
                read = ctypes.c_size_t()
                if not kernel32.ReadProcessMemory(
                    h,
                    ctypes.c_void_p(proc_params + cwd_offset),
                    ctypes.byref(us),
                    ctypes.sizeof(us),
                    ctypes.byref(read),
                ):
                    return None
                buf_addr = _ptr_value(us.Buffer)
                if not buf_addr or us.Length == 0:
                    return None
                path_buf = ctypes.create_string_buffer(us.Length)
                if not kernel32.ReadProcessMemory(
                    h, ctypes.c_void_p(buf_addr), path_buf, us.Length, ctypes.byref(read)
                ):
                    return None
                return path_buf.raw.decode("utf-16-le", errors="replace")
            finally:
                kernel32.CloseHandle(h)

        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap in (-1, 0xFFFFFFFF):
            return out_pairs
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if not kernel32.Process32FirstW(snap, ctypes.byref(entry)):
                return out_pairs
            while True:
                pid = int(entry.th32ProcessID)
                if pid > 1:
                    cwd = _process_cwd(pid)
                    if cwd:
                        out_pairs.append((pid, cwd))
                if not kernel32.Process32NextW(snap, ctypes.byref(entry)):
                    break
        finally:
            kernel32.CloseHandle(snap)
    except Exception:
        return out_pairs
    return out_pairs


def _worktree_pid_cwds() -> list[tuple[int, str]]:
    """Best-effort enumeration of (pid, cwd). Returns [] on any error."""
    if os.name == "nt":
        return _worktree_pid_cwds_windows()
    return _worktree_pid_cwds_posix()


def _taskkill_tree(pid: int) -> None:
    """Force-kill a process tree on Windows. Best-effort; never raises."""
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass


def reap_worktree_processes(path: str) -> int:
    """Terminate registered Marionette worker/indexer trees for this worktree.

    Only PIDs explicitly registered via :func:`register_worktree_process` are
    reaped. Foreign processes whose cwd happens to lie under the worktree are
    never targeted. Orphaned registered subprocesses (e.g. codegraph indexers)
    are still cleaned up before the directory is removed.
    Best-effort: never raises; returns the count signaled. Never signals
    pid<=1, this process, or its parent.
    """
    signaled = 0
    try:
        import signal as _signal
        import time as _time
        me = os.getpid()
        parent = os.getppid()
        wt_key = _normalize_worktree_path(path)
        targets = [
            pid for pid in _registered_pids_for_worktree(wt_key)
            if pid > 1 and pid != me and pid != parent
        ]
        if os.name != "posix":
            for pid in targets:
                try:
                    _taskkill_tree(pid)
                    signaled += 1
                except Exception:
                    continue
            clear_worktree_process_registry(wt_key)
            return signaled
        for pid in targets:
            try:
                os.kill(pid, _signal.SIGTERM)
                signaled += 1
            except Exception:
                continue
        if targets:
            _time.sleep(0.6)
            for pid in targets:
                try:
                    os.kill(pid, 0)          # still alive?
                    os.kill(pid, _signal.SIGKILL)
                except Exception:
                    continue
        clear_worktree_process_registry(wt_key)
    except Exception:
        return signaled
    return signaled


def remove_worktree(repo: str, path: str, force: bool = False) -> None:
    managed_dir = _get_managed_dir(repo)
    path = os.path.abspath(path)
    if not _is_confined(path, managed_dir):
        raise ValueError("Path traversal detected or path outside managed directory")

    # Reap orphaned subprocesses (e.g. codegraph indexers) whose cwd is inside
    # this worktree BEFORE git yanks the directory out from under them, so they
    # do not survive as init-reparented zombies. Never let a reap failure block
    # the actual worktree removal.
    try:
        reap_worktree_processes(path)
    except Exception as exc:
        logger.warning("worktree reaper failed for %s: %s", path, exc)

    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(path)
    
    rc, out, err = _git(repo, *args)
    if rc != 0:
        raise RuntimeError(err or out)

def prune_worktrees(repo: str) -> None:
    rc, out, err = _git(repo, "worktree", "prune")
    if rc != 0:
        raise RuntimeError(err or out)

_WORKTREES_JSON = os.path.join(os.path.expanduser("~/.pmharness"), "worktrees.json")

def get_max_worktrees() -> int:
    if os.path.exists(_WORKTREES_JSON):
        try:
            with open(_WORKTREES_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
                return int(data.get("max_worktrees", 25))
        except Exception as exc:
            logger.warning("failed to read max_worktrees from %s: %s", _WORKTREES_JSON, exc)
    return 25

def set_max_worktrees(max_count: int) -> None:
    os.makedirs(os.path.dirname(_WORKTREES_JSON), exist_ok=True)
    try:
        temp_fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(_WORKTREES_JSON))
        with os.fdopen(temp_fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"max_worktrees": max_count}, f)
        os.replace(temp_path, _WORKTREES_JSON)
        if not restrict_to_owner(_WORKTREES_JSON):
            _diag("secure_files.restrict_failed", msg=_WORKTREES_JSON)
    except Exception as exc:
        logger.warning("failed to persist max_worktrees to %s: %s", _WORKTREES_JSON, exc)

def cleanup_old_worktrees(repo: str, max_count: int = 25) -> None:
    worktrees = list_worktrees(repo)
    non_main = [wt for wt in worktrees if not wt["is_main"]]
    if len(non_main) <= max_count:
        return
        
    def get_mtime(wt):
        try:
            return os.path.getmtime(wt["path"])
        except OSError:
            return 0
            
    non_main.sort(key=get_mtime)
    to_remove = non_main[:len(non_main) - max_count]
    for wt in to_remove:
        try:
            remove_worktree(repo, wt["path"], force=True)
        except Exception as exc:
            logger.warning("failed to remove stale worktree %s: %s", wt["path"], exc)

_MANAGED_BRANCH_PREFIXES = ("pmedit-", "pmworker-")
_PROTECTED_BRANCHES = frozenset({"main", "master"})


def _is_managed_branch_name(branch: str) -> bool:
    return bool(branch) and branch.startswith(_MANAGED_BRANCH_PREFIXES)


def _current_branch(repo: str) -> str:
    rc, out, _ = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0:
        return ""
    name = (out or "").strip()
    return "" if name == "HEAD" else name


def delete_branch(repo: str, branch: str) -> None:
    """Force-delete a managed edit/worker branch.

    Only ``pmedit-*`` and ``pmworker-*`` names are eligible. Current checkout,
    ``main``, and ``master`` are never deleted; unrelated names are refused
    (no-op) so callers cannot scrub arbitrary local branches.
    """
    if not branch or not _is_managed_branch_name(branch):
        return
    if branch in _PROTECTED_BRANCHES:
        return
    if not repo or not _is_repo(repo):
        return
    if branch == _current_branch(repo):
        return
    subprocess.run(
        ["git", "-C", repo, "branch", "-D", branch],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )


def prune_orphan_edit_branches(repo: str) -> dict:
    """Delete local ``pmedit-*`` / ``pmworker-*`` branches that are not in use.

    Skips the current checkout and any branch still attached to a worktree.
    Returns ``{"deleted": [...], "count": N}``.
    """
    if not repo or not _is_repo(repo):
        return {"deleted": [], "count": 0}

    rc, out, _ = _git(repo, "branch", "--format=%(refname:short)")
    if rc != 0:
        return {"deleted": [], "count": 0}

    candidates = [
        line.strip()
        for line in (out or "").splitlines()
        if _is_managed_branch_name(line.strip())
    ]
    if not candidates:
        return {"deleted": [], "count": 0}

    current = _current_branch(repo)
    attached = {
        (wt.get("branch") or "").strip()
        for wt in list_worktrees(repo)
        if (wt.get("branch") or "").strip()
    }

    deleted: list[str] = []
    for branch in candidates:
        if branch in _PROTECTED_BRANCHES:
            continue
        if branch == current or branch in attached:
            continue
        before = _branch_exists(repo, branch)
        if not before:
            continue
        delete_branch(repo, branch)
        if not _branch_exists(repo, branch):
            deleted.append(branch)

    return {"deleted": deleted, "count": len(deleted)}

