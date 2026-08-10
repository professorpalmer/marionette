"""Product swarm routing: Settings+platform worker allowlist.

Regression: prefer_plan_billed first-picked Cursor GPT ($0 plan), then
router-fallback landed on openai/gpt-* even when Models toggles only enabled
OpenRouter pilots -- tracker showed a GPT model the picker never offered.

Product bar: when Models enables Cursor Grok alongside agentic/OR, allowed
adapters must include cursor (never hard-lock agentic-only).
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


def _pin_agentic_only_allowlist(monkeypatch) -> None:
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist.resolve_swarm_worker_allowlist",
        lambda **_k: {
            "allowed_adapters": ["agentic"],
            "prefer_plan_billed": False,
            "primary_adapter": "agentic",
            "visibility_adapters": ["agentic"],
            "platform_lock": ["agentic"],
        },
    )


def test_agentic_swarm_pins_allowed_adapters(monkeypatch, tmp_path):
    _CapturingWorkerSpec._last_captured = []
    monkeypatch.setenv("HARNESS_SWARM_ADAPTER", "agentic")
    monkeypatch.setenv("HARNESS_REPO", str(tmp_path))
    _pin_agentic_only_allowlist(monkeypatch)
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


def test_swarm_allowed_adapters_includes_cursor_when_settings_enable_it(
    monkeypatch, tmp_path,
):
    """Models-enabled Cursor Grok must not be rejected by an agentic-only lock."""
    _CapturingWorkerSpec._last_captured = []
    monkeypatch.setenv("HARNESS_SWARM_ADAPTER", "agentic")
    monkeypatch.setenv("HARNESS_REPO", str(tmp_path))
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist.resolve_swarm_worker_allowlist",
        lambda **_k: {
            "allowed_adapters": ["agentic", "cursor"],
            "prefer_plan_billed": False,
            "primary_adapter": "agentic",
            "visibility_adapters": ["agentic", "cursor"],
            "platform_lock": ["agentic", "cursor"],
        },
    )
    monkeypatch.setattr("puppetmaster.workers.WorkerSpec", _CapturingWorkerSpec)
    monkeypatch.setattr("puppetmaster.orchestrator.Orchestrator", _FakeOrchestrator)
    monkeypatch.setattr(bridge, "_warn_if_unindexed", lambda *_a, **_k: None)

    intent = DriverIntent(
        action="run_swarm",
        goal="Audit provider capability honesty in Settings",
        roles=["explore"],
    )
    result = bridge.execute_intent(intent, state_dir=str(tmp_path / "state"))
    assert result is not None
    payload = _CapturingWorkerSpec._last_captured[0].payload
    assert payload.get("allowed_adapters") == ["agentic", "cursor"]
    assert payload.get("prefer_plan_billed") is False
    assert _CapturingWorkerSpec._last_captured[0].adapter == "agentic"


def test_agentic_swarm_stamps_token_budget_from_env(monkeypatch, tmp_path):
    _CapturingWorkerSpec._last_captured = []
    monkeypatch.setenv("HARNESS_SWARM_ADAPTER", "agentic")
    monkeypatch.setenv("HARNESS_WORKER_TOKEN_BUDGET", "12345")
    monkeypatch.setenv("HARNESS_REPO", str(tmp_path))
    _pin_agentic_only_allowlist(monkeypatch)
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
    _pin_agentic_only_allowlist(monkeypatch)
    monkeypatch.setattr("puppetmaster.workers.WorkerSpec", _CapturingWorkerSpec)
    monkeypatch.setattr("puppetmaster.orchestrator.Orchestrator", _FakeOrchestrator)
    monkeypatch.setattr(bridge, "_warn_if_unindexed", lambda *_a, **_k: None)

    monkeypatch.setattr(
        "harness.swarm_model_pin.resolve_swarm_model_pin",
        lambda pin, allowed_adapters=None: {
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
            "adapter": "agentic",
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
    _pin_agentic_only_allowlist(monkeypatch)
    monkeypatch.setattr("puppetmaster.workers.WorkerSpec", _CapturingWorkerSpec)
    monkeypatch.setattr("puppetmaster.orchestrator.Orchestrator", _FakeOrchestrator)
    monkeypatch.setattr(bridge, "_warn_if_unindexed", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "harness.swarm_model_pin.resolve_swarm_model_pin",
        lambda pin, allowed_adapters=None: {
            "pin_fields": {},
            "auto_route": True,
            "requested": pin,
            "resolved": "",
            "demoted": True,
            "reason": "test demote",
            "adapter": "",
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


def test_swarm_falls_back_to_platform_cursor_when_no_agentic_keys(
    monkeypatch, tmp_path,
):
    """CURSOR_API_KEY alone must still drive real swarms (not agentic-empty fail)."""
    _CapturingWorkerSpec._last_captured = []
    monkeypatch.setenv("HARNESS_SWARM_ADAPTER", "agentic")
    monkeypatch.setenv("HARNESS_REPO", str(tmp_path))
    monkeypatch.setenv("CURSOR_API_KEY", "test-cursor-api-key")
    monkeypatch.setattr(
        "harness.auto_registry.keyed_agentic_providers",
        lambda: set(),
    )
    monkeypatch.setattr("puppetmaster.workers.WorkerSpec", _CapturingWorkerSpec)
    monkeypatch.setattr("puppetmaster.orchestrator.Orchestrator", _FakeOrchestrator)
    monkeypatch.setattr(bridge, "_warn_if_unindexed", lambda *_a, **_k: None)

    intent = DriverIntent(
        action="run_swarm",
        goal="Audit provider capability honesty in Settings",
        roles=["explore"],
    )
    result = bridge.execute_intent(intent, state_dir=str(tmp_path / "state"))
    assert result is not None
    assert result.adapter == "cursor"
    assert _CapturingWorkerSpec._last_captured
    spec = _CapturingWorkerSpec._last_captured[0]
    assert spec.adapter == "cursor"
    assert spec.payload.get("allowed_adapters") == ["cursor"]
    assert spec.payload.get("prefer_plan_billed") is True


def test_swarm_cursor_model_pin_uses_cursor_adapter_in_union(monkeypatch, tmp_path):
    """intent.model for a Cursor worker resolves across the union, not agentic-only."""
    _CapturingWorkerSpec._last_captured = []
    monkeypatch.setenv("HARNESS_SWARM_ADAPTER", "agentic")
    monkeypatch.setenv("HARNESS_REPO", str(tmp_path))
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist.resolve_swarm_worker_allowlist",
        lambda **_k: {
            "allowed_adapters": ["agentic", "cursor"],
            "prefer_plan_billed": False,
            "primary_adapter": "agentic",
            "visibility_adapters": ["agentic", "cursor"],
            "platform_lock": ["agentic", "cursor"],
        },
    )
    monkeypatch.setattr(
        "harness.swarm_model_pin.resolve_swarm_model_pin",
        lambda pin, allowed_adapters=None: {
            "pin_fields": {
                "model": "grok-4-5",
                "pinned_model": "cursor/grok-4-5",
                "pinned_adapter_model_name": "grok-4-5",
                "router_model_id": "cursor/grok-4-5",
                "auto_route": False,
                "pinned_adapter": "cursor",
            },
            "auto_route": False,
            "requested": pin,
            "resolved": "cursor/grok-4-5",
            "demoted": False,
            "reason": "exact",
            "adapter": "cursor",
        },
    )
    monkeypatch.setattr("puppetmaster.workers.WorkerSpec", _CapturingWorkerSpec)
    monkeypatch.setattr("puppetmaster.orchestrator.Orchestrator", _FakeOrchestrator)
    monkeypatch.setattr(bridge, "_warn_if_unindexed", lambda *_a, **_k: None)

    intent = DriverIntent(
        action="run_swarm",
        goal="Review auth middleware with Cursor Grok",
        roles=["explore"],
        model="cursor/grok-4-5",
    )
    result = bridge.execute_intent(intent, state_dir=str(tmp_path / "state"))
    assert result is not None
    spec = _CapturingWorkerSpec._last_captured[0]
    assert spec.adapter == "cursor"
    assert spec.payload.get("allowed_adapters") == ["agentic", "cursor"]
    assert spec.payload.get("pinned_model") == "cursor/grok-4-5"
    assert spec.payload.get("prefer_plan_billed") is False


def test_execute_intent_pins_isolated_marionette_registry_path(tmp_path, monkeypatch):
    """Direct execute_intent must boot isolated catalog (path + ladder), not ~/.puppetmaster."""
    import json

    shared = tmp_path / ".puppetmaster" / "models.json"
    shared.parent.mkdir(parents=True)
    shared.write_text(
        json.dumps({"version": 1, "models": [{"id": "shared-only", "tags": []}]}),
        encoding="utf-8",
    )
    marionette = tmp_path / ".pmharness" / "marionette-models.json"
    marionette.parent.mkdir(parents=True)
    # Flattened OpenCode-shaped Kimi row — boot ladder must stamp vision.
    marionette.write_text(
        json.dumps(
            {
                "version": 1,
                "models": [
                    {
                        "id": "agentic/kimi-k3",
                        "adapter": "agentic",
                        "adapter_model_name": "kimi-k3",
                        "tags": ["code"],
                        "capability_score": 50,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("PUPPETMASTER_MODELS_PATH", raising=False)
    monkeypatch.setattr(
        "harness.marionette_registry.marionette_models_path",
        lambda: marionette,
    )
    monkeypatch.setattr(
        "harness.marionette_registry.shared_puppetmaster_models_path",
        lambda: shared,
    )
    _CapturingWorkerSpec._last_captured = []
    monkeypatch.setenv("HARNESS_SWARM_ADAPTER", "agentic")
    monkeypatch.setenv("HARNESS_REPO", str(tmp_path))
    _pin_agentic_only_allowlist(monkeypatch)
    monkeypatch.setattr("puppetmaster.workers.WorkerSpec", _CapturingWorkerSpec)
    monkeypatch.setattr("puppetmaster.orchestrator.Orchestrator", _FakeOrchestrator)
    monkeypatch.setattr(bridge, "_warn_if_unindexed", lambda *_a, **_k: None)

    intent = DriverIntent(
        action="run_swarm",
        goal="Map auth middleware",
        roles=["explore"],
    )
    result = bridge.execute_intent(intent, state_dir=str(tmp_path / "state"))
    assert result is not None
    assert os.environ.get("PUPPETMASTER_MODELS_PATH") == str(marionette)
    # Shared Cursor catalog must stay untouched.
    shared_data = json.loads(shared.read_text(encoding="utf-8"))
    assert shared_data["models"][0]["id"] == "shared-only"
    # Isolated catalog used for routing: flattened Kimi gets vision after boot.
    if os.environ.get("PUPPETMASTER_MODELS_PATH") == str(marionette):
        catalog = json.loads(marionette.read_text(encoding="utf-8"))
        kimi = next(
            (m for m in catalog["models"] if m.get("id") == "agentic/kimi-k3"),
            None,
        )
        assert kimi is not None
        assert "vision" in (kimi.get("tags") or [])
