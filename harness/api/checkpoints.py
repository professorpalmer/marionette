"""Checkpoint HTTP route bodies (peeled from ``harness.server``)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional, Union


@dataclass
class CheckpointServices:
    """Explicit deps for checkpoint HTTP handlers."""

    cfg: Any
    get_active_session_id: Any  # Callable[[], str]


JsonPayload = Union[dict, list]


def _repo_or_error(svc: CheckpointServices) -> tuple[Optional[str], Optional[tuple[int, dict]]]:
    repo = svc.cfg.repo
    if not repo or not os.path.exists(repo):
        return None, (400, {"error": "No open workspace"})
    return repo, None


def post_checkpoints_restore(body: dict, svc: CheckpointServices) -> tuple[int, JsonPayload]:
    """POST /api/checkpoints/restore."""
    repo, err = _repo_or_error(svc)
    if err is not None:
        return err
    checkpoint_id = (body.get("id") or "").strip()
    if not checkpoint_id:
        return 400, {"error": "Missing checkpoint id"}
    from ..checkpoints import CheckpointStore
    active_sid = svc.get_active_session_id() or ""
    store = CheckpointStore(repo, session_id=active_sid or None)
    result = store.restore(
        checkpoint_id,
        session_id=active_sid or None,
        expected_repo=repo,
    )
    if result.get("ok"):
        return 200, result
    return 400, {"error": result.get("error", "Restore failed")}


def post_checkpoints_snapshot(body: dict, svc: CheckpointServices) -> tuple[int, JsonPayload]:
    """POST /api/checkpoints/snapshot."""
    repo, err = _repo_or_error(svc)
    if err is not None:
        return err
    label = (body.get("label") or "").strip() or "Manual checkpoint"
    from ..checkpoints import CheckpointStore
    active_sid = svc.get_active_session_id() or ""
    store = CheckpointStore(repo, session_id=active_sid or None)
    checkpoint_id = store.snapshot(
        label=label, trigger="manual", session_id=active_sid or None
    )
    if checkpoint_id:
        return 200, {"ok": True, "id": checkpoint_id}
    return 400, {"error": "Failed to create checkpoint snapshot"}


def get_checkpoints(svc: CheckpointServices) -> tuple[int, JsonPayload]:
    """GET /api/checkpoints."""
    repo = svc.cfg.repo
    if not repo or not os.path.exists(repo):
        return 200, []
    from ..checkpoints import CheckpointStore
    active_sid = svc.get_active_session_id() or ""
    store = CheckpointStore(repo, session_id=active_sid or None)
    return 200, store.list(session_id=active_sid or None)


def get_checkpoints_diff(checkpoint_id: str, svc: CheckpointServices) -> tuple[int, JsonPayload]:
    """GET /api/checkpoints/diff."""
    repo, err = _repo_or_error(svc)
    if err is not None:
        return err
    cid = (checkpoint_id or "").strip()
    if not cid:
        return 400, {"error": "Missing checkpoint id"}
    from ..checkpoints import CheckpointStore
    active_sid = svc.get_active_session_id() or ""
    store = CheckpointStore(repo, session_id=active_sid or None)
    result = store.diff(
        cid,
        session_id=active_sid or None,
        expected_repo=repo,
    )
    if result.get("ok"):
        return 200, result
    return 400, {"error": result.get("error", "Diff generation failed")}
