"""Read-only workspace git status/diff for the SourceControl pane HTTP fallback.

Mirrors the Electron ``git-bridge.cjs`` response shapes so ``nativeGit`` can
fall back from flaky/missing IPC to harness HTTP without inventing write APIs.
"""
from __future__ import annotations

import os
import subprocess
from typing import Any


def _git(repo: str, *args: str, timeout: int = 30) -> tuple[int, str, str]:
    p = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return p.returncode, p.stdout, p.stderr.strip()


def resolve_open_repo(open_repo: str, repo_arg: str) -> str | None:
    """Only git operations on the harness open workspace are allowed."""
    if not open_repo or not os.path.isdir(open_repo):
        return None
    raw = (repo_arg or "").strip() or "."
    if raw in (".", open_repo):
        return os.path.abspath(open_repo)
    try:
        if os.path.abspath(raw) == os.path.abspath(open_repo):
            return os.path.abspath(open_repo)
    except Exception:
        return None
    return None


def workspace_status(open_repo: str, repo_arg: str) -> dict[str, Any]:
    repo = resolve_open_repo(open_repo, repo_arg)
    if not repo:
        return {"ok": False, "error": "No open workspace"}
    rc, out, err = _git(repo, "status", "--porcelain=v1", "-b")
    if rc != 0:
        return {"ok": False, "error": err or "git status failed"}
    lines = [ln for ln in out.splitlines() if ln]
    branch = ""
    files: list[dict[str, str]] = []
    for line in lines:
        if line.startswith("## "):
            branch = line[3:].split("...")[0]
            continue
        files.append({"status": line[:2], "path": line[3:]})
    return {"ok": True, "branch": branch, "files": files}


def workspace_branches(open_repo: str, repo_arg: str) -> dict[str, Any]:
    repo = resolve_open_repo(open_repo, repo_arg)
    if not repo:
        return {"ok": False, "error": "No open workspace"}
    rc, out, err = _git(repo, "branch", "--format=%(refname:short)\t%(HEAD)")
    if rc != 0:
        return {"ok": False, "error": err or "git branch failed"}
    branches = []
    for line in out.splitlines():
        if not line.strip():
            continue
        name, head = line.split("\t", 1)
        branches.append({"name": name, "active": head == "*"})
    return {"ok": True, "branches": branches}


def workspace_diff(
    open_repo: str,
    repo_arg: str,
    file: str | None = None,
    *,
    staged: bool = False,
) -> dict[str, Any]:
    repo = resolve_open_repo(open_repo, repo_arg)
    if not repo:
        return {"ok": False, "error": "No open workspace"}
    args = ["diff", "--cached", "--no-color"] if staged else ["diff", "--no-color"]
    if file:
        args.extend(["--", file])
    rc, out, err = _git(repo, *args)
    if rc != 0:
        return {"ok": False, "error": err or "git diff failed"}
    return {"ok": True, "out": out}
