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
from harness.swarm_worker_allowlist import (
    resolve_swarm_worker_allowlist as _real_resolve_swarm_worker_allowlist,
)
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
        type(self).last_worker_mode = worker_mode
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


def test_execute_intent_explicit_subprocess_reaches_orchestrator(monkeypatch, tmp_path):
    _CapturingWorkerSpec._last_captured = []
    _FakeOrchestrator.last_worker_mode = "unset"
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
    result = bridge.execute_intent(
        intent, state_dir=str(tmp_path / "state"), worker_mode="subprocess",
    )
    assert result is not None
    assert _FakeOrchestrator.last_worker_mode == "subprocess"


def test_execute_intent_omitted_worker_mode_stays_inline(monkeypatch, tmp_path):
    _CapturingWorkerSpec._last_captured = []
    _FakeOrchestrator.last_worker_mode = "unset"
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
        worker_mode="subprocess",
    )
    result = bridge.execute_intent(intent, state_dir=str(tmp_path / "state"))
    assert result is not None
    assert _FakeOrchestrator.last_worker_mode == "inline"


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


def _capture_agentic_swarm(monkeypatch, tmp_path, intent, **env):
    _CapturingWorkerSpec._last_captured = []
    monkeypatch.setenv("HARNESS_SWARM_ADAPTER", "agentic")
    monkeypatch.setenv("HARNESS_REPO", str(tmp_path))
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    _pin_agentic_only_allowlist(monkeypatch)
    monkeypatch.setattr("puppetmaster.workers.WorkerSpec", _CapturingWorkerSpec)
    monkeypatch.setattr("puppetmaster.orchestrator.Orchestrator", _FakeOrchestrator)
    monkeypatch.setattr(bridge, "_warn_if_unindexed", lambda *_a, **_k: None)
    result = bridge.execute_intent(intent, state_dir=str(tmp_path / "state"))
    assert result is not None
    assert _CapturingWorkerSpec._last_captured
    return _CapturingWorkerSpec._last_captured[0].payload


def test_agentic_swarm_stamps_medium_reasoning_when_unset(monkeypatch, tmp_path):
    intent = DriverIntent(
        action="run_swarm",
        goal="Trace the live scoring pipeline for a points flicker",
        roles=["pipeline-mapper"],
    )
    payload = _capture_agentic_swarm(
        monkeypatch, tmp_path, intent, HARNESS_SWARM_REASONING_EFFORT=None,
    )
    assert payload.get("reasoning_effort") == "medium"


def test_agentic_swarm_intent_pin_wins_over_settings(monkeypatch, tmp_path):
    intent = DriverIntent(
        action="run_swarm",
        goal="Trace the live scoring pipeline for a points flicker",
        roles=["pipeline-mapper"],
        reasoning_effort="high",
    )
    payload = _capture_agentic_swarm(
        monkeypatch, tmp_path, intent, HARNESS_SWARM_REASONING_EFFORT="low",
    )
    assert payload.get("reasoning_effort") == "high"


def test_agentic_swarm_settings_low_stamps_low(monkeypatch, tmp_path):
    intent = DriverIntent(
        action="run_swarm",
        goal="Trace the live scoring pipeline for a points flicker",
        roles=["pipeline-mapper"],
    )
    payload = _capture_agentic_swarm(
        monkeypatch, tmp_path, intent, HARNESS_SWARM_REASONING_EFFORT="low",
    )
    assert payload.get("reasoning_effort") == "low"


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


class _RoutingOrchestrator:
    """Product-path fake: run the real router on bridge-built specs, no workers."""

    last_decisions: list = []

    def __init__(self, store: Any) -> None:
        self.store = store

    def run(self, goal: str, specs=None, worker_mode=None, label=None):
        from puppetmaster.model_registry import default_registry_path, load_registry
        from puppetmaster.router import route_task, signals_from_worker_spec

        from pathlib import Path

        env_path = os.environ.get("PUPPETMASTER_MODELS_PATH")
        registry_path = Path(env_path) if env_path else default_registry_path()
        registry = load_registry(registry_path)
        decisions = []
        for spec in specs or []:
            payload = spec.payload or {}
            if payload.get("auto_route") and not payload.get("pinned_model"):
                policy = payload.get("routing_policy") or "balanced"
                decision = route_task(
                    signals_from_worker_spec(spec), registry, policy=policy,
                )
                payload["model"] = decision.model.adapter_model_name
                payload["router_model_id"] = decision.model.id
                decisions.append((spec.role, decision.model.id, decision.to_artifact_payload()))
            else:
                model_id = str(
                    payload.get("pinned_model")
                    or payload.get("router_model_id")
                    or ""
                )
                decisions.append((spec.role, model_id, {"model_id": model_id}))
        type(self).last_decisions = decisions
        return _FakeResult()


def _two_tier_codex_catalog() -> dict:
    """Isolated catalog that mimics the flattened Luna/Sol 85/85 sync defect."""
    shared_tags = ["balanced", "fast", "code", "tools", "agentic"]
    return {
        "version": 1,
        "models": [
            {
                "id": "agentic/openai-codex/gpt-5.6-luna",
                "adapter": "agentic",
                "adapter_model_name": "gpt-5.6-luna",
                "capability_score": 85,
                "input_per_mtok_usd": 1.25,
                "output_per_mtok_usd": 5.0,
                "context_window": 200000,
                "enabled": True,
                "billing": "plan",
                "tags": list(shared_tags),
                "payload_defaults": {
                    "provider": "openai-codex",
                    "model": "gpt-5.6-luna",
                },
            },
            {
                "id": "agentic/openai-codex/gpt-5.6-sol",
                "adapter": "agentic",
                "adapter_model_name": "gpt-5.6-sol",
                "capability_score": 85,
                "input_per_mtok_usd": 2.5,
                "output_per_mtok_usd": 10.0,
                "context_window": 200000,
                "enabled": True,
                "billing": "plan",
                "tags": list(shared_tags),
                "payload_defaults": {
                    "provider": "openai-codex",
                    "model": "gpt-5.6-sol",
                },
            },
        ],
    }


def _install_two_tier_product_path(monkeypatch, tmp_path):
    """Isolated marionette-models.json + allowlist + capturing specs + real router."""
    import json

    shared = tmp_path / ".puppetmaster" / "models.json"
    shared.parent.mkdir(parents=True)
    shared.write_text(
        json.dumps({"version": 1, "models": []}),
        encoding="utf-8",
    )
    marionette = tmp_path / ".pmharness" / "marionette-models.json"
    marionette.parent.mkdir(parents=True)
    marionette.write_text(
        json.dumps(_two_tier_codex_catalog()),
        encoding="utf-8",
    )
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(marionette))
    monkeypatch.delenv("HARNESS_ANALYSIS_DEEP", raising=False)
    monkeypatch.delenv("HARNESS_ANALYSIS_MAX_CAPABILITY", raising=False)
    monkeypatch.setattr(
        "harness.marionette_registry.marionette_models_path",
        lambda: marionette,
    )
    monkeypatch.setattr(
        "harness.marionette_registry.shared_puppetmaster_models_path",
        lambda: shared,
    )
    monkeypatch.setattr(
        "puppetmaster.providers.available_providers",
        lambda: {"openai-codex"},
    )
    _CapturingWorkerSpec._last_captured = []
    _RoutingOrchestrator.last_decisions = []
    monkeypatch.setenv("HARNESS_SWARM_ADAPTER", "agentic")
    monkeypatch.setenv("HARNESS_REPO", str(tmp_path))
    _pin_agentic_only_allowlist(monkeypatch)
    monkeypatch.setattr("puppetmaster.workers.WorkerSpec", _CapturingWorkerSpec)
    monkeypatch.setattr("puppetmaster.orchestrator.Orchestrator", _RoutingOrchestrator)
    monkeypatch.setattr(bridge, "_warn_if_unindexed", lambda *_a, **_k: None)
    return marionette


def test_run_swarm_two_tier_catalog_selects_two_models(monkeypatch, tmp_path):
    """explore (~73) vs conflict-auditor (clips to 85) must cross Luna 76 / Sol 85."""
    _install_two_tier_product_path(monkeypatch, tmp_path)
    intent = DriverIntent(
        action="run_swarm",
        goal="Map the auth middleware",
        roles=["explore", "conflict-auditor"],
    )
    result = bridge.execute_intent(intent, state_dir=str(tmp_path / "state"))
    assert result is not None
    by_role = {role: model_id for role, model_id, _payload in _RoutingOrchestrator.last_decisions}
    assert by_role["explore"] == "agentic/openai-codex/gpt-5.6-luna"
    assert by_role["conflict-auditor"] == "agentic/openai-codex/gpt-5.6-sol"
    selected = {model_id for _role, model_id, _payload in _RoutingOrchestrator.last_decisions}
    assert selected == {
        "agentic/openai-codex/gpt-5.6-luna",
        "agentic/openai-codex/gpt-5.6-sol",
    }
    captured = {spec.role: spec.payload for spec in _CapturingWorkerSpec._last_captured}
    assert captured["explore"].get("router_model_id") == "agentic/openai-codex/gpt-5.6-luna"
    assert captured["conflict-auditor"].get("router_model_id") == "agentic/openai-codex/gpt-5.6-sol"
    for _role, _model_id, payload in _RoutingOrchestrator.last_decisions:
        assert payload.get("model_id")
        assert "capability_needed" in payload or "capability_score" in payload


def test_run_swarm_equivalent_roles_stay_homogeneous(monkeypatch, tmp_path):
    """Roles whose needs both sit under Luna still share one model."""
    _install_two_tier_product_path(monkeypatch, tmp_path)
    intent = DriverIntent(
        action="run_swarm",
        goal="Map the auth middleware",
        roles=["explore", "test-coverage-reviewer"],
    )
    result = bridge.execute_intent(intent, state_dir=str(tmp_path / "state"))
    assert result is not None
    selected = {model_id for _role, model_id, _payload in _RoutingOrchestrator.last_decisions}
    assert selected == {"agentic/openai-codex/gpt-5.6-luna"}


def test_run_swarm_global_pin_applies_to_every_role(monkeypatch, tmp_path):
    """A single intent.model pin stays intentionally homogeneous across roles."""
    _install_two_tier_product_path(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "harness.swarm_model_pin.resolve_swarm_model_pin",
        lambda pin, allowed_adapters=None: {
            "pin_fields": {
                "model": "gpt-5.6-luna",
                "pinned_model": "agentic/openai-codex/gpt-5.6-luna",
                "pinned_adapter_model_name": "gpt-5.6-luna",
                "router_model_id": "agentic/openai-codex/gpt-5.6-luna",
                "auto_route": False,
            },
            "auto_route": False,
            "requested": pin,
            "resolved": "agentic/openai-codex/gpt-5.6-luna",
            "demoted": False,
            "reason": "exact",
            "adapter": "agentic",
        },
    )
    intent = DriverIntent(
        action="run_swarm",
        goal="Map the auth middleware",
        roles=["explore", "conflict-auditor"],
        model="gpt-5.6-luna",
    )
    result = bridge.execute_intent(intent, state_dir=str(tmp_path / "state"))
    assert result is not None
    assert len(_CapturingWorkerSpec._last_captured) == 2
    for spec in _CapturingWorkerSpec._last_captured:
        assert spec.payload.get("auto_route") is False
        assert spec.payload.get("pinned_model") == "agentic/openai-codex/gpt-5.6-luna"
    selected = {model_id for _role, model_id, _payload in _RoutingOrchestrator.last_decisions}
    assert selected == {"agentic/openai-codex/gpt-5.6-luna"}


def test_run_swarm_settings_singleton_blocks_disabled_catalog_peer(monkeypatch, tmp_path):
    """Only Luna on in Settings: conflict-auditor must not leap to Sol / gpt-5-3."""
    import harness.swarm_worker_allowlist as swa

    _install_two_tier_product_path(monkeypatch, tmp_path)
    monkeypatch.setattr(
        swa,
        "_enabled_or_visible_specs",
        lambda: ["openai-codex:gpt-5.6-luna"],
    )
    monkeypatch.setattr(swa, "_agentic_eligible", lambda: True)
    monkeypatch.setattr(swa, "_cursor_platform_ready", lambda: False)
    monkeypatch.setattr(
        swa,
        "_platform_locked_adapters",
        lambda: frozenset({"agentic", "cursor"}),
    )
    # _install_two_tier stubs resolve_swarm_worker_allowlist (empty model ids
    # so two-tier can pick Sol). Restore the real resolver captured at import.
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist.resolve_swarm_worker_allowlist",
        _real_resolve_swarm_worker_allowlist,
    )
    intent = DriverIntent(
        action="run_swarm",
        goal="Map the auth middleware",
        roles=["explore", "conflict-auditor"],
    )
    result = bridge.execute_intent(intent, state_dir=str(tmp_path / "state"))
    assert result is not None
    captured = {spec.role: spec.payload for spec in _CapturingWorkerSpec._last_captured}
    for payload in captured.values():
        ids = [str(item).lower() for item in (payload.get("allowed_model_ids") or [])]
        blob = " ".join(ids)
        assert "gpt-5.6-luna" in blob
        assert "gpt-5.6-sol" not in blob
        assert "gpt-5-3" not in blob
    selected = {model_id for _role, model_id, _payload in _RoutingOrchestrator.last_decisions}
    assert selected == {"agentic/openai-codex/gpt-5.6-luna"}
