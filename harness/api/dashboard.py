"""GET /api/dashboard — ensure the stock Puppetmaster board and return its URL."""

from __future__ import annotations

from typing import Any

from .jobs import JobServices
from ..pm_dashboard import (
    build_dashboard_url,
    ensure_local_dashboard,
    is_benign_non_durable_job_token,
    is_dashboard_job_id,
    resolve_dashboard_state_dir,
)


def get_dashboard(qs: dict, svc: JobServices) -> tuple[int, dict[str, Any]]:
    """Resolve or start the project dashboard. Never invents a second runtime."""
    raw_job = (qs.get("job") or qs.get("job_id") or [""])[0]
    job_id = str(raw_job or "").strip()
    job_ref = None
    if any(key in qs for key in ('state_id', 'version', 'incarnation')):
        from .scoped_cancellation import parse_job_ref
        try:
            job_ref = parse_job_ref(dict(job_id=job_id, state_id=qs['state_id'][0],
                                        version=int(qs['version'][0]), incarnation=qs['incarnation'][0]))
        except (KeyError, ValueError, TypeError, IndexError):
            return 400, {'ok': False, 'error': 'invalid_job_ref'}
    if job_id.startswith("local-"):
        pilot = svc.get_pilot()
        local = pilot.get_local_job(job_id) if pilot is not None else None
        canonical = local.get("canonical") if isinstance(local, dict) else None
        if isinstance(canonical, dict):
            ref = canonical.get("job_ref")
            if isinstance(ref, dict) and is_dashboard_job_id(ref.get("job_id", "")):
                job_id = ref["job_id"]
                job_ref = ref
    # Durable job_… deep-links. Benign local aliases open the board without
    # ?job=. Path escapes and other unsafe tokens still 400.
    if job_id and not is_dashboard_job_id(job_id):
        if is_benign_non_durable_job_token(job_id):
            job_id = ""
        else:
            return 400, {"ok": False, "error": "invalid_job_id"}
    repo = str((qs.get("repo") or [""])[0] or "").strip()
    if not repo:
        repo = str(getattr(svc.cfg, "repo", "") or "").strip()
    try:
        session = svc.get_session() if job_id else None
        store = session.state().store if session is not None else None
        if job_ref is not None:
            state_dir = resolve_dashboard_state_dir(repo, job_id, job_ref=job_ref,
                                                    default_dir=getattr(store, 'root', None))
        else:
            state_dir = resolve_dashboard_state_dir(repo, job_id)
        if job_id and job_ref is None:
            from puppetmaster.state import state_owns_job
            if store is not None and state_owns_job(store.root, job_id):
                from pathlib import Path
                if state_dir and Path(state_dir).resolve() != Path(store.root).resolve():
                    raise ValueError("ambiguous job_id; multiple owning stores")
                state_dir = str(store.root)
    except Exception as exc:
        return 503, {"ok": False, "error": "state_dir_unavailable", "detail": str(exc)}
    if not state_dir:
        if job_id or not repo:
            return 503, {
                "ok": False,
                "error": "state_dir_unavailable",
                "detail": "No Puppetmaster project store for this workspace.",
            }
        try:
            from ..cli_job_merge import ensure_workspace_project_store

            state_dir = ensure_workspace_project_store(repo)
        except Exception as exc:
            return 503, {
                "ok": False,
                "error": "state_dir_unavailable",
                "detail": str(exc),
            }
    if not state_dir:
        return 503, {
            "ok": False,
            "error": "state_dir_unavailable",
            "detail": "No Puppetmaster project store for this workspace.",
        }
    try:
        result = ensure_local_dashboard(state_dir=state_dir, job_id=job_id or None)
    except Exception as exc:
        return 503, {"ok": False, "error": "dashboard_unavailable", "detail": str(exc)}
    if not result.get("ok"):
        return 503, result
    host = str(result.get("host") or "127.0.0.1")
    port = int(result.get("port") or 0)
    url = str(result.get("url") or build_dashboard_url(host, port, job_id or None))
    return 200, {
        "ok": True,
        "reused": bool(result.get("reused")),
        "host": host,
        "port": port,
        "url": url,
        "embed_url": url,
        "job_id": job_id or None,
    }
