from __future__ import annotations

"""Tool side-effect taxonomy for plan/build/edit gates.

Name-family skip lists stay the declared catalog. Undeclared kinds are
mutating — the same fail-closed rule as an unset side-effect scope.
Stdlib-only. No YAML.
"""

from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet


class SideEffectScope(str, Enum):
    READ = "read"
    WRITE = "write"
    EXEC = "exec"
    NETWORK = "network"
    UNDECLARED = "undeclared"


@dataclass(frozen=True)
class ToolCapability:
    kind: str
    scope: SideEffectScope
    read_only: bool
    destructive: bool
    risk_level: int


# Keep in lockstep with send_loop_phases.PLAN_SKIP_KINDS. Tests assert both.
MUTATING_KINDS: FrozenSet[str] = frozenset({
    "run_implement", "run_parallel",
    "write_file", "edit_file", "hash_edit", "run_command",
    "run_command_batch", "run_ipython",
    "call_mcp", "manage_mcp", "memory", "cancel_job",
    "browser_navigate", "browser_snapshot", "browser_click",
    "browser_type", "browser_scroll", "browser_back",
    "browser_get_text", "browser_screenshot", "browser_auth_handoff",
    "browser_tabs", "browser_tab_activate",
    "computer_use",
    "browser_input",
})

READ_ONLY_KINDS: FrozenSet[str] = frozenset({
    "read_file", "list_dir", "search_codegraph", "search_files",
    "web_search", "web_fetch", "read_pdf", "view_image", "lsp",
    "peek_history", "peek_artifact", "job_findings",
})

_WRITE_KINDS = frozenset({
    "write_file", "edit_file", "hash_edit", "memory", "cancel_job",
})
_EXEC_KINDS = frozenset({
    "run_command", "run_command_batch", "run_ipython",
    "run_implement", "run_parallel", "computer_use",
})
_NETWORK_KINDS = frozenset({
    "call_mcp", "manage_mcp",
})
_DESTRUCTIVE_KINDS = frozenset({
    "run_command", "run_command_batch", "computer_use",
})


def capability_for(kind: str) -> ToolCapability:
    name = str(kind or "").strip()
    if not name:
        return ToolCapability("", SideEffectScope.UNDECLARED, False, True, 3)
    if name.startswith("browser_") or name in _NETWORK_KINDS:
        return ToolCapability(name, SideEffectScope.NETWORK, False, False, 2)
    if name in READ_ONLY_KINDS:
        return ToolCapability(name, SideEffectScope.READ, True, False, 0)
    if name in _WRITE_KINDS:
        return ToolCapability(name, SideEffectScope.WRITE, False, False, 2)
    if name in _EXEC_KINDS:
        destructive = name in _DESTRUCTIVE_KINDS
        return ToolCapability(name, SideEffectScope.EXEC, False, destructive, 3 if destructive else 2)
    if name in MUTATING_KINDS:
        return ToolCapability(name, SideEffectScope.WRITE, False, False, 2)
    return ToolCapability(name, SideEffectScope.UNDECLARED, False, True, 3)


def is_workspace_mutating(cap: ToolCapability) -> bool:
    """Undeclared counts as mutating. Read-only does not."""
    if cap.scope is SideEffectScope.UNDECLARED:
        return True
    if cap.read_only:
        return False
    return True


def plan_mode_blocks(kind: str) -> bool:
    """Plan mode skips mutating tools, including undeclared kinds."""
    return is_workspace_mutating(capability_for(kind))
