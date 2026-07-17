"""Workspace HTTP route bodies (peeled from ``harness.server``).

Owns forget/get/symbols/workspaces CRUD and ``POST /api/workspace/open``.
Also owns recent-list persistence helpers (``record_recent_workspace`` /
``forget_recent_workspace``) with injected path/app-install deps.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile as _tf
from dataclasses import dataclass
from typing import Any, Callable, Optional, Type, Union


@dataclass
class WorkspaceRecentDeps:
    """Path/app-install deps for recent-list persistence (injected by server)."""

    workspace_json_path: Callable[[], str]
    resolve_existing_state_file: Callable[[str], str]
    paths_same_workspace: Callable[[str, str], bool]
    is_app_install_root: Callable[[str], bool]
    restrict_to_owner: Callable[[str], bool]
    diag: Callable[..., Any]


_recent_deps: Optional[WorkspaceRecentDeps] = None


def bind_recent_deps(deps: WorkspaceRecentDeps) -> None:
    """Wire server path helpers once during server module import."""
    global _recent_deps
    _recent_deps = deps


def _require_recent_deps() -> WorkspaceRecentDeps:
    if _recent_deps is None:
        raise RuntimeError("workspace.bind_recent_deps() was not called")
    return _recent_deps


def _persistable_recent_path(path: str, is_app_install_root: Callable[[str], bool]) -> bool:
    """True when ``path`` may be written into workspace.json recents/repo."""
    if not path:
        return False
    from ..paths import path_within
    if "PYTEST_CURRENT_TEST" not in os.environ:
        # Temp-dir only (realpath both sides). Do not use a bare
        # ``/var/folders/`` match -- macOS pytest tmp_path lives there too.
        try:
            if path_within(path, _tf.gettempdir(), allow_equal=True):
                return False
        except Exception:
            pass
    if is_app_install_root(path):
        return False
    return os.path.isdir(path)


def record_recent_workspace(target_repo: str, *, as_active: bool = True) -> list:
    """Append ``target_repo`` to workspace.json recents (cap 8, scrub ephemeral)."""
    deps = _require_recent_deps()
    ws_json_path = deps.workspace_json_path()
    ws_read_path = deps.resolve_existing_state_file("workspace.json")
    try:
        os.makedirs(os.path.dirname(ws_json_path), exist_ok=True)
        recents = []
        prior_repo = ""
        if os.path.exists(ws_read_path):
            try:
                with open(ws_read_path, encoding="utf-8", errors="replace") as f:
                    ws_data = json.load(f)
                    recents = ws_data.get("recents", []) or []
                    prior_repo = ws_data.get("repo", "") or ""
            except Exception:
                recents = []

        def persistable(path: str) -> bool:
            return _persistable_recent_path(path, deps.is_app_install_root)

        # Stable order: if path already in recents (any slash/case spelling),
        # leave its position; if new, append. Do NOT prepend-to-front on every
        # open (that snapped the rail). Still persist active "repo" below for
        # boot restore. Cap 8 + ephemeral guards unchanged. App install root is
        # never added (manual open stays process-local only).
        already = any(deps.paths_same_workspace(target_repo, r) for r in recents)
        if target_repo and not already and persistable(target_repo):
            recents = list(recents) + [target_repo]
        # Collapse slash/case duplicate spellings of the same root.
        deduped = []
        for recent in recents:
            if not persistable(recent):
                continue
            if any(deps.paths_same_workspace(recent, kept) for kept in deduped):
                continue
            deduped.append(recent)
        recents = deduped[:8]

        # The "repo" key is what boot restores as the active workspace, so a
        # temp dir / app checkout here resurrects as a phantom project on next
        # launch. Keep the prior persisted repo when the new target is not a
        # user project. as_active=False (Home seed) only appends to recents.
        if as_active and persistable(target_repo):
            persisted_repo = target_repo
        elif prior_repo and persistable(prior_repo):
            persisted_repo = prior_repo
        elif as_active:
            persisted_repo = ""
        else:
            persisted_repo = prior_repo if persistable(prior_repo) else ""

        target_dir = os.path.dirname(ws_json_path)
        fd, temp_path = _tf.mkstemp(dir=target_dir, prefix=".tmp-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump({"repo": persisted_repo, "recents": recents}, f)
            os.replace(temp_path, ws_json_path)
        except Exception as exc:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            raise exc

        if not deps.restrict_to_owner(ws_json_path):
            deps.diag("secure_files.restrict_failed", msg=ws_json_path)
        return recents
    except Exception:
        try:
            if os.path.exists(ws_json_path):
                with open(ws_json_path, encoding="utf-8", errors="replace") as f:
                    return json.load(f).get("recents", []) or []
        except Exception:
            pass
        return []


def forget_recent_workspace(forget_path: str) -> list:
    """Remove ``forget_path`` from workspace.json recents (all path spellings)."""
    deps = _require_recent_deps()
    ws_json_path = deps.workspace_json_path()
    ws_read_path = deps.resolve_existing_state_file("workspace.json")
    try:
        os.makedirs(os.path.dirname(ws_json_path), exist_ok=True)
        recents = []
        repo = ""
        if os.path.exists(ws_read_path):
            try:
                with open(ws_read_path, encoding="utf-8", errors="replace") as f:
                    data = json.load(f)
                    recents = data.get("recents", []) or []
                    repo = data.get("repo", "")
            except Exception:
                recents = []

        def persistable(path: str) -> bool:
            return _persistable_recent_path(path, deps.is_app_install_root)

        # Drop every slash/case spelling of forget_path (exact == left siblings).
        recents = [r for r in recents if not deps.paths_same_workspace(r, forget_path)]
        recents = [r for r in recents if persistable(r)]
        recents = recents[:8]

        # Forgetting the active workspace must clear the boot-restore repo key,
        # otherwise buildProjectsList re-appends currentRepo and the row sticks
        # as a phantom after Remove from list.
        if repo and deps.paths_same_workspace(repo, forget_path):
            repo = ""

        target_dir = os.path.dirname(ws_json_path)
        fd, temp_path = _tf.mkstemp(dir=target_dir, prefix=".tmp-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump({"repo": repo, "recents": recents}, f)
            os.replace(temp_path, ws_json_path)
        except Exception as exc:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            raise exc

        if not deps.restrict_to_owner(ws_json_path):
            deps.diag("secure_files.restrict_failed", msg=ws_json_path)
        return recents
    except Exception:
        try:
            if os.path.exists(ws_json_path):
                with open(ws_json_path, encoding="utf-8", errors="replace") as f:
                    return json.load(f).get("recents", []) or []
        except Exception:
            pass
        return []


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
    # POST /api/workspace/open
    sessions: Any = None
    save_active_transcript: Optional[Callable[[], None]] = None
    note_boot_repo: Optional[Callable[[str], None]] = None
    get_workspace_driver: Optional[Callable[[str], Any]] = None
    apply_model_context_window: Optional[Callable[[], None]] = None
    record_recent_workspace: Optional[Callable[..., list]] = None
    sessions_state_dir: Optional[Callable[[], str]] = None
    session_visible_for_workspace: Optional[Callable[..., bool]] = None
    attach_view: Optional[Callable[..., Any]] = None
    lease_exhausted_body: Optional[Callable[..., dict]] = None
    lease_exhausted_error: Optional[Type[BaseException]] = None
    puppetmaster_available: Optional[Callable[[], bool]] = None
    set_codegraph_status: Optional[Callable[..., None]] = None
    index_codegraph_bg: Optional[Callable[[str], None]] = None
    maybe_refresh_codegraph: Optional[Callable[..., None]] = None


JsonPayload = Union[dict, list]


def post_workspace_open(body: dict, svc: WorkspaceServices) -> tuple[int, JsonPayload]:
    """POST /api/workspace/open."""
    target_repo = (body.get("path") or "").strip()
    if not target_repo or not os.path.isdir(target_repo):
        return 400, {"error": "Path is not an existing directory"}

    # Save outgoing conversation transcript for the current active runner
    if svc.save_active_transcript is not None:
        svc.save_active_transcript()

    # Snapshot so a lease-exhausted attach can roll back without leaving
    # the process pointed at the target repo / session.
    prev_repo = svc.cfg.repo
    prev_driver = svc.cfg.driver
    prev_active = svc.sessions.active if svc.sessions is not None else None
    prev_env_repo = os.environ.get("HARNESS_REPO")

    svc.cfg.repo = target_repo
    os.environ["HARNESS_REPO"] = target_repo
    if svc.note_boot_repo is not None:
        svc.note_boot_repo(target_repo)

    # Restore the model last used in this workspace (if any + still
    # available), so each dir remembers its model across switches.
    try:
        if svc.get_workspace_driver is not None:
            saved_driver = svc.get_workspace_driver(target_repo)
            if saved_driver and saved_driver != svc.cfg.driver:
                from .. import model_visibility as _mv
                avail = {row["spec"] for row in _mv.catalog(available_only=True)}
                if saved_driver in avail or not avail:
                    svc.cfg.driver = saved_driver
                    if svc.apply_model_context_window is not None:
                        svc.apply_model_context_window()
    except Exception as e:
        svc.diag("server.restore_workspace_driver", e)

    try:
        if svc.record_recent_workspace is not None:
            svc.record_recent_workspace(target_repo)
    except Exception as e:
        svc.diag("server.record_recent_workspace", e)

    is_git = False
    branch = ""
    try:
        proc = subprocess.run(
            ["git", "-C", target_repo, "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0:
            is_git = True
            proc_branch = subprocess.run(
                ["git", "-C", target_repo, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=5,
            )
            if proc_branch.returncode == 0:
                branch = proc_branch.stdout.strip()
    except Exception:
        pass

    # Select/create the target project's session, then attach via registry
    # (do not rebuild in a way that orphans busy runners).
    if svc.sessions is not None and svc.session_visible_for_workspace is not None:
        state_dir = svc.sessions_state_dir() if svc.sessions_state_dir else ""
        target_sessions = [
            s for s in svc.sessions.list()
            if svc.session_visible_for_workspace(s, target_repo, state_dir)
        ]
        if target_sessions:
            newest_session = max(target_sessions, key=lambda s: s.get("created", 0))
            svc.sessions.switch(newest_session["id"])
        else:
            basename = os.path.basename(os.path.abspath(target_repo)) or "Workspace"
            svc.sessions.create(title=basename, repo=target_repo, branch=branch)

        if svc.sessions.active and svc.attach_view is not None:
            try:
                svc.attach_view(svc.sessions.active, defer_cold_build=True)
            except Exception as e:
                lease_cls = svc.lease_exhausted_error
                if lease_cls is not None and isinstance(e, lease_cls):
                    svc.cfg.repo = prev_repo
                    svc.cfg.driver = prev_driver
                    if svc.apply_model_context_window is not None:
                        svc.apply_model_context_window()
                    if prev_env_repo is None:
                        os.environ.pop("HARNESS_REPO", None)
                    else:
                        os.environ["HARNESS_REPO"] = prev_env_repo
                    if prev_active:
                        try:
                            svc.sessions.switch(prev_active)
                        except Exception as roll_e:
                            svc.diag("server.workspace_open_lease_rollback", roll_e)
                    body_payload = (
                        svc.lease_exhausted_body(e)
                        if svc.lease_exhausted_body is not None
                        else {"error": "lease exhausted"}
                    )
                    return 409, body_payload
                raise

    has_codegraph = os.path.isdir(os.path.join(target_repo, ".codegraph"))
    if not has_codegraph:
        # Set indexing before spawn/preflight so the open response and
        # immediate polls never flash unsupported.
        if svc.puppetmaster_available and svc.puppetmaster_available():
            if svc.set_codegraph_status is not None:
                svc.set_codegraph_status("indexing", reason=None)
        if svc.index_codegraph_bg is not None:
            svc.index_codegraph_bg(target_repo)
    else:
        if svc.puppetmaster_available and svc.puppetmaster_available():
            if svc.set_codegraph_status is not None:
                svc.set_codegraph_status("ready")
            if svc.maybe_refresh_codegraph is not None:
                svc.maybe_refresh_codegraph(target_repo)
        else:
            if svc.set_codegraph_status is not None:
                svc.set_codegraph_status("unsupported")

    return 200, {
        "ok": True,
        "repo": target_repo,
        "branch": branch,
        "is_git": is_git,
        "codegraph": svc.get_codegraph_status(target_repo),
        "active_session": svc.sessions.active if svc.sessions is not None else None,
    }


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
    from ..paths import path_within

    tmproot = _tf.gettempdir()
    recents = [
        r for r in recents
        if r and os.path.isdir(r)
        and not path_within(r, tmproot, allow_equal=True)
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
