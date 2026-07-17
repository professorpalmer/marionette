"""Codegraph admin HTTP route bodies (peeled from ``harness.server``).

GET ``/api/codegraph`` (large status panel) stays on Handler for now; this
module owns the POST reindex / apply-excludes cluster.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Optional, Union


@dataclass
class CodegraphServices:
    """Explicit deps for codegraph POST handlers."""

    cfg: Any
    index_alive: Callable[[], bool]
    reindex_bg: Callable[[str], None]
    index_bg: Callable[[str], None]
    get_status: Callable[[str], str]
    get_reason: Callable[[], Optional[str]]
    set_reason: Callable[[str], None]


JsonPayload = Union[dict, list]


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
