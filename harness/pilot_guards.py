from __future__ import annotations

"""Deterministic pilot behavior guards for the tool-execution layer.

Per-turn guards wired before native tool dispatch:

1. LOOP BREAKER — suppress repeated (tool, normalized-args) calls within a turn.
2. SWARM GATE — on broad-intent user messages, block native exploration until
   run_swarm / run_parallel / run_implement is dispatched. After dispatch,
   list_dir / search_files / exploration run_command stay blocked on broad
   turns (read_file + search_codegraph remain allowed to validate concrete
   findings); thin swarm results require re-dispatch, not an inline campaign.
3. DELEGATE GATE — after too many native exploration calls without delegation,
   redirect the pilot to search_codegraph or Puppetmaster dispatch verbs.
4. ITERATION BUDGET — hard cap on total tool calls per pilot turn.

Disable via HARNESS_LOOP_GUARD=0 / HARNESS_SWARM_GATE=0 / HARNESS_DELEGATE_GATE=0 /
HARNESS_PILOT_TOOL_BUDGET=0 / HARNESS_CLI_REDIRECT=0 (or numeric HARNESS_TURN_BUDGET >= 2
for cap override).
"""

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

# Thresholds (override via env for tuning in the field).
LOOP_REPEAT_CAP = int(os.environ.get("HARNESS_LOOP_REPEAT_CAP", "3"))
DELEGATE_THRESHOLD = int(os.environ.get("HARNESS_DELEGATE_THRESHOLD", "4"))
SWARM_GATE_READ_ALLOWANCE = int(os.environ.get("HARNESS_SWARM_GATE_READ_ALLOWANCE", "2"))
# How many full swarm-gate redirect messages to emit per turn before switching
# to a short cached replay (stops broad-intent turns burning N unique SUPPRESSED
# payloads on list_dir/search_files/grep before the model finally calls run_swarm).
SWARM_GATE_FULL_REDIRECT_CAP = int(os.environ.get("HARNESS_SWARM_GATE_FULL_REDIRECT_CAP", "1"))
TURN_TOOL_BUDGET_DEFAULT = int(os.environ.get("HARNESS_PILOT_TOOL_BUDGET", "25"))

# Puppetmaster / structural tools — never blocked by the delegate gate.
DELEGATION_EXEMPT_KINDS = frozenset({
    "search_codegraph",
    "query_wiki",
    "run_swarm",
    "run_implement",
    "run_parallel",
    "route_task",
})

SWARM_DISPATCH_KINDS = frozenset({
    "run_swarm",
    "run_implement",
    "run_parallel",
})

BROAD_SWARM_ROLES = (
    "explore",
    "pipeline-mapper",
    "decision-explainer",
    "conflict-auditor",
    "test-coverage-reviewer",
)

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

_BARE_DIR_PROBE_RE = re.compile(
    r"^(?:ls(?:\s+-1)?|dir)\s*$",
    re.IGNORECASE,
)

_ECHO_PROBE_RE = re.compile(
    r"^echo\b",
    re.IGNORECASE,
)

_PUPPETMASTER_CLI_RE = re.compile(
    r"(?:^|[\s;&|])"
    r"(?:python(?:\d+(?:\.\d+)*)?\s+-m\s+puppetmaster|puppetmaster(?:\.exe)?)"
    r"(?:\s+"
    r"(swarm|analysis|cursor|agentic|implement|edit|status|artifacts|route|should-delegate)"
    r")?\b",
    re.IGNORECASE,
)

_CLI_SWARM_SUBCMDS = frozenset({"swarm", "analysis"})
_CLI_IMPLEMENT_SUBCMDS = frozenset({"implement", "edit", "cursor", "agentic"})
_CLI_ROUTE_SUBCMDS = frozenset({"route", "should-delegate"})
_CLI_STATUS_SUBCMDS = frozenset({"status", "artifacts"})

_BROAD_INTENT_RE = re.compile(
    r"(?:"
    r"\baudit\b|"
    r"\breview\b(?:\s+(?:the|this|my|our|entire|whole|full|platform|codebase|repo|project|directory|dir|folder|module|system|app|service|quality|security|architecture))?|"
    r"look\s+through|"
    r"find\s+all\b|"
    r"find\s+out\b|"
    r"figure\s+out\b|"
    r"dig\s+into\b|"
    r"\btrace\b|"
    r"\binvestigate\b|"
    r"\bimpacting\b|"
    r"\binherit\b|"
    r"\bsubprocess\b|"
    r"how\s+does\b.{0,120}?\baffect\b|"
    r"map\s+the\b|"
    r"improve\s+quality|"
    r"what\s+could\s+break|"
    r"\bsweep\b|"
    r"refactor\s+plan|"
    r"give\s+me\s+an?\s+(?:audit|review|assessment|overview|analysis)|"
    r"comprehensive\s+(?:review|audit|analysis)|"
    r"across\s+the\s+(?:codebase|repo|project|directory)|"
    r"whole\s+(?:codebase|repo|project|directory)"
    r")",
    re.IGNORECASE,
)

_NARROW_INTENT_RE = re.compile(
    r"(?:"
    r"where\s+is\b|"
    r"what\s+(?:calls|defines|implements)\b|"
    r"how\s+does\s+\S+\s+work|"
    r"definition\s+of\b|"
    r"show\s+me\s+(?:the\s+)?(?:function|class|method|symbol)\b|"
    r"find\s+(?:the\s+)?(?:function|class|method|symbol)\b"
    r")",
    re.IGNORECASE,
)

_WINDOWS_OS_RE = re.compile(r"\bwindows\b", re.IGNORECASE)
_OTHER_OS_RE = re.compile(r"\b(?:mac|macos|linux)\b", re.IGNORECASE)


def loop_guard_enabled() -> bool:
    return os.environ.get("HARNESS_LOOP_GUARD", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def swarm_gate_enabled() -> bool:
    return os.environ.get("HARNESS_SWARM_GATE", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def delegate_gate_enabled() -> bool:
    return os.environ.get("HARNESS_DELEGATE_GATE", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def cli_redirect_enabled() -> bool:
    return os.environ.get("HARNESS_CLI_REDIRECT", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def iteration_budget_enabled() -> bool:
    return turn_tool_budget_cap() > 0


def turn_tool_budget_cap() -> int:
    pilot_raw = os.environ.get("HARNESS_PILOT_TOOL_BUDGET", "").strip()
    if pilot_raw:
        try:
            return max(0, int(pilot_raw))
        except (TypeError, ValueError):
            pass
    turn_raw = os.environ.get("HARNESS_TURN_BUDGET", "").strip()
    if turn_raw.isdigit():
        val = int(turn_raw)
        if val >= 2:
            return val
    return TURN_TOOL_BUDGET_DEFAULT


def guards_active() -> bool:
    return (
        loop_guard_enabled()
        or swarm_gate_enabled()
        or delegate_gate_enabled()
        or iteration_budget_enabled()
        or cli_redirect_enabled()
    )


@dataclass
class IterationBudget:
    """Hard cap on tool calls per pilot turn (consume/refund pattern)."""

    cap: int
    used: int = 0

    @property
    def exhausted(self) -> bool:
        return self.used >= self.cap

    @property
    def remaining(self) -> int:
        return max(0, self.cap - self.used)

    def consume(self) -> bool:
        if self.exhausted:
            return False
        self.used += 1
        return True

    def refund(self) -> None:
        if self.used > 0:
            self.used -= 1


@dataclass
class TurnGuardState:
    """Mutable per-turn state; reset at the start of each pilot action batch."""

    execution_counts: dict[tuple[str, str], int] = field(default_factory=dict)
    # Prior successful tool-result content keyed by (kind, normalized_args).
    # Used by the loop guard to replay identical calls instead of re-executing.
    successful_results: dict[tuple[str, str], str] = field(default_factory=dict)
    exploration_count: int = 0
    delegation_seen: bool = False
    user_message: str = ""
    broad_intent: bool = False
    swarm_dispatched: bool = False
    read_file_count: int = 0
    # Count of swarm-gate suppressions this turn (full redirect + short replays).
    swarm_gate_suppress_count: int = 0
    iteration_budget: IterationBudget | None = None


@dataclass(frozen=True)
class GuardVerdict:
    suppress: bool
    reason: str = ""
    message: str = ""
    # When True, ``message`` is a cached prior result to return as a successful
    # action_result (loop-guard replay) rather than an error.
    replay: bool = False


def _is_cross_platform_compare(text: str) -> bool:
    """True when a message contrasts Windows with Mac/macOS/Linux."""
    return bool(_WINDOWS_OS_RE.search(text) and _OTHER_OS_RE.search(text))


def is_broad_intent_user_message(message: str) -> bool:
    """Classify user text for broad audit/review/investigate tasks (pure function)."""
    text = _norm_whitespace(message or "")
    if not text:
        return False
    if _NARROW_INTENT_RE.search(text):
        return False
    if _BROAD_INTENT_RE.search(text):
        return True
    return _is_cross_platform_compare(text)


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
    elif kind == "relocate_session":
        payload["workspace_root"] = _norm_path(
            getattr(act, "path", "") or getattr(act, "repo", "")
            or args.get("workspace_root", "") or args.get("path", "") or ""
        )
        payload["session_id"] = (args.get("session_id") or args.get("id") or "").strip()
        payload["title"] = _norm_whitespace(args.get("title", "") or "")
    elif kind == "session_bank":
        payload["query"] = _norm_whitespace(getattr(act, "query", "") or args.get("query", "") or "")
        payload["session_id"] = (args.get("session_id") or args.get("id") or "").strip()
        payload["limit"] = _norm_optional_int(args.get("limit") if "limit" in args else getattr(act, "limit", None))
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
    if _BARE_DIR_PROBE_RE.match(cmd):
        return True
    if _ECHO_PROBE_RE.match(cmd):
        return True
    return bool(_EXPLORATION_CMD_RE.search(cmd))


def is_puppetmaster_cli_command(command: str) -> bool:
    cmd = _norm_whitespace(command)
    if not cmd:
        return False
    return bool(_PUPPETMASTER_CLI_RE.search(cmd))


def _extract_puppetmaster_subcommand(command: str) -> str:
    cmd = _norm_whitespace(command)
    match = _PUPPETMASTER_CLI_RE.search(cmd)
    if not match:
        return ""
    subcmd = (match.group(1) or "").strip().lower()
    return subcmd


def puppetmaster_cli_native_mapping(command: str) -> tuple[str, str]:
    """Return (native_kind, one-line example) for a Puppetmaster CLI command."""
    subcmd = _extract_puppetmaster_subcommand(command)
    if subcmd in _CLI_SWARM_SUBCMDS or not subcmd:
        return (
            "run_swarm",
            'goal="...", roles=["explore","pipeline-mapper"]',
        )
    if subcmd in _CLI_IMPLEMENT_SUBCMDS:
        return ("run_implement", 'goal="..."')
    if subcmd in _CLI_ROUTE_SUBCMDS:
        return ("route_task", 'instruction="..."')
    if subcmd in _CLI_STATUS_SUBCMDS:
        return ("action_result", "read prior action_result/swarm_result; or search_state")
    return (
        "run_swarm",
        'goal="...", roles=["explore","pipeline-mapper"]',
    )


def _cli_redirect_message(native_kind: str, example: str) -> str:
    if native_kind == "action_result":
        return (
            "(REDIRECT: Puppetmaster CLI status/artifacts results are ALREADY in "
            "history as action_result/swarm_result records — read those instead of "
            "run_command. Use search_state to look up durable job/artifact state.)"
        )
    return (
        f"(REDIRECT: use native {native_kind} instead of Puppetmaster CLI. "
        f"Example: {native_kind}({example}))"
    )


def is_native_exploration(kind: str, act: Any) -> bool:
    if kind in NATIVE_EXPLORATION_KINDS:
        return True
    if kind == "run_command":
        return is_exploration_command(getattr(act, "command", "") or "")
    return False


def is_swarm_gate_blocked_exploration(state: TurnGuardState, kind: str, act: Any) -> bool:
    if not state.broad_intent:
        return False

    # After a swarm/implement/parallel dispatch on a broad turn: still allow
    # search_codegraph (never gated here) and read_file to validate concrete
    # findings, but keep blocking list_dir / search_files / exploration
    # run_command so a thin swarm cannot be replaced by an inline campaign.
    if state.swarm_dispatched:
        if kind == "read_file":
            return False
        if kind in ("list_dir", "search_files"):
            return True
        if kind == "run_command" and is_exploration_command(getattr(act, "command", "") or ""):
            return True
        return False

    if kind == "read_file":
        return state.read_file_count >= SWARM_GATE_READ_ALLOWANCE
    if kind in ("list_dir", "search_files"):
        return True
    if kind == "run_command" and is_exploration_command(getattr(act, "command", "") or ""):
        return True
    return False


def _loop_suppress_message(kind: str, repeat_count: int) -> str:
    return (
        f"(SUPPRESSED: repeat {kind} call #{repeat_count + 1} this turn — identical or "
        f"near-identical arguments to a call already executed. Change your approach: try "
        f"search_codegraph for structure, dispatch run_swarm/run_implement for broad work, "
        f"or reformulate with different parameters. Loop guard cap={LOOP_REPEAT_CAP}.)"
    )


def _swarm_gate_suppress_message(kind: str, *, swarm_dispatched: bool = False) -> str:
    roles = ", ".join(BROAD_SWARM_ROLES)
    if swarm_dispatched:
        return (
            f"(SUPPRESSED: native exploration {kind} — a swarm already ran this turn and "
            f"broad list_dir/search_files/grep sweeps stay blocked. If swarm findings were "
            f"empty, vague, verification-only, or insufficient for the user's ask, "
            f"re-dispatch a narrowed run_swarm (or run_parallel analysis roles) with a "
            f"sharper objective. search_codegraph and read_file of paths cited in swarm "
            f"findings remain allowed to validate concrete findings — do NOT substitute "
            f"an inline exploration campaign.)"
        )
    return (
        f"(SUPPRESSED: native exploration {kind} — this turn's user message is a broad "
        f"audit/review/sweep task and you have not dispatched run_swarm/run_parallel/"
        f"run_implement yet. STOP exploring. Your ONLY allowed next tools are "
        f"run_swarm, run_implement, or run_parallel (search_codegraph remains available "
        f"for narrow symbol lookups). Dispatch run_swarm with MULTIPLE roles "
        f"({roles}) and auto-routed models so parallel workers map the space. The "
        f"durable artifact store makes every swarm cheaper on follow-up turns "
        f"(artifact recall is zero-token). After dispatch, search_codegraph and "
        f"read_file of paths cited in findings remain allowed to validate concrete "
        f"findings; list_dir/search_files/grep sweeps stay blocked. If findings are "
        f"shallow or empty, re-dispatch a narrowed swarm — never substitute an inline "
        f"exploration campaign.)"
    )


def _swarm_gate_replay_message(kind: str, *, swarm_dispatched: bool = False) -> str:
    """Short cached redirect after the first full swarm-gate suppress this turn."""
    if swarm_dispatched:
        return (
            f"[swarm_gate redirect already issued this turn — stop broad native "
            f"exploration ({kind}). If findings were thin, re-dispatch a narrowed "
            f"run_swarm/run_parallel. search_codegraph and read_file of cited paths "
            f"remain allowed.]"
        )
    return (
        f"[swarm_gate redirect already issued this turn — stop native exploration "
        f"({kind}). Call run_swarm, run_implement, or run_parallel now. "
        f"search_codegraph remains allowed for narrow symbols.]"
    )


def _delegate_suppress_message(kind: str, exploration_count: int) -> str:
    return (
        f"(SUPPRESSED: native exploration {kind} — {exploration_count} exploration call(s) "
        f"already made this turn without delegating (threshold={DELEGATE_THRESHOLD}). "
        f"Use search_codegraph for codebase structure, or dispatch run_swarm for broad "
        f"analysis / run_implement for multi-file edits instead of more grep/read/list "
        f"sweeps.)"
    )


def _iteration_budget_suppress_message(cap: int) -> str:
    return (
        f"(SUPPRESSED: per-turn tool-call budget exhausted ({cap}/{cap} calls used). "
        f"Summarize findings for the user and/or dispatch background workers "
        f"(run_swarm/run_implement/run_parallel) instead of more inline tool calls.)"
    )


def check_cli_redirect(state: TurnGuardState, kind: str, act: Any) -> GuardVerdict:
    del state
    if not cli_redirect_enabled():
        return GuardVerdict(False)
    if kind != "run_command":
        return GuardVerdict(False)

    command = getattr(act, "command", "") or ""
    if not is_puppetmaster_cli_command(command):
        return GuardVerdict(False)

    native_kind, example = puppetmaster_cli_native_mapping(command)
    return GuardVerdict(
        suppress=True,
        reason="cli_redirect",
        message=_cli_redirect_message(native_kind, example),
    )


def check_loop_guard(state: TurnGuardState, kind: str, act: Any) -> GuardVerdict:
    if not loop_guard_enabled():
        return GuardVerdict(False)

    key = (kind, normalize_action_args(kind, act))
    prior = state.execution_counts.get(key, 0)
    if prior < 1:
        return GuardVerdict(False)

    # LOOP_REPEAT_CAP bounds how many times the same (kind, args) may run this
    # turn (1 original + up to CAP-1 cached replays). The (CAP+1)th identical
    # call hard-suppresses with the existing error -- so the cap finally means
    # something (previously every repeat was suppressed immediately).
    if prior >= LOOP_REPEAT_CAP:
        return GuardVerdict(
            suppress=True,
            reason="loop",
            message=_loop_suppress_message(kind, prior),
        )

    cached = state.successful_results.get(key)
    if cached is not None:
        return GuardVerdict(
            suppress=True,
            reason="loop_replay",
            message=f"[cached repeat of identical call]\n{cached}",
            replay=True,
        )

    # Identical call but no successful prior result to replay -- hard suppress.
    return GuardVerdict(
        suppress=True,
        reason="loop",
        message=_loop_suppress_message(kind, prior),
    )


def record_successful_result(state: TurnGuardState, kind: str, act: Any, content: str) -> None:
    """Store a successful tool result for loop-guard replay within this turn."""
    try:
        key = (kind, normalize_action_args(kind, act))
        state.successful_results[key] = content or ""
    except Exception:
        pass


def check_swarm_gate(state: TurnGuardState, kind: str, act: Any) -> GuardVerdict:
    if not swarm_gate_enabled():
        return GuardVerdict(False)

    if not is_swarm_gate_blocked_exploration(state, kind, act):
        return GuardVerdict(False)

    prior = state.swarm_gate_suppress_count
    state.swarm_gate_suppress_count = prior + 1
    dispatched = bool(state.swarm_dispatched)

    # First suppression(s) this turn get the full redirect so the model sees a
    # clear "stop exploring, call run_swarm now" signal. Further identical-class
    # suppressions reuse a short cached replay (loop-guard style) so broad-intent
    # turns cannot burn many unique SUPPRESSED payloads before dispatch.
    if prior < SWARM_GATE_FULL_REDIRECT_CAP:
        return GuardVerdict(
            suppress=True,
            reason="swarm_gate",
            message=_swarm_gate_suppress_message(kind, swarm_dispatched=dispatched),
        )

    return GuardVerdict(
        suppress=True,
        reason="swarm_gate_replay",
        message=_swarm_gate_replay_message(kind, swarm_dispatched=dispatched),
        replay=True,
    )


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


def check_iteration_budget(state: TurnGuardState, kind: str, act: Any) -> GuardVerdict:
    del kind, act
    if not iteration_budget_enabled():
        return GuardVerdict(False)

    budget = state.iteration_budget
    if budget is None or not budget.exhausted:
        return GuardVerdict(False)

    return GuardVerdict(
        suppress=True,
        reason="budget",
        message=_iteration_budget_suppress_message(budget.cap),
    )


def check_pilot_guards(state: TurnGuardState, kind: str, act: Any) -> GuardVerdict:
    """Apply CLI redirect, loop breaker, swarm gate, delegate gate, then budget."""
    cli_verdict = check_cli_redirect(state, kind, act)
    if cli_verdict.suppress:
        return cli_verdict

    loop_verdict = check_loop_guard(state, kind, act)
    if loop_verdict.suppress:
        return loop_verdict

    swarm_verdict = check_swarm_gate(state, kind, act)
    if swarm_verdict.suppress:
        return swarm_verdict

    delegate_verdict = check_delegate_gate(state, kind, act)
    if delegate_verdict.suppress:
        return delegate_verdict

    return check_iteration_budget(state, kind, act)


def record_action_execution(state: TurnGuardState, kind: str, act: Any) -> None:
    """Record a guard-eligible action that is about to execute."""
    key = (kind, normalize_action_args(kind, act))
    state.execution_counts[key] = state.execution_counts.get(key, 0) + 1

    if kind in SWARM_DISPATCH_KINDS:
        state.swarm_dispatched = True

    if kind in DELEGATION_EXEMPT_KINDS:
        state.delegation_seen = True
    elif is_native_exploration(kind, act):
        state.exploration_count += 1

    if kind == "read_file":
        state.read_file_count += 1

    if state.iteration_budget is not None:
        state.iteration_budget.consume()


def new_turn_guard_state(user_message: str = "") -> TurnGuardState:
    cap = turn_tool_budget_cap()
    return TurnGuardState(
        user_message=user_message or "",
        broad_intent=is_broad_intent_user_message(user_message or ""),
        iteration_budget=IterationBudget(cap) if cap > 0 else None,
    )
