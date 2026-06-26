from __future__ import annotations

import os
import re
import uuid
import subprocess
import contextlib
from dataclasses import dataclass, field
from typing import Optional, Iterator, TYPE_CHECKING

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


def is_obviously_destructive(cmd: str) -> bool:
    """
    Halt or block obviously destructive commands in the headless worker.
    Flags patterns like 'rm -rf /', 'rm -rf ~', ':(){:|:&};:', 'mkfs', 'dd if=', 
    '> /dev/sd', 'git push --force' to a denylist.
    """
    if not cmd:
        return False
    
    cmd_lower = cmd.lower().strip()
    
    # Specific denylist patterns from instructions and tool_guardrails
    denylist = [
        r"rm\s+-rf\s+/",
        r"rm\s+-rf\s+~",
        r":\(\)\{\s*:\|\s*&\s*\}\s*;\s*:",  # Fork bomb
        r"\bmkfs\b",
        r"\bdd\s+if=",
        r">\s*/dev/sd",
        r"git\s+push\s+.*--force"
    ]
    
    for pattern in denylist:
        if re.search(pattern, cmd) or re.search(pattern, cmd_lower):
            return True
            
    # Substring literal matches to be safe and clear
    literals = [
        "rm -rf /",
        "rm -rf ~",
        ":(){:|:&};:",
        "git push --force",
        "dd if="
    ]
    for lit in literals:
        if lit in cmd or lit in cmd_lower:
            return True
            
    return False


@contextlib.contextmanager
def patch_subprocess_run(repo_path: str):
    """
    Temporarily patch subprocess.run to guard against obviously destructive commands.
    """
    original_run = subprocess.run
    
    def guarded_run(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args")
        
        if isinstance(cmd, list):
            cmd_str = " ".join(cmd)
        else:
            cmd_str = str(cmd or "")
            
        if is_obviously_destructive(cmd_str):
            return subprocess.CompletedProcess(
                args=cmd or [],
                returncode=1,
                stdout="Command rejected by safety guardrails: obviously destructive.",
                stderr="Command rejected by safety guardrails: obviously destructive."
            )
            
        return original_run(*args, **kwargs)
        
    subprocess.run = guarded_run
    try:
        yield
    finally:
        subprocess.run = original_run


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
                    
            # 4. Finalize -> PATCH
            # Run git add -A
            rc_add, out_add, err_add = _git(wt_path, "add", "-A")
            if rc_add != 0:
                return WorkerResult(
                    ok=False,
                    error=f"git add failed: {err_add or out_add}",
                    events=events,
                    worktree=wt_path
                )
                
            # Run git diff --cached --no-color using subprocess.run directly to preserve formatting/newlines
            p_diff = subprocess.run(
                ["git", "-C", wt_path, "diff", "--cached", "--no-color"],
                capture_output=True,
                text=True,
                timeout=30
            )
            if p_diff.returncode != 0:
                return WorkerResult(
                    ok=False,
                    error=f"git diff failed: {p_diff.stderr or p_diff.stdout}",
                    events=events,
                    worktree=wt_path
                )
                
            patch = p_diff.stdout
            
            # Parse files changed from git diff --cached --name-only
            p_files = subprocess.run(
                ["git", "-C", wt_path, "diff", "--cached", "--name-only"],
                capture_output=True,
                text=True,
                timeout=15
            )
            files_changed = [line.strip() for line in p_files.stdout.splitlines() if line.strip()]
            
            if not patch.strip():
                success = True
                return WorkerResult(
                    ok=False,
                    summary="no changes produced",
                    events=events,
                    worktree=wt_path
                )
                
            # 5. Optional self-test execution
            test_output = ""
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
                except subprocess.TimeoutExpired as te:
                    out_str = te.stdout.decode('utf-8', errors='replace') if isinstance(te.stdout, bytes) else (te.stdout or "")
                    test_output = out_str + f"\n\n[Test execution timed out after {test_timeout} seconds]"
                except Exception as e:
                    test_output = f"Failed to run tests: {e}"
                    
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
            return WorkerResult(
                ok=True,
                patch=patch,
                files_changed=files_changed,
                summary=summary,
                worktree=wt_path,
                test_output=test_output,
                events=events
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
            except Exception:
                pass

            try:
                delete_branch(self.repo, branch_name)
            except Exception:
                pass
