"""Sync analysis run_swarm must land in Swarm Tracker and refuse green badges
for routing/verification-only (plumbing) results."""
from __future__ import annotations

import tempfile

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from pmharness.bridge import BridgeResult
from pmharness.drivers.openai_compat import DriverResponse


def _plumbing_result():
    return BridgeResult(
        job_id="job_plumbing_abc",
        status="complete",
        mode="analysis",
        num_artifacts=5,
        artifact_types=["routing", "verification"],
        summary="plumbing only",
        artifacts=[
            {"type": "routing", "headline": "routed explore"},
            {"type": "routing", "headline": "routed review"},
            {"type": "verification", "headline": "ok"},
            {"type": "verification", "headline": "ok"},
            {"type": "verification", "headline": "ok"},
        ],
        adapter="agentic",
    )


def _signal_result():
    return BridgeResult(
        job_id="job_signal_xyz",
        status="complete",
        mode="analysis",
        num_artifacts=3,
        artifact_types=["finding", "routing", "verification"],
        summary="has signal",
        artifacts=[
            {"type": "finding", "headline": "auth gap in session store: harness/sessions.py line 88 skips token check"},
            {"type": "routing", "headline": "routed explore"},
            {"type": "verification", "headline": "ok"},
        ],
        adapter="agentic",
    )


class _SwarmOncePilot:
    name = "swarm-once"

    def __init__(self):
        self.n = 0

    def complete(self, prompt, *, system=None, tools=None):
        self.n += 1
        if self.n == 1:
            return DriverResponse(
                text=(
                    '{"say":"auditing",'
                    '"actions":[{"kind":"run_swarm","goal":"Audit the repo",'
                    '"roles":["explore","conflict-auditor"]}]}'
                ),
                tokens_out=8,
                latency_ms=1.0,
            )
        return DriverResponse(
            text='{"say":"done.","actions":[]}',
            tokens_out=4,
            latency_ms=1.0,
        )


def test_plumbing_only_swarm_marks_failed_and_registers_tracker(monkeypatch):
    monkeypatch.setattr(
        "harness.send_loop_phases.execute_intent",
        lambda intent, **kw: _plumbing_result(),
    )
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    s = ConversationalSession(cfg)
    s.pilot = _SwarmOncePilot()
    events = list(s.send("please audit"))

    pending = [e for e in events if e.kind == "swarm_pending"]
    assert pending, "sync swarm must emit swarm_pending for the tracker"

    results = [e for e in events if e.kind == "swarm_result"]
    assert results
    badge = results[0].data["result"]
    assert badge["applied"] is False
    assert "degraded" in (badge.get("summary") or "").lower() or badge.get("error")
    assert badge["job_id"] == "job_plumbing_abc"

    assert "job_plumbing_abc" in s._session_job_ids
    live = s.live_local_jobs()
    ids = {j.get("id") for j in live}
    assert "job_plumbing_abc" in ids or any(
        j.get("id", "").startswith("local-swarm-") for j in live
    )


def test_signal_swarm_marks_applied_true(monkeypatch):
    monkeypatch.setattr(
        "harness.send_loop_phases.execute_intent",
        lambda intent, **kw: _signal_result(),
    )
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    s = ConversationalSession(cfg)
    s.pilot = _SwarmOncePilot()
    events = list(s.send("please audit"))
    results = [e for e in events if e.kind == "swarm_result"]
    assert results
    badge = results[0].data["result"]
    assert badge["applied"] is True
    assert "finding" in (badge.get("summary") or "").lower()
    assert badge.get("error") in (None, "")
