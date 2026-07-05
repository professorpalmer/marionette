from __future__ import annotations

import os
import re
import json
import logging
import subprocess
import tempfile
from typing import Optional

from .paths import path_within

logger = logging.getLogger("pmharness.worktrees")

def _git(repo: str, *args: str, timeout: int = 15) -> tuple[int, str, str]:
    if not repo:
        return 1, "", "No repository configured"
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=timeout)
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


def _worktree_pid_cwds() -> list[tuple[int, str]]:
    """Best-effort enumeration of (pid, cwd) via lsof. Returns [] on any error."""
    out_pairs: list[tuple[int, str]] = []
    try:
        # -Fn emits n<cwd>, -Fp emits p<pid>; -d cwd restricts to the cwd fd.
        p = subprocess.run(
            ["lsof", "-a", "-d", "cwd", "-Fpn"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return out_pairs
    cur_pid: Optional[int] = None
    for line in (p.stdout or "").splitlines():
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


def reap_worktree_processes(path: str) -> int:
    """Terminate any still-running process whose cwd is inside worktree `path`.

    Orphaned `codegraph index` (+ node) subprocesses can outlive their worker
    worktree, reparent to init, and keep walking the now-deleted dir -- a real
    resource leak. Reap them (SIGTERM, then SIGKILL) BEFORE the dir is removed
    so their cwd still resolves. Best-effort: never raises; returns the count
    signaled. Never signals pid<=1, this process, or its parent.
    """
    signaled = 0
    try:
        import signal as _signal
        import time as _time
        me = os.getpid()
        parent = os.getppid()
        targets = [
            pid for pid, cwd in _worktree_pid_cwds()
            if pid > 1 and pid != me and pid != parent and _cwd_under(cwd, path)
        ]
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
        with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
            json.dump({"max_worktrees": max_count}, f)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, _WORKTREES_JSON)
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

def delete_branch(repo: str, branch: str) -> None:
    if not branch.startswith("pmworker-"):
        return
    if not repo or not _is_repo(repo):
        return
    subprocess.run(["git", "-C", repo, "branch", "-D", branch], capture_output=True, text=True, timeout=15)

