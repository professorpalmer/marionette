"""Session- and workspace-scoped full-auto command approval routes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Union


@dataclass
class CommandApprovalServices:
    """Explicit dependencies for command approval HTTP handlers."""

    get_runners: Callable[[], Any]


JsonPayload = Union[dict, list]
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _resolve_runner(
    body: dict,
    svc: CommandApprovalServices,
) -> tuple[int, JsonPayload] | tuple[None, Any]:
    session_id = str(body.get("session_id") or "").strip()
    workspace_root = str(body.get("workspace_root") or "").strip()
    command_hash = str(body.get("command_hash") or "").strip().lower()
    if not session_id:
        return 400, {"error": "session_id is required"}
    if not workspace_root:
        return 400, {"error": "workspace_root is required"}
    if not _SHA256_HEX.fullmatch(command_hash):
        return 400, {"error": "command_hash must be a SHA-256 hex digest"}

    runner = svc.get_runners().get(session_id)
    if runner is None:
        return 404, {"error": "session runner not found"}
    if getattr(runner, "harness_session_id", "") != session_id:
        return 409, {"error": "session runner scope mismatch"}
    return None, (runner, session_id, workspace_root, command_hash)


def _decide(
    body: dict,
    svc: CommandApprovalServices,
    *,
    approve: bool,
) -> tuple[int, JsonPayload]:
    resolved = _resolve_runner(body, svc)
    if resolved[0] is not None:
        return resolved  # type: ignore[return-value]
    runner, session_id, workspace_root, command_hash = resolved[1]

    decide = getattr(runner, "decide_command_approval", None)
    if decide is None:
        return 409, {"error": "session runner cannot accept command approvals"}

    from ..approval_identity import approval_turn

    turn_raw = body.get("turn_id")
    try:
        with approval_turn(turn_raw):
            pending = decide(
                command_hash=command_hash,
                workspace_root=workspace_root,
                approve=approve,
            )
    except PermissionError as exc:
        return 403, {"error": str(exc)}
    if pending is None:
        return 404, {"error": "pending command approval not found"}

    payload: dict[str, Any] = {
        "ok": True,
        "decision": "approved" if approve else "rejected",
        "session_id": session_id,
        "workspace_root": pending["workspace_root"],
        "command_hash": command_hash,
        # Returned only from the server-side pending record. The client uses this
        # exact text to request a full-auto retry; a changed danger command has a
        # different hash and remains blocked.
        "retry_command": pending["command"] if approve else "",
    }
    if pending.get("turn_id"):
        payload["turn_id"] = pending["turn_id"]
    return 200, payload


def post_command_approval(
    body: dict, svc: CommandApprovalServices
) -> tuple[int, JsonPayload]:
    """POST /api/commands/approve."""
    return _decide(body, svc, approve=True)


def post_command_rejection(
    body: dict, svc: CommandApprovalServices
) -> tuple[int, JsonPayload]:
    """POST /api/commands/reject."""
    return _decide(body, svc, approve=False)


def post_command_approval_amendment(
    body: dict, svc: CommandApprovalServices
) -> tuple[int, JsonPayload]:
    """POST /api/commands/approve-amendment.

    Consumes the original pending hash and registers a one-shot approval for
    ``sha256(suggested_amendment)``. Returns the amendment as ``retry_command``
    plus the new ``command_hash``.
    """
    resolved = _resolve_runner(body, svc)
    if resolved[0] is not None:
        return resolved  # type: ignore[return-value]
    runner, session_id, workspace_root, command_hash = resolved[1]

    decide = getattr(runner, "decide_command_approval_amendment", None)
    if decide is None:
        return 409, {"error": "session runner cannot accept command amendments"}

    from ..approval_identity import approval_turn

    try:
        with approval_turn(body.get("turn_id")):
            pending = decide(
                command_hash=command_hash,
                workspace_root=workspace_root,
            )
    except PermissionError as exc:
        return 403, {"error": str(exc)}
    except ValueError as exc:
        return 400, {"error": str(exc)}
    if pending is None:
        return 404, {"error": "pending command approval not found"}

    return 200, {
        "ok": True,
        "decision": "approved_amendment",
        "session_id": session_id,
        "workspace_root": pending["workspace_root"],
        "command_hash": pending["command_hash"],
        "original_command_hash": pending.get("original_command_hash") or command_hash,
        "retry_command": pending["command"],
    }
