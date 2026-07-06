from __future__ import annotations

"""Workspaces: a git-branch-per-workspace model with instant swap (the Cursor /
Hermes pattern Cary loves). A workspace IS a git branch in the target repo; the
active workspace = the currently checked-out branch. Switching a workspace checks
out its branch. Creating one makes a new branch.

This is deliberately thin and SAFE: it never force-switches over uncommitted
changes (it reports dirty and refuses unless allowed), and it shells to git in
the configured repo only. No repo configured -> no workspaces (empty list).
"""

import subprocess
from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class Workspace:
    name: str
    branch: str
    active: bool
    dirty: bool = False


def _git(repo: str, *args: str, timeout: int = 15) -> tuple[int, str, str]:
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def _is_repo(repo: str) -> bool:
    if not repo:
        return False
    rc, out, _ = _git(repo, "rev-parse", "--is-inside-work-tree")
    return rc == 0 and out == "true"


def _dirty(repo: str) -> bool:
    rc, out, _ = _git(repo, "status", "--porcelain")
    return rc == 0 and bool(out.strip())


def list_workspaces(repo: str) -> list[dict]:
    """Each local branch is a workspace; the checked-out one is active."""
    if not _is_repo(repo):
        return []
    rc, out, _ = _git(repo, "branch", "--format=%(refname:short)\t%(HEAD)")
    if rc != 0:
        return []
    dirty = _dirty(repo)
    rows = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        name = parts[0].strip()
        active = len(parts) > 1 and parts[1].strip() == "*"
        rows.append(asdict(Workspace(name=name, branch=name, active=active,
                                     dirty=dirty and active)))
    return rows


def switch_workspace(repo: str, name: str, *, allow_dirty: bool = False) -> dict:
    """Check out the workspace's branch. Refuses over uncommitted changes unless
    allow_dirty (git itself also refuses if the checkout would clobber)."""
    if not _is_repo(repo):
        return {"ok": False, "error": "no git repo configured"}
    if _dirty(repo) and not allow_dirty:
        return {"ok": False, "error": f"uncommitted changes in {repo}; commit/stash first "
                f"or allow_dirty", "dirty": True}
    if name.startswith("-"):
        return {"ok": False, "error": "invalid workspace name (cannot start with '-')"}
    rc, out, err = _git(repo, "checkout", name)
    if rc != 0:
        return {"ok": False, "error": err or out}
    return {"ok": True, "active": name}


def create_workspace(repo: str, name: str, base: Optional[str] = None) -> dict:
    """Create a new workspace = a new git branch (from base or current HEAD)."""
    if not _is_repo(repo):
        return {"ok": False, "error": "no git repo configured"}
    if name.startswith("-") or (base and base.startswith("-")):
        return {"ok": False, "error": "invalid workspace name/base (cannot start with '-')"}
    args = ["checkout", "-b", name] + ([base] if base else [])
    rc, out, err = _git(repo, *args)
    if rc != 0:
        return {"ok": False, "error": err or out}
    return {"ok": True, "active": name}
