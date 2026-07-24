from __future__ import annotations

"""ConversationalSession: the PILOT loop (the product UX).

Difference from Session (the eval loop): Session emits one bare intent per task
and is for measuring drivers. ConversationalSession is the human-facing product:
the pilot CONVERSES (prose) and fires orchestration ACTIONS as collapsible
tool-calls, reacting to the artifacts they return, until it finishes a turn with
no actions and yields back to the user.

Transcript model:
- The pilot carries a running transcript (system + user + pilot prose + compact
  action results) ACROSS turns within a session. This is the conversation the
  user follows.
- Swarm workers receive only the distilled `goal` brief (+ CodeGraph). The
  transcript never enters a worker. Conversation and investigation are decoupled.

Events yielded (for GUI/CLI):
- ("thinking", {text, delta?, stream_id?,      -> live reasoning deltas (delta=true);
       output_index?, channel?})                 post-answer envelope thinking is not emitted
- ("message_delta", {text, stream_id?,         -> visible assistant/progress deltas;
       output_index?, channel?})                 channel=progress|answer when known
- ("stream_item_done", {stream_id})            -> seal one identity-bearing surface
- ("tool_prep", {name})                        -> tool name assembling before action_start
- ("message", {role:"assistant", text})        -> pilot prose (conversation)
- ("action_start", {id, kind, goal, cwd})      -> a collapsible card opens
- ("action_result", {id, job_id, num, types,   -> the card's body (artifacts)
       artifacts, adapter, mode})
- ("assistant_done", {turns, swarms})          -> turn complete, yield to user
- ("error", {error})
"""

import os
import sys
import hashlib
import threading
import time
import subprocess
import re
from dataclasses import dataclass, field, replace as _dc_replace
from typing import Iterator, Literal, Optional, Any, get_args

from ._exec import _puppetmaster_python, _puppetmaster_available, _puppetmaster_cmd
from .paths import git_toplevel, path_within

from pmharness import registry as reg
from . import providers as prov
from pmharness.intent import DriverIntent
from pmharness.bridge import execute_intent, BridgeResult
from .pilot import (
    PilotAction,
    PilotError,
    PilotTurn,
    PILOT_SYSTEM,
    WORKER_SYSTEM,
    is_invalid_action,
    parse_pilot_turn,
)
from .wiki import WikiClient
from .text_clean import clean_say
from .checkpoints import CheckpointStore
from .tool_dispatch import (
    ToolDispatchMixin,
    _ANSI_ESCAPE,
    _strip_ansi,
    is_safe_path,
)
from .prompt_queue import PromptQueueMixin
from .steer_mixin import SteerMixin
from .adapter_resolve import AdapterResolveMixin
from .compaction_mixin import CompactionContextMixin
from .local_jobs import LocalJobsMixin
from .conversation_jobs import ConversationJobsMixin
from .wiki_distill import WikiDistillMixin
from .review_memory import ReviewMemoryMixin
from .busy_control import BusyControlMixin
from .send_loop import SendLoopMixin
from .pilot_guards import (
    guards_active,
    check_pilot_guards,
    check_cli_redirect,
    check_backend_restart,
    cli_redirect_enabled,
    new_turn_guard_state,
    record_action_execution,
    dedupe_dispatch_actions,
    normalize_objective_key,
)
from .diag import note as _diag_note


_WORKER_IMPORTS_WARMED = False
# Serializes first-touch install of approval lock/containers on legacy/minimal
# sessions constructed via ``__new__`` (see ``_command_approval_lock_guard``).
_COMMAND_APPROVAL_INSTALL_LOCK = threading.Lock()


def _prewarm_worker_imports() -> None:
    """Warm the ENTIRE worker-reachable module graph ONCE, single-threaded, before
    any run_parallel/run_implement worker thread starts.

    Why: run_parallel dispatches provider workers onto a ThreadPoolExecutor, and
    each worker lazily first-imports harness.worker / harness.edit_engines and a
    large slice of puppetmaster.*. In the PACKAGED (PyInstaller) app, several of
    those first-time imports happen concurrently across the pool, and in the field
    that produced two paired swarm failures:
      - "cannot import name 'WorkerResult' from 'harness.worker'", and
      - "Error -3 while decompressing data: incorrect header check".
    The unfrozen repo never reproduces it (real .py files, different timing), so
    rather than chase the exact frozen import-machinery interaction, we remove the
    whole class of bug: if every module a worker could touch is already in
    sys.modules, no worker thread ever performs a first-import, so there is nothing
    left to race. This warms the full harness + pmharness + puppetmaster graph on
    the main thread; the earlier version only warmed five modules, leaving the rest
    to be first-imported concurrently.

    Best-effort and idempotent: __main__/test modules are skipped, and any module
    that fails to import is ignored (the worker surfaces its own error later,
    exactly as before). Cost is a one-time few-hundred-ms walk on first parallel
    dispatch, paid once per process.
    """
    global _WORKER_IMPORTS_WARMED
    if _WORKER_IMPORTS_WARMED:
        return
    _WORKER_IMPORTS_WARMED = True

    import importlib
    import pkgutil

    # Essentials first, so the worker path is guaranteed warm even if the broad
    # walk below is interrupted.
    for mod in ("harness.worker", "harness.edit_engines"):
        try:
            importlib.import_module(mod)
        except Exception:
            pass

    for pkg_name in ("harness", "pmharness", "puppetmaster"):
        try:
            pkg = importlib.import_module(pkg_name)
        except Exception:
            continue
        pkg_path = getattr(pkg, "__path__", None)
        if not pkg_path:
            continue
        try:
            modules = list(pkgutil.walk_packages(pkg_path, prefix=pkg_name + "."))
        except Exception:
            continue
        for info in modules:
            name = info.name
            if name.endswith("__main__") or ".tests" in name or ".test_" in name:
                continue
            try:
                importlib.import_module(name)
            except Exception:
                pass


def _is_stub_tool_result(msg: dict) -> bool:
    """True for the synthesized "(no result: ...)" placeholder that
    _sanitize_tool_pairs inserts for interrupted actions. Used to prefer a
    real, later-arriving result over the stub when both exist for one id."""
    content = msg.get("content")
    return isinstance(content, str) and content.startswith("(no result:")


def _clamp_tool_result(text: str, max_chars: Optional[int] = None) -> str:
    if max_chars is None:
        # Slightly tighter than the old 24k default so nested-repo scan dumps
        # (list_dir / shell type / large reads) hit spill sooner; env override
        # and context_budget's 8k default still win when set / used elsewhere.
        try:
            max_chars = int(os.environ.get("HARNESS_MAX_TOOL_RESULT_CHARS", "16000"))
        except ValueError:
            max_chars = 16000
    if len(text) <= max_chars:
        return text
    head_len = max_chars // 2
    tail_len = max_chars - head_len
    head = text[:head_len]
    tail = text[-tail_len:]
    m = len(text)
    n = m - max_chars
    marker = f"\n... [truncated {n} chars of {m}-char tool result -- middle elided to fit context] ...\n"
    return head + marker + tail


def _hardwrap_long_tokens(text: str, width: int = 200) -> str:
    """Soft-wrap any single unbroken run of non-whitespace longer than ``width``
    by inserting a newline every ``width`` chars. A pasted key/sha/base64 blob
    with no whitespace can overflow a single line; breaking it keeps a steer (or
    any wrapped text) contained. Idempotent for runs already <= width."""
    if not text:
        return text
    out = []
    run = []

    def _flush_run() -> None:
        if not run:
            return
        token = "".join(run)
        if len(token) > width:
            for k in range(0, len(token), width):
                out.append(token[k:k + width])
                if k + width < len(token):
                    out.append("\n")
        else:
            out.append(token)
        run.clear()

    for ch in text:
        if ch.isspace():
            _flush_run()
            out.append(ch)
        else:
            run.append(ch)
    _flush_run()
    return "".join(out)


def load_workspace_rules(repo: Optional[str]) -> str:
    if not repo or not os.path.isdir(repo):
        return ""
    try:
        repo_abs = os.path.abspath(repo)
    except Exception:
        return ""
    files_to_try = []
    files_to_try.append(("AGENTS.md", os.path.join(repo_abs, "AGENTS.md")))
    files_to_try.append(("CLAUDE.md", os.path.join(repo_abs, "CLAUDE.md")))
    files_to_try.append((".cursorrules", os.path.join(repo_abs, ".cursorrules")))
    cursor_rules_dir = os.path.join(repo_abs, ".cursor", "rules")
    if os.path.isdir(cursor_rules_dir) and is_safe_path(cursor_rules_dir, repo_abs):
        try:
            cursor_files = []
            for f in os.listdir(cursor_rules_dir):
                if f.endswith(".md"):
                    full_p = os.path.join(cursor_rules_dir, f)
                    if os.path.isfile(full_p):
                        cursor_files.append((f, full_p))
            cursor_files.sort(key=lambda x: x[0])
            for name, full_p in cursor_files:
                files_to_try.append((f".cursor/rules/{name}", full_p))
        except Exception:
            pass
    files_to_try.append((".github/copilot-instructions.md", os.path.join(repo_abs, ".github", "copilot-instructions.md")))
    blocks = []
    total_bytes_read = 0
    max_file_size = 8 * 1024
    max_total_size = 16 * 1024
    for name, full_path in files_to_try:
        if total_bytes_read >= max_total_size:
            break
        if not is_safe_path(full_path, repo_abs):
            continue
        if not os.path.isfile(full_path):
            continue
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(max_file_size)
            if not content:
                continue
            available_bytes = max_total_size - total_bytes_read
            if len(content) > available_bytes:
                content = content[:available_bytes]
            total_bytes_read += len(content)
            blocks.append(f"# Workspace rules (from {name})\n{content}")
        except Exception:
            pass
    if blocks:
        return "\n\n" + "\n\n".join(blocks)
    return ""


from .skill_store import SkillStore
from .rule_store import RuleStore
from .memory_store import MemoryStore


def _mcp_result_text(out: dict) -> str:
    """Flatten an MCP tools/call result into plain text for the transcript."""
    if not isinstance(out, dict):
        return str(out)
    parts = []
    for block in out.get("content", []) or []:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif "text" in block:
                parts.append(str(block["text"]))
    return "\n".join(parts) if parts else str(out)


def _format_mcp_tools_section(mcp_manager, tool_catalog=None, *, no_delegation: bool = False, browser_enabled: bool = True) -> str:
    """Format connected MCP tools for the system prompt."""
    if tool_catalog is not None:
        from .tool_discovery import discovery_enabled

        if discovery_enabled():
            if not mcp_manager:
                return ""
            try:
                mcp_tools = mcp_manager.discovered_tools()
            except Exception:
                return ""
            tool_catalog.refresh(
                mcp_tools=mcp_tools,
                no_delegation=no_delegation,
                browser_enabled=browser_enabled,
            )
            return tool_catalog.mcp_prompt_summary()

    if not mcp_manager:
        return ""
    try:
        tools = mcp_manager.discovered_tools()
    except Exception:
        return ""
    if not tools:
        return ""

    lines = []
    lines.append('## Connected MCP tools (call via {"kind":"call_mcp","tool":"<server>.<tool>","arguments":{...}}):')
    for t in tools:
        schema = t.input_schema or {}
        properties = schema.get("properties", {})
        required = schema.get("required", []) or []
        arg_parts = []
        if isinstance(properties, dict):
            for name, prop in properties.items():
                if not isinstance(prop, dict):
                    prop = {}
                arg_type = prop.get("type", "any")
                is_req = name in required
                req_marker = " (required)" if is_req else ""
                arg_parts.append(f"{name}:{arg_type}{req_marker}")
        
        args_str = ", ".join(arg_parts) if arg_parts else "none"
        desc = t.description.strip() if t.description else "No description"
        lines.append(f"- {t.qualified}: {desc} (args: {args_str})")
    
    return "\n".join(lines)

from .autobudget import AutoBudget
from .config import HarnessConfig
from .state import DurableState


_HARD_PILOT_STEPS_DEFAULT = 40  # safety cap on pilot<->swarm round-trips per user message


def _driver_is_plan_billing(driver_spec: str) -> bool:
    """True when the pilot burns a subscription (not a metered API key)."""
    prov = (driver_spec or "").split(":", 1)[0].strip().lower()
    return prov in ("cursor-cli", "cursor-agent", "openai-codex", "xai-oauth", "nous")


def _friendly_pilot_model_name(model_id: str) -> str:
    """Human label for common OpenAI family ids (Luna / Sol / Terra, …)."""
    import re
    mid = (model_id or "").strip()
    if not mid:
        return ""
    m = re.match(
        r"^(?:openai/)?gpt-(\d+(?:\.\d+)?)-(sol|terra|luna)(-pro)?(?:-|$)",
        mid,
        flags=re.IGNORECASE,
    )
    if m:
        ver, family, pro = m.group(1), m.group(2).title(), (" Pro" if m.group(3) else "")
        return f"{family} {ver}{pro}"
    return ""


def _hard_pilot_steps() -> int:
    """Live-read the step cap so test isolation (conftest deleting the env var)
    actually takes effect even if the module was imported while the app had
    set HARNESS_MAX_PILOT_STEPS=0. A module-level constant captured at import
    would freeze the app's value and defeat the per-test monkeypatch."""
    try:
        return int(os.environ.get("HARNESS_MAX_PILOT_STEPS", str(_HARD_PILOT_STEPS_DEFAULT)))
    except ValueError:
        return _HARD_PILOT_STEPS_DEFAULT


def append_failed_declarative_checks_summary(summary: str, declarative_checks) -> str:
    """Append a compact failed-check line for session/transcript surfacing."""
    try:
        from harness.declarative_checks import failed_checks_summary_line_from_dicts

        check_line = failed_checks_summary_line_from_dicts(declarative_checks)
    except Exception:
        return summary
    if not check_line:
        return summary
    return f"{summary}\n{check_line}" if summary else check_line


_TURN_CONTEXT_MARKER = "[context for this turn]"
_CODEGRAPH_INJECTION_PREFIX = "CODEGRAPH HAS ALREADY BEEN QUERIED"


def strip_turn_context_trailer(text: str) -> str:
    """Remove append-only turn-context injection from user-visible text.

    Pilots keep the trailer in history (prefix-cache / append-only). Display and
    UI paths must never surface the marker or following CODEGRAPH/WIKI/budget
    blocks. Pure string helper — no session or Puppetmaster dependency.
    """
    if not text:
        return text
    markers = (
        f"\n\n{_TURN_CONTEXT_MARKER}\n",
        f"\n\n{_TURN_CONTEXT_MARKER}",
        f"{_TURN_CONTEXT_MARKER}\n",
        _TURN_CONTEXT_MARKER,
    )
    cut: int | None = None
    for marker in markers:
        idx = text.find(marker)
        if idx != -1 and (cut is None or idx < cut):
            cut = idx
    if cut is not None:
        text = text[:cut].rstrip()
    stripped = text.lstrip()
    if stripped.startswith(_CODEGRAPH_INJECTION_PREFIX):
        return ""
    return text


# Emitted conversational SSE kinds (chat/auto paths). Intentionally excludes
# SessionEvent kinds and the framing-only "done" sentinel written by sse_pump.
# Keep in sync with yield sites in send_loop / conversation mixins — typing only;
# unknown kinds are not rejected at runtime.
ConvEventKind = Literal[
    "action_result",
    "action_start",
    "assistant_done",
    "auto_halt",
    "auto_status",
    "auto_verify",
    "checkpoint",
    "codegraph_context",
    "command_blocked",
    "command_approval_pending",
    "compacting",
    "compaction",
    "distilled",
    "error",
    "interrupted",
    "memory_propose",
    "message",
    "message_delta",
    "notice",
    "pending_review",
    "pilot_resume",
    "queued_prompt",
    "steer",
    "stream_item_done",
    "swarm_auth_failure",
    "swarm_pending",
    "swarm_result",
    "thinking",
    "tool_prep",
    "verification",
    "verifying",
    "vision",
    "wiki_prepared",
    "worker_delta",
]

VALID_CONV_EVENT_KINDS: frozenset[str] = frozenset(get_args(ConvEventKind))


@dataclass
class ConvEvent:
    kind: ConvEventKind
    data: dict = field(default_factory=dict)


class ConversationalSession(
    PromptQueueMixin,
    SteerMixin,
    AdapterResolveMixin,
    CompactionContextMixin,
    LocalJobsMixin,
    ConversationJobsMixin,
    WikiDistillMixin,
    ReviewMemoryMixin,
    SendLoopMixin,
    BusyControlMixin,
    ToolDispatchMixin,
):
    def __init__(self, config: HarnessConfig) -> None:
        self.config = config
        import tempfile
        from harness.context_budget import BudgetConfig
        self.context_budget_config = BudgetConfig()
        self.state_dir = config.state_dir or tempfile.mkdtemp(prefix="pilot-")
        try:
            from harness.spill_registry import sweep_expired_spills

            raw_retention = os.environ.get("HARNESS_SPILL_RETENTION_DAYS", "").strip().lower()
            if raw_retention and raw_retention not in ("0", "off", "none", "forever"):
                retention_days = int(raw_retention)
                if retention_days > 0:
                    sweep_expired_spills(self.state_dir, retention_days)
        except Exception:
            pass
        # Harness session id (from SessionStore) for savings-ledger dedupe scope.
        self.harness_session_id: str = ""
        # Swarm job id for per-job tool-output savings attribution (worker sessions).
        self.savings_job_id: str = ""
        # Provider-aware pilot: 'provider:model' spans any provider whose key is
        # set; a bare model resolves against available providers, else OpenRouter.
        try:
            self.pilot = prov.build_pilot(config.driver)
        except prov.ProviderError:
            # fall back to the eval registry (OpenRouter field) for known names
            self.pilot = prov._finalize_driver(reg.build(config.driver, reach=config.reach))
        # propagate repo/adapter so the bridge runs real analysis when configured
        if config.repo:
            os.environ["HARNESS_REPO"] = config.repo
        if config.swarm_adapter:
            os.environ["HARNESS_SWARM_ADAPTER"] = config.swarm_adapter
        # self-learning: load ACTIVE skills into the pilot's system context so
        # the loop compounds (procedural memory). Pending skills are NOT loaded.
        self._skills = SkillStore()
        system = WORKER_SYSTEM if getattr(config, "no_delegation", False) else PILOT_SYSTEM
        active = self._skills.list("active")
        if active:
            skills_block = "\n\n".join(
                f"## Skill: {s.name}\n{s.description}\n{s.body}" for s in active)
            system = (system + "\n\n# Learned skills (apply when relevant)\n"
                      + skills_block)
        # standing conventions (always-on, terse) -- distinct from task skills
        self._rules = RuleStore()
        active_rules = self._rules.list("active")
        if active_rules:
            rules_block = "\n".join(f"- {r.text}" for r in active_rules)
            system = (system + "\n\n# Standing rules (ALWAYS honor)\n" + rules_block)
        # durable memory (persistent across sessions -- user facts and preferences)
        self._memory = MemoryStore()
        mem_block = self._memory.render_block()
        if mem_block:
            system = system + "\n\n" + mem_block
        # workspace rules (auto-loaded from repository if available)
        ws_rules = load_workspace_rules(config.repo)
        if ws_rules:
            system = system + ws_rules
        # the running transcript with the pilot (conversation memory)
        self._history: list[dict] = [{"role": "system", "content": system}]
        # Per-turn context-token estimate cache. _estimate_context_tokens is
        # called on the compaction hot path and elsewhere; walking the whole
        # history on every call is O(n) work repeated many times per turn.
        # The cache is keyed on len(self._history) so any append/replace that
        # changes length auto-invalidates. For in-place same-length rebuilds
        # (e.g. _sanitize_tool_pairs) we call _invalidate_ctx_cache() explicitly.
        self._ctx_token_cache: Optional[int] = None
        self._ctx_token_cache_len: int = -1
        # parallel clean transcript for rendering in UI
        self._display_transcript: list[dict] = []
        # One-slot stash for Hermes-style message-edit Revert (set by rewind).
        self._rewind_stash = None  # type: ignore[assignment]
        # tracking background swarm job IDs for the session
        self._session_job_ids: list[str] = []
        # optional durable-knowledge integration (portable-llm-wiki)
        self._wiki = WikiClient()
        self._wiki_auto = os.environ.get("HARNESS_WIKI_AUTO", "").strip() in ("1", "true", "yes")
        # Local-model wiki orchestration: the cheap pilot structures a raw digest
        # into entity/concept/decision pages BEFORE ingest, so the wiki never pays
        # for a frontier orchestrator. Default = prepare-and-approve (human gates
        # what lands in durable cross-LLM memory). Opt into silent auto-ingest with
        # HARNESS_WIKI_ORCHESTRATE=auto for a trusted repo.
        _wo = os.environ.get("HARNESS_WIKI_ORCHESTRATE", "").strip().lower()
        self._wiki_orchestrate = _wo in ("1", "true", "yes", "auto", "approve", "on")
        self._wiki_orchestrate_auto = _wo == "auto"
        self._wiki_prepared_hwm = 0  # findings count at last prepare, dedupe re-runs
        # optional MCP integration -- set by the server so the pilot can call MCP tools
        self._mcp = None
        # optional server hook: bust wiki graph/status cache after in-process ingest
        self._on_wiki_ingest = None
        from .tool_discovery import ToolCatalog
        self._tool_catalog = ToolCatalog()
        self._checkpoints = CheckpointStore(config.repo)
        import collections
        self._steer_queue = collections.deque()
        self._steer_lock = threading.Lock()
        self._steer_pending = False
        # Set by drop_queued_steers / interrupt; flushed as ConvEvent("notice")
        # by the abandoned stream or the next inject/drain boundary check.
        self._pending_steer_drop_notice = None
        # PROMPT QUEUE: distinct from and complementary to the steer queue.
        # steer = an OUT-OF-BAND interrupt that redirects the CURRENT running turn
        # (injected into the last tool result or delivered at finalization).
        # prompt_queue = a "playlist" of FULL user prompts that each run as their
        # OWN complete turn, one after the previous fully finishes. Items are
        # editable/removable/reorderable BEFORE they run. Drained at the turn-
        # completion boundary AFTER any pending steers so steer keeps priority.
        # Shape: [{"id": str, "text": str}, ...]. Never raises.
        self._prompt_queue: list[dict] = []
        self._prompt_queue_lock = threading.Lock()
        # self-learning: accumulate this session's real findings for distillation
        self._session_findings: list = []
        self._first_objective: str = ""
        # token accounting for the autobudget governor (real metering, not a stub)
        self._tokens_used: int = 0
        self._tokens_in: int = 0   # cumulative prompt tokens (for accurate cost)
        self._tokens_out: int = 0  # cumulative completion tokens
        self._tokens_cached: int = 0  # cumulative prompt tokens served from cache
        # Cache-write tokens (Anthropic/Bedrock); billed at a premium vs input.
        self._tokens_cache_write: int = 0
        self._tokens_cache_write_5m: int = 0
        self._tokens_cache_write_1h: int = 0
        # Delegated-worker cost tracked as DOLLARS at each worker's OWN model
        # rate, plus a parallel token split, so the session cost is not computed
        # by repricing worker tokens at the (possibly much cheaper) pilot rate.
        # server.py prices (pilot tokens = _tokens_* - _worker_tokens_*) at the
        # pilot rate and ADDS _worker_cost_usd for the worker portion. Without
        # this, a worker on opus ($5/$25) whose tokens were merged into the pool
        # and repriced at a cheap pilot rate under-reported the session total.
        self._worker_cost_usd: float = 0.0
        self._worker_tokens_in: int = 0
        self._worker_tokens_out: int = 0
        # Provider-billed pilot spend (OpenRouter ``usage.cost``). When present,
        # /api/usage prefers this over token*catalog estimates for the covered
        # token slice so session spend matches the provider receipt.
        self._provider_cost_usd: float = 0.0
        self._provider_billed_tokens_in: int = 0
        self._provider_billed_tokens_out: int = 0
        self._provider_billed_tokens_cached: int = 0
        self._provider_billed_tokens_cache_write: int = 0
        self._provider_billed_tokens_cache_write_5m: int = 0
        self._provider_billed_tokens_cache_write_1h: int = 0
        # Subscription / plan pilots (Cursor CLI, ChatGPT Codex, …) burn credits
        # rather than API dollars. Cost UI labels these ``plan_estimated`` when
        # no provider usage.cost receipt is available.
        self._plan_billing: bool = _driver_is_plan_billing(
            getattr(getattr(self, "config", None), "driver", "") or ""
        )
        self._price_source: str = ""
        # The governing AutoBudget for the CURRENT fully-auto run, if any. Set
        # by run_auto for the duration of the run and cleared afterward. When
        # present it is threaded (as a child()) into every worker/swarm spawned
        # during the run so the whole pilot->swarm->worker spawn tree shares
        # ONE decrementing ceiling that never resets per level. None in
        # supervised mode -- so supervised workers keep their own default.
        self._auto_budget: Optional["AutoBudget"] = None
        # Most recent turn's REAL prompt (input) token count, as billed by the
        # driver for the current history. 0 until a real response is metered;
        # used to make the live context estimate track reality instead of the
        # chars//4 heuristic (see _estimate_context_tokens).
        self._last_prompt_tokens: int = 0
        # concurrency: a single ConversationalSession is single-flight. Two
        # concurrent send()/run_auto() calls would interleave self._history and
        # corrupt the transcript, so we reject re-entrant streams rather than
        # silently corrupting. (The harness is a local single-user tool.)
        self._busy = threading.Lock()
        self._busy_since = 0.0  # monotonic time the lock was acquired (0 = free)
        self._interrupt_requested = False  # user hit Stop; allow faster busy recovery
        # After Stop: report runners idle + suppress swarm keep-alive resume until
        # the next real user send (abandoned generator may still hold _busy).
        self._stop_holds_idle = False
        # Set by interrupt() only when _busy was held: late steers raced during
        # that abandoned generation are dropped on the next acquire. Idle-session
        # Stop must not wipe legitimate ready-session steers on the next send.
        self._steer_boundary_drop_on_acquire = False
        # Generation guard for the single-writer lock. The watchdog can force-
        # release a wedged turn's _busy so drain/new turns recover (audit finding
        # #6); the generation lets the reaped turn's own finally detect it was
        # reaped and skip its release, so it can never free a lock a LATER turn
        # now holds (which would break the single-writer invariant). _busy_meta
        # guards these two fields and the release decision.
        self._busy_gen = 0
        self._busy_meta = threading.Lock()
        # cooperative cancel: set by explicit Stop (/api/session/interrupt), not
        # by SSE view detach (Phase A: detach drains; only interrupt cancels)
        # so run_auto halts promptly instead of burning budget for a gone client.
        self._cancel = threading.Event()
        # auto-distill: when on, run_auto proposes PENDING skill/rule candidates on
        # completion (still human-gated for approval). On by default.
        env_val = os.environ.get("HARNESS_AUTO_DISTILL", "").strip().lower()
        if env_val:
            self._auto_distill = env_val not in ("0", "false", "no")
        else:
            self._auto_distill = True

        # Track tool calls and error recovery for hard task trigger
        self._total_tool_calls = 0
        self._error_then_recovery_seen = False
        self._has_tool_failure = False
        self._turn_count = 0
        self._corrections = []

        # High-water marks to avoid duplicate auto-distill on the same signal
        self._distilled_findings_hwm = 0
        self._distilled_turns_hwm = 0
        self._distilled_corrections_hwm = 0
        # diff review: opt-in mode to hold agent edits for approval
        self._review_edits_before_apply = os.environ.get("HARNESS_REVIEW_EDITS_BEFORE_APPLY", "").strip() in ("1", "true", "yes")
        self._pending_reviews = {}
        self._pending_reviews_lock = threading.Lock()
        # Command safety guard for FULL-AUTO mode: when running unattended, screen
        # each shell command for irreversible/remote/escalating patterns and pause
        # for human approval on a DANGER verdict (the safety an autonomous loop
        # lacks vs interactive co-working, where the human sees every command).
        # HARNESS_AUTO_COMMAND_GUARD=off disables it (NOT recommended with SSH
        # reachable + timeouts off). Default ON.
        self._auto_command_guard = os.environ.get("HARNESS_AUTO_COMMAND_GUARD", "").strip().lower() not in ("0", "false", "no", "off")
        self._auto_mode = False  # set True only for the duration of run_auto()
        # End-of-turn memory proposals (interactive only). Agent memory-add queues
        # here instead of writing; flushed as non-blocking Save/Skip cards after
        # assistant_done. Never used under Autopilot (_auto_mode).
        self._pending_memory_proposals: dict = {}
        self._turn_memory_queue: list = []
        self._pending_command_approvals = {}
        self._command_approval_lock = threading.Lock()
        self._approved_commands = set()  # command hashes the user one-click approved
        self._state = "idle"
        
        import queue
        import concurrent.futures
        self._apply_lock = threading.Lock()
        self._swarm_pool = concurrent.futures.ThreadPoolExecutor(max_workers=getattr(config, "max_workers", 4))
        self._swarm_results: queue.Queue = queue.Queue()
        self._swarm_futures: set[concurrent.futures.Future] = set()
        self._swarm_futures_lock = threading.Lock()
        # Bounded-inflight ceiling for _swarm_pool submissions. The executor's
        # own work queue is unbounded, so a burst of run_parallel/run_swarm
        # dispatches faster than max_workers can drain would silently balloon
        # memory. 4x the worker count is a generous ceiling that leaves
        # everyday bursts untouched but rejects pathological floods with a
        # notice to the pilot (see _submit_swarm).
        self._swarm_capacity = max(1, int(getattr(config, "max_workers", 4)) * 4)
        self._interrupted_swarms = False
        # In-process provider-native worker jobs (job_id "local-*"). These run on
        # the user's own provider key instead of a Puppetmaster adapter, so they
        # never land in the durable job store the swarm panel reads. Without a
        # live registry the panel shows "No swarm jobs yet" while a worker is
        # visibly running. Keyed by job_id; shaped like store jobs so the SwarmPane
        # renders them uniformly. Merged into /api/swarm/live.
        self._local_jobs: dict[str, dict] = {}
        self._local_jobs_lock = threading.Lock()
        # Per-job cooperative cancel flags. Python threads cannot be force-killed,
        # so cancelling a local worker is best-effort: we set this Event and the
        # worker checks it at its wall-clock boundary (see _run_edit_worker_bounded).
        # A set event flips the job to a terminal 'cancelled' state immediately for
        # the UI even though the underlying provider call may run to completion.
        self._local_job_cancels: dict[str, "threading.Event"] = {}
        # On-disk mirror of _local_jobs so provider-worker history survives a
        # backend restart (the durable store already persists adapter jobs; this
        # in-memory dict was the only piece lost across restarts).
        self._local_jobs_path = os.path.join(self.state_dir, "swarm_local_jobs.json")
        # On-disk mirror of the server-side prompt queue so queued prompts
        # survive a backend restart (transcripts and swarm_local_jobs already
        # persist; the in-memory _prompt_queue was the only piece lost).
        self._prompt_queue_path = os.path.join(self.state_dir, "prompt_queue.json")
        # In-flight implement objectives, so the same objective cannot be
        # dispatched concurrently (audit finding #2). The "one objective -> one
        # worker / disjoint file sets" rule lived only in the system prompt; this
        # enforces it in code, which stops the PATCH-DID-NOT-APPLY re-dispatch
        # loop where a duplicate worker races the original against a moving tree.
        self._inflight_objectives: set[str] = set()
        self._inflight_lock = threading.Lock()
        # Per-originating-user-turn guard / stagnation / failed-resume state.
        # Cleared on fresh user messages; preserved across model steps and
        # keep-alive resume for the same originating turn.
        self._turn_guard_state = None
        self._stagnation_last_prose = None
        self._stagnation_last_actions = None
        self._stagnation_streak = 0
        self._failed_objective_resume_counts: dict[str, int] = {}
        # Per-message CodeGraph slice cache. The codegraph_context() call is a
        # blocking Node subprocess (~270-500ms) -- recomputing it on every step
        # of a multi-step turn (same query) is pure dead time stacked in front of
        # the first token. Cache the slice keyed by the user message so it is
        # computed at most once per turn and reused across all steps.
        self._cg_cache_key = None
        self._cg_cache_section = ""
        self._cg_cache_symbols = 0
        # Per-message wiki grounding cache (mirrors CodeGraph cache above).
        self._wiki_cache_key = None
        self._wiki_cache_section = ""
        self._wiki_cache_pages = 0
        # Tracks prose already streamed to the client this turn via the
        # StreamingSayExtractor, so the final `message` event can mark itself as
        # already-shown (the frontend finalizes the streaming bubble in place
        # rather than re-dumping the text).
        self._streamed_prose = ""
        # Per-turn output budget from +Nk / +Nk! user directive (Round 10).
        self._turn_budget: Optional[dict] = None
        self._turn_output_tokens: int = 0
        # Append-only context mode (Round 11): frozen system prefix for KV reuse.
        self._append_only: Optional[bool] = None
        self._frozen_system_prompt: Optional[str] = None
        self._last_rendered_prompt: str = ""
        self._prefix_stable_turns: int = 0
        # Compaction summarizer: after timeout/fail, skip LLM and use extractive
        # fallback until this monotonic deadline (Hermes-style cooldown).
        self._compaction_fail_until: float = 0.0
        # Latest compaction attempt diagnostic (reason code for manual compact API).
        self._last_compaction_attempt: dict = {"reason": "below_trigger"}
        # Reload any persisted provider-worker history from a prior process so the
        # swarm panel keeps its history across a backend restart. Stale 'running'
        # jobs (whose thread died with the old process) are marked interrupted.
        self._load_local_jobs()
        # Reload any persisted prompt queue from a prior process so queued
        # prompts survive a backend restart. Tolerates a missing/corrupt file.
        self._load_prompt_queue()

    def state(self) -> str:
        if self._state == "thinking":
            return "thinking"
        if self.has_pending_swarms():
            return "awaiting_swarm"
        return self._state

    def _command_approval_lock_guard(self) -> threading.Lock:
        """Return the approval lock, installing one on legacy/minimal sessions.

        Reload/rewind helpers and some unit fixtures construct a
        ``ConversationalSession`` via ``__new__`` without running ``__init__``.
        Pending-approval restore must still be safe there without weakening
        locking on fully constructed sessions (always a real ``threading.Lock``).

        Installation of the lock and empty containers is serialized through a
        module-level install lock so concurrent first-touch on a minimal session
        cannot publish distinct Lock/set/dict instances.
        """
        lock = getattr(self, "_command_approval_lock", None)
        pending = getattr(self, "_pending_command_approvals", None)
        approved = getattr(self, "_approved_commands", None)
        if lock is not None and pending is not None and approved is not None:
            return lock
        with _COMMAND_APPROVAL_INSTALL_LOCK:
            lock = getattr(self, "_command_approval_lock", None)
            if lock is None:
                lock = threading.Lock()
                self._command_approval_lock = lock
            if getattr(self, "_pending_command_approvals", None) is None:
                self._pending_command_approvals = {}
            if getattr(self, "_approved_commands", None) is None:
                self._approved_commands = set()
            return lock

    @staticmethod
    def _approval_workspace_key(workspace_root: str) -> str:
        return os.path.normcase(os.path.realpath(workspace_root or ""))

    _COMMAND_HASH_HEX = re.compile(r"^[0-9a-f]{64}$")

    def _display_command_approval_row(
        self,
        pending: dict,
        *,
        status: str,
    ) -> dict:
        """Serialize a durable display-transcript command-approval card."""
        return {
            "type": "command_approval",
            "id": pending.get("action_id") or pending.get("command_hash") or "",
            "command": pending.get("command") or "",
            "command_hash": pending.get("command_hash") or "",
            "session_id": pending.get("session_id") or "",
            "workspace_root": pending.get("workspace_root") or "",
            "category": pending.get("category") or "",
            "reason": pending.get("reason") or "",
            "matched": pending.get("matched") or "",
            "status": status,
        }

    def _pending_command_approval_from_display_row(self, row: Any) -> Optional[dict]:
        """Validate a durable pending approval card for decision-state restore.

        Decided, mismatched, or malformed rows return ``None`` and stay
        display-only. Never trusts a row enough to pre-approve a command.
        """
        if not isinstance(row, dict):
            return None
        if row.get("type") != "command_approval":
            return None
        if (row.get("status") or "") != "pending":
            return None
        command_hash = str(row.get("command_hash") or "").strip().lower()
        if not self._COMMAND_HASH_HEX.fullmatch(command_hash):
            return None
        command = row.get("command") or ""
        if not isinstance(command, str) or not command.strip():
            return None
        # Cryptographic bind: refuse benign-card / evil-hash escalation.
        # Mismatched rows stay display-only and can never authorize.
        expected_hash = hashlib.sha256(command.encode("utf-8")).hexdigest()
        if command_hash != expected_hash:
            return None
        session_id = str(row.get("session_id") or "").strip()
        if not session_id:
            return None
        owning = (self.harness_session_id or "").strip()
        if owning and session_id != owning:
            return None
        workspace_root = str(row.get("workspace_root") or "").strip()
        if not workspace_root:
            return None
        canonical = self._approval_workspace_key(self.config.repo or "")
        row_key = self._approval_workspace_key(workspace_root)
        if not canonical or not row_key or row_key != canonical:
            return None
        return {
            "session_id": session_id,
            "workspace_root": os.path.realpath(self.config.repo or ""),
            "command": command,
            "command_hash": command_hash,
            "action_id": str(row.get("action_id") or row.get("id") or ""),
            "category": str(row.get("category") or ""),
            "reason": str(row.get("reason") or ""),
            "matched": str(row.get("matched") or ""),
        }

    def _restore_pending_command_approvals_from_display(self) -> None:
        """Rebuild in-memory pending decisions from durable display cards.

        One-shot ``_approved_commands`` never survive hydrate — the operator
        must decide again via ``decide_command_approval`` after restore.
        """
        restored: dict = {}
        for row in self._display_transcript or []:
            pending = self._pending_command_approval_from_display_row(row)
            if pending is None:
                continue
            restored[pending["command_hash"]] = pending
        with self._command_approval_lock_guard():
            self._pending_command_approvals = restored
            self._approved_commands.clear()

    def _upsert_display_command_approval(
        self,
        pending: dict,
        *,
        status: str,
    ) -> None:
        """Keep the display transcript in sync with pending/decided approvals."""
        command_hash = pending.get("command_hash") or ""
        if not command_hash:
            return
        row = self._display_command_approval_row(pending, status=status)
        display = self._display_transcript
        for i, existing in enumerate(display):
            if (
                isinstance(existing, dict)
                and existing.get("type") == "command_approval"
                and existing.get("command_hash") == command_hash
            ):
                display[i] = row
                return
        display.append(row)

    def register_pending_command_approval(
        self,
        *,
        command: str,
        command_hash: str,
        action_id: str,
        category: str = "",
        reason: str = "",
        matched: str = "",
    ) -> dict:
        """Retain one blocked full-auto command for an operator decision.

        Also writes a pending ``command_approval`` row into the display
        transcript so ring-miss / cursor-gap hydrate can restore the card
        without depending on retained SSE frames.
        """
        pending = {
            "session_id": self.harness_session_id,
            "workspace_root": os.path.realpath(self.config.repo or ""),
            "command": command,
            "command_hash": command_hash,
            "action_id": action_id,
            "category": category or "",
            "reason": reason or "",
            "matched": matched or "",
        }
        with self._command_approval_lock_guard():
            self._pending_command_approvals[command_hash] = pending
            self._upsert_display_command_approval(pending, status="pending")
        return dict(pending)

    def decide_command_approval(
        self,
        *,
        command_hash: str,
        workspace_root: str,
        approve: bool,
    ) -> Optional[dict]:
        """Apply a one-shot operator decision to this session's pending command."""
        requested_workspace = self._approval_workspace_key(workspace_root)
        with self._command_approval_lock_guard():
            pending = self._pending_command_approvals.get(command_hash)
            if pending is None:
                return None
            pending_workspace = self._approval_workspace_key(
                pending.get("workspace_root") or ""
            )
            if not requested_workspace or requested_workspace != pending_workspace:
                raise PermissionError("command approval workspace does not match")
            if pending.get("session_id") != self.harness_session_id:
                raise PermissionError("command approval session does not match")
            self._pending_command_approvals.pop(command_hash, None)
            if approve:
                self._approved_commands.add(command_hash)
            else:
                self._approved_commands.discard(command_hash)
            self._upsert_display_command_approval(
                pending,
                status="approved" if approve else "rejected",
            )
            return dict(pending)

    def consume_command_approval(self, command_hash: str) -> bool:
        """Consume one exact command-hash approval atomically."""
        with self._command_approval_lock_guard():
            if command_hash not in self._approved_commands:
                return False
            self._approved_commands.remove(command_hash)
            return True

    def has_pending_swarms(self) -> bool:
        with self._swarm_futures_lock:
            return len(self._swarm_futures) > 0

    def _swarm_inflight(self) -> int:
        """Number of futures currently tracked in ``_swarm_futures``.

        Snapshot under ``_swarm_futures_lock`` so callers see a coherent
        value even while the done-callbacks are draining the set.
        """
        with self._swarm_futures_lock:
            return len(self._swarm_futures)

    def _swarm_at_capacity(self) -> bool:
        """True iff inflight count is at or above ``_swarm_capacity``."""
        return self._swarm_inflight() >= self._swarm_capacity

    def _submit_swarm(self, fn, *args) -> bool:
        """Bounded-inflight choke point for ``_swarm_pool.submit``.

        The executor's work queue is unbounded, so if the pilot fires more
        dispatches than ``max_workers`` can drain the queue can grow without
        limit. This gate rejects new submissions once ``_swarm_capacity``
        futures are already in flight; callers surface a short "swarm
        capacity reached" notice instead of silently piling work onto the
        executor. Returns True on submit, False on reject. Never blocks
        (that would risk deadlocking the pilot loop) and never raises.

        On successful submit, the future is registered in ``_swarm_futures``
        and a done-callback is attached that removes it under the lock. The
        removal is via ``discard`` so calling any additional cleanup path
        (e.g. a bulk drain elsewhere) is idempotent.
        """
        try:
            if self._swarm_at_capacity():
                return False
            future = self._swarm_pool.submit(fn, *args)
        except Exception:
            # Never raise from the gate; caller treats this as "not
            # dispatched" the same as an at-capacity reject.
            return False
        with self._swarm_futures_lock:
            self._swarm_futures.add(future)

        def _cleanup(f):
            with self._swarm_futures_lock:
                # discard, not remove: safe if another drain path already
                # took the future out of the set.
                self._swarm_futures.discard(f)

        try:
            future.add_done_callback(_cleanup)
        except Exception:
            # If registering the callback fails for any reason, the future
            # is still tracked; a bulk drain elsewhere will clean it up.
            pass
        return True

    def _submit_housekeeping(self, fn, *args) -> bool:
        """Fire-and-forget post-turn work (auto-distill / wiki prepare).

        Must NOT register in ``_swarm_futures``: that set drives
        ``has_pending_swarms`` / runners=running, and counting distill as a
        "pending swarm" re-armed Still working / Stop after the turn already
        finished (skill proposal appeared, then busy chrome came back).
        Uses a daemon thread so it never steals swarm-pool capacity either.
        """
        try:
            threading.Thread(
                target=fn, args=args, daemon=True,
                name="pmh-housekeeping",
            ).start()
            return True
        except Exception:
            return False

    @property
    def durable(self) -> DurableState:
        return DurableState(self.state_dir)

    def export_history(self) -> list:
        """Returns the non-system messages (self._history minus the seeded system prompt) as a serializable list."""
        if len(self._history) <= 1:
            return []
        return [dict(m) for m in self._history[1:]]

    def export_display_transcript(self) -> list:
        """Return the display transcript, restoring any still-pending approvals.

        Pending DANGER approvals live in session state; if a prior save omitted
        the card (or a sibling poll raced ahead of the upsert), fold them back
        in so hydrate/reattach never drops a waiting operator decision.

        User-message text is scrubbed of append-only turn-context trailers on
        a row copy — history keeps the trailer for the pilot.
        """
        display: list = []
        for row in self._display_transcript:
            if (
                isinstance(row, dict)
                and (row.get("type") or "message") == "message"
                and row.get("role") == "user"
            ):
                text = row.get("text")
                if isinstance(text, str) and text:
                    cleaned = strip_turn_context_trailer(text)
                    if cleaned != text:
                        row = {**row, "text": cleaned}
            display.append(row)
        with self._command_approval_lock_guard():
            pending_rows = [
                self._display_command_approval_row(pending, status="pending")
                for pending in self._pending_command_approvals.values()
            ]
        if not pending_rows:
            return display
        seen = {
            row.get("command_hash")
            for row in display
            if isinstance(row, dict) and row.get("type") == "command_approval"
        }
        for row in pending_rows:
            command_hash = row.get("command_hash") or ""
            if not command_hash or command_hash in seen:
                continue
            display.append(row)
            seen.add(command_hash)
        return display

    def has_pending_user_turn(self) -> bool:
        """True when the transcript ends on a user turn with no assistant reply
        after it -- i.e. a reply is owed. Used to auto-resume across a backend
        restart (self-edit apply) so an in-flight turn is not silently dropped."""
        return bool(len(self._history) > 1 and self._history[-1].get("role") == "user")

    def export_transcript_data(self) -> dict:
        # Heal any dangling tool_use BEFORE serializing so a corrupted (e.g.
        # mid-spree) transcript is never persisted. export_history() slices
        # self._history[1:], so sanitize self._history first.
        self._sanitize_tool_pairs()
        return {
            "history": self.export_history(),
            "display": self.export_display_transcript(),
            "job_ids": list(self._session_job_ids),
        }

    def load_history(self, messages: Any) -> None:
        """Replaces the conversation turns (keep the freshly-built system prompt at index 0 -- which contains current skills/rules -- then append the loaded user/assistant messages). Do NOT persist the system prompt; only persist the user/assistant turns."""
        if isinstance(messages, dict):
            history_list = messages.get("history", [])
            self._display_transcript = messages.get("display", [])
            self._session_job_ids = messages.get("job_ids", [])
        else:
            history_list = messages
            self._display_transcript = []
            self._session_job_ids = []

        if not self._history:
            self._history = [{"role": "system", "content": ""}]
        system_prompt = self._history[0]
        cleaned = [m for m in history_list if m.get("role") != "system"]
        self._history = [system_prompt] + cleaned
        # Heal a previously-corrupted transcript (dangling or non-adjacent
        # tool_use) on load so we never send an invalid history to the model.
        self._sanitize_tool_pairs()
        # Durable pending DANGER cards must remain decidable after cold
        # attach/restart — rebuild decision state from validated display rows.
        self._restore_pending_command_approvals_from_display()

    def rewind_to_user_ordinal(self, user_ordinal: int) -> dict:
        """Hermes-style undo for message edit: truncate at the Nth user turn (0-based).

        Soft-stashes the discarded tail on ``_rewind_stash`` so the UI can offer
        Revert. Prefill is that user message's text (composer edit/resubmit).
        Does not auto-send.
        """
        if self.is_turn_busy():
            return {
                "ok": False,
                "error": "session busy — stop the current turn before editing a prior message",
                "code": "busy",
            }
        if user_ordinal < 0:
            return {"ok": False, "error": "user_ordinal out of range"}

        display = list(self._display_transcript or [])
        display_index = None
        seen = 0
        prefill = ""
        for i, row in enumerate(display):
            if not isinstance(row, dict):
                continue
            rtype = row.get("type") or "message"
            if rtype not in ("message", ""):
                continue
            if (row.get("role") or "") != "user":
                continue
            if seen == user_ordinal:
                display_index = i
                prefill = row.get("text") or row.get("content") or ""
                if not isinstance(prefill, str):
                    prefill = str(prefill or "")
                break
            seen += 1

        if display_index is None:
            return {"ok": False, "error": "user_ordinal out of range"}

        cut_hist = None
        seen_h = 0
        for hi, m in enumerate(self._history):
            if hi == 0:
                continue
            if (m.get("role") or "") == "user":
                if seen_h == user_ordinal:
                    cut_hist = hi
                    break
                seen_h += 1

        self._rewind_stash = {
            "history": self.export_history(),
            "display": list(display),
            "job_ids": list(self._session_job_ids or []),
            "display_index": display_index,
            "user_ordinal": user_ordinal,
            "prefill": prefill,
        }

        self._display_transcript = display[:display_index]
        if cut_hist is not None:
            system_prompt = self._history[0] if self._history else {"role": "system", "content": ""}
            self._history = [system_prompt] + list(self._history[1:cut_hist])
        self._sanitize_tool_pairs()

        removed = len(display) - display_index
        notice = (
            f"Editing from that message ({removed} turn item(s) set aside). "
            "Resubmit the edited text to start a new turn, or Cancel to restore."
        )
        return {
            "ok": True,
            "prefill": prefill,
            "notice": notice,
            "removed_count": removed,
            "kept_display": len(self._display_transcript),
            "display_index": display_index,
        }

    def rewind_to_display_index(self, display_index: int) -> dict:
        """Compatibility wrapper: map a display row index to user_ordinal then rewind."""
        display = list(self._display_transcript or [])
        if display_index < 0 or display_index >= len(display):
            return {"ok": False, "error": "display_index out of range"}
        ordinal = 0
        for i, row in enumerate(display):
            if i > display_index:
                break
            if not isinstance(row, dict):
                continue
            rtype = row.get("type") or "message"
            if rtype in ("message", "") and (row.get("role") or "") == "user":
                if i == display_index:
                    return self.rewind_to_user_ordinal(ordinal)
                ordinal += 1
        return {"ok": False, "error": "can only rewind from a user message"}

    def restore_rewind_stash(self) -> dict:
        """Restore the transcript tail saved by the last successful rewind."""
        if self.is_turn_busy():
            return {
                "ok": False,
                "error": "session busy — stop the current turn before reverting",
                "code": "busy",
            }
        stash = getattr(self, "_rewind_stash", None)
        if not isinstance(stash, dict) or not stash.get("display"):
            return {"ok": False, "error": "nothing to revert"}
        self.load_history({
            "history": stash.get("history") or [],
            "display": stash.get("display") or [],
            "job_ids": stash.get("job_ids") or [],
        })
        self._rewind_stash = None
        return {
            "ok": True,
            "display_count": len(self._display_transcript),
        }

    def clear_rewind_stash(self) -> None:
        self._rewind_stash = None

    def _render_history(self) -> str:
        """Flatten transcript into a single prompt for completion-style drivers."""
        lines = []
        for m in self._history:
            role = m["role"].upper()
            content = m.get("content") or ""
            if m.get("tool_calls"):
                tc_strs = []
                for tc in m["tool_calls"]:
                    func = tc.get("function") or {}
                    tc_strs.append(f"({func.get('name')} with arguments {func.get('arguments')})")
                if tc_strs:
                    content = (content + "\n" + "\n".join(tc_strs)).strip()
            elif m.get("role") == "tool":
                role = "USER"
                tc_id = m.get("tool_call_id") or ""
                content = f"(tool result for {tc_id}):\n{content}"
            lines.append(f"{role}: {content}")
        lines.append("ASSISTANT:")
        return "\n\n".join(lines)

    def _build_visible_tools_schema(self) -> list:
        mcp_tools = self._mcp.discovered_tools() if self._mcp else None
        return self._tool_catalog.visible_schema(
            mcp_tools=mcp_tools,
            no_delegation=getattr(self.config, "no_delegation", False),
            browser_enabled=getattr(self.config, "browser_enabled", True),
        )

    def get_context_usage(self) -> dict:
        import json
        budget = getattr(self.config, "max_context_tokens", 96000)
        
        system_content = self._history[0]["content"] if (self._history and self._history[0].get("role") == "system") else ""
        
        # Calculate skills text
        active_skills = getattr(self, "_skills", None)
        skills_text = ""
        if active_skills:
            active = active_skills.list("active")
            if active:
                skills_block = "\n\n".join(
                    f"## Skill: {s.name}\n{s.description}\n{s.body}" for s in active)
                skills_text = "\n\n# Learned skills (apply when relevant)\n" + skills_block
            
        # Calculate rules text
        rules_text_list = []
        active_rules = getattr(self, "_rules", None)
        if active_rules:
            active_r = active_rules.list("active")
            if active_r:
                rules_block = "\n".join(f"- {r.text}" for r in active_r)
                rules_text_list.append("\n\n# Standing rules (ALWAYS honor)\n" + rules_block)
        ws_rules = load_workspace_rules(self.config.repo)
        if ws_rules:
            rules_text_list.append(ws_rules)
        rules_text = "".join(rules_text_list)
        
        # Calculate token counts
        skills_tokens = len(skills_text) // 4
        rules_tokens = len(rules_text) // 4
        
        # System base: system_content minus skills_text and rules_text
        system_base_text = system_content
        if skills_text and skills_text in system_base_text:
            system_base_text = system_base_text.replace(skills_text, "")
        if rules_text and rules_text in system_base_text:
            system_base_text = system_base_text.replace(rules_text, "")
        system_prompt_tokens = len(system_base_text) // 4
        
        # MCP section
        mcp_tokens = 0
        mcp_section = _format_mcp_tools_section(
            self._mcp,
            self._tool_catalog,
            no_delegation=getattr(self.config, "no_delegation", False),
            browser_enabled=getattr(self.config, "browser_enabled", True),
        )
        if mcp_section:
            mcp_tokens = len("\n\n" + mcp_section) // 4
            
        # Tool definitions section
        tools_schema = self._build_visible_tools_schema()
        serialized_tools = json.dumps(tools_schema)
        tool_definitions_tokens = len(serialized_tools) // 4
        
        # Summarized conversation vs Conversation
        summarized_tokens = 0
        conversation_tokens = 0
        for m in self._history[1:]:
            msg_tokens = self._estimate_context_tokens_for_list([m])
            if m.get("_compressed_summary"):
                summarized_tokens += msg_tokens
            else:
                conversation_tokens += msg_tokens
                
        heuristic_total = (
            system_prompt_tokens +
            tool_definitions_tokens +
            rules_tokens +
            skills_tokens +
            mcp_tokens +
            summarized_tokens +
            conversation_tokens
        )
        # Prefer the driver's REAL last prompt-token count so the composer's
        # context-usage % matches the actual billed context, not a chars//4
        # estimate. Mirror _estimate_context_tokens: take the greater of the
        # real number and the heuristic so we never under-report usage.
        real_total = int(getattr(self, "_last_prompt_tokens", 0) or 0)
        total_tokens = max(real_total, heuristic_total) if real_total > 0 else heuristic_total
        
        categories = [
            {"name": "System prompt", "tokens": system_prompt_tokens},
            {"name": "Tool definitions", "tokens": tool_definitions_tokens},
            {"name": "Rules", "tokens": rules_tokens},
            {"name": "Skills", "tokens": skills_tokens},
            {"name": "MCP", "tokens": mcp_tokens},
            {"name": "Subagent", "tokens": 0},
            {"name": "Summarized conversation", "tokens": summarized_tokens},
            {"name": "Conversation", "tokens": conversation_tokens}
        ]
        
        return {
            "total": total_tokens,
            "limit": budget,
            "categories": categories,
            **self._tool_output_savings_fields(),
            **self._wiki_grounding_fields(),
            **self._history_compaction_fields(),
            **self._spill_usage_fields(),
            **self._turn_budget_usage_fields(),
            **self._append_only_usage_fields(),
        }

    def _append_only_usage_fields(self) -> dict:
        try:
            if not self._resolve_append_only():
                return {}
            return {
                "append_only_context": True,
                "prefix_stable_turns": int(self._prefix_stable_turns or 0),
            }
        except Exception:
            return {}

    def _turn_budget_system_note(self) -> str:
        try:
            if not self._turn_budget:
                return ""
            total = int(self._turn_budget.get("total") or 0)
            if total <= 0:
                return ""
            return f"output budget for this turn: {total} tokens"
        except Exception:
            return ""

    def _pilot_identity_system_note(self) -> str:
        """Authoritative pilot/model id for identity questions.

        ChatGPT / Claude deployments often refuse to name themselves. Marionette
        chose the model for this request — inject that fact so "what model are
        you?" is answerable instead of a hedged "I don't know".
        """
        try:
            spec = str(getattr(self.config, "driver", "") or "").strip()
            if not spec:
                return ""
            api_model = ""
            try:
                api_model = str(getattr(self.pilot, "model", "") or "").strip()
            except Exception:
                api_model = ""
            picker_model = spec.split(":", 1)[1] if ":" in spec else spec
            model_id = api_model or picker_model
            friendly = _friendly_pilot_model_name(model_id)
            lines = [
                "PILOT IDENTITY (authoritative for this Marionette session — "
                "re-read every turn):",
                f"- Configured pilot: {spec}",
                f"- API model id sent on the wire: {model_id}",
            ]
            if friendly and friendly.lower() not in model_id.lower():
                lines.append(f"- Common name: {friendly}")
            lines.append(
                "When the user asks which model / deployment you are, answer with "
                "the API model id"
                + (f" (also known as {friendly})" if friendly else "")
                + ". Do not claim you lack access to the deployment name — "
                "Marionette selected this model for the request."
            )
            return "\n".join(lines)
        except Exception:
            return ""

    def _turn_budget_exhausted(self) -> bool:
        try:
            if not self._turn_budget or not self._turn_budget.get("hard"):
                return False
            total = int(self._turn_budget.get("total") or 0)
            if total <= 0:
                return False
            return self._turn_output_tokens > total
        except Exception:
            return False

    def _turn_budget_usage_fields(self) -> dict:
        try:
            fields: dict = {}
            if self._turn_budget:
                fields["turn_budget_total"] = int(self._turn_budget.get("total") or 0)
                fields["turn_budget_hard"] = bool(self._turn_budget.get("hard"))
            fields["turn_output_tokens"] = int(self._turn_output_tokens or 0)
            if self._turn_budget_exhausted():
                fields["turn_budget_exhausted"] = True
            return fields
        except Exception:
            return {}

    _TURN_CONTEXT_TRAILER = "\n\n[context for this turn]\n"

    def _resolve_append_only(self) -> bool:
        if self._append_only is not None:
            return self._append_only
        try:
            driver_name = str(getattr(self.config, "driver", "") or "")
            base_url = str(getattr(self.pilot, "base_url", "") or "")
            self._append_only = self._turn_economy.resolve_append_only(
                base_url, driver_name
            )
        except Exception:
            self._append_only = False
        return self._append_only

    def _reset_append_only_freeze(self) -> None:
        self._frozen_system_prompt = None
        self._last_rendered_prompt = ""
        self._prefix_stable_turns = 0

    def _build_turn_cg_section(self, user_message: str) -> str:
        cg_section = ""
        _no_deleg = getattr(self.config, "no_delegation", False)
        if not self.config.repo or _no_deleg:
            return cg_section
        try:
            from puppetmaster.codegraph import codegraph_context, codegraph_prompt_section

            cg_slice = codegraph_context(task=user_message, cwd=self.config.repo)
            if cg_slice:
                authoritative = (
                    "CODEGRAPH HAS ALREADY BEEN QUERIED FOR THIS TASK. The relevant "
                    "symbols, definitions, and code are provided in the section below. "
                    "USE THIS as your primary source. Do NOT re-read entire files that "
                    "already appear here -- only read_file specific additional lines you "
                    "still need (with start_line + limit), or call search_codegraph to "
                    "widen the graph. Whole-file dumps when the answer is already below "
                    "are wasteful and wrong.\n"
                )
                cg_section = authoritative + codegraph_prompt_section(cg_slice)
            self._cg_cache_key = user_message
            self._cg_cache_section = cg_section
            self._cg_cache_symbols = cg_slice.count("- **") + cg_slice.count("#### ") if cg_slice else 0
        except Exception:
            pass
        return cg_section

    def _append_turn_context_trailer(self, message: str, user_message: str) -> str:
        try:
            parts = []
            cg_section = self._build_turn_cg_section(user_message)
            if cg_section:
                parts.append(cg_section)
            wiki_section = self._build_turn_wiki_section(user_message)
            if wiki_section:
                parts.append(wiki_section)
            turn_note = self._turn_budget_system_note()
            if turn_note:
                parts.append(turn_note)
            # Pilot identity stays in the frozen system prompt for append-only
            # (not the per-turn trailer) so the rendered prefix stays stable.
            if not parts:
                return message
            return message + self._TURN_CONTEXT_TRAILER + "\n\n".join(parts)
        except Exception:
            return message

    def _ensure_frozen_system_prompt(self, base_sys: str) -> str:
        if self._frozen_system_prompt is not None:
            # Re-sync history after send()'s finally may have briefly restored
            # the pre-freeze base between turns.
            if self._history and self._history[0].get("role") == "system":
                self._history[0]["content"] = self._frozen_system_prompt
            return self._frozen_system_prompt
        try:
            sys_prompt = base_sys
            identity_note = self._pilot_identity_system_note()
            if identity_note:
                sys_prompt += "\n\n" + identity_note
            mcp_section = _format_mcp_tools_section(
                self._mcp,
                self._tool_catalog,
                no_delegation=getattr(self.config, "no_delegation", False),
                browser_enabled=getattr(self.config, "browser_enabled", True),
            )
            if mcp_section:
                sys_prompt += "\n\n" + mcp_section
            self._frozen_system_prompt = sys_prompt
            if self._history and self._history[0].get("role") == "system":
                self._history[0]["content"] = self._frozen_system_prompt
        except Exception:
            self._frozen_system_prompt = base_sys
        return self._frozen_system_prompt or base_sys

    def _record_prompt_stability(self, prompt: str) -> None:
        try:
            if self._last_rendered_prompt and prompt.startswith(self._last_rendered_prompt):
                self._prefix_stable_turns += 1
            self._last_rendered_prompt = prompt
        except Exception:
            pass

    def _spill_usage_fields(self) -> dict:
        try:
            return self._turn_economy.spill_usage_fields()
        except Exception:
            return {"spill_count": 0, "spill_chars": 0}

    def _tool_output_savings_fields(self) -> dict:
        """Compact tool-output savings for context/usage APIs."""
        try:
            from pmharness.registry import resolve_price

            price_in, _ = resolve_price(self.config.driver)
            # Empty harness session id => unscoped summarize (prior behavior).
            return self._turn_economy.tool_output_savings_fields(
                price_in,
                session_id=self.harness_session_id or "",
            )
        except Exception:
            return {
                "tool_output_tokens_saved": 0,
                "tool_output_savings_usd": 0.0,
                "tool_output_compactions": 0,
            }

    @property
    def _state_dir_or_tempdir(self) -> str:
        import tempfile
        return getattr(self, "state_dir", None) or tempfile.gettempdir()

    @property
    def _turn_economy(self):
        """Session-scoped TurnEconomy bound to current state/session/job ids."""
        from harness.turn_economy import TurnEconomy

        return TurnEconomy(
            state_dir=self._state_dir_or_tempdir,
            session_id=self.harness_session_id or "default",
            job_id=self.savings_job_id or None,
            config=self.context_budget_config,
        )

    @staticmethod
    def _interruption_stub(tool_call_id: str) -> dict:
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": "(no result: the previous action was interrupted before it completed)",
        }

    def _replace_stub_tool_result(self, tool_call_id: str, msg: dict) -> bool:
        """Replace an interruption stub for ``tool_call_id`` with ``msg``.

        Returns True when a stub was replaced (or a real result already exists
        and the append should be skipped). Returns False when the caller should
        append ``msg`` normally.
        """
        if not tool_call_id:
            return False
        for i in range(len(self._history) - 1, -1, -1):
            existing = self._history[i]
            if existing.get("role") != "tool":
                continue
            if existing.get("tool_call_id") != tool_call_id:
                continue
            if _is_stub_tool_result(existing):
                self._history[i] = msg
                self._invalidate_ctx_cache()
                return True
            # A real result already answers this id — drop the duplicate.
            return True
        return False

    def _sanitize_tool_pairs(self) -> None:
        """Guarantee every assistant tool_call has a matching tool result before
        the next model request. Anthropic 400s ("tool_use ids were found without
        tool_result blocks immediately after") if an assistant message carries
        tool_calls that are never answered -- which happens when the action loop
        is cut short mid-spree (cancel, steer, a worker hitting its step ceiling,
        an exception). For any dangling tool_call id, synthesize a stub tool
        result so history stays valid. Idempotent and cheap; safe to call before
        every request.

        Adjacency (not mere presence) is enforced: Anthropic requires each
        assistant tool_use to be answered by tool_result block(s) IMMEDIATELY
        after that assistant message. A steer or any other message wedged
        between the assistant tool_use and its tool result breaks the
        "immediately after" rule even if the result appears later in history.
        So for each assistant message with tool_calls we consume ONLY the
        contiguous run of tool-role messages that directly follow it; any
        tool_call id not present in that adjacent run gets a stub tool result
        inserted right after the run.

        Uniqueness is enforced too: Anthropic also 400s ("each tool_use must
        have a single result. Found multiple tool_result blocks with id: ...")
        when one tool_use id is answered more than once -- which happens when an
        interrupted turn synthesized a stub result and the real result was later
        appended anyway (crash-resume, steer races). Within each adjacent run we
        keep only the FIRST result per id and drop the rest. Results whose id
        matches no tool_call on the preceding assistant message are orphans
        (equally rejected by the API) and are dropped as well."""
        history = self._history
        out = []
        i = 0
        n = len(history)
        while i < n:
            m = history[i]
            if m.get("role") == "tool":
                # A tool result with no adjacent assistant tool_use (the run
                # consumer below eats all valid ones). Anthropic rejects it in
                # any position, so recast it as plain user content instead of
                # dropping what may be real output.
                out.append({
                    "role": "user",
                    "content": "(recovered tool output)\n" + str(m.get("content") or ""),
                })
                i += 1
                continue
            out.append(m)
            if m.get("role") == "assistant" and m.get("tool_calls"):
                # Consume the contiguous run of tool-role messages right after
                # this assistant message. Keep one result per expected id (the
                # first); drop duplicates and orphans -- both are API-rejected.
                expected_ids = {tc.get("id") for tc in (m.get("tool_calls") or []) if tc.get("id")}
                j = i + 1
                kept_at: dict = {}  # tool_call_id -> index in `out` of the kept result
                while j < n and history[j].get("role") == "tool":
                    tmsg = history[j]
                    tcid = tmsg.get("tool_call_id")
                    if not tcid:
                        # No id to pair on -- leave it rather than lose content.
                        out.append(tmsg)
                    elif tcid in expected_ids:
                        if tcid not in kept_at:
                            kept_at[tcid] = len(out)
                            out.append(tmsg)
                        elif _is_stub_tool_result(out[kept_at[tcid]]) and not _is_stub_tool_result(tmsg):
                            # The kept copy is an interruption stub and a real
                            # result arrived later (crash-resume race): the real
                            # one wins.
                            out[kept_at[tcid]] = tmsg
                        # else: genuine duplicate -- drop it.
                    # else: orphan result for an id this assistant message never
                    # issued -- equally API-rejected, drop it.
                    j += 1
                run_ids = set(kept_at)
                # For any tool_call id NOT answered in that adjacent run, insert
                # a stub tool result immediately after the run.
                for tc in m.get("tool_calls") or []:
                    tcid = tc.get("id")
                    if tcid and tcid not in run_ids:
                        out.append(self._interruption_stub(tcid))
                        run_ids.add(tcid)
                i = j
                continue
            i += 1
        self._history = out
        # In-place rebuild MAY leave len(self._history) unchanged (dropping
        # zero orphans while inserting zero stubs) -- the len-keyed cache
        # would then stale-read. Explicitly invalidate.
        self._invalidate_ctx_cache()

    def _messages_for_provider(self) -> list:
        """Heal tool_use/tool_result pairs, then return the elided non-system
        history for an outbound provider chat/chat_stream call.

        Call this immediately before every provider dispatch so interactive
        send, streaming, resume, and steer-after-interrupt share one seam.
        """
        self._sanitize_tool_pairs()
        return self._elide_stale_reads(self._history[1:])

    def _grounded_wiki_answer(self, question: str, raw: str) -> str:
        """Grounded synthesis over a raw wiki query result.

        Wraps harness.nl_memory.answer_from_memory: builds a small candidate
        entry list from the wiki blob (parsing the "Wiki search results:" list
        format when present, else a single-entry fallback around the whole
        blob), then asks the current pilot to produce a cited, concise answer.

        Returns the grounded answer text (including a compact citations line)
        when synthesis succeeds and is not the not-found sentinel; returns an
        empty string in ALL guard cases so the caller falls back to the raw
        result. This method never raises: any exception -> "" .
        """
        try:
            question = (question or "").strip()
            raw_text = (raw or "").strip()
            if not question or not raw_text:
                return ""
            # Skip synthesis for degenerate/error blobs the wiki returns as
            # prose -- grounding on a "not configured" message is noise.
            low = raw_text.lower()
            if low.startswith("wiki not configured") or low.startswith("wiki query failed"):
                return ""

            entries: list[dict] = []
            # Best-effort parse of the /wiki/search fallback format the client
            # renders as "Wiki search results:\n- title (slug): snippet". Each
            # bulleted line becomes a candidate entry.
            if raw_text.startswith("Wiki search results:"):
                for line in raw_text.splitlines()[1:]:
                    s = line.strip()
                    if not s.startswith("- "):
                        continue
                    s = s[2:].strip()
                    # Split "title (slug): snippet"
                    title = s
                    slug = ""
                    body = ""
                    if ":" in s:
                        head, _, rest = s.partition(":")
                        head = head.strip()
                        body = rest.strip()
                        if head.endswith(")") and "(" in head:
                            open_i = head.rfind("(")
                            title = head[:open_i].strip() or head
                            slug = head[open_i + 1:-1].strip()
                        else:
                            title = head
                    src = f"wiki/{slug}" if slug else "wiki"
                    if title or body:
                        entries.append({"title": title or "wiki", "body": body or title, "source": src})
            if not entries:
                # Fallback: wrap the whole blob as a single entry so nl_memory
                # still has something to ground on.
                entries.append({"title": question or "wiki", "body": raw_text, "source": "wiki"})

            # Build a small `complete(prompt)->str` closure over the existing
            # pilot's chat/complete surface. No streaming, no tools, single call.
            def _complete(prompt: str) -> str:
                pilot = getattr(self, "pilot", None)
                if pilot is None:
                    raise RuntimeError("no pilot available for grounded synthesis")
                sysmsg = (
                    "You are answering ONLY from the provided context entries. "
                    "Be concise. Cite entries by number as [n]."
                )
                if hasattr(pilot, "chat"):
                    resp = pilot.chat([{"role": "user", "content": prompt}], system=sysmsg)
                else:
                    resp = pilot.complete(prompt, system=sysmsg)
                if resp is None:
                    raise RuntimeError("empty pilot response")
                if getattr(resp, "error", None):
                    raise RuntimeError(f"pilot error: {resp.error}")
                text = getattr(resp, "text", None)
                return text or ""

            from .nl_memory import answer_from_memory, NOT_FOUND
            try:
                out = answer_from_memory(question, entries, complete=_complete)
            except Exception:
                return ""
            if not isinstance(out, dict):
                return ""
            ans = (out.get("answer") or "").strip()
            if not ans or ans == NOT_FOUND:
                return ""
            citations = out.get("citations") or []
            used = out.get("used_entry_ids") or []
            if citations:
                # Compact "[1] Title, [2] Title" citation trailer -- keeps the
                # cited entry titles visible without dumping full bodies.
                pairs = []
                for i, n in enumerate(citations):
                    label = used[i] if i < len(used) else f"entry-{n}"
                    pairs.append(f"[{n}] {label}")
                return f"{ans}\nCitations: " + ", ".join(pairs)
            return ans
        except Exception:
            return ""

    def _append_action_result(
        self, act: Any, aid: str, content: str, is_native: bool, *, ok: bool = True,
    ) -> None:
        tc_id = getattr(act, "tool_call_id", None) or aid
        clamped_content = self._turn_economy.persist_tool_result(content, tc_id)
        # Tag full-file reads with their path so the pre-send pass can elide an
        # EARLIER read of the same file once a newer read supersedes it (the
        # stale copy still costs tokens every turn otherwise). Only whole-file
        # reads (no start_line/limit) are safe to elide -- a ranged read is a
        # distinct slice the model may still need.
        read_path = None
        try:
            if (getattr(act, "kind", "") == "read_file"
                    and getattr(act, "path", None)
                    and getattr(act, "start_line", None) is None
                    and getattr(act, "limit", None) is None):
                read_path = str(act.path)
        except Exception:
            read_path = None

        if is_native:
            msg = {"role": "tool", "tool_call_id": tc_id, "content": clamped_content}
            if read_path:
                msg["_read_path"] = read_path
            # Crash/resume race: an interruption stub may already answer this
            # id (export/load/sanitize after cancel). Prefer the real result
            # in place so we never emit duplicate tool_result rows for one id.
            if not self._replace_stub_tool_result(tc_id, msg):
                self._history.append(msg)
        else:
            msg = {"role": "user", "content": clamped_content}
            if read_path:
                msg["_read_path"] = read_path
            self._history.append(msg)

        # Loop-guard cache: remember successful results so identical repeats
        # within the turn can replay instead of re-executing (token bleed).
        # Skip suppress/redirect/error-shaped content even if ok was left True.
        if ok:
            try:
                from .pilot_guards import record_successful_result
                gs = getattr(self, "_turn_guard_state", None)
                kind = getattr(act, "kind", "") or ""
                head = (clamped_content or "")[:24]
                if (
                    gs is not None
                    and kind
                    and not is_invalid_action(act)
                    and not head.startswith("(SUPPRESSED")
                    and not head.startswith("(REDIRECT")
                    and " failed:" not in (clamped_content or "")[:120]
                ):
                    record_successful_result(gs, kind, act, clamped_content)
            except Exception:
                pass

    def _read_allowed_roots(self) -> list:
        """Roots read_file may read from: the open workspace, its git toplevel
        when the workspace is nested inside a larger clone, plus the app's own
        results-spill dir. Oversized tool outputs (a big web_fetch, a long
        command) are persisted to {state_dir}/pmharness-results/<id>.txt and the
        model is explicitly told to read them back with read_file. That dir lives
        outside the workspace (a temp pilot-XXXX dir when no state_dir is set), so
        without whitelisting it every such read was rejected as path traversal --
        the pilot was told to read a file it was then refused, and stranded. Only
        reads get these extra roots; writes/edits stay workspace-confined."""
        roots = []
        if self.config.repo:
            roots.append(self.config.repo)
            try:
                toplevel = git_toplevel(self.config.repo)
            except Exception:
                toplevel = None
            if toplevel and not any(
                path_within(toplevel, root, allow_equal=True) for root in roots
            ):
                # Workspace is a subdirectory of the clone (e.g. addon under a
                # monorepo). Allow reading siblings / parent README under the
                # git root; true escapes outside toplevel + spill still fail.
                roots.append(toplevel)
        try:
            spill_root = os.path.join(
                os.path.abspath(self._state_dir_or_tempdir), "pmharness-results"
            )
            roots.append(spill_root)
        except Exception:
            pass
        return roots

    def release_warm_acp(
        self, *, reason: str = "close", cwd: Optional[str] = None
    ) -> None:
        """Best-effort close/reap of an owned Cursor warm ACP session.

        Ownership reasons: ``session_switch``, ``workspace``, ``interrupt``,
        ``shutdown``. ``cwd`` is the live workspace root for workspace retargets.
        No-op when the pilot is not a warm ACP driver. Never raises.
        """
        try:
            from pmharness.drivers.cursor_acp import release_owned_warm_acp

            release_owned_warm_acp(self, reason=reason, cwd=cwd)
        except Exception:
            pass

    def cancel(self) -> None:
        """Signal any in-flight run_auto/send to stop at the next checkpoint."""
        self._cancel.set()
        # interrupt()/_cancel: best-effort -- on interrupt, set a flag so completed-but-unfolded
        # swarm results are still delivered but no NEW swarm work is started.
        # There is a small gap where background swarm futures already submitted to self._swarm_pool
        # cannot be forcefully aborted immediately since Python threads cannot be killed, but they will
        # exit when they check self._cancel or finish subprocess await, and we won't start new swarm work.
        self._interrupted_swarms = True

    def _humanize_pilot_error(self, raw: str) -> str:
        """Turn a raw provider error into a clear, actionable one-liner.

        Users should NEVER see raw provider JSON. We classify the error (via the
        shared error_classifier) and map each class to plain guidance + the fix,
        keeping a short '[provider said: ...]' tail for the technically curious.
        The model-not-available case is detected specifically because it is the
        most common confusing one (a model the user's key/plan doesn't include).
        """
        s = str(raw or "").strip()
        if not s:
            return "pilot: the request failed with no detail. Try again."
        try:
            from harness.api.redaction import redact_api_secrets

            s = str(redact_api_secrets(s) or s)
        except Exception:
            pass
        low = s.lower()
        model = getattr(self, "config", None) and getattr(self.config, "driver", "") or ""
        # Bound + redacted: never echo raw provider JSON / token-ish fragments.
        tail = f" [provider said: {s[:160]}]"

        # Pull an HTTP status out of the string when present ("HTTP 429: ...").
        import re as _re
        m = _re.search(r"\b(4\d\d|5\d\d)\b", s)
        status = int(m.group(1)) if m else None
        try:
            from pmharness.drivers import error_classifier as _ec
            cls = _ec.classify(status, s)
        except Exception:
            cls = None

        # Model not available / not authorized for this key or plan -- checked
        # first because the classifier lumps it under FATAL/404 but it deserves
        # its own specific, common-case fix.
        model_signals = ("does not exist", "no access", "not authorized",
                         "not permitted", "invalid model", "does not have access",
                         "model not found", "unknown model", "unavailable")
        if ("model" in low and any(sig in low for sig in model_signals)) or \
           (status == 404 and "model" in low):
            mtxt = f" '{model}'" if model else ""
            return (f"pilot: the selected model{mtxt} isn't available on your current "
                    f"API key or plan. Switch to a model your key includes (model "
                    f"picker in the bottom bar), or add a key that grants access." + tail)

        if cls is not None:
            EC = _ec.ErrorClass
            if cls == EC.AUTH:
                # No provider tail — AUTH bodies often echo key material.
                return ("pilot: your API key was rejected (authentication failed). "
                        "Check the key for this provider in Settings > Providers.")
            if cls == EC.RATE_LIMIT:
                return ("pilot: the provider is rate-limiting your key (too many "
                        "requests, or you hit a quota). Wait a moment and retry, or "
                        "switch to another provider/model in the bottom bar." + tail)
            if cls == EC.CONTEXT_OVERFLOW:
                return ("pilot: this turn exceeded the model's context window. Try "
                        "/compact to shrink history, start a fresh session, or pick a "
                        "longer-context model." + tail)
            if cls == EC.RETRYABLE:
                return ("pilot: the provider had a transient error (server/network). "
                        "It usually clears on a retry -- send again in a moment." + tail)

        # Credit/quota exhaustion is a frequent, confusing FATAL case.
        if any(k in low for k in ("insufficient", "quota", "billing", "credit",
                                  "payment", "exceeded your current")):
            return ("pilot: the provider reports insufficient credit/quota on this "
                    "key. Top up or switch to a provider/model with available "
                    "budget." + tail)

        return f"pilot: {s}"

    def _accumulate_session_meters(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        estimated_cost_usd: float = 0.0,
    ) -> None:
        """Persist cumulative usage on the active harness session (distinct from
        the boot-scoped pricing pill in /api/usage)."""
        sid = self.harness_session_id or ""
        store = getattr(self, "_session_store", None)
        if sid and store is not None:
            try:
                store.accumulate_meters(
                    sid,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read_tokens,
                    estimated_cost_usd=estimated_cost_usd,
                )
            except Exception:
                pass

    def _job_dispatch_label_args(self) -> list:
        from harness.job_scoping import job_label_for_session

        label = job_label_for_session(self.harness_session_id or "")
        return ["--label", label] if label else []

    def _attribute_worker_cost(self, tokens_in: int, tokens_out: int,
                               real_cost_usd: float = 0.0,
                               model_spec: str = "",
                               count_dollars: bool = True) -> None:
        """Record a delegated worker's spend as DOLLARS at the worker's OWN model
        rate (plus a parallel token split), so the session cost is correct even
        when the worker ran on a pricier/cheaper model than the pilot.

        If the worker already reported a real cost (from a routing artifact /
        job est_cost_usd), use it verbatim. Otherwise derive it from the
        worker's own model rate via resolve_price(model_spec), falling back to
        the pilot's driver rate if that model is unknown. Never raises. Always
        records the token split so server.py can subtract worker tokens from the
        pilot-priced portion (no double counting).

        ``count_dollars=False`` records ONLY the token split. Use it for
        swarm-store jobs, whose dollars /api/usage already computes
        authoritatively from the job's own usage artifacts x the model
        registry -- adding dollars here too would bill the same job twice."""
        try:
            ti = int(tokens_in or 0)
            to = int(tokens_out or 0)
            self._worker_tokens_in += ti
            self._worker_tokens_out += to
            cost = float(real_cost_usd or 0.0)
            if count_dollars:
                if cost <= 0.0:
                    spec = model_spec or getattr(self.config, "swarm_adapter", "") \
                        or getattr(self.config, "driver", "")
                    try:
                        from pmharness.registry import resolve_price
                        price_in, price_out = resolve_price(spec)
                    except Exception:
                        try:
                            from pmharness.registry import resolve_price
                            price_in, price_out = resolve_price(getattr(self.config, "driver", ""))
                        except Exception:
                            price_in, price_out = 0.0, 0.0
                    cost = (ti * float(price_in) + to * float(price_out)) / 1_000_000.0
                self._worker_cost_usd += max(0.0, cost)
            else:
                cost = 0.0
            if ti or to:
                self._accumulate_session_meters(
                    input_tokens=ti,
                    output_tokens=to,
                    estimated_cost_usd=cost if count_dollars else 0.0,
                )
        except Exception:
            pass

    def _add_worker_tokens_from_artifacts(self, artifacts_json: Any) -> tuple[int, int, int]:
        """Extracts token counts from worker job artifacts defensively, summing
        tokens_in/out (and tokens_cached) while deduping across the same
        task_id to avoid double-counting.

        Returns (sum_in, sum_out, sum_cached). tokens_cached is a subset of
        tokens_in and is NOT added to self._tokens_used -- it only feeds
        self._tokens_cached so the parent session's cache_savings_usd
        (server.py._cache_savings) reflects worker/swarm cache reads.
        """
        if isinstance(artifacts_json, str):
            try:
                import json
                artifacts = json.loads(artifacts_json)
            except Exception:
                return (0, 0, 0)
        elif isinstance(artifacts_json, list):
            artifacts = artifacts_json
        else:
            return (0, 0, 0)

        task_map = {}
        no_task_seen = set()

        for art in artifacts:
            if not isinstance(art, dict):
                continue
            payload = art.get("payload")
            if not isinstance(payload, dict):
                payload = {}

            task_id = art.get("task_id") or payload.get("task_id")

            tokens_in = art.get("tokens_in")
            if tokens_in is None:
                tokens_in = payload.get("tokens_in")
            tokens_out = art.get("tokens_out")
            if tokens_out is None:
                tokens_out = payload.get("tokens_out")
            tokens_cached = art.get("tokens_cached")
            if tokens_cached is None:
                tokens_cached = payload.get("tokens_cached")

            t_in = 0
            if tokens_in is not None:
                try:
                    t_in = int(tokens_in)
                except (ValueError, TypeError):
                    t_in = 0
            t_out = 0
            if tokens_out is not None:
                try:
                    t_out = int(tokens_out)
                except (ValueError, TypeError):
                    t_out = 0
            t_cached = 0
            if tokens_cached is not None:
                try:
                    t_cached = int(tokens_cached)
                except (ValueError, TypeError):
                    t_cached = 0

            if t_in == 0 and t_out == 0 and t_cached == 0:
                continue

            if task_id:
                if task_id in task_map:
                    old_in, old_out, old_cached = task_map[task_id]
                    task_map[task_id] = (
                        max(old_in, t_in),
                        max(old_out, t_out),
                        max(old_cached, t_cached),
                    )
                else:
                    task_map[task_id] = (t_in, t_out, t_cached)
            else:
                no_task_seen.add((t_in, t_out, t_cached))

        sum_in = 0
        sum_out = 0
        sum_cached = 0
        for t_in, t_out, t_cached in task_map.values():
            sum_in += t_in
            sum_out += t_out
            sum_cached += t_cached
        for t_in, t_out, t_cached in no_task_seen:
            sum_in += t_in
            sum_out += t_out
            sum_cached += t_cached

        self._tokens_used += sum_in + sum_out
        # tokens_cached is a subset of tokens_in already counted above; only
        # feed the cache-savings meter, do NOT add to _tokens_used.
        self._tokens_cached += sum_cached
        # Record the worker token split so server.py excludes these tokens from
        # the pilot-priced portion -- but attribute NO dollars. These artifacts
        # come from swarm-store jobs, which /api/usage prices authoritatively
        # from their own usage artifacts x the model registry (swarm_cost).
        # Attributing dollars here too billed every awaited swarm twice, and at
        # the PILOT's model rate (resolve_price cannot price adapter names like
        # 'agentic'), so cheap-model workers were charged at e.g. opus rates.
        self._attribute_worker_cost(sum_in, sum_out, count_dollars=False)
        if sum_cached:
            self._accumulate_session_meters(cache_read_tokens=sum_cached)
        return (sum_in, sum_out, sum_cached)

    def _apply_worker_patch(self, artifacts: list, job_id: str = "") -> tuple[bool, list[str], str]:
        """Finds the patch artifact (type=="patch"), extracts its unified_diff,
        and applies it cleanly/idempotently via git apply to the effective git
        checkout for ``self.config.repo`` (Home parent → single git child).
        Returns (applied_bool, files_changed, message). Checkpoint id (if any) is stashed on self._last_checkpoint_id.

        Resolution is per-operation only — never mutates ``config.repo``.
        """
        import os
        import tempfile
        import subprocess

        from .repo_resolve import resolve_effective_repo

        if not self.config.repo or not os.path.exists(self.config.repo):
            self._last_checkpoint_id = None
            return False, [], "no workspace directory (config.repo) is open"

        # Home / workspace root may not be a git checkout; workers already
        # dispatch into the resolved child — apply must use the same target.
        repo_root = resolve_effective_repo(self.config.repo)
        if not repo_root or not os.path.exists(repo_root):
            self._last_checkpoint_id = None
            return False, [], "no workspace directory (config.repo) is open"

        # Check if the directory is a git repo
        try:
            p_check = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=repo_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            if p_check.returncode != 0:
                self._last_checkpoint_id = None
                return False, [], (
                    f"not a git repository: {repo_root}. "
                    "Open the project checkout or ensure Home has a marionette child."
                )
        except Exception as e:
            self._last_checkpoint_id = None
            return False, [], f"failed to check git repo: {e}"

        patch_art = next((a for a in artifacts if isinstance(a, dict) and a.get("type") == "patch"), None)
        if not patch_art:
            self._last_checkpoint_id = None
            return False, [], "no patch to apply"

        payload = patch_art.get("payload") or {}
        diff_text = payload.get("unified_diff") or ""
        if not diff_text.strip():
            self._last_checkpoint_id = None
            return False, [], "no patch to apply"

        files = payload.get("files") or []

        # Write diff to a temporary file. Binary mode: Windows text mode would
        # rewrite \n as \r\n, corrupting the unified diff before git apply sees it.
        fd, temp_path = tempfile.mkstemp(suffix=".patch")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(diff_text.encode("utf-8"))
            
            # a. First check if already applied (idempotent): git apply --reverse --check on the diff
            rev_p = subprocess.run(
                ["git", "apply", "--reverse", "--check", temp_path],
                cwd=repo_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            if rev_p.returncode == 0:
                self._last_checkpoint_id = None
                return True, files, "already applied"

            # Take checkpoint before applying patch (bind to effective checkout
            # when the session store still points at a non-git Home parent).
            checkpoint_id = None
            try:
                label_suffix = f" {job_id}" if job_id else ""
                cp_store = self._checkpoints
                try:
                    bound = getattr(cp_store, "repo", None) or ""
                    enabled = bool(getattr(cp_store, "_enabled", False))
                    same = (
                        bound
                        and os.path.realpath(bound) == os.path.realpath(repo_root)
                    )
                except Exception:
                    enabled, same = False, False
                if not (enabled and same):
                    cp_store = CheckpointStore(
                        repo_root,
                        session_id=self.harness_session_id or None,
                    )
                checkpoint_id = cp_store.snapshot(
                    label=f"Before swarm patch{label_suffix}".strip(),
                    trigger="swarm_patch",
                    session_id=self.harness_session_id or None,
                )
            except Exception as cp_err:
                import sys
                print(f"Checkpoint error during swarm patch: {cp_err}", file=sys.stderr)

            # b. Else git apply --check to verify it applies cleanly
            check_p = subprocess.run(
                ["git", "apply", "--check", temp_path],
                cwd=repo_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            if check_p.returncode == 0:
                # It applies cleanly, so apply it!
                apply_p = subprocess.run(
                    ["git", "apply", temp_path],
                    cwd=repo_root,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )
                if apply_p.returncode == 0:
                    self._last_checkpoint_id = checkpoint_id
                    return True, files, "applied cleanly"
                else:
                    err_msg = apply_p.stderr.strip() or "git apply failed"
                    self._last_checkpoint_id = checkpoint_id
                    return False, files, f"git apply failed: {err_msg}"
            else:
                # c. --check failed (context drift / partial overlap). Try a REAL
                # 3-way merge. git apply --3way can leave conflict markers in the
                # working tree AND return non-zero on a genuine conflict, so we
                # snapshot every target file first and restore it verbatim if the
                # merge does not apply. An autonomous re-derive must read clean
                # source -- never marker-polluted files -- and we must never
                # clobber an earlier worker's already-landed (unstaged) edit.
                pre_apply_bytes = {}
                for rel_path in files:
                    abs_path = os.path.join(repo_root, rel_path)
                    try:
                        with open(abs_path, "rb") as snap_f:
                            pre_apply_bytes[rel_path] = snap_f.read()
                    except OSError:
                        pre_apply_bytes[rel_path] = None  # absent pre-apply

                three_way_p = subprocess.run(
                    ["git", "apply", "--3way", temp_path],
                    cwd=repo_root,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )
                if three_way_p.returncode == 0:
                    self._last_checkpoint_id = checkpoint_id
                    return True, files, "applied with 3way merge"

                # --3way failed: a genuine content conflict. Restore every target
                # file to its exact pre-apply bytes so no conflict markers or
                # half-applied hunks survive for the re-derive.
                for rel_path, original in pre_apply_bytes.items():
                    abs_path = os.path.join(repo_root, rel_path)
                    try:
                        if original is None:
                            if os.path.exists(abs_path):
                                os.remove(abs_path)
                        else:
                            with open(abs_path, "wb") as restore_f:
                                restore_f.write(original)
                    except OSError:
                        pass

                # d. --3way is the terminal apply tier -- and it is a REAL 3-way
                # merge, not context-guessing: our worker patches are produced by
                # `git diff` (finalize_worktree_patch), so each hunk carries the
                # `index <blob>..<blob>` ancestor SHAs. git reconstructs the base
                # blob and merges onto the CURRENT tree, so disjoint hunks land
                # cleanly even after HEAD moved under a parallel wave, and only a
                # genuine content conflict fails here.
                #
                # We deliberately do NOT fall back to a lenient
                # `--recount -C1` force. That tier discarded context matching and
                # could land an intact hunk in the WRONG place after the tree
                # moved, while reporting success ("reduced-context 3way merge") --
                # a silent-corruption risk (audit finding #8). A --3way failure is
                # a true conflict: the correct move is to re-derive the change
                # against the current file contents, not to force a stale diff.
                raw = three_way_p.stderr.strip() or check_p.stderr.strip() or "patch did not apply cleanly"
                # Make the failure actionable for the pilot's keep-alive resume:
                # the fix is almost never to retry the same stale diff -- it is to
                # re-derive the change against the CURRENT tree state.
                err_msg = (
                    f"{raw} -- the target files likely already moved (an overlapping "
                    "edit landed, or the base shifted). Re-generate the change against "
                    "the current file contents rather than re-applying this diff."
                )
                self._last_checkpoint_id = checkpoint_id
                return False, files, f"patch did not apply cleanly: {err_msg}"
        except Exception as e:
            self._last_checkpoint_id = None
            return False, files, f"error during patch application: {e}"
        finally:
            try:
                os.remove(temp_path)
            except Exception:
                pass

    def _answer_remaining_tool_calls(self, actions, current_idx, is_native, action_seq):
        """Answer sibling tool_calls abandoned by a pause-point dispatch.

        When run_implement/run_parallel returns early, any later tool_calls in
        the same model message would otherwise lack a tool result -- native
        providers then re-issue them, producing the twin-swarm bug. Emit a
        skipped result for each remaining action so the turn is well-formed.
        """
        for later in (actions or [])[current_idx + 1:]:
            action_seq += 1
            # Prefer the provider tool_call_id so a prior action_start (or
            # tool_prep) with that id is settled — synthetic a{n} left orphans
            # that the UI painted as "missing action_result".
            _tcid = str(getattr(later, "tool_call_id", None) or "").strip()
            skip_aid = _tcid or f"a{action_seq}"
            kind = getattr(later, "kind", "") or "action"
            skip_msg = (
                f"(skipped {kind}: prior background dispatch is a pause-point; "
                "wait for that worker instead of issuing a twin)"
            )
            yield ConvEvent("action_result", {
                "id": skip_aid,
                "status": "skipped",
                "message": skip_msg,
                "call_id": _tcid or None,
            })
            self._append_action_result(later, skip_aid, skip_msg, is_native, ok=True)

    @staticmethod
    def _normalize_objective(goal: str) -> str:
        """Canonical form for objective dedup (path-separator + punctuation aware)."""
        return normalize_objective_key(goal)

    def _claim_objective(self, goal: str) -> bool:
        """Atomically reserve an objective for dispatch. Returns False if an
        identical objective is already in flight (caller must NOT dispatch a
        duplicate), True if the claim succeeded. Empty objectives are never
        deduped (nothing meaningful to collide on)."""
        key = self._normalize_objective(goal)
        if not key:
            return True
        with self._inflight_lock:
            if key in self._inflight_objectives:
                return False
            self._inflight_objectives.add(key)
            return True

    def _release_objective(self, goal: str) -> None:
        """Release an in-flight objective once its worker settles so the same
        work can be legitimately dispatched again later."""
        key = self._normalize_objective(goal)
        if not key:
            return
        with self._inflight_lock:
            self._inflight_objectives.discard(key)

    def _run_distill_and_wiki_background(self, objective: str) -> None:
        try:
            # 1. Run auto distill
            if self._auto_distill:
                try:
                    d = self._maybe_auto_distill()
                    if d:
                        self._swarm_results.put({
                            "job_id": f"distill-{self._turn_count}",
                            "objective": objective,
                            "result": {
                                "kind": "distilled",
                                "data": d
                            }
                        })
                except Exception:
                    pass

            # 2. Run wiki orchestrate
            if self._wiki_orchestrate:
                try:
                    w = self.prepare_wiki_pages()
                    if w and w.get("status") == "prepared" and w.get("pages"):
                        self._swarm_results.put({
                            "job_id": f"wiki-{self._turn_count}",
                            "objective": objective,
                            "result": {
                                "kind": "wiki_prepared",
                                "data": w
                            }
                        })
                except Exception:
                    pass
        except Exception:
            pass

    def _run_swarm_background(self, job_id: str, objective: str, state_dir: Optional[str] = None) -> None:
        try:
            # CORRECTNESS: Do NOT touch self._history here to maintain single-writer invariant.
            # Background threads are strictly read-only or local-variable-only with respect to transcript memory,
            # ensuring that the self._history shared list is never corrupted by concurrent modifications.
            res_dict = self._await_and_apply_job(job_id, state_dir=state_dir, objective=objective)
            
            # Put result in queue
            self._swarm_results.put({
                "job_id": job_id,
                "objective": objective,
                "result": res_dict,
                "state_dir": state_dir
            })
        except Exception as e:
            # Put error result in queue
            self._swarm_results.put({
                "job_id": job_id,
                "objective": objective,
                "result": {
                    "job_id": job_id,
                    "applied": False,
                    "files": [],
                    "tokens_in": 0,
                    "tokens_out": 0,
                    "summary": f"Failed background await: {e}",
                    "error": str(e),
                    "artifacts": [],
                    "has_patch_art": False,
                    "apply_msg": str(e),
                    "num_artifacts": 0,
                    "artifact_types": [],
                    "ar_list": []
                },
                "state_dir": state_dir
            })
        finally:
            # Free the objective claimed at run_implement dispatch (external path).
            self._release_objective(objective)
            # Cleanup state_dir if present
            if state_dir:
                import shutil
                shutil.rmtree(state_dir, ignore_errors=True)

    def _worker_deadline_seconds(self) -> float:
        """Hard wall-clock ceiling for a single background edit worker. Bounds a
        wedged provider call or runaway agentic loop so it cannot occupy a
        _swarm_pool slot forever (audit finding #4). 0 disables the bound."""
        try:
            v = float(os.environ.get("HARNESS_WORKER_DEADLINE_SECONDS", "").strip() or 900)
        except ValueError:
            v = 900.0
        return v if v > 0 else 0.0

    def _run_edit_worker_bounded(self, objective: str, requested_adapter: str,
                                 job_id: str = "", target_repo: str = "",
                                 expects_diff: bool = True, on_event=None):
        """Run the edit worker under a hard wall-clock deadline. The work runs in
        a daemon thread; if it blows the deadline we return None so the caller can
        free its _swarm_pool slot immediately. The orphaned worker thread is a
        daemon (dies with the process) and its worktree is reclaimed by the
        engine's own managed_worktree/finally, so the slot -- the scarce resource
        -- is what we protect here.

        Cancellation is best-effort and cooperative: a Python thread cannot be
        force-killed, so when a per-job cancel Event is set we simply stop WAITING
        on the worker and return None. The daemon thread keeps running to natural
        completion (the provider call cannot be interrupted mid-flight) but is
        detached and dies with the process; the caller treats the job as
        cancelled immediately for the UI."""
        from harness.edit_engines import run_edit_worker
        deadline = self._worker_deadline_seconds()
        cancel_ev = self._local_job_cancels.get(job_id) if job_id else None
        box: dict = {}
        done = threading.Event()

        # Per-dispatch target repo (optional): build a shallow-copied
        # HarnessConfig with .repo overridden so the engines and the worktree
        # finalizer transparently target the requested repo. When empty, the
        # original self.config is used unchanged (existing behavior).
        _effective_config = self.config
        effective_cwd = self.config.repo or ""
        if target_repo:
            try:
                _effective_config = _dc_replace(self.config, repo=target_repo)
                effective_cwd = target_repo
            except Exception:
                _effective_config = self.config

        # Thread the governing budget (fully-auto only) into the worker so its
        # spend rolls up into the ONE tree-wide ceiling. ProviderWorker binds a
        # child() of the ambient budget installed on ITS thread; supervised runs
        # leave it None so the worker keeps its own independent default budget.
        from harness.worker import ambient_budget as _ambient_budget_ctx
        _governing = self._auto_budget

        def _run():
            try:
                with _ambient_budget_ctx(_governing):
                    box["res"] = run_edit_worker(
                        _effective_config,
                        objective,
                        requested_adapter=requested_adapter,
                        job_id=job_id,
                        session_id=self.harness_session_id or "",
                        cwd=effective_cwd,
                        expects_diff=expects_diff,
                        on_event=on_event,
                    )
            except Exception as exc:  # surfaced to the caller after join
                box["exc"] = exc
            finally:
                done.set()

        t = threading.Thread(target=_run, name="edit-worker-bounded", daemon=True)
        t.start()
        # Poll in short slices so a cancel request is observed promptly rather than
        # only at the full deadline. Without a per-job cancel Event this collapses
        # to the original single wait.
        import time as _t
        start = _t.monotonic()
        poll = 0.25
        while True:
            if done.wait(timeout=poll):
                break
            if cancel_ev is not None and cancel_ev.is_set():
                return None  # cooperative cancel -> free the pool slot
            if deadline and (_t.monotonic() - start) >= deadline:
                return None  # timed out -> free the pool slot
        if "exc" in box:
            raise box["exc"]
        return box.get("res")

    def _run_verification(self) -> tuple[bool, str]:
        import os
        import subprocess
        import shlex

        verify_cmd = self.config.verify_cmd
        if not verify_cmd:
            return True, ""

        timeout_env = os.environ.get("HARNESS_VERIFY_TIMEOUT", "180")
        try:
            timeout = int(timeout_env)
        except ValueError:
            timeout = 180

        cwd = self.config.repo or None

        # Operator-provided command is safe to run with shell=True if needed
        # (e.g., if it has shell metacharacters or piping), but prefer shlex.split
        # where simple. Operator-provided config, not model-provided.
        # Windows always uses the shell: shlex.split is POSIX-quoting-only and
        # strips the backslashes out of paths like C:\...\python.exe.
        has_meta = os.name == "nt" or any(c in verify_cmd for c in ";&|><$`*?~")
        
        try:
            if has_meta:
                res = subprocess.run(
                    verify_cmd,
                    shell=True,
                    cwd=cwd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    timeout=timeout
                )
            else:
                args = shlex.split(verify_cmd)
                res = subprocess.run(
                    args,
                    shell=False,
                    cwd=cwd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    timeout=timeout
                )
            passed = (res.returncode == 0)
            output = res.stdout or ""
        except subprocess.TimeoutExpired as te:
            passed = False
            out_str = te.stdout or ""
            if isinstance(out_str, bytes):
                out_str = out_str.decode('utf-8', errors='replace')
            output = out_str + f"\n[Verification timed out after {timeout} seconds]"
        except Exception as e:
            passed = False
            output = f"Verification failed to run: {e}"

        if len(output) > 4000:
            output = output[:4000] + "\n[Output truncated...]"
        return passed, output

    def run_auto(self, objective: str, budget: "AutoBudget" = None,
                 *, require_codegraph: bool = True,
                 analysis_mode: bool = False):
        """FULL-AUTO entry point. Thin wrapper that marks unattended mode for the
        duration of the run (so run_command applies the safety guard) and always
        resets it, even on exception or early return, so the next interactive
        command is never wrongly gated.

        ``analysis_mode``: leaf read-only workers never emit swarm findings, so
        the default "no swarms => objective met" early halt must not fire; they
        continue until a structured FINDING/RISK/DECISION summary or a budget
        ceiling.
        """
        self._auto_mode = True
        try:
            yield from self._run_auto_inner(
                objective, budget,
                require_codegraph=require_codegraph,
                analysis_mode=analysis_mode,
            )
        finally:
            self._auto_mode = False
            # Drop the governing budget so the next interactive/supervised
            # command is never wrongly threaded onto a stale tree ceiling.
            self._auto_budget = None

    def _run_auto_inner(self, objective: str, budget: "AutoBudget" = None,
                 *, require_codegraph: bool = True,
                 analysis_mode: bool = False):
        """FULLY-AUTO (unattended) mode: pursue an objective across many pilot
        turns WITHOUT user re-prompting, bounded by an AutoBudget governor. Yields
        the same ConvEvents as send(), plus 'auto_status' (governor snapshots) and
        a terminal 'auto_halt' with the reason.

        SAFETY PRECONDITIONS (refused otherwise):
          - a governor is required (no ceilings == no unattended run)
          - if real analysis is configured, the repo MUST be CodeGraph-indexed
            (the accuracy benchmark proved unindexed -> ~30% blind guessing, which
            is exactly the confident-garbage failure mode you must not run all
            night). Override only with require_codegraph=False.
        """
        budget = (budget or AutoBudget.from_env()).start()
        # Publish the governing budget so background worker/swarm spawns thread a
        # child() of it and share this run's single tree-wide ceiling.
        self._auto_budget = budget

        # Precondition: real analysis on an unindexed repo is refused unattended.
        if (require_codegraph and self.config.swarm_adapter == "openai"
                and self.config.repo):
            import os.path as _op
            if not _op.isdir(_op.join(self.config.repo, ".codegraph")):
                yield ConvEvent("auto_halt", {"reason":
                    f"REFUSED: {self.config.repo} has no .codegraph index. Unattended "
                    f"analysis would run blind (~30% accuracy). Run: python -m "
                    f"puppetmaster codegraph init --index", "snapshot": budget.snapshot()})
                return

        # Seed the objective + an instruction to self-continue until done.
        message = (f"{objective}\n\n(AUTONOMOUS MODE: pursue this objective to "
                   f"completion across multiple investigation rounds. After each "
                   f"round, if more investigation is warranted and useful, continue "
                   f"with another swarm; finish with no actions only when the "
                   f"objective is genuinely met or no further progress is possible.)")

        loop_msg = message
        failed_verifications = 0
        cycle = 0
        self._cancel.clear()
        self._interrupted_swarms = False
        while True:
            if self._cancel.is_set():
                yield ConvEvent("auto_halt", {"reason": "cancelled",
                                              "snapshot": budget.snapshot()})
                return
            halt = budget.check()
            if halt:
                yield ConvEvent("auto_halt", {"reason": halt, "snapshot": budget.snapshot()})
                d = self._maybe_auto_distill()
                if d:
                    yield ConvEvent("distilled", d)
                self._maybe_ingest(objective, [], [])
                return
            cycle += 1
            findings_before = 0
            tokens_at_cycle_start = self._tokens_used
            # one pilot turn (send() drives say->act->react until it yields back)
            turn_findings_count = 0
            turn_had_retryable_error = False
            last_cycle_message = ""
            tripped = None
            # Only real delegation actions burn the swarm ceiling. Counting
            # every read_file/write_file as a "swarm" made analysis workers
            # with max_swarms=2 halt after two tool calls
            # ("swarm ceiling reached (2/2)") before any FINDING summary.
            _swarm_budget_kinds = frozenset({
                "run_swarm", "run_implement", "run_parallel",
            })
            for ev in self.send(loop_msg):
                # meter the governor off the stream
                if ev.kind == "message":
                    _msg = (ev.data.get("text") or "").strip()
                    if _msg:
                        last_cycle_message = _msg
                if ev.kind == "action_result" and not ev.data.get("error"):
                    _akind = (ev.data.get("kind") or "").strip().lower()
                    if _akind in _swarm_budget_kinds:
                        budget.add_swarm()
                    # Leaf tool progress (reads/searches) counts as activity so
                    # the idle stall does not fire mid-investigation.
                    _n = int(ev.data.get("num", 0) or 0)
                    turn_findings_count += _n if _n > 0 else 1
                elif ev.kind == "action_result" and ev.data.get("error"):
                    # a tool error (e.g. malformed write_file) is recoverable -- the model
                    # gets the error in history and should retry; do NOT let this turn count
                    # as idle and trip a premature "objective met" halt.
                    _err_txt = str(ev.data.get("error") or "").upper()
                    if "INVALID TOOL CALL" in _err_txt or "REQUIRES A" in _err_txt:
                        turn_had_retryable_error = True
                    # If verification failed previously, we should clear the failed status if they are fixing it?
                    # No, the prompt says max_retries limit is for consecutive failure loops. Let's keep it simple.
                yield ev
                if ev.kind == "assistant_done":
                    break
                # CHECK THE CEILING MID-STREAM: a never-stopping pilot fires swarms
                # inside one send() call; without this the governor only catches it
                # between cycles and burns the whole inner budget first.
                if self._cancel.is_set():
                    tripped = "cancelled"
                    break
                # Feed token delta to the governor mid-stream so a token ceiling
                # trips inside a single send() call (not just between cycles).
                # Without this, an unbounded pilot loop with HARNESS_MAX_PILOT_STEPS=0
                # would burn through the entire swarm ceiling before tokens are checked.
                # Best-effort: depends on the provider populating _tokens_used during
                # the stream. If usage only arrives in the stream footer, _mid_delta
                # stays 0 here and the between-cycle budget.check() remains the net.
                _mid_delta = self._tokens_used - tokens_at_cycle_start
                if _mid_delta > 0:
                    budget.add_tokens(_mid_delta)
                    tokens_at_cycle_start = self._tokens_used
                tripped = budget.check()
                if tripped:
                    break
            if tripped:
                yield ConvEvent("auto_halt", {"reason": tripped, "snapshot": budget.snapshot()})
                d = self._maybe_auto_distill()
                if d:
                    yield ConvEvent("distilled", d)
                self._maybe_ingest(objective, [], [])
                return

            # Immediately reset loop_msg to default for subsequent cycles, unless overridden by verification failure.
            loop_msg = "(continue toward the objective, or finish if met)"

            # account for stall + emit a governor heartbeat
            budget.note_findings(turn_findings_count)
            # REAL token metering: feed the delta consumed this cycle into the
            # governor so the documented token ceiling actually trips.
            delta = self._tokens_used - tokens_at_cycle_start
            if delta > 0:
                budget.add_tokens(delta)
            yield ConvEvent("auto_status", {"cycle": cycle, "snapshot": budget.snapshot()})
            # if the pilot finished a turn with no swarms at all, it considers the
            # objective met -> stop the autonomous loop.
            # Analysis leaf workers never emit swarm findings; do not treat that
            # as done until a structured FINDING/RISK/DECISION summary lands.
            if turn_findings_count == 0 and budget.idle_steps >= 1 and not turn_had_retryable_error:
                if analysis_mode:
                    try:
                        from harness.worker import _analysis_output_is_structured
                        structured_ok, _ = _analysis_output_is_structured(
                            last_cycle_message,
                        )
                    except Exception:
                        structured_ok = bool((last_cycle_message or "").strip())
                    if structured_ok:
                        yield ConvEvent("auto_halt", {
                            "reason": "analysis findings submitted",
                            "snapshot": budget.snapshot(),
                        })
                        d = self._maybe_auto_distill()
                        if d:
                            yield ConvEvent("distilled", d)
                        self._maybe_ingest(objective, [], [])
                        return
                    loop_msg = (
                        "(system) Do not stop yet. End with a structured "
                        "FINDING/RISK/DECISION summary citing file:line evidence. "
                        "Do not end on planning or mid-thought reasoning alone."
                    )
                elif self.config.verify_cmd:
                    yield ConvEvent("verifying", {"cmd": self.config.verify_cmd})
                    passed, out = self._run_verification()
                    yield ConvEvent("verification", {"passed": passed, "output": out[:1000]})
                    if passed:
                        yield ConvEvent("auto_halt", {"reason": "objective met and verified (verify_cmd passed)", "snapshot": budget.snapshot()})
                        d = self._maybe_auto_distill()
                        if d:
                            yield ConvEvent("distilled", d)
                        self._maybe_ingest(objective, [], [])
                        return
                    else:
                        failed_verifications += 1
                        import os
                        max_retries_env = os.environ.get("HARNESS_VERIFY_MAX_RETRIES", "2")
                        try:
                            max_retries = int(max_retries_env)
                        except ValueError:
                            max_retries = 2
                        
                        if failed_verifications >= max_retries:
                            yield ConvEvent("auto_halt", {
                                "reason": f"objective NOT verified after {max_retries} retries (verify_cmd still failing)",
                                "snapshot": budget.snapshot(),
                                "last_output": out
                            })
                            d = self._maybe_auto_distill()
                            if d:
                                yield ConvEvent("distilled", d)
                            self._maybe_ingest(objective, [], [])
                            return
                        else:
                            loop_msg = f"Verification command failed. Output:\n{out}\nFix the issue so the verification passes, then finish."
                else:
                    yield ConvEvent("auto_halt", {"reason": "pilot reports objective met "
                        "(no further investigation)", "snapshot": budget.snapshot()})
                    d = self._maybe_auto_distill()
                    if d:
                        yield ConvEvent("distilled", d)
                    self._maybe_ingest(objective, [], [])
                    return

