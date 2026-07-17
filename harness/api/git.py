"""Git provision / workspace HTTP route bodies (peeled from ``harness.server``).

Auth/token gates stay on ``server.Handler``; this module owns JSON bodies only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Union


@dataclass
class GitServices:
    """Explicit deps for git HTTP handlers."""

    cfg: Any


JsonPayload = Union[dict, list]


def post_git_connect(body: dict) -> tuple[int, JsonPayload]:
    """POST /api/git/connect."""
    method = body.get("method")
    if method not in ("gh", "device"):
        return 400, {"error": f"Invalid method: {method}"}
    from ..git_provision import GitProvisioner, save_connection, get_status
    prov = GitProvisioner()
    if method == "gh":
        info = prov.detect_gh()
        if not info["available"]:
            return 400, {"error": "GitHub CLI not authenticated or not installed"}
        token = prov.github_token()
        if not token:
            return 400, {"error": "Could not retrieve GitHub CLI token"}
        res = prov.provision_wiki_repo(token)
        if not res.get("ok"):
            return 500, {"error": res.get("error", "Failed to provision repository")}
        save_connection("gh", res["repo_full_name"], res["html_url"])
        return 200, get_status()
    # method == "device"
    res = prov.device_flow_start()
    if "error" in res:
        return 500, {"error": res["error"]}
    return 200, res


def post_git_device_poll(body: dict) -> tuple[int, JsonPayload]:
    """POST /api/git/device/poll."""
    device_code = body.get("device_code")
    if not device_code:
        return 400, {"error": "Missing device_code"}
    from ..git_provision import (
        GitProvisioner,
        save_connection,
        save_device_token,
        get_status,
    )
    prov = GitProvisioner()
    res = prov.device_flow_poll(None, device_code)
    if res.get("status") == "authorized":
        token = res.get("token")
        if not token:
            return 500, {"error": "No token in authorized response"}
        repo_res = prov.provision_wiki_repo(token)
        if not repo_res.get("ok"):
            return 500, {"error": repo_res.get("error", "Failed to provision repository")}
        save_device_token(token)
        save_connection("device", repo_res["repo_full_name"], repo_res["html_url"])
        return 200, get_status()
    if res.get("status") == "pending":
        return 200, {"status": "pending"}
    return 400, {"error": res.get("error", "Verification failed")}


def post_git_disconnect() -> tuple[int, JsonPayload]:
    """POST /api/git/disconnect."""
    from ..git_provision import delete_connection, get_status
    delete_connection()
    return 200, get_status()


def get_git_status(repo_q: Optional[str], svc: GitServices) -> tuple[int, JsonPayload]:
    """GET /api/git/status."""
    if (repo_q or "").strip():
        from ..git_workspace import workspace_status
        return 200, workspace_status(svc.cfg.repo, repo_q)
    from ..git_provision import get_status
    return 200, get_status()


def get_git_branches(repo_q: Optional[str], svc: GitServices) -> tuple[int, JsonPayload]:
    """GET /api/git/branches."""
    from ..git_workspace import workspace_branches
    return 200, workspace_branches(svc.cfg.repo, repo_q or "")


def get_git_diff(
    repo_q: Optional[str],
    file_q: Optional[str],
    staged: bool,
    svc: GitServices,
) -> tuple[int, JsonPayload]:
    """GET /api/git/diff."""
    from ..git_workspace import workspace_diff
    return 200, workspace_diff(
        svc.cfg.repo,
        repo_q or "",
        (file_q or "").strip() or None,
        staged=staged,
    )
