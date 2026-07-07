"""Marionette must hand the router a capability CEILING (max_capability), not a
floor. payload.min_capability FORCES the classifier output to one exact value,
so every swarm worker "needed" the same score and the balanced policy pinned
them all to the single cheapest model that cleared it (the all-swarms-route-to-
glm-5.2 symptom). max_capability only clips the top: cheap roles still classify
low and route to cheap models."""
import pytest

from pmharness.bridge import (
    _analysis_capability_payload,
    _router_supports_max_capability,
)


def test_local_router_supports_max_capability():
    # The bundled Puppetmaster ships TaskSignals.explicit_max_capability; the
    # legacy min_capability fallback exists only for older PyPI builds.
    assert _router_supports_max_capability() is True


def test_analysis_payload_is_ceiling_not_floor(monkeypatch):
    monkeypatch.delenv("HARNESS_ANALYSIS_DEEP", raising=False)
    monkeypatch.delenv("HARNESS_ANALYSIS_MAX_CAPABILITY", raising=False)
    payload = _analysis_capability_payload()
    assert payload == {"max_capability": 85}
    assert "min_capability" not in payload


def test_analysis_payload_env_override(monkeypatch):
    monkeypatch.setenv("HARNESS_ANALYSIS_MAX_CAPABILITY", "70")
    assert _analysis_capability_payload() == {"max_capability": 70}


def test_analysis_payload_deep_removes_cap(monkeypatch):
    monkeypatch.setenv("HARNESS_ANALYSIS_DEEP", "1")
    assert _analysis_capability_payload() == {}


def test_ceiling_still_differentiates_cheap_and_hard_tasks():
    """End-to-end through the real router: with the ceiling Marionette sends,
    an easy role must classify far below the cap while a hard role clips to it
    -- the property min_capability destroyed."""
    router = pytest.importorskip("puppetmaster.router")
    ceiling = _analysis_capability_payload().get("max_capability")
    if ceiling is None:
        pytest.skip("deep mode or legacy router in this environment")

    easy = router.classify_capability_needed(router.TaskSignals(
        instruction="verify the build output", role="verify-runtime",
        explicit_max_capability=ceiling,
    ))
    hard = router.classify_capability_needed(router.TaskSignals(
        instruction="security audit of authentication across every endpoint",
        role="security-review", explicit_max_capability=ceiling,
    ))
    assert easy < ceiling
    assert hard == ceiling
