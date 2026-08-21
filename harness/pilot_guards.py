from __future__ import annotations

"""Deterministic pilot behavior guards for the tool-execution layer.

Per-turn guards wired before native tool dispatch:

1. LOOP BREAKER — suppress repeated (tool, normalized-args) calls within a turn.
2. SWARM GATE — on broad-intent user messages, block native exploration until
   run_swarm / run_parallel / run_implement is dispatched. Explicit "swarm" /
   "multi-worker" asks also block git, launch scripts, and run_implement until
   run_swarm / run_parallel actually fire. After a healthy
   dispatch, list_dir / search_files / exploration run_command stay blocked on
   broad turns (read_file + search_codegraph remain allowed to validate
   concrete findings); thin swarm results require re-dispatch, not an inline
   campaign. After a kernel failure (HTTP 400 / preflight / PM unavailable)
   the gate lifts so the pilot can diagnose locally instead of deadlocking.
3. DELEGATE GATE — after too many native exploration calls without delegation,
   redirect the pilot to search_codegraph or Puppetmaster dispatch verbs.
   Clipboard / local handoff commands are never exploration. Kernel recovery
   also lifts this gate.
4. ITERATION BUDGET — hard cap on total tool calls per pilot turn.

``TurnGuardState`` persists across model steps and keep-alive resume for the
same originating user turn (cleared on a fresh user message). Companion helpers
here also power the send-loop stagnation governor, failed-objective resume
caps, read-only audit goal detection for bare ``run_implement``, and the
substantive-analysis gate that keeps plumbing-only jobs from rendering green.

Disable via HARNESS_LOOP_GUARD=0 / HARNESS_SWARM_GATE=0 / HARNESS_DELEGATE_GATE=0 /
HARNESS_PILOT_TOOL_BUDGET=0 / HARNESS_CLI_REDIRECT=0 /
HARNESS_ALLOW_MID_TURN_RESTART=1 (opt-in; mid-turn /api/restart is blocked by
default) (or numeric HARNESS_TURN_BUDGET >= 2 for cap override).
Tune HARNESS_STAGNATION_STREAK_CAP / HARNESS_FAILED_OBJECTIVE_RESUME_CAP as needed.
Tiny workspaces tighten the tool cap via HARNESS_TINY_WORKSPACE_TOOL_BUDGET
(default 12) for the foreground pilot only. Nested native implement workers
skip that tiny tighten and instead use an edit-first gate
(HARNESS_EDIT_FIRST_READ_ALLOWANCE, default 2). After a successful implement,
remaining tools clamp to HARNESS_POST_IMPLEMENT_TOOL_ALLOWANCE (default 4).
"""

import json
import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Optional

# Thresholds (override via env for tuning in the field).
LOOP_REPEAT_CAP = int(os.environ.get("HARNESS_LOOP_REPEAT_CAP", "3"))
DELEGATE_THRESHOLD = int(os.environ.get("HARNESS_DELEGATE_THRESHOLD", "4"))
SWARM_GATE_READ_ALLOWANCE = int(os.environ.get("HARNESS_SWARM_GATE_READ_ALLOWANCE", "2"))
# How many full swarm-gate redirect messages to emit per turn before switching
# to a short cached replay (stops broad-intent turns burning N unique SUPPRESSED
# payloads on list_dir/search_files/grep before the model finally calls run_swarm).
SWARM_GATE_FULL_REDIRECT_CAP = int(os.environ.get("HARNESS_SWARM_GATE_FULL_REDIRECT_CAP", "1"))
# Literal default — never bake HARNESS_PILOT_TOOL_BUDGET at import time.
# Ambient Marionette Settings (budget=0) would otherwise poison pytest hermeticity
# after conftest delenv; read the env only inside _explicit_or_default_tool_budget.
TURN_TOOL_BUDGET_DEFAULT = 25
# Tiny-workspace tool budget (scale-aware tighten only; never raises explicit cap).
# Applies to the *foreground* pilot only — nested implement workers use an
# edit-first policy instead so a tiny repo cannot burn the whole cap exploring.
TINY_WORKSPACE_TOOL_BUDGET_DEFAULT = 12
TINY_WORKSPACE_SOURCE_FILE_CAP = 15
TINY_WORKSPACE_LOC_CAP = 5000
# Residual tools allowed after a successful implement lands (never raises cap).
POST_IMPLEMENT_TOOL_ALLOWANCE_DEFAULT = 4
# Nested implement workers may read this many target files before an edit is
# required; broader exploration (list/search/ipython) is blocked until then.
EDIT_FIRST_READ_ALLOWANCE_DEFAULT = 2
# Tool kinds that satisfy the nested-implement "required write" gate.
EDIT_FIRST_WRITE_KINDS = frozenset({
    "edit_file",
    "write_file",
    "hash_edit",
})
# Broad exploration that must not consume a nested implement budget before a write.
EDIT_FIRST_BLOCKED_KINDS = frozenset({
    "list_dir",
    "search_files",
    "run_ipython",
    "search_codegraph",
})
# How many consecutive identical (normalized prose + action fingerprint) steps
# may run before the send-loop stagnation governor ends the turn calmly.
STAGNATION_STREAK_CAP = int(os.environ.get("HARNESS_STAGNATION_STREAK_CAP", "3"))
# How many keep-alive resume chains are allowed for the same normalized
# failed/degraded objective before further pilot_resume events are suppressed.
FAILED_OBJECTIVE_RESUME_CAP = int(os.environ.get("HARNESS_FAILED_OBJECTIVE_RESUME_CAP", "2"))

# Puppetmaster / structural tools — never blocked by the delegate gate.
# search_state is exempt so durable recall (job:// / artifact:// / spill://)
# stays available before a broad redispatch without counting as exploration.
DELEGATION_EXEMPT_KINDS = frozenset({
    "search_codegraph",
    "search_state",
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

# First-argv exploration only. Searching the whole command string falsely
# classified `cat <<EOF` / `python …` writes as exploration whenever the
# body mentioned find/which/where/ls.
_EXPLORATION_COMMANDS = frozenset({
    "rg",
    "ripgrep",
    "grep",
    "find",
    "fd",
    "tree",
    "ls",
    "dir",
    "ack",
    "ag",
    "locate",
    "get-childitem",
    "select-string",
    "gci",
})
_LEADING_SEGMENT_RE = re.compile(r"[;&|\n]")
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

_BARE_DIR_PROBE_RE = re.compile(
    r"^(?:ls(?:\s+-1)?|dir)\s*$",
    re.IGNORECASE,
)

# Local clipboard / handoff sinks — not codebase exploration. ``echo hello``
# stays an exploration probe; ``echo … | pbcopy`` is a handoff.
_CLIPBOARD_SINK_RE = re.compile(
    r"(?:"
    r"\bpbcopy\b|\bpbpaste\b|"
    r"\bclip\.exe\b|(?:^|[\s|&;])clip(?:\s|$)|"
    r"\bwl-copy\b|\bwl-paste\b|"
    r"\bxclip\b|\bxsel\b|"
    r"Set-Clipboard|Get-Clipboard"
    r")",
    re.IGNORECASE,
)

# Failed-before-start / kernel-unavailable markers. Tight on purpose: a
# plumbing-only swarm is not kernel recovery (that stays on the thrash path).
_KERNEL_FAILURE_RE = re.compile(
    r"(?:"
    r"HTTP\s+400|"
    r"reasoning_effort|"
    r"Function tools with reasoning|"
    r"Use /v1/responses|"
    r"preflight[_ ](?:blocked|failed)|"
    r"puppetmaster (?:is )?(?:unavailable|not (?:installed|available)|broken)|"
    r"failed before start|"
    r"No module named ['\"]puppetmaster|"
    r"kernel (?:unavailable|offline)"
    r")",
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
_CLI_LAUNCH_SUBCMDS = _CLI_SWARM_SUBCMDS | _CLI_IMPLEMENT_SUBCMDS
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

_EXPLICIT_SWARM_RE = re.compile(
    r"(?:"
    r"\brun_swarm\b|"
    r"\bpuppetmaster\s+(?:swarm|agentic)\b|"
    r"\b(?:start|spin\s+up|launch|dispatch|run|do)\s+"
    r"(?:(?:an?\s+)?(?:\w+\s+){0,3})?swarm\b|"
    r"\b(?:please\s+)?swarm\s+(?:the\s+)?(?:repo|codebase|project|code)\b|"
    r"\bswarm\s+(?:glm|kimi|qwen|gpt|deepseek|claude|via|with|using|multi)|"
    r"\bagentic\s+swarm\b|"
    r"\b(?:spin\s+up|launch|start|run)\s+(?:an?\s+)?agentic\s+workers\b|"
    r"\bagentic\s+workers\b|"
    r"\bmulti[- ]workers?\b|"
    r"\bfan-?out\b|"
    # Model/provider/multi-qualified "use … workers" only — not
    # "use the existing workers" / singular worker / discussion fillers.
    r"\buse\s+(?:"
    r"(?:the|a|an|this|that|these|those|our|your|their|my|"
    r"existing|current|available|same|other|more|some|all|both|"
    r"new|old|local|just|only|any)\s+"
    r")*"
    r"(?!(?:the|a|an|this|that|these|those|our|your|their|my|"
    r"existing|current|available|same|other|more|some|all|both|"
    r"new|old|local|just|only|any)\b)"
    r"\S+(?:\s+\S+){0,7}\s+workers\b"
    r")",
    re.IGNORECASE,
)

_EXPLICIT_SWARM_NEG_RE = re.compile(
    r"(?:"
    r"don'?t\s+swarm|"
    r"do\s+not\s+swarm|"
    r"swarm\s+tracker|"
    r"where\s+is\s+run_swarm|"
    r"what\s+is\s+(?:an?\s+)?(?:agentic\s+)?swarm|"
    r"use\s+(?:just\s+|only\s+)?one\s+worker"
    r")",
    re.IGNORECASE,
)

# Continuation of a prior explicit swarm after a provider error / interstitial.
# Tight on purpose: "test" and new work must not reactivate the mandate.
# "pick back up" / "pickback up" stay a strong substring (screenshot phrasing).
# "continue" / "resume" match only when the whole message is a short
# continuation, not "continue implementing …" / "resume parsing …".
_SWARM_PICKBACK_RE = re.compile(
    r"\bpick\s*back\s*up\b",
    re.IGNORECASE,
)

_SWARM_SHORT_CONTINUE_RE = re.compile(
    r"^(?:please\s+)?"
    r"(?:"
    r"continue(?:\s+(?:it|the\s+swarm|where\s+you\s+left\s+off))?"
    r"|"
    r"resume(?:\s+(?:it|the\s+swarm|where\s+you\s+left\s+off))?"
    r")"
    r"(?:\s*,?\s*please)?"
    r"[.!]?"
    r"$",
    re.IGNORECASE,
)

# Bounds one simple command. Used to slice a single Puppetmaster invocation
# out of a wrapper (cd … && puppetmaster … | tail) without running any shell.
_CLI_SHELL_OPERATORS = frozenset({
    "&&", "||", ";", "|",
    ">", "<", ">>", "<<", ">&", "&>", ">|",
})

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


def _explicit_or_default_tool_budget() -> int:
    """Absolute ceiling from env (or default 25). Does not apply tiny tighten."""
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


def tiny_workspace_tool_budget() -> int:
    """Max tools for a classified-tiny workspace (default 12)."""
    raw = os.environ.get("HARNESS_TINY_WORKSPACE_TOOL_BUDGET", "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            pass
    return TINY_WORKSPACE_TOOL_BUDGET_DEFAULT


def post_implement_tool_allowance() -> int:
    """Residual tool calls after a successful implement (default 4)."""
    raw = os.environ.get("HARNESS_POST_IMPLEMENT_TOOL_ALLOWANCE", "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            pass
    return POST_IMPLEMENT_TOOL_ALLOWANCE_DEFAULT


def edit_first_read_allowance() -> int:
    """Target-file reads allowed before a nested implement must write."""
    raw = os.environ.get("HARNESS_EDIT_FIRST_READ_ALLOWANCE", "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            pass
    return EDIT_FIRST_READ_ALLOWANCE_DEFAULT


def turn_tool_budget_cap(
    repo_path: Optional[str] = None,
    *,
    nested_implement: bool = False,
) -> int:
    """Effective per-turn tool cap.

    Explicit ``HARNESS_PILOT_TOOL_BUDGET`` / ``HARNESS_TURN_BUDGET`` is the
    absolute ceiling. A tiny workspace may only *tighten* that ceiling via
    ``HARNESS_TINY_WORKSPACE_TOOL_BUDGET`` (default 12) for the foreground
    pilot. Nested native implement workers skip the tiny tighten so they keep
    an edit-first bounded policy instead of burning a 12-call thrash budget.
    """
    base = _explicit_or_default_tool_budget()
    if base <= 0:
        return base
    if nested_implement:
        return base
    if repo_path and is_tiny_workspace(repo_path):
        return min(base, tiny_workspace_tool_budget())
    return base


def guards_active() -> bool:
    return (
        loop_guard_enabled()
        or swarm_gate_enabled()
        or delegate_gate_enabled()
        or iteration_budget_enabled()
        or cli_redirect_enabled()
    )


# Directories skipped while classifying workspace scale (build/cache/vendor).
_TINY_SKIP_DIR_NAMES = frozenset({
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".cache",
    "cache",
    "coverage",
    "dist",
    "build",
    "release",
    "releases",
    "target",
    ".next",
    ".nuxt",
    ".turbo",
    "vendor",
    ".codegraph",
    "eggs",
    ".eggs",
})

_TINY_BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".svg",
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".mp4", ".wav", ".webm", ".mov",
    ".exe", ".dll", ".so", ".dylib", ".o", ".a", ".pyc", ".pyo",
    ".class", ".jar", ".wasm", ".bin", ".dat", ".db", ".sqlite",
})

# Text-ish source extensions counted toward tiny-workspace scale.
_TINY_SOURCE_EXTENSIONS = frozenset({
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".html", ".htm", ".css", ".scss", ".sass", ".less",
    ".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".md", ".mdx", ".rst", ".txt",
    ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd",
    ".go", ".rs", ".java", ".kt", ".c", ".h", ".cc", ".cpp", ".hpp",
    ".rb", ".php", ".swift", ".cs", ".vue", ".svelte",
    ".sql", ".graphql", ".proto",
})


def _env_int_cap(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return default


def workspace_source_stats(repo_path: str) -> tuple[int, int]:
    """Count source files and LOC under ``repo_path`` (cheap, deterministic).

    Ignores ``.git``, ``node_modules``, build/release/cache dirs, and binary
    files. Returns ``(source_file_count, loc)``. Missing/unreadable roots
    yield ``(0, 0)``.
    """
    root = (repo_path or "").strip()
    if not root or not os.path.isdir(root):
        return 0, 0

    source_files = 0
    loc = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d not in _TINY_SKIP_DIR_NAMES
                and not d.endswith(".egg-info")
                and not d.startswith(".egg")
            ]
            for name in filenames:
                path = os.path.join(dirpath, name)
                ext = os.path.splitext(name)[1].lower()
                if ext in _TINY_BINARY_EXTENSIONS:
                    continue
                if ext and ext not in _TINY_SOURCE_EXTENSIONS:
                    # Unknown extension: only count if it looks like text.
                    if not _file_looks_like_text(path):
                        continue
                elif not ext:
                    if not _file_looks_like_text(path):
                        continue
                try:
                    with open(path, "rb") as fh:
                        raw = fh.read()
                except OSError:
                    continue
                if b"\x00" in raw[:8192]:
                    continue
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    try:
                        text = raw.decode("latin-1")
                    except Exception:
                        continue
                source_files += 1
                loc += text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    except OSError:
        return source_files, loc
    return source_files, loc


def _file_looks_like_text(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            sample = fh.read(8192)
    except OSError:
        return False
    if not sample:
        return True
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def is_tiny_workspace(repo_path: str) -> bool:
    """True when the repo is small enough to warrant a tighter tool budget.

    Defaults: at most 15 source files AND at most 5,000 LOC. Thresholds are
    overridable via ``HARNESS_TINY_WORKSPACE_SOURCE_FILES`` /
    ``HARNESS_TINY_WORKSPACE_LOC`` for field tuning.
    """
    if not repo_path or not os.path.isdir(repo_path):
        return False
    files, loc = workspace_source_stats(repo_path)
    file_cap = _env_int_cap(
        "HARNESS_TINY_WORKSPACE_SOURCE_FILES", TINY_WORKSPACE_SOURCE_FILE_CAP,
    )
    loc_cap = _env_int_cap("HARNESS_TINY_WORKSPACE_LOC", TINY_WORKSPACE_LOC_CAP)
    if file_cap <= 0 or loc_cap <= 0:
        return False
    return files <= file_cap and loc <= loc_cap


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
    """Mutable per-originating-user-turn state.

    Persists across model steps and keep-alive resume calls for the same
    originating user turn. Fresh user messages clear ``session._turn_guard_state``
    so the next action batch allocates a new instance.
    """

    execution_counts: dict[tuple[str, str], int] = field(default_factory=dict)
    # Prior successful tool-result content keyed by (kind, normalized_args).
    # Used by the loop guard to replay identical calls instead of re-executing.
    successful_results: dict[tuple[str, str], str] = field(default_factory=dict)
    exploration_count: int = 0
    delegation_seen: bool = False
    user_message: str = ""
    broad_intent: bool = False
    # User named a swarm (run_swarm / puppetmaster swarm / multi-workers).
    # Stronger than broad_intent: git/scripts/run_implement stay blocked
    # until run_swarm / run_parallel actually dispatch.
    explicit_swarm: bool = False
    swarm_dispatched: bool = False
    read_file_count: int = 0
    # Count of swarm-gate suppressions this turn (full redirect + short replays).
    swarm_gate_suppress_count: int = 0
    iteration_budget: IterationBudget | None = None
    # Set when empty managed implement recovery already ran against dirty live checkout.
    last_implement_exhausted: bool = False
    # Set when a completed implement/local job returned real success/patch provenance.
    implement_success_seen: bool = False
    # Cached at turn start from the effective repo path (scale-aware budget / chrome guard).
    tiny_workspace: bool = False
    # Nested native implement worker (ProviderWorker expects_diff): edit-first policy.
    nested_implement: bool = False
    # True after edit_file / write_file / hash_edit on a nested implement turn.
    edit_seen: bool = False
    # (objective_key, model_key) pairs for plumbing-only / no-FINDING swarms.
    # Identical redispatch soft-refuses; changing model pin or goal is allowed.
    plumbing_degraded_swarms: set[tuple[str, str]] = field(default_factory=set)
    # Adaptive task depth for this turn (MICRO / STANDARD / DEEP). Separate from
    # tiny_workspace (repo scale). MICRO disables swarm-gate suppress only.
    task_profile: str = ""
    # Set when a swarm/implement/parallel attempt failed before the kernel
    # produced usable work (HTTP 400, preflight, PM unavailable). Lifts
    # swarm/delegate exploration gates so the pilot can diagnose locally.
    # Loop/thrash still block identical redispatch.
    kernel_recovery: bool = False


@dataclass
class PendingSwarmMandate:
    """Session-level explicit swarm obligation (survives interstitial turns)."""

    goal: str = ""
    model: str = ""


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


def is_explicit_swarm_user_message(message: str) -> bool:
    """True when the user asked to launch a swarm, not merely mentioned one."""
    text = _norm_whitespace(message or "")
    if not text:
        return False
    if _NARROW_INTENT_RE.search(text):
        return False
    if _EXPLICIT_SWARM_NEG_RE.search(text):
        return False
    return bool(_EXPLICIT_SWARM_RE.search(text))


def is_swarm_continuation_user_message(message: str) -> bool:
    """True for clear resume/continue/pick-back-up phrases only."""
    text = _norm_whitespace(message or "")
    if not text:
        return False
    if _SWARM_PICKBACK_RE.search(text):
        return True
    return bool(_SWARM_SHORT_CONTINUE_RE.fullmatch(text))


def apply_session_pending_swarm_mandate(session: Any, user_message: str) -> bool:
    """Capture, reactivate, or leave dormant a session pending swarm mandate.

    Returns True when this originating turn should treat explicit swarm as
    active. Unrelated turns such as ``test`` leave a stored mandate in place
    but do not activate it.
    """
    if session is None:
        return False
    text = user_message or ""
    if is_explicit_swarm_user_message(text):
        prior = getattr(session, "_pending_swarm_mandate", None)
        prior_model = getattr(prior, "model", "") if prior is not None else ""
        session._pending_swarm_mandate = PendingSwarmMandate(
            goal=_norm_whitespace(text),
            model=str(prior_model or ""),
        )
        session._pending_swarm_active = True
        return True
    pending = getattr(session, "_pending_swarm_mandate", None)
    if pending and is_swarm_continuation_user_message(text):
        session._pending_swarm_active = True
        return True
    session._pending_swarm_active = False
    return False


def session_pending_swarm_active(session: Any) -> bool:
    return bool(getattr(session, "_pending_swarm_active", False))


def session_pending_swarm_goal(session: Any) -> str:
    pending = getattr(session, "_pending_swarm_mandate", None)
    if pending is None:
        return ""
    return _norm_whitespace(getattr(pending, "goal", "") or "")


def session_pending_swarm_model(session: Any) -> str:
    pending = getattr(session, "_pending_swarm_mandate", None)
    if pending is None:
        return ""
    return _norm_whitespace(getattr(pending, "model", "") or "")


def clear_session_pending_swarm_mandate(session: Any) -> None:
    """Drop the session mandate once native run_swarm / run_parallel starts."""
    if session is None:
        return
    session._pending_swarm_mandate = None
    session._pending_swarm_active = False


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


def normalize_objective_key(goal: str) -> str:
    """Canonical objective fingerprint for in-flight + same-turn dispatch dedupe.

    Collapses whitespace/case, normalizes path separators, and strips decorative
    punctuation so near-identical implement goals (slash vs backslash, trailing
    periods) still collide.
    """
    text = _norm_whitespace(goal or "").lower().replace("\\", "/")
    text = re.sub(r"[^\w\s/.:_-]+", " ", text)
    text = _norm_whitespace(text).strip(".:_-")
    return _norm_whitespace(text)


def dedupe_dispatch_actions(actions: list) -> list:
    """Keep the first run_implement/run_swarm/run_parallel per objective fingerprint.

    Models often emit two nearly-identical implement tool_calls in one turn.
    Filtering before execution is the bulletproof layer above in-flight claims.
    """
    seen: set[tuple] = set()
    out: list = []
    for act in actions or []:
        kind = getattr(act, "kind", "") or ""
        if kind in ("run_implement", "run_swarm"):
            key = (kind, normalize_objective_key(getattr(act, "goal", "") or ""))
            if key[1]:
                if key in seen:
                    continue
                seen.add(key)
        elif kind == "run_parallel":
            goals = getattr(act, "goals", None) or []
            norm_goals = tuple(
                normalize_objective_key(g) for g in goals if normalize_objective_key(g)
            )
            key = (kind, norm_goals)
            if norm_goals:
                if key in seen:
                    continue
                seen.add(key)
        out.append(act)
    return out


def _swarm_model_key(act: Any) -> str:
    """Normalize optional worker model pin for thrash / loop fingerprinting."""
    raw = getattr(act, "model", None)
    if raw is None:
        args = getattr(act, "arguments", None) or {}
        if isinstance(args, dict):
            raw = args.get("model")
    return _norm_whitespace(str(raw or "")).lower()


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
        if kind == "write_file":
            import hashlib
            content = getattr(act, "content", None)
            if content is None or content == "":
                content = args.get("content") or args.get("text") or ""
            text = str(content or "")
            payload["content_fingerprint"] = (
                hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] if text else ""
            )
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
    elif kind == "run_command_batch":
        # Fingerprint commands — never put raw shell text into the guard key.
        try:
            from harness.command_jobs import command_fingerprint
            cmds = getattr(act, "commands", None) or []
            payload["command_fingerprints"] = [
                command_fingerprint(str(c)) for c in cmds if str(c or "").strip()
            ]
        except Exception:
            payload["command_fingerprints"] = []
        payload["max_concurrency"] = int(getattr(act, "max_concurrency", 0) or 0)
    elif kind == "run_ipython":
        import hashlib
        code = (getattr(act, "content", "") or "").strip()
        payload["code_fingerprint"] = (
            hashlib.sha256(code.encode("utf-8")).hexdigest()[:16] if code else ""
        )
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
        payload["goal"] = normalize_objective_key(getattr(act, "goal", "") or "")
        roles = getattr(act, "roles", None) or []
        payload["roles"] = sorted(roles) if isinstance(roles, list) else []
        payload["repo"] = _norm_path(getattr(act, "repo", "") or "")
        # Model pin is part of identity so changing pin after a plumbing
        # degrade is a real new dispatch, not a loop-guard collision.
        if kind == "run_swarm":
            payload["model"] = _swarm_model_key(act)
    elif kind == "run_parallel":
        goals = getattr(act, "goals", None) or []
        payload["goals"] = (
            [normalize_objective_key(g) for g in goals] if isinstance(goals, list) else []
        )
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


def is_local_handoff_command(command: str) -> bool:
    """True for clipboard copy/paste — local I/O, not codebase exploration."""
    cmd = _norm_whitespace(command)
    if not cmd:
        return False
    return bool(_CLIPBOARD_SINK_RE.search(cmd))


def _leading_command_tokens(command: str) -> list[str]:
    """First argv tokens of the first pipeline/simple command (ignore env assigns)."""
    cmd = _norm_whitespace(command)
    if not cmd:
        return []
    segment = _LEADING_SEGMENT_RE.split(cmd, 1)[0].strip()
    tokens: list[str] = []
    for raw in segment.split():
        if _ENV_ASSIGN_RE.match(raw):
            continue
        name = raw.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
        if name.endswith(".exe"):
            name = name[:-4]
        tokens.append(name)
        if len(tokens) >= 2:
            break
    return tokens


def is_exploration_command(command: str) -> bool:
    cmd = _norm_whitespace(command)
    if not cmd:
        return False
    if is_local_handoff_command(cmd):
        return False
    if _BARE_DIR_PROBE_RE.match(cmd):
        return True
    tokens = _leading_command_tokens(cmd)
    if not tokens:
        return False
    lead = tokens[0]
    if lead == "echo":
        return True
    if lead == "git" and len(tokens) > 1 and tokens[1] == "grep":
        return True
    return lead in _EXPLORATION_COMMANDS


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


def puppetmaster_cli_native_mapping(
    command: str,
    state: Optional[TurnGuardState] = None,
) -> tuple[str, str]:
    """Return (native_kind, one-line example) for a Puppetmaster CLI command.

    An active explicit swarm mandate remaps every launch-shaped verb
    (including ``agentic`` / ``implement``) to ``run_swarm``.
    """
    subcmd = _extract_puppetmaster_subcommand(command)
    explicit_pending = bool(
        state is not None
        and getattr(state, "explicit_swarm", False)
        and not getattr(state, "swarm_dispatched", False)
    )
    if subcmd in _CLI_STATUS_SUBCMDS:
        return ("action_result", "read prior action_result/swarm_result; or search_state")
    if subcmd in _CLI_ROUTE_SUBCMDS:
        return ("route_task", 'instruction="..."')
    if explicit_pending and (subcmd in _CLI_LAUNCH_SUBCMDS or not subcmd):
        return (
            "run_swarm",
            'goal="...", roles=["explore","pipeline-mapper"]',
        )
    if subcmd in _CLI_SWARM_SUBCMDS or not subcmd:
        return (
            "run_swarm",
            'goal="...", roles=["explore","pipeline-mapper"]',
        )
    if subcmd in _CLI_IMPLEMENT_SUBCMDS:
        return ("run_implement", 'goal="..."')
    return (
        "run_swarm",
        'goal="...", roles=["explore","pipeline-mapper"]',
    )


def is_puppetmaster_cli_launch_command(command: str) -> bool:
    """True for swarm/implement/agentic launch verbs, not status/artifacts/route."""
    return parse_puppetmaster_cli_launch(command) is not None


def parse_puppetmaster_cli_launch(command: str) -> Optional[tuple[str, str, str]]:
    """Best-effort ``(subcmd, goal, model)`` for a launch-shaped PM CLI.

    Extracts exactly one ``puppetmaster`` / ``python -m puppetmaster``
    invocation from a wrapper command via shlex tokenization. Wrapper
    tokens and operators are ignored — none of the shell text is executed.
    Returns None when quoting is malformed, zero or multiple invocations
    exist, or the extracted subcommand is status/artifacts/route.
    """
    cmd = _norm_whitespace(command)
    if not cmd:
        return None
    tokens = _tokenize_cli_shell(cmd)
    if not tokens:
        return None
    rest = _extract_single_puppetmaster_cli_argv(tokens)
    if rest is None:
        return None
    subcmd = ""
    payload = rest
    if rest:
        head = rest[0].lower()
        if head in _CLI_STATUS_SUBCMDS or head in _CLI_ROUTE_SUBCMDS:
            return None
        if head in _CLI_LAUNCH_SUBCMDS:
            subcmd = head
            payload = rest[1:]
    goal, model = _parse_cli_goal_and_model(payload)
    return (subcmd, goal, model)


def _tokenize_cli_shell(command: str) -> Optional[list[str]]:
    """shlex-split with operator punctuation. None if quoting is malformed."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        return [tok for tok in lexer if tok]
    except ValueError:
        return None


def _cli_command_basename(token: str) -> str:
    lead = token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    if lead.endswith(".exe"):
        lead = lead[:-4]
    return lead


def _cli_argv_until_operator(tokens: list[str], start: int) -> list[str]:
    out: list[str] = []
    for tok in tokens[start:]:
        if tok in _CLI_SHELL_OPERATORS:
            break
        out.append(tok)
    return out


def _extract_single_puppetmaster_cli_argv(tokens: list[str]) -> Optional[list[str]]:
    """Rest argv of the sole PM invocation, stopped at the next operator.

    Locates ``puppetmaster`` or ``python -m puppetmaster`` anywhere, including
    after env-assign prefixes. None if zero or multiple invocations exist.
    """
    found: list[list[str]] = []
    i = 0
    n = len(tokens)
    while i < n:
        if tokens[i] in _CLI_SHELL_OPERATORS:
            i += 1
            continue
        name = _cli_command_basename(tokens[i])
        if (
            name.startswith("python")
            and i + 2 < n
            and tokens[i + 1] == "-m"
            and tokens[i + 2].lower() == "puppetmaster"
        ):
            rest = _cli_argv_until_operator(tokens, i + 3)
            found.append(rest)
            i = i + 3 + len(rest)
            continue
        if name == "puppetmaster":
            rest = _cli_argv_until_operator(tokens, i + 1)
            found.append(rest)
            i = i + 1 + len(rest)
            continue
        i += 1
    if len(found) != 1:
        return None
    return found[0]


def _parse_cli_goal_and_model(tokens: list[str]) -> tuple[str, str]:
    """Extract --goal / --model / first positional from launch argv."""
    goal = ""
    model = ""
    positionals: list[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok.startswith("--goal="):
            goal = tok.split("=", 1)[1].strip()
            i += 1
            continue
        if tok in ("--goal", "-g"):
            i += 1
            parts: list[str] = []
            while i < n and not tokens[i].startswith("-"):
                parts.append(tokens[i])
                i += 1
            goal = " ".join(parts).strip()
            continue
        if tok.startswith("--model="):
            model = tok.split("=", 1)[1].strip()
            i += 1
            continue
        if tok == "--model":
            i += 1
            if i < n:
                model = tokens[i].strip()
                i += 1
            continue
        if tok.startswith("-"):
            i += 1
            if i < n and not tokens[i].startswith("-"):
                i += 1
            continue
        positionals.append(tok)
        i += 1
    if not goal and positionals:
        goal = positionals[0].strip()
    return goal, model


def translate_puppetmaster_cli_action(
    act: Any,
    state: Optional[TurnGuardState] = None,
    *,
    pending_goal: str = "",
    pending_model: str = "",
) -> Optional[Any]:
    """Rewrite a launch-shaped PM ``run_command`` into a native PilotAction.

    Status/artifacts/route and unsafe parses return None so the existing
    REDIRECT refusal remains. Never executes the shell command.
    """
    if getattr(act, "kind", "") != "run_command":
        return None
    command = getattr(act, "command", "") or ""
    parsed = parse_puppetmaster_cli_launch(command)
    if parsed is None:
        return None
    _subcmd, parsed_goal, parsed_model = parsed
    native_kind, _example = puppetmaster_cli_native_mapping(command, state)
    if native_kind not in ("run_swarm", "run_implement"):
        return None
    goal = (parsed_goal or pending_goal or "").strip()
    if not goal:
        return None
    model = (parsed_model or pending_model or "").strip()
    try:
        from .pilot import PilotAction
    except Exception:
        return None
    tool_call_id = str(getattr(act, "tool_call_id", "") or "")
    return PilotAction(
        kind=native_kind,
        goal=goal,
        model=model,
        tool_call_id=tool_call_id,
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


def _is_durable_recall_read(act: Any) -> bool:
    """True when read_file targets artifact:// / job:// / spill:// (etc.)."""
    path = getattr(act, "path", None) or ""
    if not path and isinstance(getattr(act, "arguments", None), dict):
        path = act.arguments.get("path") or ""
    try:
        from harness.validation_reuse import is_durable_recall_uri
        return is_durable_recall_uri(str(path or ""))
    except Exception:
        text = str(path or "").strip().lower()
        return text.startswith((
            "artifact://", "job://", "spill://", "agent://", "conflict://",
        ))


def is_swarm_gate_blocked_exploration(state: TurnGuardState, kind: str, act: Any) -> bool:
    if getattr(state, "explicit_swarm", False):
        if state.kernel_recovery:
            return False
        if not state.swarm_dispatched:
            if kind in ("run_swarm", "run_parallel"):
                return False
            if kind in ("search_codegraph", "search_state", "route_task", "query_wiki"):
                return False
            if kind == "read_file" and _is_durable_recall_read(act):
                return False
            return True
        if not state.broad_intent:
            return False

    if not state.broad_intent:
        return False

    # Kernel failed before producing usable work — do not deadlock the pilot
    # on "delegate again" when the kernel is the broken path.
    if state.kernel_recovery:
        return False

    # Durable recall is never swarm-gated: search_state and read_file of
    # artifact:// / job:// / spill:// must stay available before redispatch.
    if kind == "search_state":
        return False
    if kind == "read_file" and _is_durable_recall_read(act):
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


def _swarm_gate_suppress_message(
    kind: str,
    *,
    swarm_dispatched: bool = False,
    explicit_swarm: bool = False,
) -> str:
    roles = ", ".join(BROAD_SWARM_ROLES)
    if explicit_swarm and not swarm_dispatched:
        return (
            f"(SUPPRESSED: {kind} — this turn asked for a swarm. Do not git, "
            f"write launch scripts, or shell the Puppetmaster CLI. Call run_swarm "
            f"now with MULTIPLE roles ({roles}) and auto-routed OpenRouter/"
            f"agentic models. run_implement is one worker, not a swarm.)"
        )
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
        f"for narrow symbol lookups; search_state and read_file of artifact:// / "
        f"job:// / spill:// remain available for durable recall before redispatch). "
        f"Dispatch run_swarm with MULTIPLE roles "
        f"({roles}) and auto-routed models so parallel workers map the space. The "
        f"durable artifact store makes every swarm cheaper on follow-up turns "
        f"(artifact recall is zero-token). After dispatch, search_codegraph and "
        f"read_file of paths cited in findings remain allowed to validate concrete "
        f"findings; list_dir/search_files/grep sweeps stay blocked. If findings are "
        f"shallow or empty, re-dispatch a narrowed swarm — never substitute an inline "
        f"exploration campaign.)"
    )


def _swarm_gate_replay_message(
    kind: str,
    *,
    swarm_dispatched: bool = False,
    explicit_swarm: bool = False,
) -> str:
    """Short cached redirect after the first full swarm-gate suppress this turn."""
    if explicit_swarm and not swarm_dispatched:
        return (
            f"[swarm_gate redirect already issued this turn — stop {kind}. "
            f"Call run_swarm or run_parallel now. Do not git or shell "
            f"Puppetmaster CLI.]"
        )
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


_EDIT_FIRST_SUPPRESS_MESSAGE = (
    "(SUPPRESSED: edit-first nested implement) Read only the target file(s) you must "
    "change, then call edit_file/hash_edit/write_file immediately. Do not burn the "
    "tool budget on list_dir, search_files, run_ipython, or broad exploration before "
    "the required write."
)


def check_edit_first(state: TurnGuardState, kind: str, act: Any) -> GuardVerdict:
    """Keep nested implement workers edit-first until a write lands.

    Broad exploration is blocked outright; target ``read_file`` is allowed up to
    ``edit_first_read_allowance()`` before a write is required. Foreground pilots
    and analysis workers are unaffected (``nested_implement`` stays False).
    """
    if not state.nested_implement or state.edit_seen:
        return GuardVerdict(False)
    if kind in EDIT_FIRST_WRITE_KINDS:
        return GuardVerdict(False)
    if kind in EDIT_FIRST_BLOCKED_KINDS:
        return GuardVerdict(
            suppress=True,
            reason="edit_first",
            message=_EDIT_FIRST_SUPPRESS_MESSAGE,
        )
    if kind == "run_command" and is_exploration_command(getattr(act, "command", "") or ""):
        return GuardVerdict(
            suppress=True,
            reason="edit_first",
            message=_EDIT_FIRST_SUPPRESS_MESSAGE,
        )
    if kind == "read_file" and not _is_durable_recall_read(act):
        allowance = edit_first_read_allowance()
        if state.read_file_count >= allowance:
            return GuardVerdict(
                suppress=True,
                reason="edit_first",
                message=_EDIT_FIRST_SUPPRESS_MESSAGE,
            )
    return GuardVerdict(False)


def _iteration_budget_suppress_message(
    cap: int, *, implement_success: bool = False,
) -> str:
    if implement_success:
        return (
            "(SUPPRESSED: post-implement validation allowance exhausted — "
            "the worker patch already landed. Report the outcome to the user "
            "and stop; do not launch more verification, browser smoke, or "
            "re-investigation.)"
        )
    return (
        f"(SUPPRESSED: per-turn tool-call budget exhausted ({cap}/{cap} calls used). "
        f"Summarize findings for the user and/or dispatch background workers "
        f"(run_swarm/run_implement/run_parallel) instead of more inline tool calls.)"
    )


_CHROME_FILE_SMOKE_BROWSER_RE = re.compile(
    r"(?:^|[\s;/\\\"'])(?:google-chrome(?:-stable)?|chromium(?:-browser)?|chrome)"
    r"(?:\.exe)?\b",
    re.IGNORECASE,
)
_CHROME_HEADLESS_OR_DUMP_RE = re.compile(
    r"(?:--headless(?:=[^\s]*)?|--dump-dom)\b",
    re.IGNORECASE,
)
_CHROME_LOCAL_TARGET_RE = re.compile(
    r"(?:file://|(?:^|[\s\"'=])(?:\./|../|[A-Za-z]:[\\/])?(?:[\w./\\-]*/)?index\.html\b)",
    re.IGNORECASE,
)
_EXPLICIT_BROWSER_QA_RE = re.compile(
    r"(?:"
    r"\bbrowser\s+qa\b|"
    r"\bvisual\s+(?:qa|check|validation|test|review)\b|"
    r"\bscreenshot\b|"
    r"\bopen\s+(?:it\s+)?(?:in\s+)?(?:the\s+)?browser\b|"
    r"\bbrowser\s+(?:check|test|validation|smoke)\b"
    r")",
    re.IGNORECASE,
)

_CHROME_SMOKE_SUPPRESS_MESSAGE = (
    "[suppressed: headless Chrome file:// / dump-dom smoke] On a tiny or "
    "post-implement workspace, do not launch headless Chrome/Chromium against "
    "local file:// or index.html for smoke checks. Prefer one cheap static "
    "verify (read the changed file, node --check, or a focused grep), then "
    "report and stop. Use native browser_* tools only when the user explicitly "
    "asks for browser QA / visual validation."
)


def user_requests_browser_qa(message: str) -> bool:
    """True when the user explicitly asked for browser / visual QA."""
    return bool(_EXPLICIT_BROWSER_QA_RE.search(message or ""))


def is_headless_chrome_file_smoke_command(command: str) -> bool:
    """Detect headless Chrome/Chromium file:// or local index.html dump-dom smoke."""
    cmd = command or ""
    if not _CHROME_FILE_SMOKE_BROWSER_RE.search(cmd):
        return False
    if not _CHROME_HEADLESS_OR_DUMP_RE.search(cmd):
        return False
    return bool(_CHROME_LOCAL_TARGET_RE.search(cmd))


def check_chrome_file_smoke(state: TurnGuardState, kind: str, act: Any) -> GuardVerdict:
    """Suppress headless Chrome local smoke when tiny / post-implement is active.

    Does not touch real ``browser_*`` tools. Honors explicit browser-QA asks.
    """
    if kind != "run_command":
        return GuardVerdict(False)
    if not (state.tiny_workspace or state.implement_success_seen):
        return GuardVerdict(False)
    if user_requests_browser_qa(state.user_message):
        return GuardVerdict(False)
    command = getattr(act, "command", "") or ""
    if not is_headless_chrome_file_smoke_command(command):
        return GuardVerdict(False)
    return GuardVerdict(
        suppress=True,
        reason="chrome_file_smoke",
        message=_CHROME_SMOKE_SUPPRESS_MESSAGE,
    )


def check_cli_redirect(state: TurnGuardState, kind: str, act: Any) -> GuardVerdict:
    if not cli_redirect_enabled():
        return GuardVerdict(False)
    if kind != "run_command":
        return GuardVerdict(False)

    command = getattr(act, "command", "") or ""
    if not is_puppetmaster_cli_command(command):
        return GuardVerdict(False)

    native_kind, example = puppetmaster_cli_native_mapping(command, state)
    return GuardVerdict(
        suppress=True,
        reason="cli_redirect",
        message=_cli_redirect_message(native_kind, example),
    )


# Pilot must not POST /api/restart (or equivalent) mid-turn — that tears down
# the SSE turn and surfaces as "[aborted] Connection closed…". MCP wiring and
# env hatches never need a mid-turn restart; harness/** self-edits wait for the
# user via Settings → Restart after the turn ends.
_BACKEND_RESTART_RE = re.compile(
    r"(?:"
    r"/api/restart\b"
    r"|"
    r"\bapi[/\\]restart\b"
    r"|"
    r"harness:restart\b"
    r"|"
    r"\brestart[-_\s]?backend\b"
    r")",
    re.IGNORECASE,
)

_BACKEND_RESTART_MESSAGE = (
    "[suppressed: mid-turn backend restart] Do NOT call /api/restart (or "
    "equivalent) during an active turn — it drops the live SSE connection and "
    "aborts the chat. For Docker/local MCP: use manage_mcp (localhost HTTP is "
    "allowed without a restart). For harness/** code that needs a reload: finish "
    "the turn and tell the user to use Settings → Advanced → Restart backend. "
    "Kill switch if you truly must: HARNESS_ALLOW_MID_TURN_RESTART=1."
)


def mid_turn_restart_blocked() -> bool:
    """True when mid-turn backend restarts are refused (default)."""
    raw = (os.environ.get("HARNESS_ALLOW_MID_TURN_RESTART") or "").strip().lower()
    return raw not in ("1", "true", "yes", "on")


def is_backend_restart_command(command: str) -> bool:
    return bool(_BACKEND_RESTART_RE.search(command or ""))


_IMPLEMENT_EXHAUSTED_PROVENANCE_KEY = "empty_managed_implement_exhausted"

_IMPLEMENT_EXHAUSTED_MESSAGE = (
    "[suppressed: empty managed implement exhausted] Recovery already ran "
    "against dirty live checkout — do not re-dispatch run_implement, "
    "run_parallel, or run_swarm; use hash_edit/edit_file on the live files "
    "or ask the user to commit/stash first."
)


def note_implement_exhausted_from_provenance(
    state: TurnGuardState, provenance: Any,
) -> None:
    """Best-effort: mark turn guard when worker provenance says recovery exhausted."""
    try:
        if isinstance(provenance, dict) and provenance.get(_IMPLEMENT_EXHAUSTED_PROVENANCE_KEY):
            state.last_implement_exhausted = True
    except Exception:
        pass


def job_result_shows_implement_success(
    res_job: Any, stamped: Any = None,
) -> bool:
    """True when a completed implement/local job returned real patch/success.

    Empty managed-implement recovery exhaustion is NOT success. Analysis-only
    green results (no patch) are also excluded.
    """
    if not isinstance(res_job, dict):
        return False
    try:
        provenance = res_job.get("worker_provenance") or {}
        if isinstance(provenance, dict) and provenance.get(_IMPLEMENT_EXHAUSTED_PROVENANCE_KEY):
            return False
        if res_job.get("error"):
            return False
        role = ""
        if isinstance(stamped, dict):
            role = str(stamped.get("role") or "").strip().lower()
        if role in ("analysis", "review"):
            return False
        # Analysis-ok findings with no patch are not implement success.
        if (
            res_job.get("analysis_ok")
            and not res_job.get("applied")
            and not res_job.get("has_patch_art")
        ):
            return False
        if res_job.get("applied"):
            return True
        if res_job.get("has_patch_art") and (
            res_job.get("held_for_review") or res_job.get("applied")
        ):
            return True
        if isinstance(provenance, dict) and provenance.get("worktree_diff_empty") is False:
            return True
    except Exception:
        return False
    return False


def clamp_post_implement_iteration_budget(state: TurnGuardState) -> None:
    """Clamp remaining tools to the post-implement allowance (never raise cap)."""
    budget = state.iteration_budget
    if budget is None:
        return
    allowance = post_implement_tool_allowance()
    # Preserve already-used calls; never raise the existing ceiling.
    budget.cap = min(budget.cap, budget.used + allowance)


def note_implement_success_from_job_result(
    state: TurnGuardState,
    res_job: Any,
    stamped: Any = None,
) -> None:
    """Mark implement success + clamp residual budget when provenance is real.

    Idempotent: subsequent calls after ``implement_success_seen`` are no-ops so
    the allowance is not re-applied.
    """
    try:
        if state.implement_success_seen:
            return
        if not job_result_shows_implement_success(res_job, stamped):
            return
        state.implement_success_seen = True
        clamp_post_implement_iteration_budget(state)
    except Exception:
        pass


def check_implement_exhausted(state: TurnGuardState, kind: str, act: Any) -> GuardVerdict:
    """Soft-refuse implement fan-out after empty managed implement recovery exhausted.

    Covers every SWARM_DISPATCH_KINDS entry (run_implement / run_parallel /
    run_swarm) so a reformulated parallel or swarm dispatch cannot mint another
    implement lifecycle after exhaustion.
    """
    if kind not in SWARM_DISPATCH_KINDS:
        return GuardVerdict(False)
    try:
        prov = getattr(act, "worker_provenance", None)
        if prov is not None:
            note_implement_exhausted_from_provenance(state, prov)
    except Exception:
        pass
    if not state.last_implement_exhausted:
        return GuardVerdict(False)
    return GuardVerdict(
        suppress=True,
        reason="implement_exhausted",
        message=_IMPLEMENT_EXHAUSTED_MESSAGE,
    )


def check_backend_restart(state: TurnGuardState, kind: str, act: Any) -> GuardVerdict:
    """Soft-refuse run_command that would restart the harness mid-turn."""
    del state
    if not mid_turn_restart_blocked():
        return GuardVerdict(False)
    if kind != "run_command":
        return GuardVerdict(False)
    command = getattr(act, "command", "") or ""
    if not is_backend_restart_command(command):
        return GuardVerdict(False)
    return GuardVerdict(
        suppress=True,
        reason="mid_turn_restart",
        message=_BACKEND_RESTART_MESSAGE,
    )


def check_loop_guard(state: TurnGuardState, kind: str, act: Any) -> GuardVerdict:
    if not loop_guard_enabled():
        return GuardVerdict(False)

    key = (kind, normalize_action_args(kind, act))
    prior = state.execution_counts.get(key, 0)
    if prior < 1:
        return GuardVerdict(False)

    cached = state.successful_results.get(key)
    # Swarm/implement/parallel: one dispatch per objective fingerprint per turn.
    # Never allow LOOP_REPEAT_CAP re-runs -- twin workers race the same files.
    if kind in SWARM_DISPATCH_KINDS:
        if cached is not None:
            return GuardVerdict(
                suppress=True,
                reason="loop_replay",
                message=f"[cached repeat of identical call]\n{cached}",
                replay=True,
            )
        return GuardVerdict(
            suppress=True,
            reason="loop",
            message=_loop_suppress_message(kind, prior),
        )

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

    # MICRO disables swarm-gate suppress (orchestration skip only). INVARIANT:
    # never hide run_swarm while swarm_gate is on — MICRO leaves the gate off.
    try:
        from .task_profile import profile_disables_swarm_gate

        profile = getattr(state, "task_profile", "") or ""
        if profile_disables_swarm_gate(profile) and not getattr(
            state, "explicit_swarm", False
        ):
            return GuardVerdict(False)
    except Exception:
        pass

    if not is_swarm_gate_blocked_exploration(state, kind, act):
        return GuardVerdict(False)

    prior = state.swarm_gate_suppress_count
    state.swarm_gate_suppress_count = prior + 1
    dispatched = bool(state.swarm_dispatched)
    explicit = bool(getattr(state, "explicit_swarm", False))

    # First suppression(s) this turn get the full redirect so the model sees a
    # clear "stop exploring, call run_swarm now" signal. Further identical-class
    # suppressions reuse a short cached replay (loop-guard style) so broad-intent
    # turns cannot burn many unique SUPPRESSED payloads before dispatch.
    if prior < SWARM_GATE_FULL_REDIRECT_CAP:
        return GuardVerdict(
            suppress=True,
            reason="swarm_gate",
            message=_swarm_gate_suppress_message(
                kind, swarm_dispatched=dispatched, explicit_swarm=explicit,
            ),
        )

    return GuardVerdict(
        suppress=True,
        reason="swarm_gate_replay",
        message=_swarm_gate_replay_message(
            kind, swarm_dispatched=dispatched, explicit_swarm=explicit,
        ),
        replay=True,
    )


def check_delegate_gate(state: TurnGuardState, kind: str, act: Any) -> GuardVerdict:
    if not delegate_gate_enabled():
        return GuardVerdict(False)

    if kind in DELEGATION_EXEMPT_KINDS:
        return GuardVerdict(False)

    if not is_native_exploration(kind, act):
        return GuardVerdict(False)

    if state.delegation_seen or state.kernel_recovery:
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
        message=_iteration_budget_suppress_message(
            budget.cap, implement_success=bool(state.implement_success_seen),
        ),
    )


def plumbing_swarm_fingerprint(goal: str, model: str = "") -> tuple[str, str]:
    """Fingerprint for plumbing-only swarm thrash soft-refuse."""
    return (normalize_objective_key(goal or ""), _norm_whitespace(model or "").lower())


def result_shows_kernel_failure(content: str) -> bool:
    """True when a dispatch result is a failed-before-start / PM-unavailable error."""
    text = str(content or "")
    if not text.strip():
        return False
    return bool(_KERNEL_FAILURE_RE.search(text))


def note_kernel_recovery(state: TurnGuardState) -> None:
    """Open the local diagnosis lane after a kernel-unavailable dispatch."""
    state.kernel_recovery = True


def note_kernel_recovery_from_result(
    state: TurnGuardState, kind: str, content: str,
) -> None:
    """Best-effort: mark recovery when a swarm/implement result is a kernel fault."""
    if kind not in SWARM_DISPATCH_KINDS:
        return
    if result_shows_kernel_failure(content):
        note_kernel_recovery(state)


def record_plumbing_degraded_swarm(
    state: TurnGuardState, goal: str, model: str = "",
) -> None:
    """Remember a plumbing-only / no-FINDING swarm so identical redispatches soft-refuse."""
    key = plumbing_swarm_fingerprint(goal, model)
    if key[0]:
        state.plumbing_degraded_swarms.add(key)


def check_plumbing_swarm_thrash(
    state: TurnGuardState, kind: str, act: Any,
) -> GuardVerdict:
    """Soft-refuse identical run_swarm after a plumbing-only / no-FINDING degrade.

    Changing the model pin or the goal is allowed; looping the same audit is not.
    """
    if kind != "run_swarm":
        return GuardVerdict(False)
    goal = getattr(act, "goal", "") or ""
    model = _swarm_model_key(act)
    key = plumbing_swarm_fingerprint(goal, model)
    if not key[0] or key not in state.plumbing_degraded_swarms:
        return GuardVerdict(False)
    return GuardVerdict(
        suppress=True,
        reason="plumbing_swarm_thrash",
        message=(
            "(SUPPRESSED: identical run_swarm after a plumbing-only / no-FINDING "
            "degrade for this goal+model. Do NOT loop the same audit. Change the "
            "worker model pin (Settings-enabled agentic or Cursor worker) or "
            "reformulate the goal, then re-dispatch; or tell the user the prior "
            "swarm produced no FINDING/RISK/DECISION.)"
        ),
    )


def check_pilot_guards(state: TurnGuardState, kind: str, act: Any) -> GuardVerdict:
    """Apply CLI redirect, loop breaker, swarm gate, delegate gate, then budget."""
    restart_verdict = check_backend_restart(state, kind, act)
    if restart_verdict.suppress:
        return restart_verdict

    cli_verdict = check_cli_redirect(state, kind, act)
    if cli_verdict.suppress:
        return cli_verdict

    chrome_verdict = check_chrome_file_smoke(state, kind, act)
    if chrome_verdict.suppress:
        return chrome_verdict

    implement_exhausted_verdict = check_implement_exhausted(state, kind, act)
    if implement_exhausted_verdict.suppress:
        return implement_exhausted_verdict

    thrash_verdict = check_plumbing_swarm_thrash(state, kind, act)
    if thrash_verdict.suppress:
        return thrash_verdict

    loop_verdict = check_loop_guard(state, kind, act)
    if loop_verdict.suppress:
        return loop_verdict

    edit_first_verdict = check_edit_first(state, kind, act)
    if edit_first_verdict.suppress:
        return edit_first_verdict

    swarm_verdict = check_swarm_gate(state, kind, act)
    if swarm_verdict.suppress:
        return swarm_verdict

    # Nested implement workers already have an edit-first gate; skip the
    # foreground delegate redirect (which tells them to call run_implement).
    if not state.nested_implement:
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

    if kind in EDIT_FIRST_WRITE_KINDS:
        state.edit_seen = True

    if kind in DELEGATION_EXEMPT_KINDS:
        state.delegation_seen = True
    elif is_native_exploration(kind, act):
        state.exploration_count += 1

    if kind == "read_file" and not _is_durable_recall_read(act):
        # Durable artifact:// / job:// / spill:// reads do not consume the
        # pre-dispatch read allowance — they are validation-reuse recall.
        state.read_file_count += 1

    if state.iteration_budget is not None:
        state.iteration_budget.consume()


def new_turn_guard_state(
    user_message: str = "",
    *,
    repo_path: Optional[str] = None,
    nested_implement: bool = False,
    task_profile: str = "",
) -> TurnGuardState:
    tiny = bool(repo_path) and is_tiny_workspace(repo_path or "")
    # Foreground tiny pilots tighten to 12; nested implement workers keep the
    # base ceiling and rely on check_edit_first instead.
    if nested_implement:
        cap = turn_tool_budget_cap(repo_path, nested_implement=True)
    else:
        cap = turn_tool_budget_cap()
        if tiny:
            cap = min(cap, tiny_workspace_tool_budget())
    return TurnGuardState(
        user_message=user_message or "",
        broad_intent=is_broad_intent_user_message(user_message or ""),
        explicit_swarm=is_explicit_swarm_user_message(user_message or ""),
        iteration_budget=IterationBudget(cap) if cap > 0 else None,
        tiny_workspace=tiny,
        nested_implement=bool(nested_implement),
        task_profile=(task_profile or "").strip().upper(),
    )


def reuse_or_new_turn_guard_state(
    prior: Optional[TurnGuardState],
    user_message: str = "",
    *,
    repo_path: Optional[str] = None,
    nested_implement: bool = False,
    task_profile: str = "",
) -> TurnGuardState:
    """Reuse prior guard state across model steps / keep-alive resume.

    Fresh user turns clear ``session._turn_guard_state`` before the first action
    batch, so ``prior is None`` means a brand-new originating turn. Carries
    execution counts, successful-result cache, broad-intent, delegation, and
    swarm-gate progress safely without preview/unrelated state leaks.
    """
    if prior is not None:
        if task_profile and not getattr(prior, "task_profile", ""):
            prior.task_profile = (task_profile or "").strip().upper()
        return prior
    return new_turn_guard_state(
        user_message,
        repo_path=repo_path,
        nested_implement=nested_implement,
        task_profile=task_profile,
    )


_READ_ONLY_ANALYSIS_GOAL_RE = re.compile(
    r"\b(?:"
    r"audit|review|analy[sz]e|investigat(?:e|ion)|assess(?:ment)?|"
    r"map(?:ping)?|find(?:\s+all)?|look\s+through|dead[- ]?code|"
    r"read[- ]only|code\s*review|security\s+review|quality\s+review"
    r")\b",
    re.IGNORECASE,
)

_EDIT_CAPABLE_GOAL_RE = re.compile(
    r"\b(?:"
    r"implement|fix|patch|edit|write|add|create|refactor|migrate|"
    r"delete|remove|rename|update|apply|land|ship"
    r")\b",
    re.IGNORECASE,
)


def is_read_only_analysis_goal(goal: str) -> bool:
    """True when a bare run_implement goal is a read-only audit/review ask.

    Broad audit/review goals must not silently default to edit-capable
    implement mode. Edit verbs in the goal win (force implement stays allowed).
    """
    text = _norm_whitespace(goal or "")
    if not text:
        return False
    if _EDIT_CAPABLE_GOAL_RE.search(text):
        return False
    if _READ_ONLY_ANALYSIS_GOAL_RE.search(text):
        return True
    return is_broad_intent_user_message(text)


def normalize_assistant_prose(text: str) -> str:
    """Canonical fingerprint for repeated assistant prose detection."""
    return _norm_whitespace(text or "").lower()


def fingerprint_turn_actions(actions: list | None) -> str:
    """Stable fingerprint of a turn's action list (kind + normalized args)."""
    parts: list[str] = []
    for act in actions or []:
        kind = getattr(act, "kind", "") or ""
        if not kind:
            continue
        try:
            parts.append(f"{kind}:{normalize_action_args(kind, act)}")
        except Exception:
            parts.append(kind)
    return "|".join(parts)


def stagnation_streak_cap() -> int:
    try:
        return max(1, int(os.environ.get("HARNESS_STAGNATION_STREAK_CAP", str(STAGNATION_STREAK_CAP))))
    except (TypeError, ValueError):
        return 3


def failed_objective_resume_cap() -> int:
    try:
        return max(1, int(os.environ.get(
            "HARNESS_FAILED_OBJECTIVE_RESUME_CAP", str(FAILED_OBJECTIVE_RESUME_CAP),
        )))
    except (TypeError, ValueError):
        return 2


def analysis_summary_is_substantive(summary: str) -> bool:
    """True when an analysis-job summary carries real findings, not plumbing.

    Generic completion blurbs and verification-only notes must not turn the
    badge green.
    """
    text = _norm_whitespace(summary or "")
    if not text:
        return False
    low = text.lower()
    plumbing_markers = (
        "successfully completed analysis task",
        "verification/plumbing only",
        "verification only",
        "only routing/verification plumbing",
        "plumbing artifacts",
        "no structured findings",
        "no_tool_calls",
        "without structured findings",
        "no findings",
        "audit findings: none",
        "findings: none",
        "no issues found",
        "nothing to report",
    )
    if any(m in low for m in plumbing_markers):
        return False
    # Generic completion blurbs with no cite / no body.
    if low in ("ok", "done", "complete", "completed", "success", "passed"):
        return False
    # Require either a long body or a path/line cite — same bar as swarm substance.
    if len(text) >= 200:
        return True
    path_cite = re.search(
        r"[\w./\\-]+\.(py|ts|tsx|js|jsx|md|json|toml|yml|yaml)\b|line\s+\d+|:\d+\b",
        text,
        re.IGNORECASE,
    )
    return bool(path_cite) and len(text) >= 40
