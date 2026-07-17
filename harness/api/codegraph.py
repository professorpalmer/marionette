"""Codegraph admin HTTP route bodies (peeled from ``harness.server``).

Owns POST reindex / apply-excludes and GET ``/api/codegraph`` status panel.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Union


@dataclass
class CodegraphServices:
    """Explicit deps for codegraph HTTP handlers."""

    cfg: Any
    index_alive: Callable[[], bool]
    reindex_bg: Callable[[str], None]
    index_bg: Callable[[str], None]
    get_status: Callable[[str], str]
    get_reason: Callable[[], Optional[str]]
    set_reason: Callable[[str], None]
    # GET /api/codegraph panel (live module status + status subprocess cache)
    get_live_status: Callable[[], str]
    set_live_status: Callable[[str], None]
    get_preflight: Callable[[], Any]
    get_suggested_action: Callable[[], Any]
    puppetmaster_available: Callable[[], bool]
    codegraph_indexed: Callable[[str], bool]
    status_cache_get: Callable[[str], Optional[tuple]]
    status_cache_put: Callable[[str, Any], None]
    status_cache_pop: Callable[[str], None]
    fail_until_for: Callable[[str], float]
    puppetmaster_cmd: Callable[..., list]
    status_ttl: float = 30.0


JsonPayload = Union[dict, list]


def _empty_panel(
    *,
    repo: str = "",
    status: str = "none",
    last_indexed=None,
    include_reason: bool = False,
    reason=None,
    include_scope: bool = False,
    preflight=None,
    suggested_action=None,
) -> dict:
    """Build a null-metrics panel payload.

    Optional keys match the historical Handler shapes: bare ``none`` /
    subprocess-fail omit ``reason``; unsupported-no-puppetmaster includes
    ``reason`` only; needs_scope / indexing include reason+preflight+action.
    """
    payload = {
        "indexed": False,
        "status": status,
        "nodes": None,
        "edges": None,
        "files": None,
        "languages": None,
        "last_indexed": last_indexed,
        "repo": repo,
    }
    if include_reason or include_scope:
        payload["reason"] = reason
    if include_scope:
        payload["preflight"] = preflight
        payload["suggested_action"] = suggested_action
    return payload


def _last_indexed_iso(repo: str) -> Optional[str]:
    try:
        import puppetmaster.codegraph as cg
        mtime = cg.codegraph_index_mtime(repo)
        if mtime:
            import datetime
            return datetime.datetime.fromtimestamp(mtime).isoformat()
    except Exception:
        try:
            db_path = os.path.join(repo, ".codegraph", "db")
            if not os.path.exists(db_path):
                db_path = os.path.join(repo, ".codegraph")
            if os.path.exists(db_path):
                mtime = os.path.getmtime(db_path)
                import datetime
                return datetime.datetime.fromtimestamp(mtime).isoformat()
        except Exception:
            pass
    return None


def get_codegraph(svc: CodegraphServices) -> tuple[int, JsonPayload]:
    """GET /api/codegraph — status panel payload."""
    repo = svc.cfg.repo
    if not repo or not os.path.isdir(repo):
        return 200, _empty_panel(repo="", status="none")

    if not svc.puppetmaster_available():
        return 200, _empty_panel(
            repo=repo,
            status="unsupported",
            include_reason=True,
            reason=svc.get_reason() or "puppetmaster not found -- codegraph/swarm unavailable",
        )

    # Only report "indexing" while the indexer subprocess is actually
    # alive. If the flag is stale (job finished), fall through to the real
    # status query so the panel shows live metrics instead of nulls --
    # this is what previously stuck the panel on INDEXING until a restart.
    # Preserve needs_scope: do not collapse it to unsupported.
    live = svc.get_live_status()
    if live == "indexing" and not svc.index_alive():
        if svc.codegraph_indexed(repo):
            svc.set_live_status("ready")
            live = "ready"
        elif live != "needs_scope":
            svc.set_live_status("unsupported")
            live = "unsupported"
        svc.status_cache_pop(repo)

    if live == "needs_scope":
        return 200, _empty_panel(
            repo=repo,
            status="needs_scope",
            include_scope=True,
            reason=svc.get_reason(),
            preflight=svc.get_preflight(),
            suggested_action=svc.get_suggested_action(),
        )

    if live == "indexing" and svc.index_alive():
        return 200, _empty_panel(
            repo=repo,
            status="indexing",
            include_scope=True,
            reason=svc.get_reason() or None,
            preflight=svc.get_preflight(),
            suggested_action=svc.get_suggested_action(),
            last_indexed=_last_indexed_iso(repo),
        )

    # No built DB yet and no indexer running: start one and report
    # "indexing" rather than shelling out to `codegraph status --json`,
    # which hangs on a config-only checkout until the 20s timeout and then
    # mis-reports "unsupported". This makes a fresh install self-heal.
    # Skip the kick when preflight already said the tree is unindexable,
    # the path is gone, or we recently failed for this repo.
    if not svc.codegraph_indexed(repo) and not svc.index_alive():
        if not os.path.isdir(repo):
            return 200, _empty_panel(
                repo=repo,
                status="unsupported",
                include_scope=True,
                reason=svc.get_reason() or f"Workspace path is missing: {repo}",
                preflight=svc.get_preflight(),
                suggested_action=svc.get_suggested_action(),
            )
        fail_until = float(svc.fail_until_for(repo) or 0)
        if fail_until > time.monotonic():
            status = live if live in ("unsupported", "needs_scope") else "unsupported"
            return 200, _empty_panel(
                repo=repo,
                status=status,
                include_scope=True,
                reason=svc.get_reason(),
                preflight=svc.get_preflight(),
                suggested_action=svc.get_suggested_action(),
            )

        def _kick_index():
            svc.index_bg(repo)

        threading.Thread(target=_kick_index, daemon=True).start()
        # Preflight inside _index_codegraph_bg may flip to needs_scope;
        # report indexing briefly, next poll picks up the real status.
        return 200, _empty_panel(
            repo=repo,
            status="indexing",
            include_scope=True,
            reason=svc.get_reason(),
            preflight=svc.get_preflight(),
            suggested_action=svc.get_suggested_action(),
        )

    # Serve a recent cached payload instead of re-spawning the status
    # subprocess on every poll (the main source of panel load lag).
    cached = svc.status_cache_get(repo)
    if cached and cached[0] > time.monotonic():
        return 200, cached[1]

    try:
        import subprocess
        # 20s (not 5s): codegraph status on a large indexed repo
        # (e.g. 60k+ nodes) takes ~5s in the packaged/frozen binary --
        # right at a 5s limit, which intermittently tripped a timeout
        # and showed "UNSUPPORTED" in the panel even though the repo is
        # fully indexed. The 30s status cache means this slower call is
        # only paid on a cache miss, so the panel stays responsive.
        proc = subprocess.run(
            svc.puppetmaster_cmd("codegraph", "status", "--json"),
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if proc.returncode == 0:
            data = json.loads(proc.stdout)
            initialized = data.get("initialized", False)
            status_val = "ready" if initialized else "unsupported"
            cg_payload = {
                "indexed": initialized,
                "status": status_val,
                "nodes": data.get("nodeCount"),
                "edges": data.get("edgeCount"),
                "files": data.get("fileCount"),
                "languages": data.get("languages"),
                "last_indexed": _last_indexed_iso(repo),
                "repo": repo,
            }
            svc.status_cache_put(repo, cg_payload)
            return 200, cg_payload
        return 200, _empty_panel(repo=repo, status="unsupported")
    except Exception:
        return 200, _empty_panel(repo=repo, status="unsupported")


def post_codegraph_reindex(svc: CodegraphServices) -> tuple[int, JsonPayload]:
    """POST /api/codegraph/reindex."""
    repo = svc.cfg.repo
    if not repo or not os.path.isdir(repo):
        return 400, {"error": "No open workspace"}
    # Don't stack a second indexer on top of a running one -- concurrent
    # codegraph indexers collide on the same SQLite and wedge the panel.
    if svc.index_alive():
        return 200, {"ok": True, "status": "indexing", "note": "already indexing"}
    svc.reindex_bg(repo)
    return 200, {
        "ok": True,
        "status": svc.get_status(repo),
        "reason": svc.get_reason(),
    }


def post_codegraph_apply_excludes(
    body: dict, svc: CodegraphServices
) -> tuple[int, JsonPayload]:
    """POST /api/codegraph/apply-excludes."""
    repo = svc.cfg.repo
    if not repo or not os.path.isdir(repo):
        return 400, {"error": "No open workspace"}
    from ..codegraph_preflight import (
        DEFAULT_ASSET_EXCLUDES,
        child_exclude_globs,
        merge_codegraph_excludes,
    )
    names = body.get("excludes") if isinstance(body, dict) else None
    if isinstance(names, list) and names:
        globs = []
        for n in names:
            s = str(n or "").strip()
            if not s:
                continue
            if "*" in s or "/" in s or "\\" in s:
                globs.append(s.replace("\\", "/"))
            else:
                globs.extend(child_exclude_globs([s]))
        extra = globs or None
    else:
        extra = None
    try:
        cfg = merge_codegraph_excludes(repo, extra_excludes=extra)
    except Exception as e:
        return 500, {"error": str(e)}
    svc.set_reason("Asset excludes applied; indexing source only.")
    if not svc.index_alive():
        svc.index_bg(repo)
    return 200, {
        "ok": True,
        "status": svc.get_status(repo),
        "reason": svc.get_reason(),
        "exclude_count": len(cfg.get("exclude") or []),
        "defaults": DEFAULT_ASSET_EXCLUDES[:8],
    }
