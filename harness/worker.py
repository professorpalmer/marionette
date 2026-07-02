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
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession, ConvEvent
from harness.worktrees import (
    _is_repo,
    add_worktree,
    remove_worktree,
    _safe_branch_name,
    _git,
    delete_branch
)

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
    tokens_out: int = 0  # normalized token count so every edit engine reports spend


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
        self.budget = budget or AutoBudget(
            max_tokens=40000,
            max_seconds=300,
            max_swarms=2,
            max_idle_steps=2
        )
        self.run_tests = run_tests
        self.keep_worktree_on_failure = keep_worktree_on_failure
        self.require_codegraph = require_codegraph

    def run(self) -> WorkerResult:
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
            
            # 3. Construct ConversationalSession and drive run_auto
            session = ConversationalSession(worker_cfg)
            
            with patch_subprocess_run(wt_path):
                for ev in session.run_auto(
                    worker_objective,
                    budget=self.budget,
                    require_codegraph=self.require_codegraph
                ):
                    events.append(ev)
                    
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

            if not patch.strip():
                # Empty diff is a benign no-op, not an execution failure. `ok`
                # is False (no usable patch) but `success` is True so the finally
                # block cleans up the worktree -- keep_worktree_on_failure retains
                # only on exceptions, and an unchanged worktree has nothing to
                # inspect. (See test_worker_empty_change; the ok/success split is
                # intentional, not an inconsistency.)
                success = True
                return WorkerResult(
                    ok=False,
                    summary="no changes produced",
                    events=events,
                    worktree=wt_path
                )
                
            # 5. Optional self-test execution
            test_output = ""
            test_passed = True
            if self.run_tests:
                test_timeout = max(10, int(self.budget.max_seconds - self.budget.elapsed))
                try:
                    p_test = subprocess.run(
                        self.run_tests,
                        shell=True,
                        cwd=wt_path,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        timeout=test_timeout
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

            return WorkerResult(
                ok=bool(patch) if not self.run_tests else (bool(patch) and test_passed),
                patch=patch,
                files_changed=files_changed,
                summary=summary,
                worktree=wt_path,
                test_output=test_output,
                error=error_msg,
                events=events,
                test_passed=test_passed
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
