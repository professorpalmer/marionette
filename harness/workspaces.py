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


def _dirty_tracked_paths(repo: str) -> list[str]:
    """Paths with real tracked dirt (M/A/D/R/C/U) — not untracked or ignored.

    ``git status --porcelain`` treats ``??`` untracked (and ignored noise when
    shown) as dirty; those must not block branch switch / stash prompts.

    Do not go through ``_git``: that helper ``.strip()``s stdout and eats the
    leading porcelain space (`` M README.md`` becomes ``M README.md``).
    """
    p = subprocess.run(
        ["git", "-C", repo, "status", "--porcelain", "-uno"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    if p.returncode != 0 or not (p.stdout or "").strip():
        return []
    paths: list[str] = []
    for raw in p.stdout.splitlines():
        line = raw.rstrip('\n\r')
        if len(line) < 4:
            continue
        code = line[:2]
        # Untracked / ignored are never a stash block.
        if code in ("??", "!!"):
            continue
        # Skip blank xy (should not appear with -uno, but be safe).
        if code.strip() == "":
            continue
        path = line[3:].strip()
        # Rename lines: "R  old -> new" — report the new path.
        if " -> " in path:
            path = path.split(" -> ", 1)[-1].strip()
        if path:
            paths.append(path)
    return paths


def _dirty(repo: str) -> bool:
    return bool(_dirty_tracked_paths(repo))


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
    checkout, and any still-on-origin release. Dead leftover worktrees
    (release/v0.9.318) are hidden, not deleted.

    When origin exists but only has non-release heads (typical main+dev), local
    release leftovers without a live worktree are stale. An empty remote picture
    (no origin / unread) still hides inactive local-only release rows that lack
    a live worktree — leftover release/v0.9.* are never useful on the rail.
    """
    name = str(row.get("name") or "")
    if not name.startswith("release/v0.9."):
        return False
    if row.get("active"):
        return False
    # Leftover release/v0.9.* worktrees (e.g. 318) stay on disk but are not
    # listed. Do not delete the directory; only hide the BRANCHES row.
    if name in remote:
        return False
    # Remote picture empty OR origin has no copy of this release → hide.
    return True


def is_stale_local_release_branch(
    name: str,
    *,
    active: bool = False,
    worktree_path: str | None = None,
    remote: set[str] | None = None,
) -> bool:
    """Public predicate shared with prune — same rules as list filter."""
    return _is_stale_local_release(
        {"name": name, "active": active, "worktree_path": worktree_path},
        remote if remote is not None else set(),
    )


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
    """Check out the workspace's branch. Refuses over tracked dirty paths unless
    allow_dirty (git itself also refuses if the checkout would clobber).

    Untracked / gitignored noise is not a stash block. When refusing, returns
    ``dirty_paths`` so the UI can show what actually needs stash/commit.
    """
    if not _is_repo(repo):
        return {"ok": False, "error": "no git repo configured"}
    dirty_paths = _dirty_tracked_paths(repo)
    if dirty_paths and not allow_dirty:
        shown = ", ".join(dirty_paths[:8])
        extra = f" (+{len(dirty_paths) - 8} more)" if len(dirty_paths) > 8 else ""
        return {
            "ok": False,
            "error": (
                f"uncommitted changes in {repo}: {shown}{extra}; "
                f"commit/stash first or allow_dirty"
            ),
            "dirty": True,
            "dirty_paths": dirty_paths,
        }
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
