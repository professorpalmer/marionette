from __future__ import annotations

"""Host-owned quality GATE before an interactive turn may settle to idle.

Optional shell command(s) from HarnessConfig. Skips re-running a failed gate
when the workspace fingerprint (git porcelain + path/mtime hashes) is unchanged.
Distinct from Autopilot verify_cmd and from environment_fingerprint (which
omits mtime by design).
"""

import hashlib
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

MAX_GATE_OUTPUT = 4000


def _parse_cmds(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(c).strip() for c in raw if str(c).strip()]
    text = str(raw or "").strip()
    if not text:
        return []
    # Allow newline or ";;;" separators (semicolon alone is common in shells).
    if "\n" in text:
        parts = text.splitlines()
    elif ";;;" in text:
        parts = text.split(";;;")
    else:
        parts = [text]
    return [p.strip() for p in parts if p.strip()]


def _hash_path_mtime(h: Any, abs_path: str, rel: str) -> None:
    h.update(rel.encode("utf-8", errors="replace"))
    try:
        st = os.stat(abs_path)
        h.update(str(int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))).encode("ascii"))
        h.update(str(int(st.st_size)).encode("ascii"))
    except OSError:
        h.update(b"missing")


def workspace_fingerprint(cwd: str) -> str:
    """Fingerprint dirty workspace: porcelain status + path/mtime/size hashes.

    Do NOT use environment_fingerprint here — it omits mtime by design.
    When git is unavailable, fall back to a bounded shallow walk so tests and
    non-git workspaces still observe mtime changes.
    """
    repo = (cwd or "").strip()
    h = hashlib.sha256()
    if not repo or not os.path.isdir(repo):
        h.update(b"no-repo")
        return h.hexdigest()

    porcelain = ""
    paths: List[str] = []
    try:
        from .worktree_seed import _list_git_status_porcelain_paths

        paths = list(_list_git_status_porcelain_paths(repo) or [])
    except Exception:
        paths = []

    try:
        p = subprocess.run(
            [
                "git", "-C", repo, "status", "--porcelain", "-uall",
                "--ignore-submodules=all",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if p.returncode == 0:
            porcelain = p.stdout or ""
    except Exception:
        porcelain = ""

    h.update(porcelain.encode("utf-8", errors="replace"))
    for rel in sorted(paths):
        _hash_path_mtime(h, os.path.join(repo, rel), rel)

    if not paths and not porcelain.strip():
        # Non-git / clean tree: shallow file digest (mtime+size) so a local
        # edit still invalidates a failed-gate skip.
        counted = 0
        for root, dirnames, filenames in os.walk(repo):
            dirnames[:] = [
                d for d in dirnames
                if d not in (".git", "node_modules", ".venv", "__pycache__")
            ]
            for name in sorted(filenames):
                abs_path = os.path.join(root, name)
                rel = os.path.relpath(abs_path, repo).replace("\\", "/")
                _hash_path_mtime(h, abs_path, rel)
                counted += 1
                if counted >= 200:
                    break
            if counted >= 200:
                break
    return h.hexdigest()


def run_shell_gate(
    cwd: str,
    cmd: str,
    *,
    timeout: float = 60.0,
) -> Tuple[bool, str]:
    """Run one gate command. Never raises; returns (passed, truncated_output)."""
    command = (cmd or "").strip()
    if not command:
        return True, ""
    try:
        p = subprocess.run(
            command,
            shell=True,
            cwd=cwd or None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1.0, float(timeout)),
        )
        out = ((p.stdout or "") + ("\n" + p.stderr if p.stderr else "")).strip()
        if len(out) > MAX_GATE_OUTPUT:
            out = out[: MAX_GATE_OUTPUT - 20] + "\n...[truncated]"
        return p.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, "quality gate timed out"
    except Exception as exc:
        return False, "quality gate error: %s" % exc


@dataclass
class QualityGateState:
    """Per-session gate retry / fingerprint memory."""

    attempts: int = 0
    started_at: float = 0.0
    last_failed_fingerprint: str = ""
    last_failed_cmd: str = ""
    last_output: str = ""
    halted: bool = False

    def reset_success(self) -> None:
        self.attempts = 0
        self.started_at = 0.0
        self.last_failed_fingerprint = ""
        self.last_failed_cmd = ""
        self.last_output = ""
        self.halted = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "attempts": int(self.attempts),
            "started_at": float(self.started_at),
            "last_failed_fingerprint": self.last_failed_fingerprint,
            "last_failed_cmd": self.last_failed_cmd,
            "last_output": self.last_output,
            "halted": bool(self.halted),
        }


@dataclass
class QualityGateResult:
    """Outcome of maybe_run_quality_gates."""

    outcome: str  # disabled|passed|failed|skipped_unchanged|budget_halt
    passed: bool
    output: str = ""
    cmd: str = ""
    fingerprint: str = ""
    attempts: int = 0
    block_finish: bool = False

    def event_data(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome,
            "passed": self.passed,
            "output": self.output,
            "cmd": self.cmd,
            "fingerprint": self.fingerprint,
            "attempts": self.attempts,
            "block_finish": self.block_finish,
        }


@dataclass
class QualityGateRunner:
    """Evaluate configured host gates with fingerprint skip + budgets."""

    cmds: List[str] = field(default_factory=list)
    max_attempts: int = 3
    max_seconds: float = 120.0
    on_auto: bool = False
    state: QualityGateState = field(default_factory=QualityGateState)

    @classmethod
    def from_config(cls, config: Any) -> "QualityGateRunner":
        raw = getattr(config, "quality_gate_cmds", None)
        if raw in (None, ""):
            raw = getattr(config, "quality_gate_cmd", "")
        try:
            max_attempts = int(getattr(config, "max_gate_attempts", 3) or 3)
        except (TypeError, ValueError):
            max_attempts = 3
        try:
            max_seconds = float(getattr(config, "max_gate_seconds", 120.0) or 120.0)
        except (TypeError, ValueError):
            max_seconds = 120.0
        on_auto = bool(getattr(config, "quality_gate_on_auto", False))
        return cls(
            cmds=_parse_cmds(raw),
            max_attempts=max(1, max_attempts),
            max_seconds=max(1.0, max_seconds),
            on_auto=on_auto,
        )

    def enabled(self) -> bool:
        return bool(self.cmds)

    def should_run(self, *, auto_mode: bool) -> bool:
        if not self.enabled():
            return False
        if auto_mode and not self.on_auto:
            return False
        return True

    def run(
        self,
        cwd: str,
        *,
        auto_mode: bool = False,
    ) -> QualityGateResult:
        if not self.should_run(auto_mode=auto_mode):
            return QualityGateResult(outcome="disabled", passed=True)

        if self.state.halted:
            return QualityGateResult(
                outcome="budget_halt",
                passed=False,
                output=self.state.last_output,
                cmd=self.state.last_failed_cmd,
                fingerprint=self.state.last_failed_fingerprint,
                attempts=self.state.attempts,
                block_finish=True,
            )

        fp = workspace_fingerprint(cwd)
        if (
            self.state.last_failed_fingerprint
            and fp == self.state.last_failed_fingerprint
            and self.state.last_failed_cmd
        ):
            return QualityGateResult(
                outcome="skipped_unchanged",
                passed=False,
                output=self.state.last_output or "quality gate skipped: workspace unchanged",
                cmd=self.state.last_failed_cmd,
                fingerprint=fp,
                attempts=self.state.attempts,
                block_finish=True,
            )

        now = time.time()
        if not self.state.started_at:
            self.state.started_at = now

        if self.state.attempts >= self.max_attempts:
            self.state.halted = True
            return QualityGateResult(
                outcome="budget_halt",
                passed=False,
                output="quality gate halted: max_gate_attempts reached",
                cmd=self.state.last_failed_cmd,
                fingerprint=fp,
                attempts=self.state.attempts,
                block_finish=True,
            )
        if (now - self.state.started_at) > self.max_seconds:
            self.state.halted = True
            return QualityGateResult(
                outcome="budget_halt",
                passed=False,
                output="quality gate halted: max_gate_seconds exceeded",
                cmd=self.state.last_failed_cmd,
                fingerprint=fp,
                attempts=self.state.attempts,
                block_finish=True,
            )

        remaining = max(1.0, self.max_seconds - (now - self.state.started_at))
        for cmd in self.cmds:
            self.state.attempts += 1
            ok, output = run_shell_gate(cwd, cmd, timeout=min(60.0, remaining))
            if not ok:
                self.state.last_failed_fingerprint = fp
                self.state.last_failed_cmd = cmd
                self.state.last_output = output
                if self.state.attempts >= self.max_attempts:
                    self.state.halted = True
                    return QualityGateResult(
                        outcome="budget_halt",
                        passed=False,
                        output=output or "quality gate failed",
                        cmd=cmd,
                        fingerprint=fp,
                        attempts=self.state.attempts,
                        block_finish=True,
                    )
                return QualityGateResult(
                    outcome="failed",
                    passed=False,
                    output=output or "quality gate failed",
                    cmd=cmd,
                    fingerprint=fp,
                    attempts=self.state.attempts,
                    block_finish=True,
                )

        self.state.reset_success()
        return QualityGateResult(
            outcome="passed",
            passed=True,
            fingerprint=fp,
            attempts=0,
            block_finish=False,
        )


def maybe_run_quality_gates(
    session: Any,
    *,
    auto_mode: Optional[bool] = None,
) -> Optional[QualityGateResult]:
    """Run session quality gates if configured. Returns None when disabled."""
    runner = getattr(session, "_quality_gate", None)
    if runner is None:
        try:
            runner = QualityGateRunner.from_config(getattr(session, "config", None))
            session._quality_gate = runner
        except Exception:
            return None
    if auto_mode is None:
        auto_mode = bool(getattr(session, "_auto_mode", False))
    if not runner.should_run(auto_mode=auto_mode):
        return None
    cwd = ""
    try:
        cwd = str(getattr(getattr(session, "config", None), "repo", "") or "")
    except Exception:
        cwd = ""
    return runner.run(cwd, auto_mode=auto_mode)


def gate_retry_prompt(result: QualityGateResult) -> str:
    """Continuation nudge after a failed gate (within budgets)."""
    cmd = result.cmd or "quality gate"
    snippet = (result.output or "").strip()
    if len(snippet) > 800:
        snippet = snippet[:780] + "\n...[truncated]"
    return (
        "Quality gate failed (%s). Fix the workspace so the gate passes, "
        "then continue. Output:\n%s" % (cmd, snippet or "(no output)")
    )
