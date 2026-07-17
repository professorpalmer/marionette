"""Table-driven HTTP path → handler wiring for ``Handler`` (dispatch only).

Route *bodies* live under ``harness.api.*``; this module only maps paths to
callables and standardizes the auth-once + query-parse + ``_send`` pattern.
Status codes and JSON shapes are preserved from the previous if-ladders.
"""
from __future__ import annotations

import json
import os
import signal
import threading
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

# (handler, body) -> response side-effect via handler._send
PostHandler = Callable[[Any, dict], Any]
# (handler, parsed_url, query_dict) -> response side-effect via handler._send
GetHandler = Callable[[Any, Any, dict], Any]


def send_json(handler: Any, status: int, payload: Any) -> Any:
    return handler._send(status, json.dumps(payload))


def post_json(
    api_fn: Callable[..., tuple[int, Any]],
    *,
    services: Optional[Callable[[], Any]] = None,
    needs_body: bool = True,
) -> PostHandler:
    """Wrap an api.* function that returns ``(status, payload)``."""

    def handle(handler: Any, body: dict) -> Any:
        if needs_body and services is not None:
            status, payload = api_fn(body, services())
        elif services is not None:
            status, payload = api_fn(services())
        elif needs_body:
            status, payload = api_fn(body)
        else:
            status, payload = api_fn()
        return send_json(handler, status, payload)

    return handle


def get_json(
    api_fn: Callable[..., tuple[int, Any]],
    *,
    services: Optional[Callable[[], Any]] = None,
    qs_arg: Optional[str] = None,
    qs_args: Optional[tuple[str, ...]] = None,
    empty_as_none: bool = False,
    pass_qs: bool = False,
) -> GetHandler:
    """Wrap an api.* GET that returns ``(status, payload)``."""

    def handle(handler: Any, u: Any, qs: dict) -> Any:
        args: list[Any] = []
        if pass_qs:
            args.append(qs)
        elif qs_args:
            for key in qs_args:
                args.append(qs.get(key, [""])[0])
        elif qs_arg is not None:
            val = qs.get(qs_arg, [""])[0]
            if empty_as_none:
                val = val or None
            args.append(val)
        if services is not None:
            status, payload = api_fn(*args, services())
        else:
            status, payload = api_fn(*args)
        return send_json(handler, status, payload)

    return handle


def build_post_json_routes(svc: Any) -> dict[str, PostHandler]:
    """Build path → POST JSON handler map. ``svc`` holds service factories."""
    from .api import auth as _auth_api
    from .api import checkpoints as _ckpt_api
    from .api import codegraph as _cg_api
    from .api import commands as _cmd_api
    from .api import files as _files_api
    from .api import git as _git_api
    from .api import hooks as _hooks_api
    from .api import jobs as _jobs_api
    from .api import mcp as _mcp_api
    from .api import platform as _plat_api
    from .api import providers as _prov_api
    from .api import registry as _reg_api
    from .api import reviews as _rev_api
    from .api import session_control as _sc_api
    from .api import sessions as _sessions_api
    from .api import settings as _settings_api
    from .api import skills as _skills_api
    from .api import terminals as _term_api
    from .api import wiki as _wiki_api
    from .api import workspace as _ws_api
    from .api import worktrees as _wt_api

    routes: dict[str, PostHandler] = {
        "/api/reviews/apply": post_json(
            _rev_api.post_reviews_apply, services=svc.review_services),
        "/api/reviews/dismiss": post_json(
            _rev_api.post_reviews_dismiss, services=svc.review_services),
        "/api/swarm/cancel": post_json(
            _jobs_api.post_swarm_cancel, services=svc.job_services),
        "/api/session/persist": post_json(
            _sc_api.post_session_persist, services=svc.session_control_services,
            needs_body=False),
        "/api/restart": _post_restart,
        "/api/session/compact": post_json(
            _sc_api.post_session_compact, services=svc.session_control_services,
            needs_body=False),
        "/api/checkpoints/restore": post_json(
            _ckpt_api.post_checkpoints_restore, services=svc.checkpoint_services),
        "/api/checkpoints/snapshot": post_json(
            _ckpt_api.post_checkpoints_snapshot, services=svc.checkpoint_services),
        "/api/codegraph/reindex": post_json(
            _cg_api.post_codegraph_reindex, services=svc.codegraph_services,
            needs_body=False),
        "/api/codegraph/apply-excludes": post_json(
            _cg_api.post_codegraph_apply_excludes, services=svc.codegraph_services),
        "/api/commands/render": post_json(
            _cmd_api.post_commands_render, services=svc.commands_services),
        "/api/inline-edit": post_json(
            _rev_api.post_inline_edit, services=svc.review_services),
        "/api/file/write": post_json(
            _files_api.post_file_write, services=svc.file_services),
        "/api/file/delete": post_json(
            _files_api.post_file_delete, services=svc.file_services),
        "/api/file/rename": post_json(
            _files_api.post_file_rename, services=svc.file_services),
        "/api/file/mkdir": post_json(
            _files_api.post_file_mkdir, services=svc.file_services),
        "/api/file/reveal": post_json(
            _files_api.post_file_reveal, services=svc.file_services),
        "/api/workspace/open": post_json(
            _ws_api.post_workspace_open, services=svc.workspace_services),
        "/api/workspace/forget": post_json(
            _ws_api.post_workspace_forget, services=svc.workspace_services),
        "/api/workspaces/switch": post_json(
            _ws_api.post_workspaces_switch, services=svc.workspace_services),
        "/api/workspaces/create": post_json(
            _ws_api.post_workspaces_create, services=svc.workspace_services),
        "/api/mcp/add": post_json(
            _mcp_api.post_mcp_add, services=svc.mcp_services),
        "/api/mcp/remove": post_json(
            _mcp_api.post_mcp_remove, services=svc.mcp_services),
        "/api/mcp/start": post_json(
            _mcp_api.post_mcp_start, services=svc.mcp_services),
        "/api/mcp/stop": post_json(
            _mcp_api.post_mcp_stop, services=svc.mcp_services),
        "/api/mcp/call": post_json(
            _mcp_api.post_mcp_call, services=svc.mcp_services),
        "/api/skills/distill": post_json(
            _skills_api.post_skills_distill, services=svc.skills_services,
            needs_body=False),
        "/api/wiki/ingest-prepared": post_json(
            _wiki_api.post_wiki_ingest_prepared, services=svc.wiki_services),
        "/api/models/toggle": post_json(
            _prov_api.post_models_toggle, services=svc.provider_services),
        "/api/models/set": post_json(
            _prov_api.post_models_set, services=svc.provider_services),
        "/api/skills/approve": post_json(
            _skills_api.post_skills_approve, services=svc.skills_services),
        "/api/skills/add": post_json(
            _skills_api.post_skills_add, services=svc.skills_services),
        "/api/skills/update": post_json(
            _skills_api.post_skills_update, services=svc.skills_services),
        "/api/skills/remove": post_json(
            _skills_api.post_skills_remove, services=svc.skills_services),
        "/api/skills/reject": post_json(
            _skills_api.post_skills_reject, services=svc.skills_services),
        "/api/skills/archive": post_json(
            _skills_api.post_skills_archive, services=svc.skills_services),
        "/api/rules/approve": post_json(
            _skills_api.post_rules_approve, services=svc.skills_services),
        "/api/rules/add": post_json(
            _skills_api.post_rules_add, services=svc.skills_services),
        "/api/rules/update": post_json(
            _skills_api.post_rules_update, services=svc.skills_services),
        "/api/rules/remove": post_json(
            _skills_api.post_rules_remove, services=svc.skills_services),
        "/api/rules/reject": post_json(
            _skills_api.post_rules_reject, services=svc.skills_services),
        "/api/memory/add": post_json(
            _skills_api.post_memory_add, services=svc.skills_services),
        "/api/memory/remove": post_json(
            _skills_api.post_memory_remove, services=svc.skills_services),
        "/api/memory/propose/accept": post_json(
            _skills_api.post_memory_propose_accept, services=svc.skills_services),
        "/api/memory/propose/dismiss": post_json(
            _skills_api.post_memory_propose_dismiss, services=svc.skills_services),
        "/api/sessions/create": post_json(
            _sessions_api.post_sessions_create, services=svc.session_services),
        "/api/sessions/relocate": _post_session_relocate,
        "/api/sessions/move": _post_session_relocate,
        "/api/sessions/switch": post_json(
            _sessions_api.post_sessions_switch, services=svc.session_services),
        "/api/sessions/delete": post_json(
            _sessions_api.post_sessions_delete, services=svc.session_services),
        "/api/sessions/clear": post_json(
            _sessions_api.post_sessions_clear, services=svc.session_services,
            needs_body=False),
        "/api/sessions/archive": post_json(
            _sessions_api.post_sessions_archive, services=svc.session_services),
        "/api/sessions/rename": post_json(
            _sessions_api.post_sessions_rename, services=svc.session_services),
        "/api/chat/stash": post_json(
            _sc_api.post_chat_stash, services=svc.session_control_services),
        "/api/session/interrupt": _post_session_interrupt,
        "/api/session/rewind": post_json(
            _sc_api.post_session_rewind, services=svc.session_control_services),
        "/api/session/rewind/restore": post_json(
            _sc_api.post_session_rewind_restore,
            services=svc.session_control_services, needs_body=False),
        "/api/session/steer": post_json(
            _sc_api.post_session_steer, services=svc.session_control_services),
        "/api/session/queue": post_json(
            _sc_api.post_session_queue, services=svc.session_control_services),
        "/api/session/queue/reorder": post_json(
            _sc_api.post_session_queue_reorder,
            services=svc.session_control_services),
        "/api/terminal/create": post_json(
            _term_api.post_terminal_create, services=svc.terminal_services),
        "/api/terminal/write": post_json(
            _term_api.post_terminal_write, services=svc.terminal_services),
        "/api/terminal/resize": post_json(
            _term_api.post_terminal_resize, services=svc.terminal_services),
        "/api/terminal/kill": post_json(
            _term_api.post_terminal_kill, services=svc.terminal_services),
        "/api/wiki/config": post_json(_wiki_api.post_wiki_config),
        "/api/wiki/disconnect": post_json(
            _wiki_api.post_wiki_disconnect, needs_body=False),
        "/api/bedrock": post_json(_plat_api.post_bedrock),
        "/api/auth/pools": post_json(_auth_api.post_auth_pools),
        "/api/auth/pools/add": post_json(
            _auth_api.post_auth_pools_add, services=svc.provider_services),
        "/api/auth/pools/remove": post_json(_auth_api.post_auth_pools_remove),
        "/api/auth/pools/strategy": post_json(_auth_api.post_auth_pools_strategy),
        "/api/auth/pools/reset": post_json(_auth_api.post_auth_pools_reset),
        "/api/auth/oauth/start": post_json(_auth_api.post_auth_oauth_start),
        "/api/auth/oauth/poll": post_json(_auth_api.post_auth_oauth_poll),
        "/api/auth/oauth/complete": post_json(_auth_api.post_auth_oauth_complete),
        "/api/auth/oauth/cancel": post_json(_auth_api.post_auth_oauth_cancel),
        "/api/auth/cursor-cli/status": post_json(
            _auth_api.post_auth_cursor_cli_status),
        "/api/auth/cursor-cli/login": post_json(
            _auth_api.post_auth_cursor_cli_login, services=svc.provider_services),
        "/api/auth/cursor-cli/trust": post_json(
            _auth_api.post_auth_cursor_cli_trust, services=svc.provider_services),
        "/api/auth/cursor-cli/logout": post_json(
            _auth_api.post_auth_cursor_cli_logout, needs_body=False),
        "/api/auth/cursor-cli/models": post_json(
            _auth_api.post_auth_cursor_cli_models, needs_body=False),
        "/api/wiki/handoff": _post_wiki_handoff,
        "/api/git/connect": post_json(_git_api.post_git_connect),
        "/api/git/device/poll": post_json(_git_api.post_git_device_poll),
        "/api/git/disconnect": post_json(
            _git_api.post_git_disconnect, needs_body=False),
        "/api/platform": post_json(
            _plat_api.post_platform, services=svc.platform_services),
        "/api/settings": post_json(
            _settings_api.post_settings, services=svc.settings_services),
        "/api/providers/probe": post_json(_prov_api.post_providers_probe),
        "/api/providers/key": post_json(
            _prov_api.post_providers_key, services=svc.provider_services),
        "/api/registry": post_json(_reg_api.post_registry),
        "/api/roles": post_json(
            _reg_api.post_roles, services=svc.registry_services),
        "/api/pilot/validate": post_json(_reg_api.post_pilot_validate),
        "/api/worktrees/add": post_json(
            _wt_api.post_worktrees_add, services=svc.worktree_services),
        "/api/worktrees/remove": post_json(
            _wt_api.post_worktrees_remove, services=svc.worktree_services),
        "/api/worktrees/prune": post_json(
            _wt_api.post_worktrees_prune, services=svc.worktree_services,
            needs_body=False),
        "/api/worktrees/prune-edit-branches": post_json(
            _wt_api.post_worktrees_prune_edit_branches,
            services=svc.worktree_services, needs_body=False),
        "/api/worktrees/max": post_json(
            _wt_api.post_worktrees_max, services=svc.worktree_services),
        "/api/hooks/add": post_json(_hooks_api.post_hooks_add),
        "/api/hooks/update": post_json(
            _hooks_api.post_hooks_update, services=svc.hooks_services),
        "/api/hooks/remove": post_json(_hooks_api.post_hooks_remove),
    }
    # Attach relocate helper + host_ok/diag via closure attrs on module-level fns.
    _post_session_relocate._svc = svc  # type: ignore[attr-defined]
    _post_session_interrupt._svc = svc  # type: ignore[attr-defined]
    _post_wiki_handoff._svc = svc  # type: ignore[attr-defined]
    _post_restart._svc = svc  # type: ignore[attr-defined]
    return routes


def _post_session_relocate(handler: Any, body: dict) -> Any:
    status, payload = _post_session_relocate._svc.handle_session_relocate(body)  # type: ignore[attr-defined]
    return send_json(handler, status, payload)


def _post_session_interrupt(handler: Any, body: dict) -> Any:
    from .api import session_control as _sc_api
    svc = _post_session_interrupt._svc  # type: ignore[attr-defined]
    sid = (body.get("session_id") or "").strip()
    if not sid:
        try:
            qs = parse_qs(urlparse(handler.path).query)
            sid = (qs.get("session_id") or [""])[0].strip()
        except Exception:
            sid = ""
    status, payload = _sc_api.post_session_interrupt(
        body, sid, svc.session_control_services())
    return send_json(handler, status, payload)


def _post_wiki_handoff(handler: Any, body: dict) -> Any:
    from .api import wiki as _wiki_api
    svc = _post_wiki_handoff._svc  # type: ignore[attr-defined]
    host = handler.headers.get("Host", "") or ""
    if not svc.host_ok(host):
        return send_json(handler, 400, {"error": "bad host"})
    status, payload = _wiki_api.post_wiki_handoff(host)
    return send_json(handler, status, payload)


def _post_restart(handler: Any, body: dict) -> Any:
    from .api import session_control as _sc_api
    svc = _post_restart._svc  # type: ignore[attr-defined]
    ok, err = _sc_api.prepare_session_restart(svc.session_control_services())
    if not ok:
        svc.diag("server.self_edit_restart_persist", Exception(err or "persist failed"))
    handler._send(200, json.dumps({"ok": True, "restarting": True}))

    def _delayed_self_terminate():
        import time as _t
        _t.sleep(0.4)  # let the 200 flush before we exit
        try:
            if os.name == "nt":
                os._exit(0)
            else:
                os.kill(os.getpid(), signal.SIGTERM)
        except Exception:
            os._exit(0)

    threading.Thread(target=_delayed_self_terminate, daemon=True).start()
    return


def build_get_routes(svc: Any) -> dict[str, GetHandler]:
    """Build path → GET handler map. Auth is applied once in ``do_GET``."""
    from .api import auth as _auth_api
    from .api import checkpoints as _ckpt_api
    from .api import codegraph as _cg_api
    from .api import commands as _cmd_api
    from .api import files as _files_api
    from .api import git as _git_api
    from .api import hooks as _hooks_api
    from .api import jobs as _jobs_api
    from .api import mcp as _mcp_api
    from .api import platform as _plat_api
    from .api import providers as _prov_api
    from .api import registry as _reg_api
    from .api import reviews as _rev_api
    from .api import session_control as _sc_api
    from .api import sessions as _sessions_api
    from .api import settings as _settings_api
    from .api import skills as _skills_api
    from .api import sse as _sse_api
    from .api import usage as _usage_api
    from .api import wiki as _wiki_api
    from .api import workspace as _ws_api
    from .api import worktrees as _wt_api

    def _get_git_diff(handler: Any, u: Any, qs: dict) -> Any:
        staged = qs.get("staged", ["0"])[0].strip().lower() in ("1", "true", "yes")
        status, payload = _git_api.get_git_diff(
            qs.get("repo", [""])[0],
            qs.get("file", [""])[0],
            staged,
            svc.git_services(),
        )
        return send_json(handler, status, payload)

    def _get_session_context_at(handler: Any, u: Any, qs: dict) -> Any:
        try:
            turn = int(qs.get("turn", ["0"])[0])
        except (TypeError, ValueError):
            return send_json(handler, 400, {"error": "turn must be an integer"})
        status, payload = _sc_api.get_session_context_at(
            turn, svc.session_control_services())
        return send_json(handler, status, payload)

    def _get_file_raw(handler: Any, u: Any, qs: dict) -> Any:
        rel_path = qs.get("path", [""])[0]
        status, body_or_err, ctype = _files_api.get_file_raw(
            rel_path, svc.file_services())
        if isinstance(body_or_err, dict):
            return send_json(handler, status, body_or_err)
        return handler._send(status, body_or_err, ctype)

    def _get_image(handler: Any, u: Any, qs: dict) -> Any:
        req_path = qs.get("path", [""])[0]
        status, body_or_err, ctype = _files_api.get_image(
            req_path, svc.get_upload_dir())
        if isinstance(body_or_err, dict):
            return send_json(handler, status, body_or_err)
        return handler._send(status, body_or_err, ctype)

    def _get_models_catalog(handler: Any, u: Any, qs: dict) -> Any:
        force = (qs.get("refresh", [""])[0] or "").strip().lower() in (
            "1", "true", "yes")
        status, payload = _prov_api.get_models_catalog(force=force)
        return send_json(handler, status, payload)

    def _get_auth_pools(handler: Any, u: Any, qs: dict) -> Any:
        pname = (qs.get("provider") or [""])[0].strip()
        status, payload = _auth_api.get_auth_pools(provider=pname)
        return send_json(handler, status, payload)

    def _get_jobs(handler: Any, u: Any, qs: dict) -> Any:
        repo_override = qs.get("repo", [""])[0]
        status, payload = _jobs_api.get_jobs(
            repo_override or None, svc.job_services())
        return send_json(handler, status, payload)

    def _get_swarm_live(handler: Any, u: Any, qs: dict) -> Any:
        repo_override = qs.get("repo", [""])[0]
        status, payload = _jobs_api.get_swarm_live(
            repo_override or None, svc.job_services())
        return send_json(handler, status, payload)

    def _get_registry(handler: Any, u: Any, qs: dict) -> Any:
        status, payload = _reg_api.get_registry()
        if isinstance(payload, str):
            return handler._send(status, payload)
        return send_json(handler, status, payload)

    def _get_run(handler: Any, u: Any, qs: dict) -> Any:
        from .api.streams import validate_upload_image_paths
        imgs, err = validate_upload_image_paths(
            qs.get("images", [""])[0], svc.get_upload_dir())
        if err is not None:
            return send_json(handler, err[0], err[1])
        return handler._stream_run(qs.get("prompt", [""])[0], imgs)

    def _get_chat(handler: Any, u: Any, qs: dict) -> Any:
        from .api.streams import (
            resolve_stashed_chat_message,
            validate_upload_image_paths,
        )
        message, raw_images = resolve_stashed_chat_message(
            qs.get("mid", [""])[0],
            qs.get("message", [""])[0],
            qs.get("images", [""])[0],
            svc.stash_pop,
        )
        imgs, err = validate_upload_image_paths(raw_images, svc.get_upload_dir())
        if err is not None:
            return send_json(handler, err[0], err[1])
        plan_val = qs.get("plan", ["false"])[0].lower() in ("true", "1", "yes")
        resume_val = qs.get("resume", ["false"])[0].lower() in ("true", "1", "yes")
        return handler._stream_chat(
            message, imgs, plan=plan_val, resume=resume_val)

    def _get_chat_events(handler: Any, u: Any, qs: dict) -> Any:
        since_raw = qs.get("since", ["0"])[0]
        try:
            since_c = int(since_raw or 0)
        except (TypeError, ValueError):
            since_c = 0
        gen_raw = qs.get("generation", [""])[0]
        generation = None
        if gen_raw not in ("", None):
            try:
                generation = int(gen_raw)
            except (TypeError, ValueError):
                return send_json(
                    handler, 400, {"error": "generation must be an integer"})
        status, payload = _sse_api.get_chat_events(
            svc.sse_services(),
            (qs.get("session", [""])[0] or "").strip(),
            since_c,
            generation,
        )
        return send_json(handler, status, payload)

    def _get_terminal_stream(handler: Any, u: Any, qs: dict) -> Any:
        return handler._stream_terminal(qs.get("id", [""])[0])

    def _get_pilot(handler: Any, u: Any, qs: dict) -> Any:
        return handler._swap_pilot(qs.get("model", [""])[0])

    def _get_auto(handler: Any, u: Any, qs: dict) -> Any:
        objective = qs.get("objective", [""])[0]
        mid = qs.get("mid", [""])[0]
        if mid:
            stashed = svc.stash_pop(mid)
            if stashed is not None:
                objective = stashed.get("message", "")
        return handler._stream_auto(objective)

    def _get_sessions_export(handler: Any, u: Any, qs: dict) -> Any:
        return _sessions_api.write_sessions_export(
            handler, qs, svc.session_services())

    return {
        "/api/git/status": get_json(
            _git_api.get_git_status, services=svc.git_services, qs_arg="repo"),
        "/api/git/branches": get_json(
            _git_api.get_git_branches, services=svc.git_services, qs_arg="repo"),
        "/api/git/diff": _get_git_diff,
        "/api/session/state": get_json(
            _sc_api.get_session_state, services=svc.session_control_services),
        "/api/session/context_at": _get_session_context_at,
        "/api/session/swarm-results": get_json(
            _sc_api.get_session_swarm_results,
            services=svc.session_control_services),
        "/api/session/queue": get_json(
            _sc_api.get_session_queue, services=svc.session_control_services),
        "/api/checkpoints": get_json(
            _ckpt_api.get_checkpoints, services=svc.checkpoint_services),
        "/api/checkpoints/diff": get_json(
            _ckpt_api.get_checkpoints_diff, services=svc.checkpoint_services,
            qs_arg="id"),
        "/api/mcp": get_json(_mcp_api.get_mcp, services=svc.mcp_services),
        "/api/mcp/catalog": get_json(_mcp_api.get_mcp_catalog),
        "/api/commands": get_json(
            _cmd_api.get_commands, services=svc.commands_services, qs_arg="repo"),
        "/api/skills": get_json(
            _skills_api.get_skills, services=svc.skills_services),
        "/api/rules": get_json(
            _skills_api.get_rules, services=svc.skills_services),
        "/api/memory": get_json(
            _skills_api.get_memory, services=svc.skills_services),
        "/api/file/read": get_json(
            _files_api.get_file_read, services=svc.file_services, qs_arg="path"),
        "/api/file/raw": _get_file_raw,
        "/api/image": _get_image,
        "/api/workspace/files": get_json(
            _files_api.get_workspace_files, services=svc.file_services),
        "/api/workspace/symbols": get_json(
            _ws_api.get_workspace_symbols, services=svc.workspace_services,
            qs_arg="q"),
        "/api/workspace": get_json(
            _ws_api.get_workspace, services=svc.workspace_services),
        "/api/models/catalog": _get_models_catalog,
        "/api/codegraph": get_json(
            _cg_api.get_codegraph, services=svc.codegraph_services),
        "/api/config": get_json(
            _settings_api.get_config, services=svc.settings_services),
        "/api/wiki/config": get_json(_wiki_api.get_wiki_config_payload),
        "/api/bedrock": get_json(_plat_api.get_bedrock),
        "/api/auth/pools": _get_auth_pools,
        "/api/wiki/graph": get_json(
            _wiki_api.get_wiki_graph, services=svc.wiki_services),
        "/api/wiki/status": get_json(
            _wiki_api.get_wiki_status, services=svc.wiki_services),
        "/api/settings": get_json(
            _settings_api.get_settings, services=svc.settings_services),
        "/api/reviews": get_json(
            _rev_api.get_reviews, services=svc.review_services),
        "/api/platform": get_json(
            _plat_api.get_platform, services=svc.platform_services),
        "/api/jobs": _get_jobs,
        "/api/usage": get_json(
            _usage_api.get_usage, services=svc.usage_services, qs_arg="repo"),
        "/api/artifacts": get_json(
            _jobs_api.get_artifacts, services=svc.job_services, qs_arg="job_id"),
        "/api/swarm/live": _get_swarm_live,
        "/api/providers": get_json(_prov_api.get_providers),
        "/api/registry": _get_registry,
        "/api/roles": get_json(
            _reg_api.get_roles, services=svc.registry_services),
        "/api/registry/recommend": get_json(_reg_api.get_registry_recommend),
        "/api/run": _get_run,
        "/api/chat": _get_chat,
        "/api/chat/events": _get_chat_events,
        "/api/terminal/stream": _get_terminal_stream,
        "/api/pilot": _get_pilot,
        "/api/context/usage": get_json(
            _usage_api.get_context_usage, services=svc.usage_services),
        "/api/workspaces": get_json(
            _ws_api.get_workspaces, services=svc.workspace_services),
        "/api/worktrees": get_json(
            _wt_api.get_worktrees, services=svc.worktree_services),
        "/api/hooks": get_json(_hooks_api.get_hooks),
        "/api/sessions/transcript": get_json(
            _sessions_api.get_sessions_transcript, services=svc.session_services,
            pass_qs=True),
        "/api/sessions/export": _get_sessions_export,
        "/api/sessions": get_json(
            _sessions_api.get_sessions_list, services=svc.session_services,
            pass_qs=True),
        "/api/sessions/bank": get_json(
            _sessions_api.get_sessions_bank, services=svc.session_services,
            pass_qs=True),
        "/api/sessions/search": get_json(
            _sessions_api.get_sessions_search, services=svc.session_services,
            pass_qs=True),
        "/api/auto": _get_auto,
    }
