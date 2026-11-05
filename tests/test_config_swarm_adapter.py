"""The live swarm-adapter default is 'agentic' out of the box.

Agentic is the shipped identity. Demo is never the product default -- not even
when there is no repo -- because demo findings read as a broken product.
Demo requires explicit HARNESS_ALLOW_DEMO_SWARM=1 (eval only).
HARNESS_SWARM_ADAPTER=cursor is the CURSOR_API_KEY platform path.
Agent-login cursor-cli is never a swarm adapter.
"""
import pytest

from harness.config import HarnessConfig


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.setenv("HARNESS_CONFIG", "/nonexistent/harness.json")
    monkeypatch.delenv("HARNESS_SWARM_ADAPTER", raising=False)
    monkeypatch.delenv("HARNESS_ALLOW_DEMO_SWARM", raising=False)


@pytest.mark.parametrize(
    "repo, adapter_env, expected",
    [
        pytest.param("/tmp/somerepo", None, "agentic", id="repo-defaults-to-agentic"),
        pytest.param(None, None, "agentic", id="no-repo-still-agentic"),
        pytest.param("/tmp/somerepo", "openai", "openai", id="explicit-adapter-wins"),
        pytest.param("/tmp/somerepo", "demo", "agentic", id="poisoned-demo-env-does-not-win"),
        pytest.param("/tmp/somerepo", "cursor", "cursor", id="cursor-env-keeps-platform-cursor"),
        pytest.param("/tmp/somerepo", "cursor-cli", "agentic", id="cursor-cli-normalizes-to-agentic"),
    ],
)
def test_from_env_swarm_adapter(monkeypatch, repo, adapter_env, expected):
    if repo is None:
        monkeypatch.delenv("HARNESS_REPO", raising=False)
    else:
        monkeypatch.setenv("HARNESS_REPO", repo)
    if adapter_env is not None:
        monkeypatch.setenv("HARNESS_SWARM_ADAPTER", adapter_env)
    assert HarnessConfig.from_env().swarm_adapter == expected
