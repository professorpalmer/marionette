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

    from puppetmaster.store_factory import create_store
    from puppetmaster.orchestrator import Orchestrator

    tmp = state_dir or tempfile.mkdtemp(prefix="pmh-exec-")
    store = create_store("sqlite", tmp)

    # The default role path (roles=None) uses the built-in local demo adapter:
    # no API keys, deterministic, free. If the driver named known roles we honor
    # them; execution still routes through the local adapter for the driver eval.
    result = Orchestrator(store).run(
        intent.goal,
        roles=intent.roles,
        worker_mode=worker_mode or "subprocess",
    )

    artifacts = list(result.artifacts)
    # The default role path routes through Puppetmaster's built-in local demo
    # adapter -- deterministic substrate, NOT real codebase analysis. Label it so
    # surfaces can be honest. (Real-worker routing is a configured enhancement.)
    adapter = "demo"
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
