"""Wave 5: calm full-auto operator receipt copy + terminal snapshot honesty."""
from __future__ import annotations

import tempfile

import pytest
from harness.auto_receipts import (
    format_auto_halt_receipt,
    format_auto_status_receipt,
    format_budget_meters,
    format_command_blocked_receipt,
)
from harness.autobudget import AutoBudget
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from pmharness.bridge import BridgeResult
from pmharness.drivers.openai_compat import DriverResponse


@pytest.fixture(autouse=True)
def _fast_swarm(monkeypatch):
    fake = lambda intent, **kw: BridgeResult(
        job_id="job_fake", status="complete", mode="analysis",
        num_artifacts=1, artifact_types=["finding"], summary="fake",
        artifacts=[{"type": "finding", "headline": "fake finding"}],
        adapter="local",
    )
    monkeypatch.setattr("harness.send_loop_phases.execute_intent", fake)
    monkeypatch.setattr("harness.conversation.execute_intent", fake)


def test_format_budget_meters_quiet_status_bar_style():
    meters = format_budget_meters({
        "tokens_used": 4100,
        "max_tokens": 50_000,
        "swarms_used": 2,
        "max_swarms": 20,
        "elapsed_s": 45,
    })
    assert meters == "2/20 swarms · 4.1k/50k tok · 45s"


def test_auto_status_receipt_never_implies_compaction_or_success():
    text = format_auto_status_receipt(3, {
        "tokens_used": 1000,
        "max_tokens": 50_000,
        "swarms_used": 1,
        "max_swarms": 20,
        "elapsed_s": 12,
    })
    assert text.startswith("Full-auto · cycle 3")
    assert "compact" not in text.lower()
    assert "done" not in text.lower()
    assert "executed" not in text.lower()
    assert "1/20 swarms" in text


def test_auto_halt_receipt_labels_are_truthful():
    snap = {
        "tokens_used": 800,
        "max_tokens": 50_000,
        "swarms_used": 1,
        "max_swarms": 20,
        "elapsed_s": 9,
    }
    finished = format_auto_halt_receipt("objective met and verified", snap)
    assert finished.startswith("Full-auto finished:")
    assert "objective met" in finished
    assert "compact" not in finished.lower()

    halted = format_auto_halt_receipt("swarm ceiling reached (3/3)", snap)
    assert halted.startswith("Full-auto halted:")
    assert "executed" not in halted.lower()

    blocked = format_command_blocked_receipt("remote command execution", "remote-shell")
    assert blocked.startswith("Command not run:")
    assert "remote command execution" in blocked


def test_auto_halt_event_carries_snapshot_without_success_flags():
    class _DonePilot:
        name = "done"

        def __init__(self):
            self.n = 0

        def complete(self, prompt, *, system=None, tools=None):
            self.n += 1
            if self.n == 1:
                text = '{"say":"checking","actions":[{"kind":"run_swarm","goal":"look"}]}'
            else:
                text = '{"say":"Objective met.","actions":[]}'
            return DriverResponse(text=text, tokens_out=10, latency_ms=1.0)

    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    session = ConversationalSession(cfg)
    session.pilot = _DonePilot()
    events = list(session.run_auto(
        "quick check",
        AutoBudget(max_swarms=20, max_tokens=500_000),
    ))
    halts = [e for e in events if e.kind == "auto_halt"]
    assert halts
    data = halts[-1].data
    assert "objective met" in data["reason"]
    snap = data.get("snapshot") or {}
    assert "tokens_used" in snap
    assert "swarms_used" in snap
    # Receipt payload must not claim compaction or successful shell execution.
    assert "compacted" not in snap
    assert "executed" not in snap
    assert "success" not in snap
