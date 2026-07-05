from __future__ import annotations

import os
import re
import uuid
import logging
import threading
import subprocess
import contextlib
from dataclasses import dataclass, field
from typing import Optional, Iterator, TYPE_CHECKING

logger = logging.getLogger("pmharness.worker")

from harness.autobudget import AutoBudget

# Ambient shared budget for the current spawn tree. A supervising Conversation
# running in fully-auto mode installs its governing AutoBudget here (per-thread)
# before dispatching a worker, so a ProviderWorker built deep in the
# pilot->swarm->worker tree binds to the SAME decrementing ceiling instead of
# minting a fresh default that resets the budget for its level. Supervised runs
# leave this unset, so a worker keeps its own independent default (no regression).
_ambient_budget = threading.local()


def set_ambient_budget(budget: Optional[AutoBudget]):
    """Install (or clear) the governing budget for workers spawned on THIS
    thread. Returns the previous value so callers can restore it. Passing None
    clears the ambient budget (restores supervised behavior)."""
    prev = getattr(_ambient_budget, "value", None)
    _ambient_budget.value = budget
    return prev


def get_ambient_budget() -> Optional[AutoBudget]:
    """The governing budget installed for workers on this thread, or None."""
    return getattr(_ambient_budget, "value", None)


@contextlib.contextmanager
def ambient_budget(budget: Optional[AutoBudget]):
    """Scope an ambient governing budget for the duration of a ``with`` block."""
    prev = set_ambient_budget(budget)
    try:
        yield budget
    finally:
        set_ambient_budget(prev)
from harness.config import HarnessConfig
from harness.worktrees import (
    _is_repo,
    add_worktree,
    remove_worktree,
    _safe_branch_name,
    _git,
    delete_branch
)

if TYPE_CHECKING:
    # ConvEvent is used only in annotations (strings under `from __future__ import
    # annotations`), so it never needs to exist at import time. ConversationalSession
    # is imported lazily at its single call site instead of here: conversation.py
    # transitively imports this module, so a top-level import back into conversation
    # created a cycle. See import_selftest.py for the concurrent-import failure this
    # removes ("cannot import name 'WorkerResult' from 'harness.worker'").
    from harness.conversation import ConvEvent

@dataclass
class WorkerResult:
    ok: bool
    patch: str = ""
    files_changed: list[str] = field(default_factory=list)
    summary: str = ""
    worktree: str = ""
    test_output: str = ""
    error: str = ""
    events: list[ConvEvent] = field(default_factory=list)
    test_passed: bool = True
    # Both token directions are normalized so every edit engine reports full
    # spend. tokens_in was previously dropped (hardcoded 0 downstream), which
    # undercounted every implement worker's prompt cost (audit finding #5).
    tokens_out: int = 0
    tokens_in: int = 0
    # Prompt tokens that were served from the provider's prompt cache during
    # this worker's inner session. Propagated to the parent session's
    # _tokens_cached so cache savings (server.py._cache_savings) reflect
    # swarm/worker caching, not just the parent's own turns. Optional with a
    # default to keep positional construction back-compatible.
    tokens_cached: int = 0
    # Absolute paths the worker wrote to via run_command that fall OUTSIDE its
    # worktree. Populated best-effort by _detect_escaped_writes so callers can
    # tell "no diff" apart from "the agent shelled out to another repo".
    # Optional with a default: adding it here is not a breaking change for
    # callers that construct WorkerResult positionally.
    escaped_paths: list[str] = field(default_factory=list)


# --- Escaped-write detection ------------------------------------------------
# The worker runs inside a git worktree and captures its patch via `git diff`
# on that worktree. A run_command action that shells out with an absolute-path
# redirection (`cat > /some/other/repo/file`) writes real bytes to disk that
# the worktree diff CANNOT see, so the worker would silently report "no
# changes" while having edited another repo. These regexes flag the obvious
# forms so the finalizer can surface them loudly. Best-effort only: shell is
# not fully parseable, and false negatives are acceptable -- what matters is
# that the common escape patterns do not slip through unnoticed.
_ABS_PATH = r"(/[^\s'\";|&<>()`$]+)"

# Redirection to an absolute path: `> /abs`, `>> /abs`, `2> /abs`, `&> /abs`,
# and idioms like `cat > /abs` all match on the operator itself so we do not
# need to enumerate every left-hand command.
_RE_REDIRECT = re.compile(r"(?:^|[\s;|&`])\d?>{1,2}\s*" + _ABS_PATH)

# `tee /abs` and `tee -a /abs` (any flag cluster between).
_RE_TEE = re.compile(r"\btee\b(?:\s+-[A-Za-z]+)*\s+" + _ABS_PATH)

# `mkdir -p /abs` or `mkdir /abs`.
_RE_MKDIR = re.compile(r"\bmkdir\b(?:\s+-[A-Za-z]+)*\s+" + _ABS_PATH)

# `python -c "... open('/abs','w') ..."` (also 'a', 'wb', 'w+', etc.). The
# character class requires at least one write-mode letter.
_RE_PY_OPEN = re.compile(
    r"open\(\s*['\"](/[^'\"]+)['\"]\s*,\s*['\"][rwab+xt]*[wa][rwab+xt]*['\"]"
)

# `cp SRC /abs`, `mv SRC /abs`, `install SRC /abs`, `rsync SRC /abs`. We match
# the command through the end of its shell segment and then pick the LAST
# absolute path token as the destination (POSIX convention for these tools).
_RE_CP_MV = re.compile(r"\b(?:cp|mv|install|rsync)\b[^;|&`\n]*")


def _detect_escaped_writes(events, wt_path: str) -> list[str]:
    """Scan run_command events for shell writes to absolute paths that fall
    OUTSIDE ``wt_path``. Returns a de-duplicated, sorted list of the escaped
    destinations.

    Never raises: malformed events, missing attributes, and non-string payloads
    are all tolerated and skipped. The goal is to flag the obvious "I shelled
    out and wrote to another repo" pattern that the worktree `git diff` cannot
    see, not to be a full shell parser.
    """
    if not events or not wt_path:
        return []
    try:
        wt_norm = os.path.abspath(wt_path)
    except Exception:
        return []
    # Directory-boundary-aware prefix so "/tmp/wt" is not treated as containing
    # "/tmp/wtx/y".
    wt_prefix = wt_norm.rstrip(os.sep) + os.sep

    found: set[str] = set()

    for ev in events:
        # Support both ConvEvent objects and plain dicts so this helper is
        # trivially unit-testable without importing the conversation module.
        kind = getattr(ev, "kind", None)
        data = getattr(ev, "data", None)
        if kind is None and isinstance(ev, dict):
            kind = ev.get("kind")
            data = ev.get("data")
        if kind != "action_start" or not isinstance(data, dict):
            continue
        if data.get("kind") != "run_command":
            continue
        cmd = data.get("goal") or data.get("command") or ""
        if not isinstance(cmd, str) or not cmd:
            continue

        candidates: list[str] = []
        try:
            for m in _RE_REDIRECT.finditer(cmd):
                candidates.append(m.group(1))
            for m in _RE_TEE.finditer(cmd):
                candidates.append(m.group(1))
            for m in _RE_MKDIR.finditer(cmd):
                candidates.append(m.group(1))
            for m in _RE_PY_OPEN.finditer(cmd):
                candidates.append(m.group(1))
            # cp/mv/install/rsync: destination is the last absolute path in the
            # segment. Segment-scoped so we do not wander across ; | & or a
            # backtick into an unrelated command.
            for seg in _RE_CP_MV.findall(cmd):
                abs_paths = re.findall(_ABS_PATH, seg)
                if abs_paths:
                    candidates.append(abs_paths[-1])
        except Exception:
            # Regex on adversarial input should not throw, but if it does we
            # would rather skip this event than crash the finalizer.
            continue

        for path in candidates:
            try:
                norm = os.path.abspath(path.rstrip("/") or "/")
            except Exception:
                continue
            if norm == wt_norm or norm.startswith(wt_prefix):
                continue
            found.add(norm)

    return sorted(found)


def is_obviously_destructive(cmd: str) -> bool:
    """
    Halt or block obviously destructive commands in the headless worker.
    Flags patterns like 'rm -rf /', 'rm -rf ~', ':(){:|:&};:', 'mkfs', 'dd if=', 
    '> /dev/sd', 'git push --force' to a denylist.
    """
    if not cmd:
        return False
    
    cmd_lower = cmd.lower().strip()

    # Recursive-force delete flag cluster (-rf, -fr, -rfv, ...): both r and f
    # present in any order. Case is handled by also matching cmd_lower below.
    rm_rf = r"\brm\s+-(?=[a-z]*r)(?=[a-z]*f)[a-z]+\s+"
    # Target boundary: end-of-string, whitespace, or a glob. This is what keeps
    # a catastrophic root from matching a deeper, legitimate project path --
    # `rm -rf /home` is blocked, `rm -rf /home/user/project/build` is not.
    end = r"(\s|$|\*)"
    # Only truly catastrophic targets: the filesystem root, a root glob, or a
    # bare top-level system directory. Arbitrary absolute paths are allowed on
    # purpose so the worker can clean up its own build/output dirs.
    system_roots = (
        "bin|boot|dev|etc|home|lib|lib64|opt|proc|root|run|sbin|srv|sys|"
        "usr|users|var|applications|library|system|volumes|private"
    )
    denylist = [
        rm_rf + r"/" + end,                              # rm -rf /   or   rm -rf /*
        rm_rf + r"/(" + system_roots + r")(/)?" + end,   # rm -rf /etc , /home , ...
        rm_rf + r"~(/)?" + end,                          # rm -rf ~   or   ~/   or   ~/*
        rm_rf + r"\$home(/)?" + end,                     # rm -rf $HOME
        # Fork bomb `:(){:|:&};:` -- the recursive body is `:|:&` (call, pipe to
        # call, background). Tolerate the usual whitespace variants.
        r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",
        r"\bmkfs\b",
        r"\bdd\s+if=",
        r">\s*/dev/sd",
        # Force-push, but NOT the safe `--force-with-lease` variant.
        r"git\s+push\s+.*--force(?!-with-lease)",
    ]

    # Patterns are lowercase and cmd_lower is a case-folded superset, so matching
    # cmd_lower alone catches everything the original-case pass would.
    for pattern in denylist:
        if re.search(pattern, cmd_lower):
            return True

    return False


# subprocess.run is a process-global, so the destructive-command guard is
# installed once and reference-counted: concurrent workers (run_parallel) share
# a single armed guard and it is only restored when the LAST worker exits. A
# naive save/restore-per-worker races -- whoever finishes first would disarm the
# guard for every worker still running.
_guard_lock = threading.Lock()
_guard_depth = 0
_original_run = None


def _guarded_run(*args, **kwargs):
    cmd = args[0] if args else kwargs.get("args")

    if isinstance(cmd, (list, tuple)):
        cmd_str = " ".join(str(part) for part in cmd)
    else:
        cmd_str = str(cmd or "")

    if is_obviously_destructive(cmd_str):
        return subprocess.CompletedProcess(
            args=cmd or [],
            returncode=1,
            stdout="Command rejected by safety guardrails: obviously destructive.",
            stderr="Command rejected by safety guardrails: obviously destructive.",
        )

    return _original_run(*args, **kwargs)


@contextlib.contextmanager
def patch_subprocess_run(repo_path: str):
    """
    Guard subprocess.run against obviously destructive commands for the duration
    of the context. Safe to nest and to run concurrently across worker threads --
    the guard is reference-counted and stays armed until the last holder exits.
    """
    global _guard_depth, _original_run

    with _guard_lock:
        if _guard_depth == 0:
            _original_run = subprocess.run
            subprocess.run = _guarded_run
        _guard_depth += 1

    try:
        yield
    finally:
        with _guard_lock:
            _guard_depth -= 1
            if _guard_depth == 0:
                subprocess.run = _original_run
                _original_run = None


class ProviderWorker:
    def __init__(
        self,
        repo: str,
        goal: str,
        *,
        driver: str = "",
        reach: str = "",
        base: str = "HEAD",
        budget: Optional[AutoBudget] = None,
        run_tests: str = "",
        keep_worktree_on_failure: bool = False,
        require_codegraph: bool = False,
    ):
        self.repo = os.path.abspath(repo) if repo else ""
        self.goal = goal
        self.driver = driver
        self.reach = reach
        self.base = base
        # Shared-budget threading: if a supervising fully-auto run installed a
        # governing budget for this thread, adopt a child() of it so this
        # worker's spend rolls up into the ONE tree-wide ceiling that never
        # resets per spawn level. This takes precedence over any per-level
        # default budget passed in (the whole point is that the tree ceiling
        # wins over local defaults). When no ambient budget is present
        # (supervised mode), fall back to the caller's budget or a fresh
        # per-worker default -- preserving existing behavior exactly.
        _governing = get_ambient_budget()
        if _governing is not None:
            self.budget = _governing.child()
        else:
            self.budget = budget or AutoBudget(
                max_tokens=40000,
                max_seconds=300,
                max_swarms=2,
                max_idle_steps=2
            )
        self.run_tests = run_tests
        self.keep_worktree_on_failure = keep_worktree_on_failure
        self.require_codegraph = require_codegraph
        # Inner-session token split, captured during _run_impl and read by run().
        # Defaulted here so run() is safe even if _run_impl is stubbed (tests) or
        # exits before a session exists.
        self._session_tokens_in = 0
        self._session_tokens_total = 0
        # Prompt tokens served from cache during the inner session. Mirrored to
        # WorkerResult.tokens_cached in run() so the parent session's cache
        # savings include worker/swarm cache reads.
        self._session_tokens_cached = 0

    def run(self) -> WorkerResult:
        """Drive the worker and return a normalized result whose token counts
        always reflect the metered spend. Metering happens on ``self.budget`` (the
        cumulative total, in+out) and on the inner session's ``_tokens_in`` during
        the run; we stamp both onto the result here -- once, for EVERY return path
        -- so cost accounting and autobudget attribution work no matter which
        caller invoked the worker (not only run_native_edit). Splitting out the
        prompt tokens keeps implement-worker cost from being undercounted."""
        result = self._run_impl()
        total = self.budget.tokens_used or self._session_tokens_total
        if not result.tokens_in:
            result.tokens_in = self._session_tokens_in
        if not result.tokens_out:
            # budget.tokens_used is the cumulative in+out total; the completion
            # share is the remainder after prompt tokens.
            result.tokens_out = max(0, total - result.tokens_in)
        # Backfill cached prompt tokens from the inner session onto EVERY return
        # path so the parent session's cache_savings_usd accounts for
        # worker/swarm cache reads. tokens_cached is a subset of tokens_in
        # (never added on top), so we only need to mirror it here.
        if not result.tokens_cached:
            result.tokens_cached = self._session_tokens_cached
        return result

    def _run_impl(self) -> WorkerResult:
        # Defaults so run() can stamp token counts on EVERY return path, including
        # the early exits below that never construct an inner session.
        self._session_tokens_in = 0
        self._session_tokens_total = 0
        self._session_tokens_cached = 0
        if not self.repo or not _is_repo(self.repo):
            return WorkerResult(ok=False, error="not a git repo")

        short_uuid = uuid.uuid4().hex[:8]
        branch_name = _safe_branch_name(f"pmworker-{short_uuid}")
        
        wt_path = ""
        success = False
        events: list[ConvEvent] = []
        
        try:
            # 1. Create worktree
            wt_info = add_worktree(self.repo, branch=branch_name, base=self.base)
            wt_path = wt_info["path"]
            
            # Verify worktree confinement
            from harness.worktrees import _get_managed_dir, _is_confined
            managed_dir = _get_managed_dir(self.repo)
            if not _is_confined(wt_path, managed_dir):
                raise ValueError("Confinement violation: worktree path lies outside the managed directory")

            # 2. Build worker HarnessConfig
            base_cfg = HarnessConfig.from_env()
            worker_cfg = HarnessConfig(
                driver=self.driver or base_cfg.driver,
                reach=self.reach or base_cfg.reach,
                budget=base_cfg.budget,
                state_dir=base_cfg.state_dir,
                worker_mode=base_cfg.worker_mode,
                repo=wt_path,
                swarm_adapter=base_cfg.swarm_adapter,
                wiki_url=base_cfg.wiki_url,
                wiki_auto=base_cfg.wiki_auto,
                max_context_tokens=base_cfg.max_context_tokens,
                no_delegation=True,
            )
            
            # Set the objective framing
            worker_objective = (
                f"IMPLEMENT TASK: {self.goal}\n\n"
                "Edit the file(s) directly to complete this task. Read each target file at most once, then write the change. "
                "Do not investigate beyond the files you must edit. Finish as soon as the change is complete."
            )
            
            # Start the budget
            self.budget.start()
            
            # 3. Construct ConversationalSession and drive run_auto. Imported here
            # (not at module top) to keep worker <-> conversation acyclic.
            from harness.conversation import ConversationalSession
            session = ConversationalSession(worker_cfg)
            
            with patch_subprocess_run(wt_path):
                for ev in session.run_auto(
                    worker_objective,
                    budget=self.budget,
                    require_codegraph=self.require_codegraph
                ):
                    events.append(ev)

            # Capture the inner session's real token split (in vs. total) so run()
            # can report accurate prompt/completion spend instead of dropping
            # prompt tokens.
            self._session_tokens_in = int(getattr(session, "_tokens_in", 0) or 0)
            self._session_tokens_total = int(getattr(session, "_tokens_used", 0) or 0)
            # Cached prompt tokens flow up too: the parent session's
            # _tokens_cached feeds server.py's cache_savings_usd, and without
            # this capture, worker/swarm cache reads never reach the parent.
            self._session_tokens_cached = int(getattr(session, "_tokens_cached", 0) or 0)

            # 4. Finalize -> PATCH. Stage everything, drop build/agent artifacts
            # the worker may have created (git add -A otherwise sweeps untracked
            # __pycache__/*.pyc, .pytest_cache, etc.), and capture the diff. Shared
            # with the agentic engine so both capture edits identically.
            try:
                from harness.edit_engines import finalize_worktree_patch
                patch, files_changed = finalize_worktree_patch(wt_path)
            except RuntimeError as e:
                return WorkerResult(
                    ok=False,
                    error=str(e),
                    events=events,
                    worktree=wt_path
                )

            # Detect run_command writes that escaped the worktree BEFORE
            # deciding how to report an empty diff. The worktree `git diff`
            # cannot see writes to absolute paths outside wt_path, so without
            # this check the worker would report "no changes produced" while
            # having actually edited another repo on disk.
            escaped = _detect_escaped_writes(events, wt_path)

            if not patch.strip():
                # Empty diff is a benign no-op, not an execution failure. `ok`
                # is False (no usable patch) but `success` is True so the finally
                # block cleans up the worktree -- keep_worktree_on_failure retains
                # only on exceptions, and an unchanged worktree has nothing to
                # inspect. (See test_worker_empty_change; the ok/success split is
                # intentional, not an inconsistency.)
                success = True
                if escaped:
                    # Loud, worktree-scoped summary: the user needs to know the
                    # worker DID write files, just not where the patch could
                    # capture them. Suggesting re-dispatch with the right `repo`
                    # parameter is the actionable fix.
                    joined = ", ".join(escaped)
                    summary = (
                        f"no changes captured in the worktree diff, but this "
                        f"worker wrote to {len(escaped)} path(s) OUTSIDE its "
                        f"worktree (NOT captured in the patch): {joined}. "
                        f"If you meant to edit another repo, re-dispatch "
                        f"run_implement with the repo parameter set to that repo."
                    )
                    return WorkerResult(
                        ok=False,
                        summary=summary,
                        events=events,
                        worktree=wt_path,
                        escaped_paths=escaped,
                    )
                return WorkerResult(
                    ok=False,
                    summary=f"no changes captured in the worktree diff (worktree={wt_path})",
                    events=events,
                    worktree=wt_path,
                )
                
            # 5. Optional self-test execution
            test_output = ""
            test_passed = True
            if self.run_tests:
                test_timeout = max(10, int(self.budget.max_seconds - self.budget.elapsed))
                # Sanitize PATH so any child (pytest, npm test, ...) does not
                # prefer binaries inside another app's .app/Contents/ bundle
                # (Cursor.app, VS Code.app, ...). Spawning a sibling app's
                # bundled binary is the cross-app launch that triggers macOS
                # TCC prompts and pulls in foreign runtimes. Best-effort: on
                # any failure, fall back to the current environment untouched.
                try:
                    from harness._exec import sanitized_env as _sanitized_env
                    _test_env = _sanitized_env()
                except Exception:
                    _test_env = None
                try:
                    p_test = subprocess.run(
                        self.run_tests,
                        shell=True,
                        cwd=wt_path,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        timeout=test_timeout,
                        env=_test_env,
                    )
                    test_output = p_test.stdout or ""
                    test_passed = (p_test.returncode == 0)
                except subprocess.TimeoutExpired as te:
                    out_str = te.stdout.decode('utf-8', errors='replace') if isinstance(te.stdout, bytes) else (te.stdout or "")
                    test_output = out_str + f"\n\n[Test execution timed out after {test_timeout} seconds]"
                    test_passed = False
                except Exception as e:
                    test_output = f"Failed to run tests: {e}"
                    test_passed = False
                    
            # Determine summary from final events
            last_message = ""
            halt_reason = ""
            for ev in events:
                if ev.kind == "message":
                    last_message = ev.data.get("text") or ""
                elif ev.kind == "auto_halt":
                    halt_reason = ev.data.get("reason") or ""
                    
            summary_parts = []
            if halt_reason:
                summary_parts.append(f"Halt reason: {halt_reason}")
            if last_message:
                summary_parts.append(f"Last assistant message: {last_message}")
            summary = "\n".join(summary_parts) if summary_parts else "No summary available."
            
            success = True
            error_msg = ""
            if self.run_tests and not test_passed:
                first_500 = test_output[:500]
                error_msg = f"worker tests failed: {first_500}"

            # Non-empty patch path: still warn if the agent ALSO wrote to paths
            # outside the worktree. Those writes are real but absent from the
            # captured patch, so downstream apply steps would silently drop
            # them. We do not fail the case (a useful patch was produced) --
            # just fold a warning line into the summary and expose the paths.
            if escaped:
                joined = ", ".join(escaped)
                warn = (
                    f"WARNING: worker also wrote to {len(escaped)} path(s) "
                    f"OUTSIDE its worktree, which are NOT included in the patch: "
                    f"{joined}"
                )
                summary = f"{summary}\n{warn}" if summary else warn

            return WorkerResult(
                ok=bool(patch) if not self.run_tests else (bool(patch) and test_passed),
                patch=patch,
                files_changed=files_changed,
                summary=summary,
                worktree=wt_path,
                test_output=test_output,
                error=error_msg,
                events=events,
                test_passed=test_passed,
                escaped_paths=escaped,
            )
            
        except Exception as e:
            return WorkerResult(
                ok=False,
                error=f"Worker run failed: {e}",
                events=events,
                worktree=wt_path
            )
            
        finally:
            try:
                if wt_path:
                    if not success and self.keep_worktree_on_failure:
                        pass
                    else:
                        remove_worktree(self.repo, wt_path, force=True)
            except Exception as exc:
                logger.warning("failed to remove worktree %s: %s", wt_path, exc)

            try:
                delete_branch(self.repo, branch_name)
            except Exception as exc:
                logger.warning("failed to delete branch %s: %s", branch_name, exc)
