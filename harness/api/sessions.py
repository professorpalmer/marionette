"""Session HTTP route bodies and helpers (peeled from ``harness.server``).

Pure-ish functions take a :class:`SessionServices` (or explicit kwargs) so this
module never imports ``harness.server`` at module top level. ``server.py``
builds services from its module globals and re-exports the historical names
(``_handle_session_delete``, ``_stash_put``, …) for tests and callers.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Callable
from ..diag import note as _diag_default
from ..sessions import load_transcript, session_stored_root, session_visible_for_workspace
from ..session_runners import LeaseExhaustedError


# ---------------------------------------------------------------------------
# Chat stash (self-contained; historically lived beside session routes)
# ---------------------------------------------------------------------------

_CHAT_STASH: dict[str, dict] = {}
_CHAT_STASH_MAX = 32


def stash_put(message: str, images=None) -> str:
    mid = secrets.token_hex(8)
    _CHAT_STASH[mid] = {"message": message, "images": images or []}
    # Evict oldest entries beyond the cap (insertion order == age in a dict).
    while len(_CHAT_STASH) > _CHAT_STASH_MAX:
        try:
            _CHAT_STASH.pop(next(iter(_CHAT_STASH)))
        except StopIteration:
            break
    return mid


def stash_pop(mid: str):
    """Returns the stashed {'message', 'images'} dict, or None if unknown/expired."""
    return _CHAT_STASH.pop(mid, None)


# ---------------------------------------------------------------------------
# Dependency bundle
# ---------------------------------------------------------------------------

@dataclass
class SessionServices:
    """Explicit deps for session HTTP handlers (injected by ``server.py``)."""

    sessions: Any
    runners: Any
    cfg: Any
    get_pilot: Callable[[], Any]
    sessions_state_dir: Callable[[], str]
    save_active_transcript: Callable[[], None]
    attach_view: Callable[..., Any]
    sync_pilot_session_id: Callable[[], None]
    diag: Callable[..., None]
    is_app_install_root: Callable[[str], bool]
    ensure_home_workspace: Callable[[], str]
    prepare_home_workspace: Callable[[], str]
    home_workspace_path: Callable[[], str]
    note_boot_repo: Callable[[str], None]
    record_recent_workspace: Callable[..., Any]
    puppetmaster_available: Callable[[], bool]
    index_codegraph_bg: Callable[[str], None]
    maybe_refresh_codegraph: Callable[[str], None]
    get_codegraph_status: Callable[[str], str]
    lease_exhausted_body: Callable[..., dict]
    attach_view_transcript_payload: Callable[[Any, str], dict]
    parse_bool: Callable[[Any], bool]
    # (status,) leaves reason untouched; (status, reason) sets both (reason may be None).
    set_codegraph_status: Callable[..., None]


# ---------------------------------------------------------------------------
# Module helpers (re-exported from server under historical names)
# ---------------------------------------------------------------------------

def remove_session_transcript(
    sid: str,
    *,
    state_dir: str,
    diag: Callable[..., None] = _diag_default,
) -> None:
    safe_sid = "".join(c for c in sid if c.isalnum() or c in ("-", "_"))
    if not safe_sid:
        return
    trans_dir = os.path.abspath(os.path.join(state_dir, "transcripts"))
    p = os.path.abspath(os.path.join(trans_dir, f"{safe_sid}.json"))
    if p.startswith(trans_dir) and os.path.exists(p):
        try:
            os.remove(p)
        except Exception as e:
            diag("server.session_delete_transcript", e, msg=f"sid={safe_sid}")
    try:
        from ..compaction_archive import remove_compaction_archive
        remove_compaction_archive(state_dir, safe_sid)
    except Exception as e:
        diag("server.session_delete_compaction_archive", e, msg=f"sid={safe_sid}")
    try:
        from ..session_fts import remove_session_from_index
        remove_session_from_index(state_dir, safe_sid)
    except Exception as e:
        diag("server.session_delete_fts", e, msg=f"sid={safe_sid}")
    try:
        from ..stream_performance_store import remove_session_performance_receipts
        remove_session_performance_receipts(state_dir, safe_sid)
    except Exception as e:
        diag("server.session_delete_stream_performance", e, msg=f"sid={safe_sid}")


def handle_session_delete(sid: str, svc: SessionServices) -> tuple[int, dict]:
    if not sid:
        return 400, {"error": "missing session id"}
    is_active = (svc.sessions.active == sid)
    from ..hooks import run_hooks
    run_hooks("sessionEnd", {"session_id": sid})
    new_active = svc.sessions.delete(sid)
    remove_session_transcript(sid, state_dir=svc.sessions_state_dir(), diag=svc.diag)
    try:
        svc.runners.drop(sid)
    except Exception as e:
        svc.diag("server.session_delete_drop_runner", e)
    if is_active:
        pilot = svc.get_pilot()
        if new_active:
            try:
                svc.attach_view(new_active)
            except LeaseExhaustedError:
                # Fall back to loading into the current global pilot pointer.
                history = load_transcript(svc.sessions_state_dir(), new_active)
                svc.sync_pilot_session_id()
                pilot.load_history(history)
        else:
            svc.sync_pilot_session_id()
            pilot.load_history([])
    return 200, {"ok": True, "active": new_active}


def handle_session_relocate(body: dict, svc: SessionServices) -> tuple[int, dict]:
    """Move an existing session into a project workspace (no new blank session).

    Updates ``workspace_root``/``repo``, records the target in recents, opens
    the workspace as active, and keeps the same session id / transcript file.
    """
    target_repo = (body.get("workspace_root") or body.get("path") or body.get("repo") or "").strip()
    if not target_repo:
        return 400, {"ok": False, "error": "workspace_root is required"}
    if not os.path.isdir(target_repo):
        return 400, {"ok": False, "error": f"path is not an existing directory: {target_repo}"}
    if svc.is_app_install_root(target_repo):
        return 400, {"ok": False, "error": "refusing to relocate into the Marionette app checkout"}

    sid = (body.get("session_id") or body.get("session") or body.get("id") or "").strip()
    if not sid:
        sid = (svc.sessions.active or "").strip()
    if not sid:
        return 400, {"ok": False, "error": "no session_id and no active session"}

    title = body.get("title")
    svc.save_active_transcript()

    prev_active = svc.sessions.active
    prev_repo = svc.cfg.repo
    prev_env_repo = os.environ.get("HARNESS_REPO")

    branch = ""
    try:
        import subprocess
        proc = subprocess.run(
            ["git", "-C", target_repo, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0:
            branch = (proc.stdout or "").strip()
    except Exception:
        pass

    relocated = svc.sessions.relocate(
        sid,
        target_repo,
        repo=target_repo,
        branch=branch,
        title=title if isinstance(title, str) else None,
        make_active=True,
    )
    if not relocated:
        return 404, {"ok": False, "error": "unknown session"}

    svc.cfg.repo = target_repo
    os.environ["HARNESS_REPO"] = target_repo
    try:
        from ..swarm_adapter import ensure_repo_swarm_adapter
        ensure_repo_swarm_adapter(svc.cfg)
    except Exception as e:
        svc.diag("server.session_relocate_swarm_adapter", e)
    svc.note_boot_repo(target_repo)
    try:
        svc.record_recent_workspace(target_repo)
    except Exception as e:
        svc.diag("server.session_relocate_record_recent", e)

    has_codegraph = os.path.isdir(os.path.join(target_repo, ".codegraph"))
    if not has_codegraph:
        if svc.puppetmaster_available():
            svc.set_codegraph_status("indexing", None)
        svc.index_codegraph_bg(target_repo)
    else:
        if svc.puppetmaster_available():
            svc.set_codegraph_status("ready")
            svc.maybe_refresh_codegraph(target_repo)
        else:
            svc.set_codegraph_status("unsupported")

    try:
        svc.attach_view(sid)
    except LeaseExhaustedError as e:
        if prev_active:
            try:
                svc.sessions.switch(prev_active)
            except Exception as roll_e:
                svc.diag("server.session_relocate_lease_rollback", roll_e)
        if svc.cfg.repo != prev_repo:
            svc.cfg.repo = prev_repo
            if prev_env_repo is None:
                os.environ.pop("HARNESS_REPO", None)
            else:
                os.environ["HARNESS_REPO"] = prev_env_repo
        return 409, svc.lease_exhausted_body(e)

    return 200, {
        "ok": True,
        "session": relocated,
        "active": sid,
        "repo": target_repo,
        "workspace_root": target_repo,
        "codegraph": svc.get_codegraph_status(target_repo),
    }


# ---------------------------------------------------------------------------
# POST route bodies
# ---------------------------------------------------------------------------

def post_sessions_create(body: dict, svc: SessionServices) -> tuple[int, dict]:
    svc.save_active_transcript()
    # Snapshot so a lease-exhausted attach can roll back without leaving
    # the store pointed at an unattached session.
    prev_active = svc.sessions.active
    title = body.get("title") or "New session"
    repo = (svc.cfg.repo or "").strip()
    # No Open Folder: bind the session to the durable Home workspace so
    # it appears under Projects -> Home (never a rootless orphan).
    if not repo:
        repo = svc.ensure_home_workspace()
    branch = ""
    if repo and os.path.isdir(repo):
        import subprocess
        try:
            proc = subprocess.run(
                ["git", "-C", repo, "rev-parse", "--is-inside-work-tree"],
                capture_output=True, text=True, timeout=5
            )
            if proc.returncode == 0:
                proc_branch = subprocess.run(
                    ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
                    capture_output=True, text=True, timeout=5
                )
                if proc_branch.returncode == 0:
                    branch = proc_branch.stdout.strip()
        except Exception:
            pass
    res = svc.sessions.create(title, repo=repo, branch=branch, workspace_root=repo)
    sid = res.get("id", "")
    if sid:
        try:
            # New session runner starts at zero meters (boot pill sums
            # carry + all live runners -- do not snapshot from active).
            svc.attach_view(
                sid,
                load_transcript_on_create=False,
                defer_cold_build=True,
            )
            svc.get_pilot().load_history([])
        except LeaseExhaustedError as e:
            try:
                svc.sessions.delete(sid)
            except Exception as roll_e:
                svc.diag("server.session_create_lease_delete", roll_e)
            if prev_active:
                try:
                    svc.sessions.switch(prev_active)
                except Exception as roll_e:
                    svc.diag("server.session_create_lease_rollback", roll_e)
            return 409, svc.lease_exhausted_body(e)

    from ..hooks import run_hooks
    run_hooks("sessionStart", {"session_id": sid, "title": title})

    return 200, res


def post_sessions_switch(body: dict, svc: SessionServices) -> tuple[int, dict]:
    # Multi-session: switching VIEW must not 409 just because the
    # outgoing (or another) runner is busy -- other sessions keep
    # executing under the lease. Only LeaseExhaustedError blocks.
    target_id = (body.get("id") or "").strip()
    svc.save_active_transcript()
    # Snapshot so a lease-exhausted attach can roll back active + repo.
    prev_active = svc.sessions.active
    prev_repo = svc.cfg.repo
    prev_env_repo = os.environ.get("HARNESS_REPO")
    res = svc.sessions.switch(target_id)
    if res.get("ok") and svc.sessions.active:
        target_sess = None
        for s in svc.sessions.list():
            if s.get("id") == svc.sessions.active:
                target_sess = s
                break
        target_repo = ""
        if target_sess:
            target_repo = (
                session_stored_root(target_sess)
                or (target_sess.get("repo") or "").strip()
            )

        # Never let a stale app-checkout session yank the live workspace
        # back to ~/.marionette/marionette (or the running source tree).
        # Conversation view still switches; only the project root is kept.
        if (
            target_repo
            and os.path.isdir(target_repo)
            and target_repo != svc.cfg.repo
            and not svc.is_app_install_root(target_repo)
        ):
            svc.cfg.repo = target_repo
            os.environ["HARNESS_REPO"] = target_repo
            try:
                from ..context_switch_guard import note_switch
                note_switch("session", prev_repo or "", target_repo)
            except Exception as e:
                svc.diag("server.session_switch_note_guard", e)
            try:
                from ..swarm_adapter import ensure_repo_swarm_adapter
                ensure_repo_swarm_adapter(svc.cfg)
            except Exception as e:
                svc.diag("server.session_switch_swarm_adapter", e)
            svc.note_boot_repo(target_repo)
            # Session-switch repoints must land in recents too, or the
            # dir only exists in the projects list while it is current
            # and vanishes the moment the workspace moves elsewhere.
            try:
                svc.record_recent_workspace(target_repo)
            except Exception as e:
                svc.diag("server.session_switch_record_recent", e)

            has_codegraph = os.path.isdir(os.path.join(target_repo, ".codegraph"))
            if not has_codegraph:
                if svc.puppetmaster_available():
                    svc.set_codegraph_status("indexing", None)
                svc.index_codegraph_bg(target_repo)
            else:
                if svc.puppetmaster_available():
                    svc.set_codegraph_status("ready")
                    svc.maybe_refresh_codegraph(target_repo)
                else:
                    svc.set_codegraph_status("unsupported")

        try:
            svc.attach_view(svc.sessions.active, defer_cold_build=True)
        except LeaseExhaustedError as e:
            if prev_active:
                try:
                    svc.sessions.switch(prev_active)
                except Exception as roll_e:
                    svc.diag("server.session_switch_lease_rollback", roll_e)
            if svc.cfg.repo != prev_repo:
                svc.cfg.repo = prev_repo
                if prev_env_repo is None:
                    os.environ.pop("HARNESS_REPO", None)
                else:
                    os.environ["HARNESS_REPO"] = prev_env_repo
            return 409, svc.lease_exhausted_body(e)

        res["repo"] = svc.cfg.repo
        res["codegraph"] = (
            svc.get_codegraph_status(svc.cfg.repo) if svc.cfg.repo else "none"
        )
        # Hermes-style: runner status + transcript on the switch response
        # so the UI can paint before deferred ConversationalSession lands.
        # Building placeholders report running (lease/busy honesty).
        active_id = svc.sessions.active or ""
        res["state"] = svc.runners.status(active_id) if active_id else "missing"
        res["transcript"] = svc.attach_view_transcript_payload(
            svc.get_pilot(), active_id
        )

    return 200, res


def post_sessions_delete(body: dict, svc: SessionServices) -> tuple[int, dict]:
    sid = body.get("session") or body.get("id") or ""
    return handle_session_delete(sid, svc)


def post_sessions_clear(svc: SessionServices) -> tuple[int, dict]:
    repo_root = svc.cfg.repo or ""
    state_dir = svc.sessions_state_dir()
    prior_active = svc.sessions.active
    deleted_ids, new_active = svc.sessions.clear_for_workspace(repo_root, state_dir)
    from ..hooks import run_hooks
    for sid in deleted_ids:
        run_hooks("sessionEnd", {"session_id": sid})
        remove_session_transcript(sid, state_dir=state_dir, diag=svc.diag)
        try:
            svc.runners.drop(sid)
        except Exception as e:
            svc.diag("server.session_clear_drop_runner", e)
    if prior_active in deleted_ids:
        pilot = svc.get_pilot()
        if new_active:
            try:
                svc.attach_view(new_active)
            except LeaseExhaustedError:
                history = load_transcript(state_dir, new_active)
                svc.sync_pilot_session_id()
                pilot.load_history(history)
        else:
            svc.sync_pilot_session_id()
            pilot.load_history([])
    return 200, {
        "ok": True,
        "deleted": len(deleted_ids),
        "active": new_active,
    }


def post_sessions_archive(body: dict, svc: SessionServices) -> tuple[int, dict]:
    sid = body.get("session") or body.get("id") or ""
    if not sid:
        return 400, {"error": "missing session id"}
    archived = svc.parse_bool(body.get("archived"))
    svc.sessions.archive(sid, archived)
    if archived:
        try:
            from ..chat_archive import ingest_marionette_session
            row = next((s for s in svc.sessions.rows() if s.get("id") == sid), None) or {}
            ingest_marionette_session(
                svc.sessions_state_dir(),
                sid,
                title=str(row.get("title") or ""),
                workspace=str(row.get("workspace_root") or row.get("repo") or ""),
                updated_at=int(row.get("created") or 0),
            )
        except Exception:
            pass
    else:
        try:
            from ..chat_archive import restore_pruned_transcript
            restore_pruned_transcript(svc.sessions_state_dir(), sid)
        except Exception:
            pass
    return 200, {"ok": True}


def post_sessions_settle(body: dict, svc: SessionServices) -> tuple[int, dict]:
    """Persist independent inbox triage ``settled`` (not ``archived``).

    Stricter than archive: unknown ids 404; sessions outside the active
    workspace visibility scope 403.
    """
    sid = body.get("session") or body.get("id") or ""
    if not sid:
        return 400, {"error": "missing session id"}
    settled = svc.parse_bool(body.get("settled"))
    row = next((s for s in svc.sessions.rows() if s.get("id") == sid), None)
    if row is None:
        return 404, {"error": "unknown session"}
    workspace_root = (getattr(svc.cfg, "repo", None) or "").strip()
    if workspace_root and not session_visible_for_workspace(
        row, workspace_root, svc.sessions_state_dir(),
    ):
        return 403, {"error": "session not visible in active workspace"}
    svc.sessions.settle(sid, settled)
    return 200, {"ok": True}


def post_sessions_rename(body: dict, svc: SessionServices) -> tuple[int, dict]:
    sid = body.get("session") or body.get("id") or ""
    title = body.get("title") or ""
    if not sid:
        return 400, {"error": "missing session id"}
    if not title:
        return 400, {"error": "missing title"}
    ok = svc.sessions.rename(sid, title)
    return 200, {"ok": ok}


# ---------------------------------------------------------------------------
# GET route bodies
# ---------------------------------------------------------------------------

def get_sessions_list(qs: dict, svc: SessionServices) -> tuple[int, Any]:
    # Optional ?repo=<path> lists sessions for that root WITHOUT switching
    # the active workspace (LeftRail prefetches every project row).
    # ?all=1 (or /api/sessions/bank) returns the cross-workspace bank.
    all_flag = (qs.get("all", [""])[0] or "").strip().lower()
    want_all = all_flag in ("1", "true", "yes", "on")
    if want_all:
        query = (qs.get("q", [""])[0] or qs.get("query", [""])[0] or "").strip()
        try:
            limit = int((qs.get("limit", ["50"])[0] or "50"))
        except ValueError:
            limit = 50
        return 200, svc.sessions.list_bank(
            query=query,
            limit=limit,
            state_dir=svc.sessions_state_dir(),
        )
    repo_override = (qs.get("repo", [""])[0] or "").strip()
    root = repo_override or (svc.cfg.repo or "")
    if not repo_override and not root:
        # No Open Folder: sidebar lists Home-bound sessions, not everything.
        # Prepare the bind root for listing but do not re-record Home in recents.
        try:
            root = svc.prepare_home_workspace()
        except Exception:
            try:
                root = svc.home_workspace_path()
            except Exception:
                root = ""
    return 200, svc.sessions.list(
        workspace_root=root,
        state_dir=svc.sessions_state_dir(),
    )


def get_sessions_bank(qs: dict, svc: SessionServices) -> tuple[int, Any]:
    query = (qs.get("q", [""])[0] or qs.get("query", [""])[0] or "").strip()
    try:
        limit = int((qs.get("limit", ["50"])[0] or "50"))
    except ValueError:
        limit = 50
    return 200, svc.sessions.list_bank(
        query=query,
        limit=limit,
        state_dir=svc.sessions_state_dir(),
    )


def get_sessions_search(qs: dict, svc: SessionServices) -> tuple[int, Any]:
    query = (qs.get("q", [""])[0] or qs.get("query", [""])[0] or "").strip()
    try:
        limit = int((qs.get("limit", ["20"])[0] or "20"))
    except ValueError:
        limit = 20
    from ..session_fts import search_sessions
    return 200, search_sessions(
        svc.sessions_state_dir(),
        query,
        limit=limit,
    )


def get_sessions_transcript(qs: dict, svc: SessionServices) -> tuple[int, dict]:
    sid = qs.get("session", [None])[0] or svc.sessions.active or ""
    # Prefer the live runner's in-memory transcript when present so a
    # mid-turn detached UI poll sees cards as they land (not a stale
    # disk snapshot). Fall back to disk for evicted / never-attached ids.
    data = None
    try:
        live = svc.runners.get(sid) if sid else None
        if live is not None and hasattr(live, "export_transcript_data"):
            data = live.export_transcript_data()
    except Exception as e:
        svc.diag("server.transcript_live_export", e)
        data = None
    if data is None:
        data = load_transcript(svc.cfg.state_dir or tempfile.gettempdir(), sid)
    if isinstance(data, dict):
        history_list = data.get("history", [])
        display_list = data.get("display", [])
        job_ids_list = data.get("job_ids", [])
    else:
        history_list = data
        display_list = []
        job_ids_list = []
    return 200, {
        "history": history_list,
        "display": display_list,
        "job_ids": job_ids_list,
    }


@dataclass
class SessionExportAttachment:
    content_type: str
    filename: str
    data: bytes


def _sanitize_export_filename_part(value):
    cleaned = re.sub(r"[^a-zA-Z0-9\-_]", "_", value or "")
    cleaned = re.sub(r"_+", "_", cleaned).strip("_-")
    return cleaned or "session"


def session_export_filename(title, sid, ext):
    """Hermes-style `{title}-{id}.{ext}` so the file itself carries the transcript ID."""
    id_part = _sanitize_export_filename_part(sid or "session")
    title_part = _sanitize_export_filename_part(title or id_part)
    if title_part.lower() == id_part.lower():
        return "%s.%s" % (id_part, ext)
    return "%s-%s.%s" % (title_part, id_part, ext)


def session_transcript_relpath(sid):
    safe_sid = "".join(c for c in (sid or "") if c.isalnum() or c in ("-", "_"))
    return "transcripts/%s.json" % (safe_sid or "session")


def format_session_export_markdown(payload):
    import datetime

    sid = payload.get("transcript_id") or payload.get("session_id") or ""
    title = payload.get("title") or "Unknown Session"
    relpath = payload.get("transcript_relpath") or session_transcript_relpath(sid)
    created = payload.get("created")
    exported_at = payload.get("exported_at")
    created_str = (
        datetime.datetime.fromtimestamp(created).strftime("%Y-%m-%d %H:%M:%S")
        if created
        else "Unknown"
    )
    exported_str = (
        datetime.datetime.fromtimestamp(exported_at).strftime("%Y-%m-%d %H:%M:%S")
        if exported_at
        else "Unknown"
    )
    md_lines = [
        "# %s" % title,
        "",
        "**Transcript ID:** %s  " % sid,
        "**Session ID:** %s  " % sid,
        "**On disk:** `%s`  " % relpath,
        "**Created:** %s  " % created_str,
        "**Exported:** %s" % exported_str,
        "",
    ]
    for msg in payload.get("messages") or []:
        role = (msg.get("role") or "").capitalize()
        content = msg.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content)
        md_lines.extend(["## %s" % role, "", content, ""])
    return "\n".join(md_lines)


def get_sessions_export(qs: dict, svc: SessionServices) -> SessionExportAttachment:
    sid = qs.get("session", [None])[0] or svc.sessions.active or ""
    fmt = qs.get("format", ["json"])[0]

    meta = next((s for s in svc.sessions._sessions if s["id"] == sid), None)
    data = load_transcript(svc.cfg.state_dir or tempfile.gettempdir(), sid)
    if isinstance(data, dict):
        history = data.get("history", [])
    else:
        history = data

    title = meta.get("title", "Unknown Session") if meta else "Unknown Session"
    created = meta.get("created") if meta else None
    export_data = {
        "transcript_id": sid,
        "session_id": sid,
        "transcript_relpath": session_transcript_relpath(sid),
        "title": title or "Unknown Session",
        "created": created,
        "exported_at": time.time(),
        "messages": history,
    }

    if fmt == "md":
        body = format_session_export_markdown(export_data)
        return SessionExportAttachment(
            content_type="text/markdown",
            filename=session_export_filename(title, sid, "md"),
            data=body.encode("utf-8"),
        )

    body = json.dumps(export_data, indent=2)
    return SessionExportAttachment(
        content_type="application/json",
        filename=session_export_filename(title, sid, "json"),
        data=body.encode("utf-8"),
    )


def write_sessions_export(handler: Any, qs: dict, svc: SessionServices) -> None:
    """Write the export attachment onto a BaseHTTPRequestHandler-like object."""
    att = get_sessions_export(qs, svc)
    handler.send_response(200)
    handler.send_header("Content-Type", att.content_type)
    handler.send_header("Content-Length", str(len(att.data)))
    handler.send_header(
        "Content-Disposition", f'attachment; filename="{att.filename}"'
    )
    handler._cors()
    handler.end_headers()
    handler.wfile.write(att.data)


def get_sessions_runners(svc: SessionServices) -> tuple[int, dict]:
    """GET /api/sessions/runners — daemon-owned pilot session tree."""
    statuses = svc.runners.statuses()
    titles = {s.get("id", ""): s.get("title", "") for s in svc.sessions.list()}
    rows = []
    for sid in svc.runners.ids():
        rows.append(
            {
                "session_id": sid,
                "status": statuses.get(sid, "missing"),
                "title": titles.get(sid, ""),
                "active_view": sid == svc.runners.active_view_id,
            }
        )
    return 200, {
        "ok": True,
        "active_view_id": svc.runners.active_view_id,
        "runners": rows,
    }


def post_sessions_attach(body: dict, svc: SessionServices) -> tuple[int, dict]:
    """POST /api/sessions/attach — attach UI view to a live daemon runner."""
    target_id = (body.get("id") or body.get("session_id") or "").strip()
    if not target_id:
        return 400, {"ok": False, "error": "session id required"}
    svc.save_active_transcript()
    res = svc.sessions.switch(target_id)
    if not res.get("ok"):
        return 404, res
    try:
        svc.attach_view(svc.sessions.active, defer_cold_build=True)
    except LeaseExhaustedError as exc:
        return 409, svc.lease_exhausted_body(exc)
    transcript = svc.attach_view_transcript_payload(svc.get_pilot(), target_id)
    return 200, {
        "ok": True,
        "id": target_id,
        "active": target_id,
        "runners": svc.runners.statuses(),
        "active_view_id": svc.runners.active_view_id,
        **transcript,
    }


def post_sessions_detach(body: dict, svc: SessionServices) -> tuple[int, dict]:
    """POST /api/sessions/detach — UI detach must not cancel an in-flight turn."""
    sid = (body.get("id") or body.get("session_id") or svc.sessions.active or "").strip()
    if not sid:
        return 400, {"ok": False, "error": "session id required"}
    svc.save_active_transcript()
    detached = svc.runners.detach_view(sid)
    return 200, {
        "ok": True,
        "detached": sid,
        "was_active_view": detached,
        "runners": svc.runners.statuses(),
        "active_view_id": svc.runners.active_view_id,
    }

