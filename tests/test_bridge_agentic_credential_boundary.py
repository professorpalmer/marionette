"""Hermetic bridge tests for the Marionette → Puppetmaster credential boundary.

Proves pool availability is synchronized before Orchestrator creation,
disconnected slugs are exported via PUPPETMASTER_DISABLED_PROVIDERS, and no
secret key values appear in bridge output.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import pmharness.bridge as bridge
from pmharness.intent import DriverIntent


_SECRET = "sk-bridge-boundary-secret-xyzzy-9999"


@dataclass(frozen=True)
class _FakeProvider:
    name: str
    env_vars: tuple[str, ...] = ("OPENROUTER_API_KEY",)

    def key_env(self) -> str | None:
        return self.env_vars[0] if self.env_vars else None


class _CapturingOrchestrator:
    env_at_run: dict[str, str] = {}
    call_order: list[str] = []

    def __init__(self, store: Any) -> None:
        self.store = store
        type(self).call_order.append("orchestrator_init")

    def run(self, goal: str, specs=None, worker_mode=None, label=None, **_kwargs):
        type(self).call_order.append("orchestrator_run")
        type(self).env_at_run = dict(os.environ)
        job = type("_Job", (), {"id": "job_cred", "status": "complete"})()
        return type(
            "_Result",
            (),
            {
                "job": job,
                "status": "complete",
                "mode": "inline",
                "artifacts": [],
                "summary": "ok",
            },
        )()


@dataclass
class _CapturingWorkerSpec:
    role: str
    instruction: str
    adapter: str
    payload: dict = field(default_factory=dict)


def _run_agentic_swarm(monkeypatch, tmp_path):
    _CapturingOrchestrator.env_at_run = {}
    _CapturingOrchestrator.call_order = []
    monkeypatch.setenv("HARNESS_SWARM_ADAPTER", "agentic")
    monkeypatch.setenv("HARNESS_REPO", str(tmp_path))
    monkeypatch.setattr("puppetmaster.workers.WorkerSpec", _CapturingWorkerSpec)
    monkeypatch.setattr("puppetmaster.orchestrator.Orchestrator", _CapturingOrchestrator)
    monkeypatch.setattr(bridge, "_warn_if_unindexed", lambda *_a, **_k: None)

    intent = DriverIntent(
        action="run_swarm",
        goal="Trace credential boundary wiring",
        roles=["explore"],
    )
    return bridge.execute_intent(intent, state_dir=str(tmp_path / "state"))


def test_sync_runs_before_orchestrator(monkeypatch, tmp_path):
    call_order: list[str] = []
    real_sync = bridge._sync_agentic_credential_env

    def _tracking_sync():
        call_order.append("sync")
        real_sync()

    monkeypatch.setattr(bridge, "_sync_agentic_credential_env", _tracking_sync)
    monkeypatch.setattr("harness.providers.available_providers", lambda: [])
    monkeypatch.setattr("harness.keys.get_disconnected", lambda: set())

    class _OrderOrchestrator(_CapturingOrchestrator):
        def __init__(self, store: Any) -> None:
            call_order.append("orchestrator_init")
            super().__init__(store)

        def run(self, goal: str, specs=None, worker_mode=None, label=None, **_kwargs):
            call_order.append("orchestrator_run")
            return super().run(goal, specs=specs, worker_mode=worker_mode, label=label)

    monkeypatch.setenv("HARNESS_SWARM_ADAPTER", "agentic")
    monkeypatch.setenv("HARNESS_REPO", str(tmp_path))
    monkeypatch.setattr("puppetmaster.workers.WorkerSpec", _CapturingWorkerSpec)
    monkeypatch.setattr("puppetmaster.orchestrator.Orchestrator", _OrderOrchestrator)
    monkeypatch.setattr(bridge, "_warn_if_unindexed", lambda *_a, **_k: None)

    intent = DriverIntent(
        action="run_swarm",
        goal="Trace credential boundary wiring",
        roles=["explore"],
    )
    result = bridge.execute_intent(intent, state_dir=str(tmp_path / "state"))
    assert result is not None
    assert call_order == ["sync", "orchestrator_init", "orchestrator_run"]


def test_pool_key_mirrored_into_env_before_orchestrator(monkeypatch, tmp_path):
    fake = _FakeProvider(name="openrouter")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr("harness.providers.available_providers", lambda: [fake])
    monkeypatch.setattr("harness.registry_wizard.get_provider_key", lambda _p: _SECRET)
    monkeypatch.setattr(
        "harness.credential_pool._mirror_pool_token_to_env",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "harness.credential_pool.providers_for_env_var",
        lambda _ev: ["openrouter"],
    )
    monkeypatch.setattr("harness.keys.get_disconnected", lambda: set())

    result = _run_agentic_swarm(monkeypatch, tmp_path)
    assert result is not None
    assert _CapturingOrchestrator.env_at_run.get("OPENROUTER_API_KEY") == _SECRET
    assert _SECRET not in (result.summary or "")


def test_openai_codex_oauth_token_mirrored_before_orchestrator(monkeypatch, tmp_path):
    """Codex OAuth must reach agentic workers via OPENAI_CODEX_TOKEN."""
    fake = _FakeProvider(name="openai-codex", env_vars=("OPENAI_CODEX_TOKEN",))
    monkeypatch.delenv("OPENAI_CODEX_TOKEN", raising=False)
    monkeypatch.setattr("harness.providers.available_providers", lambda: [fake])
    monkeypatch.setattr("harness.registry_wizard.get_provider_key", lambda _p: _SECRET)
    monkeypatch.setattr(
        "harness.credential_pool._mirror_pool_token_to_env",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "harness.credential_pool.providers_for_env_var",
        lambda _ev: ["openai-codex"],
    )
    monkeypatch.setattr("harness.keys.get_disconnected", lambda: set())

    result = _run_agentic_swarm(monkeypatch, tmp_path)
    assert result is not None
    assert _CapturingOrchestrator.env_at_run.get("OPENAI_CODEX_TOKEN") == _SECRET
    assert _SECRET not in (result.summary or "")


def test_disconnected_slugs_exported_to_env(monkeypatch, tmp_path):
    monkeypatch.setattr("harness.providers.available_providers", lambda: [])
    monkeypatch.setattr("harness.keys.get_disconnected", lambda: {"openai", "anthropic"})

    result = _run_agentic_swarm(monkeypatch, tmp_path)
    assert result is not None
    assert _CapturingOrchestrator.env_at_run.get("PUPPETMASTER_DISABLED_PROVIDERS") == (
        "anthropic,openai-api"
    )


def test_empty_disconnected_exports_empty_env_hint(monkeypatch, tmp_path):
    monkeypatch.setattr("harness.providers.available_providers", lambda: [])
    monkeypatch.setattr("harness.keys.get_disconnected", lambda: set())

    result = _run_agentic_swarm(monkeypatch, tmp_path)
    assert result is not None
    assert _CapturingOrchestrator.env_at_run.get("PUPPETMASTER_DISABLED_PROVIDERS") == ""


def test_sync_swallows_provider_lookup_failure(monkeypatch, tmp_path):
    def _boom():
        raise RuntimeError("providers unavailable")

    monkeypatch.setattr("harness.providers.available_providers", _boom)
    monkeypatch.setattr("harness.keys.get_disconnected", lambda: set())

    result = _run_agentic_swarm(monkeypatch, tmp_path)
    assert result is not None


def test_prewalk_syncs_before_orchestrator(monkeypatch, tmp_path):
    sync_seen: list[str] = []

    def _track_sync():
        sync_seen.append("sync")

    monkeypatch.setattr(bridge, "_sync_agentic_credential_env", _track_sync)
    monkeypatch.setenv("HARNESS_REPO", str(tmp_path))
    monkeypatch.setattr("puppetmaster.orchestrator.Orchestrator", _CapturingOrchestrator)
    monkeypatch.setattr(
        "puppetmaster.prewalk.build_prewalk_specs",
        lambda *_a, **_k: [
            _CapturingWorkerSpec(role="plan", instruction="", adapter="local"),
        ],
    )

    intent = DriverIntent(action="run_prewalk", goal="plan then implement")
    result = bridge.execute_intent(intent, state_dir=str(tmp_path / "state"))
    assert result is not None
    assert sync_seen == ["sync"]
