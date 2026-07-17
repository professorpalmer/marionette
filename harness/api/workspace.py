"""Workspace HTTP route bodies (peeled from ``harness.server``).

``POST /api/workspace/open`` stays on Handler (session attach + codegraph kick).
"""

from __future__ import annotations

import json
import os
import tempfile as _tf
from dataclasses import dataclass
from typing import Any, Callable, Optional, Union


@dataclass
class WorkspaceServices:
    """Explicit deps for workspace HTTP handlers."""

    cfg: Any
    parse_bool: Callable[[Any], bool]
    ws: Any  # harness.workspaces module-like
    paths_same_workspace: Callable[[str, str], bool]
    forget_recent_workspace: Callable[[str], list]
    clear_active_codegraph: Callable[[], None]
    get_codegraph_status: Callable[[Optional[str]], str]
    workspace_json_path: Callable[[], str]
    ensure_home_workspace: Callable[[], str]
    home_workspace_path: Callable[[], str]
    is_app_install_root: Callable[[str], bool]
    diag: Callable[..., Any]


JsonPayload = Union[dict, list]


def post_workspace_forget(body: dict, svc: WorkspaceServices) -> tuple[int, JsonPayload]:
    """POST /api/workspace/forget."""
    target_repo = (body.get("path") or "").strip()
    if not target_repo:
        return 400, {"error": "Path is required"}
    cleared_active = False
    try:
        repo = svc.cfg.repo
        # Clear live process state when forgetting the open workspace so
        # the rail does not keep re-appending currentRepo after forget.
        if repo and svc.paths_same_workspace(repo, target_repo):
            svc.cfg.repo = ""
            os.environ.pop("HARNESS_REPO", None)
            cleared_active = True
            svc.clear_active_codegraph()
        recents = svc.forget_recent_workspace(target_repo)
    except Exception as e:
        return 500, {"error": str(e)}
    return 200, {
        "ok": True,
        "recents": recents,
        "cleared_active": cleared_active,
        "repo": svc.cfg.repo or "",
    }


def post_workspaces_switch(body: dict, svc: WorkspaceServices) -> tuple[int, JsonPayload]:
    """POST /api/workspaces/switch."""
    return 200, svc.ws.switch_workspace(
        svc.cfg.repo,
        body.get("name", ""),
        allow_dirty=svc.parse_bool(body.get("allow_dirty")),
    )


def post_workspaces_create(body: dict, svc: WorkspaceServices) -> tuple[int, JsonPayload]:
    """POST /api/workspaces/create."""
    return 200, svc.ws.create_workspace(
        svc.cfg.repo,
        body.get("name", ""),
        body.get("branch") or None,
    )


def get_workspaces(svc: WorkspaceServices) -> tuple[int, JsonPayload]:
    """GET /api/workspaces."""
    return 200, svc.ws.list_workspaces(svc.cfg.repo)


def get_workspace(svc: WorkspaceServices) -> tuple[int, JsonPayload]:
    """GET /api/workspace."""
    import subprocess

    repo = svc.cfg.repo
    is_git = False
    branch = ""
    if repo and os.path.isdir(repo):
        try:
            proc = subprocess.run(
                ["git", "-C", repo, "rev-parse", "--is-inside-work-tree"],
                capture_output=True, text=True, timeout=5,
            )
            if proc.returncode == 0:
                is_git = True
                proc_branch = subprocess.run(
                    ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
                    capture_output=True, text=True, timeout=5,
                )
                if proc_branch.returncode == 0:
                    branch = proc_branch.stdout.strip()
        except Exception:
            pass
    cg_status = svc.get_codegraph_status(repo) if repo else "none"
    recents = []
    try:
        ws_path = svc.workspace_json_path()
        if os.path.exists(ws_path):
            with open(ws_path, encoding="utf-8", errors="replace") as f:
                recents = json.load(f).get("recents", []) or []
    except Exception:
        recents = []
    tmproot = os.path.realpath(_tf.gettempdir())
    recents = [
        r for r in recents
        if r and os.path.isdir(r)
        and not os.path.realpath(r).startswith(tmproot)
        and "/var/folders/" not in os.path.realpath(r)
        and not svc.is_app_install_root(r)
    ]
    try:
        home = svc.ensure_home_workspace()
        if home and os.path.isdir(home) and not any(
            svc.paths_same_workspace(home, r) for r in recents
        ):
            recents = list(recents) + [home]
    except Exception as e:
        svc.diag("server.workspace_home_recent", e)
    return 200, {
        "repo": repo,
        "branch": branch,
        "is_git": is_git,
        "codegraph_status": cg_status,
        "recents": recents,
        "home": svc.home_workspace_path(),
    }


def get_workspace_symbols(query: str, svc: WorkspaceServices) -> tuple[int, JsonPayload]:
    """GET /api/workspace/symbols."""
    repo = svc.cfg.repo
    cg_status = svc.get_codegraph_status(repo) if repo else "unsupported"

    if not repo or not os.path.isdir(repo):
        return 200, {"symbols": [], "status": cg_status}

    try:
        import puppetmaster.codegraph as cg
        if not cg.codegraph_available() or not cg.codegraph_ready(repo):
            return 200, {"symbols": [], "status": cg_status}
    except Exception:
        return 200, {"symbols": [], "status": "unsupported"}

    q = (query or "").strip()
    if len(q) < 1:
        return 200, {"symbols": [], "status": "ready"}

    try:
        import puppetmaster.codegraph as cg
        res = cg.codegraph_query(search=q, cwd=repo, limit=20)
        symbols_list = []
        if res.get("ok") and res.get("stdout"):
            try:
                data = json.loads(res["stdout"])
                if isinstance(data, list):
                    for item in data:
                        node = item.get("node")
                        if not node:
                            continue
                        name = node.get("name")
                        kind = node.get("kind")
                        file_path = node.get("filePath")
                        start_line = node.get("startLine")
                        if name and file_path and start_line is not None:
                            symbols_list.append({
                                "name": str(name),
                                "kind": str(kind or "unknown"),
                                "path": str(file_path),
                                "line": int(start_line),
                            })
                        if len(symbols_list) >= 20:
                            break
            except Exception:
                pass
        return 200, {"symbols": symbols_list, "status": "ready"}
    except Exception as e:
        return 200, {"symbols": [], "error": str(e), "status": cg_status}
