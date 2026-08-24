"""smart_approve verdict helper.

Precedence (no Guardian, no Otel):
  allowlist hit -> approve
  known amendment rewrite -> amend
  else -> pending

Surfaces as ``{action, reason}`` on pending approval payloads. Danger is never
auto-run by this helper — callers must still require a prior allowlist hit
(or one-shot approve) before executing.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from .command_allowlist import allowlist_contains
from .command_policy import CommandVerdict, classify_command, suggested_amendment

SmartVerdict = Literal["approve", "amend", "pending"]


def smart_approve(
    command: str,
    *,
    allowlist_hit: bool = False,
    verdict: Optional[CommandVerdict] = None,
    state_dir: str | None = None,
    workspace_root: str = "",
) -> dict[str, str]:
    """Return ``{action, reason}`` for a full-auto danger gate decision."""
    cmd = command or ""
    hit = allowlist_hit or (
        bool(cmd.strip())
        and allowlist_contains(
            cmd, state_dir=state_dir, workspace_root=workspace_root
        )
    )
    if hit:
        return {
            "action": "approve",
            "reason": "command matches the persistent allowlist",
        }
    amendment = suggested_amendment(cmd)
    if amendment:
        return {
            "action": "amend",
            "reason": "known safer rewrite available",
        }
    if verdict is None:
        verdict = classify_command(cmd)
    reason = (verdict.reason if verdict else "") or "requires operator approval"
    return {"action": "pending", "reason": reason}


def smart_approve_verdict(
    command: str,
    *,
    state_dir: str | None = None,
    suggested: str | None = None,
    workspace_root: str = "",
) -> SmartVerdict:
    """Return ``approve`` / ``amend`` / ``pending`` for a command string."""
    cmd = (command or "").strip()
    if not cmd:
        return "pending"
    if allowlist_contains(cmd, state_dir=state_dir, workspace_root=workspace_root):
        return "approve"
    amendment = (suggested or "").strip() or (suggested_amendment(cmd) or "")
    if amendment and amendment != cmd:
        return "amend"
    return "pending"


def as_payload(verdict: Any) -> dict[str, str]:
    """Normalize a string or dict verdict into the pending-payload shape."""
    if isinstance(verdict, dict):
        action = str(verdict.get("action") or "pending")
        reason = str(verdict.get("reason") or "")
        return {"action": action, "reason": reason}
    action = str(verdict or "pending")
    return {"action": action, "reason": ""}
