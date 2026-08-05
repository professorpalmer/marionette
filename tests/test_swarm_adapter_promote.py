"""Marionette product path never runs or surfaces the demo substrate."""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from harness.config import HarnessConfig
from harness.swarm_adapter import (
    allow_demo_swarm,
    ensure_repo_swarm_adapter,
    normalize_swarm_adapter,
    publish_swarm_adapter,
    refuse_demo_result,
    resolve_bridge_swarm_adapter,
)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.setenv("HARNESS_CONFIG", "/nonexistent/harness.json")
    monkeypatch.delenv("HARNESS_SWARM_ADAPTER", raising=False)
    monkeypatch.delenv("HARNESS_REPO", raising=False)
    monkeypatch.delenv("HARNESS_ALLOW_DEMO_SWARM", raising=False)


def test_allow_demo_default_off():
    assert allow_demo_swarm() is False


def test_allow_demo_opt_in(monkeypatch):
    monkeypatch.setenv("HARNESS_ALLOW_DEMO_SWARM", "1")
    assert allow_demo_swarm() is True


def test_normalize_demo_becomes_agentic_in_product():
    assert normalize_swarm_adapter("demo") == "agentic"
    assert normalize_swarm_adapter("") == "agentic"


def test_normalize_keeps_platform_cursor():
    # Platform CURSOR_API_KEY path — distinct from agent-login cursor-cli.
    assert normalize_swarm_adapter("cursor") == "cursor"
    assert normalize_swarm_adapter("cursor-sdk") == "cursor"
    assert normalize_swarm_adapter("cursor-cli") == "agentic"


def test_normalize_demo_kept_when_allowed(monkeypatch):
    monkeypatch.setenv("HARNESS_ALLOW_DEMO_SWARM", "1")
    assert normalize_swarm_adapter("demo") == "demo"


def test_resolve_never_returns_demo_in_product():
    assert resolve_bridge_swarm_adapter("demo", repo_cwd="/tmp/repo") == "agentic"
    assert resolve_bridge_swarm_adapter("demo", repo_cwd="") == "agentic"
    assert resolve_bridge_swarm_adapter("", repo_cwd="") == "agentic"


def test_resolve_keeps_real_adapters():
    assert resolve_bridge_swarm_adapter("agentic", repo_cwd="/tmp/repo") == "agentic"
    assert resolve_bridge_swarm_adapter("openai", repo_cwd="/tmp/repo") == "openai"
    assert resolve_bridge_swarm_adapter("cursor", repo_cwd="/tmp/repo") == "cursor"


def test_resolve_allows_explicit_demo_opt_in(monkeypatch):
    monkeypatch.setenv("HARNESS_ALLOW_DEMO_SWARM", "1")
    assert resolve_bridge_swarm_adapter("demo", repo_cwd="/tmp/repo") == "demo"


def test_from_env_defaults_agentic_even_without_repo(monkeypatch):
    monkeypatch.delenv("HARNESS_REPO", raising=False)
    assert HarnessConfig.from_env().swarm_adapter == "agentic"


def test_from_env_promotes_poisoned_demo_env(monkeypatch):
    monkeypatch.setenv("HARNESS_REPO", "/tmp/somerepo")
    monkeypatch.setenv("HARNESS_SWARM_ADAPTER", "demo")
    assert HarnessConfig.from_env().swarm_adapter == "agentic"


def test_ensure_upgrades_demo_cfg(monkeypatch):
    monkeypatch.setenv("HARNESS_SWARM_ADAPTER", "demo")
    cfg = SimpleNamespace(repo="/tmp/wiki", swarm_adapter="demo")
    assert ensure_repo_swarm_adapter(cfg) is True
    assert cfg.swarm_adapter == "agentic"
    assert os.environ["HARNESS_SWARM_ADAPTER"] == "agentic"


def test_publish_never_stamps_demo_in_product(monkeypatch):
    publish_swarm_adapter("demo", repo="/tmp/wiki")
    assert os.environ.get("HARNESS_SWARM_ADAPTER") == "agentic"


def test_refuse_demo_result():
    assert refuse_demo_result("demo") is True
    assert refuse_demo_result("agentic") is False


def test_bridge_refuses_demo_without_allow(monkeypatch, tmp_path):
    pytest.importorskip("puppetmaster")
    from pmharness.intent import DriverIntent
    from pmharness import bridge

    monkeypatch.setenv("HARNESS_SWARM_ADAPTER", "demo")
    monkeypatch.delenv("HARNESS_ALLOW_DEMO_SWARM", raising=False)

    class _Job:
        id = "job-test"
        status = "complete"

    class _Result:
        job = _Job()
        mode = "swarm"
        summary = "ok"
        artifacts = []

    class _Orch:
        def __init__(self, store):
            pass

        def run(self, goal, specs=None, roles=None, worker_mode=None, label=None):
            return _Result()

    monkeypatch.setattr("puppetmaster.orchestrator.Orchestrator", _Orch)
    res = bridge.execute_intent(
        DriverIntent(action="run_swarm", goal="audit memory", rationale="x"),
        state_dir=str(tmp_path / "state"),
        cwd=str(tmp_path),
    )
    assert res is not None
    assert res.adapter == "agentic"
