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


def get_checkpoints_hunks(svc: CheckpointServices) -> tuple[int, JsonPayload]:
    """GET /api/checkpoints/hunks — live hunks with Agent vs External attribution."""
    repo, err = _repo_or_error(svc)
    if err is not None:
        return err
    from ..checkpoints import CheckpointStore
    active_sid = svc.get_active_session_id() or ""
    store = CheckpointStore(repo, session_id=active_sid or None)
    result = store.hunk_tracker(session_id=active_sid or None).recompute()
    if result.get("ok"):
        return 200, result
    return 400, {"error": result.get("error", "Hunk recompute failed")}


def post_checkpoints_hunks_accept(body: dict, svc: CheckpointServices) -> tuple[int, JsonPayload]:
    """POST /api/checkpoints/hunks/accept."""
    repo, err = _repo_or_error(svc)
    if err is not None:
        return err
    hunk_id = (body.get("hunk_id") or body.get("id") or "").strip()
    if not hunk_id:
        return 400, {"error": "Missing hunk id"}
    from ..checkpoints import CheckpointStore
    active_sid = svc.get_active_session_id() or ""
    store = CheckpointStore(repo, session_id=active_sid or None)
    result = store.hunk_tracker(session_id=active_sid or None).accept_hunk(hunk_id)
    if result.get("ok"):
        return 200, result
    return 400, {"error": result.get("error", "Accept failed")}


def post_checkpoints_hunks_revert(body: dict, svc: CheckpointServices) -> tuple[int, JsonPayload]:
    """POST /api/checkpoints/hunks/revert."""
    repo, err = _repo_or_error(svc)
    if err is not None:
        return err
    hunk_id = (body.get("hunk_id") or body.get("id") or "").strip()
    if not hunk_id:
        return 400, {"error": "Missing hunk id"}
    from ..checkpoints import CheckpointStore
    active_sid = svc.get_active_session_id() or ""
    store = CheckpointStore(repo, session_id=active_sid or None)
    result = store.hunk_tracker(session_id=active_sid or None).revert_hunk(hunk_id)
    if result.get("ok"):
        return 200, result
    return 400, {"error": result.get("error", "Revert failed")}
