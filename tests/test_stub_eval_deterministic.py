"""Deterministic offline stub harness evaluation for the Wave 6 CI gate.

Hermetic: no API keys, no real Puppetmaster Orchestrator. Proves the offline
oracle ceilings stay perfect for Stage-1 decision quality and Stage-3.5
budget-aware trajectories (with a synthetic substrate in place of live PM).

Live-key / provider evals stay outside required CI.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from pmharness.bridge import BridgeResult
from pmharness.ledger import Ledger
from pmharness.registry import build
from pmharness.runner import new_run_id, run_driver

pytestmark = pytest.mark.full_auto_safety


def _synthetic_bridge_result(_intent=None, **_kwargs):
    return BridgeResult(
        job_id="stub-gate-job",
        status="JobStatus.COMPLETE",
        mode="analyze",
        num_artifacts=1,
        artifact_types=["finding"],
        summary="synthetic substrate for offline ceiling",
        artifacts=[{"type": "finding", "headline": "established conclusion"}],
        adapter="demo",
    )


def test_stub_oracle_stage1_decision_ceiling_without_execution():
    """Stage-1 battery: stub decides every labeled action correctly (no PM)."""
    tmp = tempfile.mkdtemp(prefix="pmh-stub-gate-")
    ledger = Ledger(Path(tmp) / "ledger.sqlite")
    run_id = new_run_id()
    try:
        scores = run_driver(
            build("stub-oracle"), ledger, run_id=run_id, execute=False,
        )
        assert len(scores) == 10
        assert all(
            s.json_valid and s.schema_valid and s.action_correct for s in scores
        )
        # Without the must_execute weight, swarm cases top at 0.8; others at 1.0.
        mean = sum(s.score for s in scores) / len(scores)
        assert mean == 0.9, f"stub-oracle decision ceiling broke: mean={mean}"
    finally:
        ledger.close()


def test_stub_oracle_v2_ceiling_with_synthetic_substrate(monkeypatch):
    """Stage-3.5 battery: stub + synthetic PM feedback scores perfect offline."""
    from pmharness.episode_v2 import EPISODES_V2
    from pmharness.episode_v2_runner import run_episode_v2
    from pmharness.scoring_v2 import score_v2
    import pmharness.episode_v2_runner as runner_mod

    monkeypatch.setattr(runner_mod, "execute_intent", _synthetic_bridge_result)
    drv = build("stub-oracle-v2")
    scores = [score_v2(ep, run_episode_v2(drv, ep)) for ep in EPISODES_V2]
    assert len(scores) == len(EPISODES_V2)
    mean = sum(s.score for s in scores) / len(scores)
    assert mean == 1.0, f"stub-oracle-v2 ceiling broke: mean={mean}"
