from __future__ import annotations

"""pm_bridge: execute a validated DriverIntent against Puppetmaster's
in-process Orchestrator and normalize the result.

This is the proven seam (Stage 1): MCP and CLI are both thin transports over
Orchestrator(store).run(...). The bridge calls that engine directly -- no MCP,
no CLI subprocess -- which is the entire point of a PM-native harness.

Execution uses an isolated temp SQLite store and the default role path, which
runs on Puppetmaster's free local adapter. For the DRIVER eval that is exactly
what we want: deterministic, key-free ground truth so we measure the driver
model, not worker quality (a separate question).
"""

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .intent import DriverIntent


def _analysis_provider_payload() -> dict:
    """Provider knobs for the read-only analysis worker. Defaults to OpenRouter
    (funded, open models) since the OpenAI adapter speaks the OpenAI-compatible
    schema; set HARNESS_ANALYSIS_REACH=openai to use the native OpenAI API.

    The API KEY is NOT placed in the payload (transiting tool/secret layers can
    truncate it); instead _prepare_analysis_env() sets OPENAI_API_KEY +
    OPENAI_BASE_URL in the process env, which the adapter reads natively."""
    import os
    reach = (os.environ.get("HARNESS_ANALYSIS_REACH", "openrouter") or "openrouter").lower()
    if reach == "openai":
        return {"skip_preflight": True}
    model = os.environ.get("HARNESS_ANALYSIS_MODEL", "qwen/qwen3-coder-30b-a3b-instruct")
    return {
        "model": model,
        "openai_allow_untrusted_base_url": True,
        "skip_preflight": True,
    }


def _codegraph_indexed(repo_cwd: str) -> bool:
    """True when the target repo has a CodeGraph index. Without it, the analysis
    worker gets NO source context and guesses -- the benchmark proved accuracy
    collapses from ~81% to ~30% (blind). We surface this loudly."""
    import os
    return os.path.isdir(os.path.join(repo_cwd, ".codegraph"))


def _warn_if_unindexed(repo_cwd: str) -> None:
    """Emit a clear warning (stderr) when real analysis runs on an unindexed repo.
    Set HARNESS_REQUIRE_CODEGRAPH=1 to hard-fail instead of degrade silently."""
    import os, sys
    if _codegraph_indexed(repo_cwd):
        return
    msg = (f"[harness] WARNING: {repo_cwd} has no .codegraph index -- real analysis "
           f"will run BLIND (no source context, ~30% accuracy vs ~81% indexed). "
           f"Run: python -m puppetmaster codegraph init --index  (cwd={repo_cwd})")
    if os.environ.get("HARNESS_REQUIRE_CODEGRAPH", "").strip() in ("1", "true", "yes"):
        raise RuntimeError(msg.replace("WARNING", "ERROR") +
                           "  [HARNESS_REQUIRE_CODEGRAPH=1]")
    print(msg, file=sys.stderr)


def _prepare_analysis_env() -> None:
    """Point the OpenAI adapter at OpenRouter via process env (masker-safe).
    Only acts when reach is openrouter (default) and a key is present."""
    import os
    reach = (os.environ.get("HARNESS_ANALYSIS_REACH", "openrouter") or "openrouter").lower()
    if reach == "openai":
        return
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        os.environ["OPENAI_API_KEY"] = key
        os.environ["OPENAI_BASE_URL"] = "https://openrouter.ai/api/v1"


@dataclass
class BridgeResult:
    job_id: str
    status: str
    mode: str
    num_artifacts: int
    artifact_types: list
    summary: str
    artifacts: list  # list of compact dicts (type, claim/decision/etc snippet)
    adapter: str = "demo"  # "demo" = local deterministic substrate (not real
    #   codebase analysis); set to a real worker adapter when configured. Surfaces
    #   use this to label generic substrate so it is never mistaken for real
    #   findings.


def _compact_artifact(a: Any) -> dict:
    """Reduce a Puppetmaster Artifact to a small dict suitable for feeding back
    to a driver model on a follow-up turn without blowing context."""
    payload = getattr(a, "payload", {}) or {}
    headline = (
        payload.get("claim")
        or payload.get("decision")
        or payload.get("risk")
        or payload.get("check")
        or payload.get("summary")
        or payload.get("change")
        or ""
    )
    return {
        "type": str(getattr(a, "type", "")),
        "headline": str(headline)[:240],
        "confidence": getattr(a, "confidence", None),
    }


def execute_intent(
    intent: DriverIntent,
    *,
    state_dir: Optional[str] = None,
    worker_mode: Optional[str] = None,
) -> Optional[BridgeResult]:
    """Run a run_swarm intent against Puppetmaster. Returns None for non-swarm
    actions (answer/stop) since there is nothing to execute.

    Imports of puppetmaster are local so the schema/validation layer stays
    importable with zero PM dependency (keeps unit tests fast and hermetic).
    """
    if intent.action != "run_swarm":
        return None
    if not intent.goal:
        raise ValueError("cannot execute run_swarm intent without a goal")

    import os as _os
    from puppetmaster.store_factory import create_store
    from puppetmaster.orchestrator import Orchestrator

    tmp = state_dir or tempfile.mkdtemp(prefix="pmh-exec-")
    store = create_store("sqlite", tmp)

    # Swarm adapter selection (safety-first):
    #   demo (default)  -> built-in local demo adapter: deterministic, free, no
    #                      real code analysis. The substrate for driver eval.
    #   openai          -> REAL LLM analysis of REAL code. We build READ-ONLY
    #                      analysis WorkerSpecs pointed at the target repo cwd so
    #                      Puppetmaster injects CodeGraph context. The "openai"
    #                      adapter is NOT in _EDIT_CAPABLE_ADAPTERS, and we also
    #                      stamp read_only=True -- a triple guard so a real run
    #                      can NEVER edit a target repo (safe even on live repos).
    swarm_adapter = (_os.environ.get("HARNESS_SWARM_ADAPTER", "demo") or "demo").lower()
    repo_cwd = _os.environ.get("HARNESS_REPO", "").strip()

    if swarm_adapter == "agentic" and repo_cwd:
        # Standalone path: run READ-ONLY analysis workers on the built-in
        # 'agentic' adapter, which calls a provider API directly on the user's
        # own key -- no external agent CLI. auto_route lets Puppetmaster's router
        # pick the right-sized model among ONLY the providers the user's keys
        # unlock (key-aware filter) and the enabled platform lock. The agentic
        # adapter's analyze mode exposes no edit tools, so this is safe on live
        # repos even before the triple read-only guard below.
        _warn_if_unindexed(repo_cwd)
        from puppetmaster.workers import WorkerSpec
        roles = intent.roles or ["explore"]
        specs = []
        for r in roles:
            specs.append(WorkerSpec(
                role=r,
                instruction=(
                    f"{intent.goal}\n\nAnalyze the REAL codebase at {repo_cwd}. "
                    f"Emit evidenced findings/risks/decisions as artifacts. This is "
                    f"a READ-ONLY analysis: do not edit, create, or delete any files."
                ),
                adapter="agentic",
                payload={
                    "read_only": True, "no_edit": True, "dry_run": True,
                    "cwd": repo_cwd, "prompt": intent.goal,
                    "auto_route": True,
                },
            ))
        result = Orchestrator(store).run(
            intent.goal, specs=specs, worker_mode=worker_mode or "inline",
        )
        adapter = "agentic"
    elif swarm_adapter == "openai" and repo_cwd:
        _prepare_analysis_env()
        _warn_if_unindexed(repo_cwd)
        from puppetmaster.workers import WorkerSpec
        roles = intent.roles or ["explore"]
        specs = []
        for r in roles:
            specs.append(WorkerSpec(
                role=r,
                instruction=(
                    f"{intent.goal}\n\nAnalyze the REAL codebase at {repo_cwd}. "
                    f"Emit evidenced findings/risks/decisions as artifacts. This is "
                    f"a READ-ONLY analysis: do not edit, create, or delete any files."
                ),
                adapter="openai",
                payload={
                    "read_only": True, "no_edit": True, "dry_run": True,
                    "cwd": repo_cwd, "prompt": intent.goal,
                    "auto_route": False,
                    # Route analysis through OpenRouter (funded, open models) by
                    # default; the OpenAI adapter speaks the OpenAI-compatible
                    # schema so base_url + key + an open model just works. Falls
                    # back to native OpenAI only if HARNESS_ANALYSIS_REACH=openai.
                    **_analysis_provider_payload(),
                },
            ))
        # inline: the analysis worker runs in-process so the env-based key
        # wiring propagates reliably, and it yields richer multi-artifact output.
        result = Orchestrator(store).run(
            intent.goal, specs=specs, worker_mode=worker_mode or "inline",
        )
        adapter = "openai"
    else:
        # The default role path (roles=None) uses the built-in local demo adapter:
        # no API keys, deterministic, free. Label as demo substrate honestly.
        result = Orchestrator(store).run(
            intent.goal,
            roles=intent.roles,
            worker_mode=worker_mode or "subprocess",
        )
        adapter = "demo"

    artifacts = list(result.artifacts)
    return BridgeResult(
        job_id=result.job.id,
        status=str(result.job.status),
        mode=str(result.mode),
        num_artifacts=len(artifacts),
        artifact_types=sorted({str(a.type) for a in artifacts}),
        summary=result.summary or "",
        artifacts=[_compact_artifact(a) for a in artifacts],
        adapter=adapter,
    )
