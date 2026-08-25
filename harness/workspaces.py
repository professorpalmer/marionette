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
import os
from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class Workspace:
    name: str
    branch: str
    active: bool
    dirty: bool = False
    # Sibling worktree path when this branch is checked out elsewhere (e.g.
    # Marionette pmedit-* / pmworker-*). UI opens that folder instead of
    # attempting ``git checkout`` in the main tree.
    worktree_path: Optional[str] = None


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
    # One worktree list for the whole branch scan (avoid N porcelain calls).
    held_by_branch: dict[str, str] = {}
    try:
        from .worktrees import list_worktrees
        real_repo = os.path.realpath(repo)
        for wt in list_worktrees(repo):
            wt_branch = (wt.get("branch") or "").strip()
            wt_path = (wt.get("path") or "").strip()
            if not wt_branch or not wt_path:
                continue
            try:
                if os.path.realpath(wt_path) == real_repo:
                    continue
            except Exception:
                if wt.get("is_main"):
                    continue
            held_by_branch[wt_branch] = wt_path
    except Exception:
        held_by_branch = {}
    rows = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        name = parts[0].strip()
        active = len(parts) > 1 and parts[1].strip() == "*"
        row = asdict(Workspace(
            name=name,
            branch=name,
            active=active,
            dirty=dirty and active,
            worktree_path=held_by_branch.get(name),
        ))
        # Drop null worktree_path so older clients / tests stay compact.
        if not row.get("worktree_path"):
            row.pop("worktree_path", None)
        rows.append(row)
    remote = _origin_branch_names(repo)
    return [row for row in rows if not _is_stale_local_release(row, remote)]


def _origin_branch_names(repo: str) -> set[str]:
    """Short names of origin/* heads (empty when origin is missing or unread)."""
    rc, out, _ = _git(repo, "branch", "-r", "--format=%(refname:short)")
    if rc != 0:
        return set()
    names: set[str] = set()
    for line in out.splitlines():
        name = line.strip()
        if not name.startswith("origin/"):
            continue
        short = name[len("origin/"):]
        if not short or short == "HEAD" or short.startswith("HEAD "):
            continue
        names.add(short)
    return names


def _is_live_worktree_path(path: str | None) -> bool:
    if not path:
        return False
    try:
        return os.path.isdir(path)
    except Exception:
        return False


def _is_stale_local_release(row: dict, remote: set[str]) -> bool:
    """True when BRANCHES should hide a leftover local release/v0.9.* head.

    Origin already deleted these. Keep main/dev (not this prefix), the current
    checkout, any still-on-origin release, and a *live* worktree checkout.
    Do not delete the worktree — only hide the row.
    """
    name = str(row.get("name") or "")
    if not name.startswith("release/v0.9."):
        return False
    if row.get("active"):
        return False
    if _is_live_worktree_path(row.get("worktree_path")):
        return False
    if name in remote:
        return False
    # No origin picture: do not hide (offline / no-remote test repos).
    if not remote:
        return False
    return True


def _worktree_holding_branch(repo: str, branch: str) -> Optional[str]:
    """Path of another worktree that already has ``branch`` checked out, if any.

    Git refuses ``checkout`` of a branch that is locked to a sibling worktree
    (``fatal: '…' is already used by worktree at '…'``). Detect that up front
    so the Branches list can open the worktree folder instead of a raw fatal —
    common for Marionette ``pmedit-*`` / ``pmworker-*`` branches.
    """
    if not repo or not branch:
        return None
    try:
        from .worktrees import list_worktrees
    except Exception:
        return None
    try:
        real_repo = os.path.realpath(repo)
    except Exception:
        real_repo = repo
    for wt in list_worktrees(repo):
        wt_branch = (wt.get("branch") or "").strip()
        if wt_branch != branch:
            continue
        wt_path = (wt.get("path") or "").strip()
        if not wt_path:
            continue
        try:
            if os.path.realpath(wt_path) == real_repo:
                continue
        except Exception:
            if wt.get("is_main"):
                continue
        return wt_path
    return None


def _friendly_worktree_busy_error(branch: str, wt_path: str) -> dict:
    kind = "edit"
    if branch.startswith("pmworker-"):
        kind = "worker"
    elif branch.startswith("pmedit-"):
        kind = "edit"
    return {
        "ok": False,
        "error": f"Opening {kind} worktree for {branch}",
        "worktree_busy": True,
        "worktree_path": wt_path,
    }


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
    held = _worktree_holding_branch(repo, name)
    if held:
        return _friendly_worktree_busy_error(name, held)
    rc, out, err = _git(repo, "checkout", name)
    if rc != 0:
        msg = err or out
        # Fallback when porcelain detection missed (stale worktree metadata).
        if "already used by worktree" in (msg or "").lower():
            # Prefer a path parsed from git's message when present.
            wt_path = held or ""
            marker = "worktree at '"
            low = msg.lower()
            if marker in low:
                start = low.index(marker) + len(marker)
                end = msg.find("'", start)
                if end > start:
                    wt_path = msg[start:end]
            if wt_path:
                return _friendly_worktree_busy_error(name, wt_path)
        return {"ok": False, "error": msg}
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
