"""Marionette must hand the router a capability CEILING (max_capability), not a
floor. payload.min_capability FORCES the classifier output to one exact value,
so every swarm worker "needed" the same score and the balanced policy pinned
them all to the single cheapest model that cleared it (the all-swarms-route-to-
glm-5.2 symptom). max_capability only clips the top: cheap roles still classify
low and route to cheap models.

The installed puppetmaster may predate the ceiling (PyPI puppetmaster-ai
<= 1.10.0 only knows min_capability), so the payload key is capability-detected:
these tests assert the fallback contract, not one fixed key.
"""
import pytest

from pmharness.bridge import (
    _analysis_capability_payload,
    _router_supports_max_capability,
)

ROUTER_HAS_CEILING = _router_supports_max_capability()
EXPECTED_KEY = "max_capability" if ROUTER_HAS_CEILING else "min_capability"


def test_support_probe_matches_installed_router():
    from puppetmaster.router import TaskSignals

    has_field = "explicit_max_capability" in getattr(
        TaskSignals, "__dataclass_fields__", {})
    assert ROUTER_HAS_CEILING is has_field


def test_analysis_payload_uses_ceiling_when_router_supports_it(monkeypatch):
    monkeypatch.delenv("HARNESS_ANALYSIS_DEEP", raising=False)
    monkeypatch.delenv("HARNESS_ANALYSIS_MAX_CAPABILITY", raising=False)
    payload = _analysis_capability_payload()
    assert payload == {EXPECTED_KEY: 85}


def test_analysis_payload_env_override(monkeypatch):
    monkeypatch.setenv("HARNESS_ANALYSIS_MAX_CAPABILITY", "70")
    assert _analysis_capability_payload() == {EXPECTED_KEY: 70}


def test_analysis_payload_deep_removes_cap(monkeypatch):
    monkeypatch.setenv("HARNESS_ANALYSIS_DEEP", "1")
    assert _analysis_capability_payload() == {}


def test_ceiling_still_differentiates_cheap_and_hard_tasks():
    """End-to-end through the real router: with the ceiling Marionette sends,
    an easy role must classify far below the cap while a hard role clips to it
    -- the property min_capability destroyed."""
    if not ROUTER_HAS_CEILING:
        pytest.skip("installed puppetmaster predates max_capability")
    from puppetmaster.router import TaskSignals, classify_capability_needed

    ceiling = _analysis_capability_payload().get("max_capability")
    if ceiling is None:
        pytest.skip("deep mode enabled in this environment")

    easy = classify_capability_needed(TaskSignals(
        instruction="verify the build output", role="verify-runtime",
        explicit_max_capability=ceiling,
    ))
    hard = classify_capability_needed(TaskSignals(
        instruction="security audit of authentication across every endpoint",
        role="security-review", explicit_max_capability=ceiling,
    ))
    assert easy < ceiling
    assert hard == ceiling
