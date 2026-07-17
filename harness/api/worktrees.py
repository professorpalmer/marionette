"""Worktree admin HTTP route bodies (peeled from ``harness.server``)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class WorktreeServices:
    """Explicit deps for worktree HTTP handlers."""

    cfg: Any
    parse_bool: Callable[[Any], bool]


def get_worktrees(svc: WorktreeServices) -> tuple[int, dict]:
    """GET /api/worktrees."""
    from .. import worktrees as _wt
    return 200, {
        "worktrees": _wt.list_worktrees(svc.cfg.repo),
        "max": _wt.get_max_worktrees(),
    }


def post_worktrees_add(body: dict, svc: WorktreeServices) -> tuple[int, dict]:
    """POST /api/worktrees/add."""
    from .. import worktrees as _wt
    branch = body.get("branch", "").strip()
    base = body.get("base") or "HEAD"
    if not branch or branch.startswith("-") or (base and base.startswith("-")):
        return 400, {"error": "invalid branch or base name"}
    try:
        new_wt = _wt.add_worktree(svc.cfg.repo, branch, base)
        _wt.cleanup_old_worktrees(svc.cfg.repo, _wt.get_max_worktrees())
        return 200, new_wt
    except ValueError as e:
        return 400, {"error": str(e)}
    except Exception as e:
        return 400, {"error": f"Failed to add worktree: {str(e)}"}


def post_worktrees_remove(body: dict, svc: WorktreeServices) -> tuple[int, dict]:
    """POST /api/worktrees/remove."""
    from .. import worktrees as _wt
    wt_path = body.get("path", "").strip()
    force = svc.parse_bool(body.get("force"))
    if not wt_path:
        return 400, {"error": "missing path"}
    try:
        _wt.remove_worktree(svc.cfg.repo, wt_path, force=force)
        return 200, {"ok": True}
    except ValueError as e:
        return 400, {"error": str(e)}
    except Exception as e:
        return 400, {"error": f"Failed to remove worktree: {str(e)}"}


def post_worktrees_prune(svc: WorktreeServices) -> tuple[int, dict]:
    """POST /api/worktrees/prune."""
    from .. import worktrees as _wt
    try:
        _wt.prune_worktrees(svc.cfg.repo)
        return 200, {"ok": True}
    except Exception as e:
        return 400, {"error": f"Failed to prune worktrees: {str(e)}"}


def post_worktrees_prune_edit_branches(svc: WorktreeServices) -> tuple[int, dict]:
    """POST /api/worktrees/prune-edit-branches."""
    from .. import worktrees as _wt
    try:
        result = _wt.prune_orphan_edit_branches(svc.cfg.repo)
        return 200, {
            "ok": True,
            "deleted": result.get("deleted", []),
            "count": int(result.get("count", 0) or 0),
        }
    except Exception as e:
        return 400, {"error": f"Failed to prune edit branches: {str(e)}"}


def post_worktrees_max(body: dict, svc: WorktreeServices) -> tuple[int, dict]:
    """POST /api/worktrees/max."""
    from .. import worktrees as _wt
    try:
        max_val = int(body.get("max") or body.get("max_worktrees") or 25)
        _wt.set_max_worktrees(max_val)
        _wt.cleanup_old_worktrees(svc.cfg.repo, max_val)
        return 200, {"ok": True}
    except (ValueError, TypeError):
        return 400, {"error": "Invalid max value"}
