"""The live swarm-adapter default is 'agentic' out of the box.

Agentic is the shipped identity: a repo-scoped swarm routes directly through the
user's own provider keys (router-picked model), and it works the moment a key is
added. We do NOT silently fall back to the 'demo' substrate when keyless -- that
would surface deterministic placeholder findings that read as a broken product.
Keyless users are nudged to add a key in the UI instead (ProviderKeyBanner), and
'agentic_ready' on /api/config reflects the real key posture.

'demo' remains the default only when there's no repo to analyze, and as an
explicit opt-in via HARNESS_SWARM_ADAPTER / config.
"""
import pytest

from harness.config import HarnessConfig


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    # Ignore the developer's real ~/.harness.json and any ambient override so the
    # default-resolution logic is what's under test.
    monkeypatch.setenv("HARNESS_CONFIG", "/nonexistent/harness.json")
    monkeypatch.delenv("HARNESS_SWARM_ADAPTER", raising=False)


def test_repo_defaults_to_agentic_out_of_the_box(monkeypatch):
    # Agentic even without a key visible -- the app ships agentic and nudges the
    # user to add a key rather than dropping to a misleading demo run.
    monkeypatch.setenv("HARNESS_REPO", "/tmp/somerepo")
    assert HarnessConfig.from_env().swarm_adapter == "agentic"


def test_no_repo_defaults_to_demo(monkeypatch):
    # Nothing to analyze -> the safe, free demo substrate.
    monkeypatch.delenv("HARNESS_REPO", raising=False)
    assert HarnessConfig.from_env().swarm_adapter == "demo"


def test_explicit_adapter_always_wins(monkeypatch):
    monkeypatch.setenv("HARNESS_REPO", "/tmp/somerepo")
    monkeypatch.setenv("HARNESS_SWARM_ADAPTER", "openai")
    assert HarnessConfig.from_env().swarm_adapter == "openai"
