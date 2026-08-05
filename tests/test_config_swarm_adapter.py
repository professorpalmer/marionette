"""The live swarm-adapter default is 'agentic' out of the box.

Agentic is the shipped identity. Demo is never the product default -- not even
when there is no repo -- because demo findings read as a broken product.
Demo requires explicit HARNESS_ALLOW_DEMO_SWARM=1 (eval only).
"""
import pytest

from harness.config import HarnessConfig


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.setenv("HARNESS_CONFIG", "/nonexistent/harness.json")
    monkeypatch.delenv("HARNESS_SWARM_ADAPTER", raising=False)
    monkeypatch.delenv("HARNESS_ALLOW_DEMO_SWARM", raising=False)


def test_repo_defaults_to_agentic_out_of_the_box(monkeypatch):
    monkeypatch.setenv("HARNESS_REPO", "/tmp/somerepo")
    assert HarnessConfig.from_env().swarm_adapter == "agentic"


def test_no_repo_still_defaults_to_agentic(monkeypatch):
    monkeypatch.delenv("HARNESS_REPO", raising=False)
    assert HarnessConfig.from_env().swarm_adapter == "agentic"


def test_explicit_adapter_always_wins(monkeypatch):
    monkeypatch.setenv("HARNESS_REPO", "/tmp/somerepo")
    monkeypatch.setenv("HARNESS_SWARM_ADAPTER", "openai")
    assert HarnessConfig.from_env().swarm_adapter == "openai"


def test_poisoned_demo_env_does_not_win(monkeypatch):
    monkeypatch.setenv("HARNESS_REPO", "/tmp/somerepo")
    monkeypatch.setenv("HARNESS_SWARM_ADAPTER", "demo")
    assert HarnessConfig.from_env().swarm_adapter == "agentic"


def test_cursor_env_keeps_platform_cursor(monkeypatch):
    """HARNESS_SWARM_ADAPTER=cursor is the CURSOR_API_KEY platform path."""
    monkeypatch.setenv("HARNESS_REPO", "/tmp/somerepo")
    monkeypatch.setenv("HARNESS_SWARM_ADAPTER", "cursor")
    assert HarnessConfig.from_env().swarm_adapter == "cursor"


def test_cursor_cli_env_normalizes_to_agentic(monkeypatch):
    """Agent-login cursor-cli is never a swarm adapter."""
    monkeypatch.setenv("HARNESS_REPO", "/tmp/somerepo")
    monkeypatch.setenv("HARNESS_SWARM_ADAPTER", "cursor-cli")
    assert HarnessConfig.from_env().swarm_adapter == "agentic"
