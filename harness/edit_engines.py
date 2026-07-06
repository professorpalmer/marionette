from __future__ import annotations

"""In-process edit engines for run_implement / run_parallel.

Two engines, one normalized result (:class:`harness.worker.WorkerResult`), so the
downstream apply/review/checkpoint pipeline never has to care which one ran:

* ``agentic`` -- Puppetmaster's first-class, provider-agnostic adapter. Runs its
  own tool-use loop directly against a provider HTTP API on the user's own key
  (no external agent CLI), with the router picking a right-sized model among the
  providers the keys unlock. This is the standalone default whenever a provider
  key is present. We run it inside an isolated worktree and capture the diff, so
  edits never touch the live repo until the normal review/apply gate passes --
  identical isolation to the native engine.
* ``native`` -- Marionette's own pilot (:class:`ConversationalSession`) driven
  inside the worktree. Richer toolset (run_command, tests, codegraph, web) and
  the automatic fallback when no provider key is available.

Engine selection is provider-key-aware and overridable via ``HARNESS_EDIT_ENGINE``
or an explicit adapter on the action. The dispatcher falls back from agentic to
native only when agentic genuinely cannot run (no key / router could not pick a
model) -- never when agentic ran and simply produced no changes.
"""

import contextlib
import os
import shutil
import subprocess
import tempfile
import uuid
from typing import TYPE_CHECKING, Iterator, Optional

from harness.diag import note as _diag

if TYPE_CHECKING:
    from harness.config import HarnessConfig
    from harness.worker import WorkerResult


# Untracked build/agent artifacts a worker may create when it runs tests; kept
# out of the captured diff so a patch is only real source edits.
_ARTIFACT_PATHSPECS = [
    "*.pyc", "*.pyo", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "*.egg-info", ".coverage",
    "node_modules", ".DS_Store",
]

# Machine-readable reasons that mean "the agentic engine could not run at all"
# (as opposed to "ran fine, made no changes"). Only these trigger native fallback.
AGENTIC_UNAVAILABLE = "agentic_unavailable"
AGENTIC_ROUTE_FAILED = "agentic_route_failed"
AGENTIC_ERROR = "agentic_error"
_FALLBACK_REASONS = (AGENTIC_UNAVAILABLE, AGENTIC_ROUTE_FAILED, AGENTIC_ERROR)


@contextlib.contextmanager
def managed_worktree(repo: str, base: str = "HEAD") -> Iterator[str]:
    """Create a confined git worktree for `repo`, yield its path, always clean up.

    Both engines edit inside the worktree so the live repo is untouched until the
    review/apply gate runs. The worktree and its throwaway branch are removed on
    exit even when the body raises.
    """
    from harness.worktrees import (
        _get_managed_dir,
        _is_confined,
        _safe_branch_name,
        add_worktree,
        delete_branch,
        remove_worktree,
    )

    branch_name = _safe_branch_name(f"pmedit-{uuid.uuid4().hex[:8]}")
    wt_path = ""
    try:
        wt_info = add_worktree(repo, branch=branch_name, base=base)
        wt_path = wt_info["path"]
        if not _is_confined(wt_path, _get_managed_dir(repo)):
            raise ValueError(
                "Confinement violation: worktree path lies outside the managed directory"
            )
        yield wt_path
    finally:
        if wt_path:
            with contextlib.suppress(Exception):
                remove_worktree(repo, wt_path, force=True)
        with contextlib.suppress(Exception):
            delete_branch(repo, branch_name)


def finalize_worktree_patch(wt_path: str) -> tuple[str, list[str]]:
    """Stage everything in `wt_path`, drop build artifacts, return (patch, files).

    Returns the ``git diff --cached`` unified diff and the list of changed paths.
    Raises RuntimeError when a git step fails so the caller can report honestly.
    """
    rc_add, out_add, err_add = _git(wt_path, "add", "-A")
    if rc_add != 0:
        raise RuntimeError(f"git add failed: {err_add or out_add}")

    reset_specs: list[str] = []
    for spec in _ARTIFACT_PATHSPECS:
        reset_specs.append(f":(glob){spec}")
        reset_specs.append(f":(glob)**/{spec}")
    _git(wt_path, "reset", "-q", "--", *reset_specs)

    p_diff = subprocess.run(
        ["git", "-C", wt_path, "diff", "--cached", "--no-color"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )
    if p_diff.returncode != 0:
        raise RuntimeError(f"git diff failed: {p_diff.stderr or p_diff.stdout}")
    patch = p_diff.stdout

    p_files = subprocess.run(
        ["git", "-C", wt_path, "diff", "--cached", "--name-only"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
    )
    files_changed = [ln.strip() for ln in p_files.stdout.splitlines() if ln.strip()]
    return patch, files_changed


def _git(cwd: str, *args: str) -> tuple[int, str, str]:
    from harness.worktrees import _git as _worktree_git

    return _worktree_git(cwd, *args)


def agentic_available() -> bool:
    """True when the agentic engine can actually run: a provider key is visible
    to this process. Mirrors Puppetmaster's key-aware adapter availability so the
    UI and dispatcher agree on whether keys-only edits are possible."""
    try:
        from puppetmaster import providers

        available = providers.available_providers()
        return bool(available)
    except Exception as exc:
        _diag("edit_engines.agentic_available", exc)
        # Fall back to a direct env check so a provider API shift never silently
        # disables the default engine.
        return any(
            os.environ.get(k, "").strip()
            for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
                      "GOOGLE_API_KEY", "OPENROUTER_API_KEY")
        )


def select_edit_engine(config: "HarnessConfig", requested_adapter: str = "") -> str:
    """Pick the in-process edit engine: 'agentic' or 'native'.

    Precedence: explicit action adapter > HARNESS_EDIT_ENGINE env > provider-key
    availability. External CLI adapters (cursor/claude-code/codex) are handled by
    the caller before this point and never reach here.
    """
    requested = (requested_adapter or "").strip().lower()
    if requested in ("native", "provider"):
        return "native"
    if requested == "agentic":
        return "agentic" if agentic_available() else "native"

    env_choice = (os.environ.get("HARNESS_EDIT_ENGINE", "") or "").strip().lower()
    if env_choice in ("native", "agentic"):
        return env_choice if (env_choice == "native" or agentic_available()) else "native"

    return "agentic" if agentic_available() else "native"


def run_edit_worker(
    config: "HarnessConfig", goal: str, requested_adapter: str = "",
    job_id: str = "",
) -> "WorkerResult":
    """Run the selected in-process edit engine and return a normalized result.

    Falls back from agentic to native only when agentic could not run at all.
    """
    engine = select_edit_engine(config, requested_adapter)
    if engine == "agentic":
        result = run_agentic_edit(config, goal)
        if result.error in _FALLBACK_REASONS:
            _diag("edit_engines.run_edit_worker",
                  msg=f"agentic engine unavailable ({result.error}); falling back to native")
            return run_native_edit(config, goal, job_id=job_id)
        return result
    return run_native_edit(config, goal, job_id=job_id)


def run_native_edit(config: "HarnessConfig", goal: str, job_id: str = "") -> "WorkerResult":
    """Marionette's own pilot loop driven in a worktree (the rich engine)."""
    from harness.autobudget import AutoBudget
    from harness.worker import ProviderWorker

    worker = ProviderWorker(
        config.repo, goal,
        driver=config.driver, reach=config.reach,
        budget=AutoBudget.from_env(), require_codegraph=False,
        job_id=job_id,
    )
    # ProviderWorker.run() stamps tokens_out from the budget on every return path.
    return worker.run()


def run_agentic_edit(config: "HarnessConfig", goal: str) -> "WorkerResult":
    """Puppetmaster's first-class agentic adapter in implement mode, run in an
    isolated worktree; the diff is captured for the normal review/apply gate.

    Never raises for a run failure -- it returns a WorkerResult whose ``error`` is
    one of the ``AGENTIC_*`` reasons so the dispatcher can fall back to native.
    """
    from harness.worker import WorkerResult

    if not agentic_available():
        return WorkerResult(ok=False, error=AGENTIC_UNAVAILABLE,
                            summary="No provider key visible for the agentic engine.")

    try:
        from puppetmaster.orchestrator import Orchestrator
        from puppetmaster.store_factory import create_store
        from puppetmaster.workers import WorkerSpec
    except Exception as exc:
        _diag("edit_engines.run_agentic_edit.import", exc)
        return WorkerResult(ok=False, error=AGENTIC_UNAVAILABLE,
                            summary=f"Puppetmaster unavailable: {exc}")

    provider = (os.environ.get("HARNESS_IMPLEMENT_PROVIDER", "") or "").strip().lower()
    model = (os.environ.get("HARNESS_IMPLEMENT_MODEL", "") or "").strip()

    try:
        with managed_worktree(config.repo) as wt_path:
            payload: dict = {
                "mode": "implement",
                "cwd": wt_path,
                "prompt": goal,
                "auto_route": not (provider and model),
            }
            if not (provider and model):
                # Cost guardrail (mirrors the analysis-swarm cap in bridge.py):
                # role="implement" has a high base score that first-picks the
                # frontier model (opus, ~$15/$75 per Mtok). A balanced policy with
                # a capability ceiling lands on a strong-but-far-cheaper coder
                # (sonnet, ~$3/$15) that is more than capable of edits. Opt into
                # frontier depth with HARNESS_IMPLEMENT_DEEP=1.
                payload["routing_policy"] = "balanced"
                if os.environ.get("HARNESS_IMPLEMENT_DEEP", "").strip() not in ("1", "true", "yes"):
                    try:
                        payload["min_capability"] = int(
                            os.environ.get("HARNESS_IMPLEMENT_MAX_CAPABILITY", "86"))
                    except (TypeError, ValueError):
                        payload["min_capability"] = 86
            if provider:
                payload["provider"] = provider
            if model:
                payload["model"] = model

            spec = WorkerSpec(
                role="implement",
                instruction=goal,
                adapter="agentic",
                payload=payload,
            )
            # The PM sqlite store is scratch state for this single inline run; we
            # read everything we need from the returned `result`, not the store,
            # so it is removed as soon as the run returns. Without this every
            # agentic implement worker leaked a pmh-edit-* dir (audit finding #3).
            tmp = tempfile.mkdtemp(prefix="pmh-edit-")
            try:
                store = create_store("sqlite", tmp)
                result = Orchestrator(store).run(goal, specs=[spec], worker_mode="inline")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

            patch, files_changed = finalize_worktree_patch(wt_path)
            tokens_out, tokens_in, failure, final_text = _summarize_agentic_result(result)

            if not patch.strip():
                # Distinguish "engine could not run" (route/provider failure) from
                # "ran fine but changed nothing" so fallback only fires for the former.
                if failure in ("no_model", "unknown_provider", "route_failed"):
                    return WorkerResult(ok=False, error=AGENTIC_ROUTE_FAILED,
                                        summary=final_text or "Agentic engine could not select a model/provider.")
                return WorkerResult(ok=False, tokens_out=tokens_out, tokens_in=tokens_in,
                                    summary=final_text or "no changes produced")

            return WorkerResult(
                ok=True, patch=patch, files_changed=files_changed,
                tokens_out=tokens_out, tokens_in=tokens_in,
                summary=final_text or (f"Files changed: {', '.join(files_changed)}" if files_changed else "Patch generated"),
            )
    except Exception as exc:
        _diag("edit_engines.run_agentic_edit", exc)
        return WorkerResult(ok=False, error=AGENTIC_ERROR, summary=f"Agentic engine error: {exc}")


def _summarize_agentic_result(result) -> tuple[int, int, str, str]:
    """Pull (tokens_out, tokens_in, failure_reason, final_text) from PM artifacts.

    Both token directions are summed so cost/telemetry counts the prompt tokens
    an implement worker burned, not just its completion tokens (audit finding #5)."""
    tokens_out = 0
    tokens_in = 0
    failure = ""
    final_text = ""
    for art in getattr(result, "artifacts", []) or []:
        payload = getattr(art, "payload", {}) or {}
        tokens_out += int(payload.get("tokens_out") or 0)
        tokens_in += int(payload.get("tokens_in") or 0)
        if not failure and payload.get("failure"):
            failure = str(payload.get("failure"))
        stdout = payload.get("stdout")
        if stdout and not final_text:
            final_text = str(stdout)[:2000]
    return tokens_out, tokens_in, failure, final_text
