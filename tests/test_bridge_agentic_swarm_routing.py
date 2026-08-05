"""Agentic analysis swarms must stay on the agentic adapter.

Regression: prefer_plan_billed first-picked Cursor GPT ($0 plan), then
router-fallback landed on openai/gpt-* even when Models toggles only enabled
OpenRouter pilots -- tracker showed a GPT model the picker never offered.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

import pmharness.bridge as bridge
from pmharness.intent import DriverIntent


@dataclass
class _CapturingWorkerSpec:
    role: str
    instruction: str
    adapter: str
    payload: dict = field(default_factory=dict)
    captured: list = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        type(self)._last_captured.append(self)


class _FakeJob:
    id = "job_test"
    status = "complete"


class _FakeResult:
    job = _FakeJob()
    status = "complete"
    mode = "inline"
    artifacts: list = []
    summary = "ok"


class _FakeOrchestrator:
    def __init__(self, store: Any) -> None:
        self.store = store

    def run(self, goal: str, specs=None, worker_mode=None, label=None):
        return _FakeResult()


def test_agentic_swarm_pins_allowed_adapters(monkeypatch, tmp_path):
    _CapturingWorkerSpec._last_captured = []
    monkeypatch.setenv("HARNESS_SWARM_ADAPTER", "agentic")
    monkeypatch.setenv("HARNESS_REPO", str(tmp_path))
    monkeypatch.setattr("puppetmaster.workers.WorkerSpec", _CapturingWorkerSpec)
    monkeypatch.setattr("puppetmaster.orchestrator.Orchestrator", _FakeOrchestrator)
    monkeypatch.setattr(bridge, "_warn_if_unindexed", lambda *_a, **_k: None)

    intent = DriverIntent(
        action="run_swarm",
        goal="Trace the live scoring pipeline for a points flicker",
        roles=["pipeline-mapper"],
    )
    result = bridge.execute_intent(intent, state_dir=str(tmp_path / "state"))
    assert result is not None
    assert _CapturingWorkerSpec._last_captured
    payload = _CapturingWorkerSpec._last_captured[0].payload
    assert payload.get("auto_route") is True
    assert not payload.get("model")
    assert not payload.get("pinned_model")
    assert payload.get("allowed_adapters") == ["agentic"]
    assert payload.get("prefer_plan_billed") is False
    assert payload.get("token_budget") == 250000
    assert _CapturingWorkerSpec._last_captured[0].adapter == "agentic"


def test_agentic_swarm_stamps_token_budget_from_env(monkeypatch, tmp_path):
    _CapturingWorkerSpec._last_captured = []
    monkeypatch.setenv("HARNESS_SWARM_ADAPTER", "agentic")
    monkeypatch.setenv("HARNESS_WORKER_TOKEN_BUDGET", "12345")
    monkeypatch.setenv("HARNESS_REPO", str(tmp_path))
    monkeypatch.setattr("puppetmaster.workers.WorkerSpec", _CapturingWorkerSpec)
    monkeypatch.setattr("puppetmaster.orchestrator.Orchestrator", _FakeOrchestrator)
    monkeypatch.setattr(bridge, "_warn_if_unindexed", lambda *_a, **_k: None)

    intent = DriverIntent(
        action="run_swarm",
        goal="Trace the live scoring pipeline for a points flicker",
        roles=["pipeline-mapper"],
    )
    result = bridge.execute_intent(intent, state_dir=str(tmp_path / "state"))
    assert result is not None
    payload = _CapturingWorkerSpec._last_captured[0].payload
    assert payload.get("token_budget") == 12345


def test_agentic_swarm_explicit_model_pin_disables_auto_route(monkeypatch, tmp_path):
    _CapturingWorkerSpec._last_captured = []
    monkeypatch.setenv("HARNESS_SWARM_ADAPTER", "agentic")
    monkeypatch.setenv("HARNESS_REPO", str(tmp_path))
    monkeypatch.setattr("puppetmaster.workers.WorkerSpec", _CapturingWorkerSpec)
    monkeypatch.setattr("puppetmaster.orchestrator.Orchestrator", _FakeOrchestrator)
    monkeypatch.setattr(bridge, "_warn_if_unindexed", lambda *_a, **_k: None)

    monkeypatch.setattr(
        "harness.swarm_model_pin.resolve_swarm_model_pin",
        lambda pin: {
            "pin_fields": {
                "model": "meta/muse-spark-1.1",
                "provider": "openrouter",
                "pinned_model": "agentic/meta/muse-spark-1.1",
                "pinned_adapter_model_name": "meta/muse-spark-1.1",
                "router_model_id": "agentic/meta/muse-spark-1.1",
                "auto_route": False,
            },
            "auto_route": False,
            "requested": pin,
            "resolved": "agentic/meta/muse-spark-1.1",
            "demoted": False,
            "reason": "exact",
        },
    )

    intent = DriverIntent(
        action="run_swarm",
        goal="Trace scoring finalize flicker",
        roles=["explore"],
        model="meta/muse-spark-1.1",
    )
    result = bridge.execute_intent(intent, state_dir=str(tmp_path / "state"))
    assert result is not None
    payload = _CapturingWorkerSpec._last_captured[0].payload
    assert payload.get("auto_route") is False
    assert payload.get("model") == "meta/muse-spark-1.1"
    assert payload.get("provider") == "openrouter"
    assert payload.get("pinned_model") == "agentic/meta/muse-spark-1.1"
    assert payload.get("allowed_adapters") == ["agentic"]


def test_agentic_swarm_unknown_model_pin_demotes_to_auto_route(monkeypatch, tmp_path):
    """Unknown/pilot-session pins must not fail the swarm — demote to auto-route."""
    _CapturingWorkerSpec._last_captured = []
    monkeypatch.setenv("HARNESS_SWARM_ADAPTER", "agentic")
    monkeypatch.setenv("HARNESS_REPO", str(tmp_path))
    monkeypatch.setattr("puppetmaster.workers.WorkerSpec", _CapturingWorkerSpec)
    monkeypatch.setattr("puppetmaster.orchestrator.Orchestrator", _FakeOrchestrator)
    monkeypatch.setattr(bridge, "_warn_if_unindexed", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "harness.swarm_model_pin.resolve_swarm_model_pin",
        lambda pin: {
            "pin_fields": {},
            "auto_route": True,
            "requested": pin,
            "resolved": "",
            "demoted": True,
            "reason": "test demote",
        },
    )

    intent = DriverIntent(
        action="run_swarm",
        goal="Trace scoring finalize flicker",
        roles=["explore"],
        model="cursor/gpt-5-6-luna",
    )
    result = bridge.execute_intent(intent, state_dir=str(tmp_path / "state"))
    assert result is not None
    assert _CapturingWorkerSpec._last_captured
    payload = _CapturingWorkerSpec._last_captured[0].payload
    assert payload.get("auto_route") is True
    assert not payload.get("pinned_model")
    assert not payload.get("model")
    assert payload.get("allowed_adapters") == ["agentic"]
