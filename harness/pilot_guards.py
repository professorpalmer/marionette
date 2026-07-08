from __future__ import annotations

"""Deterministic pilot behavior guards for the tool-execution layer.

Two pure, per-turn guards wired before native tool dispatch:

1. LOOP BREAKER — suppress repeated (tool, normalized-args) calls within a turn.
2. DELEGATE GATE — after too many native exploration calls without delegation,
   redirect the pilot to search_codegraph or Puppetmaster dispatch verbs.

Disable via HARNESS_LOOP_GUARD=0 / HARNESS_DELEGATE_GATE=0.
"""

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

# Thresholds (override via env for tuning in the field).
LOOP_REPEAT_CAP = int(os.environ.get("HARNESS_LOOP_REPEAT_CAP", "3"))
DELEGATE_THRESHOLD = int(os.environ.get("HARNESS_DELEGATE_THRESHOLD", "8"))

# Puppetmaster / structural tools — never blocked by the delegate gate.
DELEGATION_EXEMPT_KINDS = frozenset({
    "search_codegraph",
    "query_wiki",
    "run_swarm",
    "run_implement",
    "run_parallel",
    "route_task",
})

NATIVE_EXPLORATION_KINDS = frozenset({
    "search_files",
    "read_file",
    "list_dir",
})

_EXPLORATION_CMD_RE = re.compile(
    r"(?:^|[\s;&|])(?:"
    r"rg|ripgrep|grep|find|fd|tree|ls|dir|ack|ag|locate|where|which|"
    r"Get-ChildItem|Select-String|gci|git\s+grep"
    r")\b",
    re.IGNORECASE,
)


def loop_guard_enabled() -> bool:
    return os.environ.get("HARNESS_LOOP_GUARD", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def delegate_gate_enabled() -> bool:
    return os.environ.get("HARNESS_DELEGATE_GATE", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def guards_active() -> bool:
    return loop_guard_enabled() or delegate_gate_enabled()


@dataclass
class TurnGuardState:
    """Mutable per-turn state; reset at the start of each pilot action batch."""

    execution_counts: dict[tuple[str, str], int] = field(default_factory=dict)
    exploration_count: int = 0
    delegation_seen: bool = False


@dataclass(frozen=True)
class GuardVerdict:
    suppress: bool
    reason: str = ""
    message: str = ""


def _norm_path(path: str) -> str:
    p = (path or "").strip().replace("\\", "/")
    while "//" in p:
        p = p.replace("//", "/")
    return p.lower()


def _norm_optional_int(value: Any) -> Any:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _norm_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def normalize_action_args(kind: str, act: Any) -> str:
    """Canonical JSON key for near-duplicate detection."""
    args = getattr(act, "arguments", None) or {}
    if not isinstance(args, dict):
        args = {}

    payload: dict[str, Any] = {"kind": kind}

    if kind in ("read_file", "write_file", "edit_file", "hash_edit", "view_image", "list_dir", "open_project"):
        payload["path"] = _norm_path(getattr(act, "path", "") or "")
        if kind == "read_file":
            payload["start_line"] = _norm_optional_int(getattr(act, "start_line", None))
            payload["limit"] = _norm_optional_int(getattr(act, "limit", None))
        if kind == "edit_file":
            payload["old_str"] = _norm_whitespace(getattr(act, "old_str", "") or "")
            payload["new_str"] = _norm_whitespace(getattr(act, "new_str", "") or "")
        if kind == "hash_edit":
            ops = args.get("ops")
            payload["ops"] = ops if isinstance(ops, list) else []
    elif kind == "run_command":
        payload["command"] = _norm_whitespace(getattr(act, "command", "") or "")
    elif kind in ("search_files", "search_codegraph", "search_state", "search_tools", "web_search"):
        payload["query"] = _norm_whitespace(getattr(act, "query", "") or args.get("query", "") or "")
        if kind == "search_files":
            payload["path"] = _norm_path(args.get("path", "") or "")
            payload["max_results"] = _norm_optional_int(args.get("max_results"))
        if kind == "search_codegraph":
            payload["kind_arg"] = (args.get("kind") or "search").strip().lower()
    elif kind == "query_wiki":
        payload["question"] = _norm_whitespace(args.get("question", "") or "")
    elif kind in ("run_swarm", "run_implement"):
        payload["goal"] = _norm_whitespace(getattr(act, "goal", "") or "")
        roles = getattr(act, "roles", None) or []
        payload["roles"] = sorted(roles) if isinstance(roles, list) else []
        payload["repo"] = _norm_path(getattr(act, "repo", "") or "")
    elif kind == "run_parallel":
        goals = getattr(act, "goals", None) or []
        payload["goals"] = [_norm_whitespace(g) for g in goals] if isinstance(goals, list) else []
        payload["mode"] = (getattr(act, "mode", "") or "").strip().lower()
        payload["repo"] = _norm_path(getattr(act, "repo", "") or "")
    elif kind == "call_mcp":
        payload["tool"] = (getattr(act, "tool", "") or "").strip().lower()
        payload["arguments"] = args
    else:
        payload["arguments"] = args
        for attr in ("path", "query", "command", "goal", "url"):
            val = getattr(act, attr, None)
            if val:
                payload[attr] = _norm_whitespace(str(val)) if attr in ("query", "command", "goal") else _norm_path(str(val))

    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def is_exploration_command(command: str) -> bool:
    cmd = _norm_whitespace(command)
    if not cmd:
        return False
    return bool(_EXPLORATION_CMD_RE.search(cmd))


def is_native_exploration(kind: str, act: Any) -> bool:
    if kind in NATIVE_EXPLORATION_KINDS:
        return True
    if kind == "run_command":
        return is_exploration_command(getattr(act, "command", "") or "")
    return False


def _loop_suppress_message(kind: str, repeat_count: int) -> str:
    return (
        f"(SUPPRESSED: repeat {kind} call #{repeat_count + 1} this turn — identical or "
        f"near-identical arguments to a call already executed. Change your approach: try "
        f"search_codegraph for structure, dispatch run_swarm/run_implement for broad work, "
        f"or reformulate with different parameters. Loop guard cap={LOOP_REPEAT_CAP}.)"
    )


def _delegate_suppress_message(kind: str, exploration_count: int) -> str:
    return (
        f"(SUPPRESSED: native exploration {kind} — {exploration_count} exploration call(s) "
        f"already made this turn without delegating (threshold={DELEGATE_THRESHOLD}). "
        f"Use search_codegraph for codebase structure, or dispatch run_swarm for broad "
        f"analysis / run_implement for multi-file edits instead of more grep/read/list "
        f"sweeps.)"
    )


def check_loop_guard(state: TurnGuardState, kind: str, act: Any) -> GuardVerdict:
    if not loop_guard_enabled():
        return GuardVerdict(False)

    key = (kind, normalize_action_args(kind, act))
    prior = state.execution_counts.get(key, 0)
    if prior >= 1:
        return GuardVerdict(
            suppress=True,
            reason="loop",
            message=_loop_suppress_message(kind, prior),
        )
    return GuardVerdict(False)


def check_delegate_gate(state: TurnGuardState, kind: str, act: Any) -> GuardVerdict:
    if not delegate_gate_enabled():
        return GuardVerdict(False)

    if kind in DELEGATION_EXEMPT_KINDS:
        return GuardVerdict(False)

    if not is_native_exploration(kind, act):
        return GuardVerdict(False)

    if state.delegation_seen:
        return GuardVerdict(False)

    if state.exploration_count >= DELEGATE_THRESHOLD:
        return GuardVerdict(
            suppress=True,
            reason="delegate",
            message=_delegate_suppress_message(kind, state.exploration_count),
        )
    return GuardVerdict(False)


def check_pilot_guards(state: TurnGuardState, kind: str, act: Any) -> GuardVerdict:
    """Apply loop breaker first, then delegate gate."""
    loop_verdict = check_loop_guard(state, kind, act)
    if loop_verdict.suppress:
        return loop_verdict
    return check_delegate_gate(state, kind, act)


def record_action_execution(state: TurnGuardState, kind: str, act: Any) -> None:
    """Record a guard-eligible action that is about to execute."""
    key = (kind, normalize_action_args(kind, act))
    state.execution_counts[key] = state.execution_counts.get(key, 0) + 1

    if kind in DELEGATION_EXEMPT_KINDS:
        state.delegation_seen = True
    elif is_native_exploration(kind, act):
        state.exploration_count += 1


def new_turn_guard_state() -> TurnGuardState:
    return TurnGuardState()
