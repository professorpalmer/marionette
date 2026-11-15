"""Anthropic picker rows bill as the account the pilot uses.

A Claude Max sign-in is a plan; an API key is metered. Context and price
come from the live model list, then models.dev.
"""

from __future__ import annotations

import pytest

from harness import credential_pool as cp
from harness import keys as keys_mod
from harness import model_fetch
from harness.providers import get_provider
from harness.usage_ledger.accounts import billing_for_spec, resolve_route


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN"):
        monkeypatch.setenv(name, "")
    stored: dict = {}
    monkeypatch.setattr(keys_mod, "_read_keys", lambda: dict(stored))
    monkeypatch.setattr(keys_mod, "_write_keys",
                        lambda k: (stored.clear(), stored.update(k)))
    cp.clear_pools_for_tests()
    yield
    cp.clear_pools_for_tests()


def test_a_claude_max_sign_in_bills_as_plan(state):
    cp.add_oauth_entry("anthropic", access_token="sk-ant-oat01-fresh",
                       refresh_token="rt", label="cary@example.com")
    assert get_provider("anthropic").billing == "plan"
    assert billing_for_spec("anthropic:claude-opus-5-5") == "plan"
    assert resolve_route("anthropic:claude-opus-5-5", "", "").billing == "plan"


def test_an_api_key_bills_as_metered(state, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-abcdefabcdef")
    assert get_provider("anthropic").billing == "metered"
    assert billing_for_spec("anthropic:claude-opus-5-5") == "metered"


def test_an_expired_sign_in_is_still_a_plan_account(state, monkeypatch):
    """The pool entry is dead, the env keeps the token: still a Max account."""
    entry = cp.add_api_key("anthropic", "sk-ant-oat01-expired")
    cp.report_failure("anthropic", entry.id, status_code=401, message="expired")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-oat01-expired")
    assert get_provider("anthropic").billing == "plan"


def test_declared_billing_of_other_providers_is_unchanged(state):
    assert get_provider("opencode-go").billing == "plan"
    assert get_provider("openrouter").billing == "metered"


def test_anthropic_live_list_carries_context_and_price(state, monkeypatch):
    monkeypatch.setattr(model_fetch, "_get", lambda url, headers: {"data": [
        {"id": "claude-opus-5-5", "display_name": "Claude Opus 5.5"},
        {"id": "claude-sonnet-9", "display_name": "Claude Sonnet 9",
         "max_input_tokens": 400000},
    ]})

    def lookup(provider, model_id, *, allow_network=True):
        if model_id == "claude-opus-5-5":
            return {"id": model_id, "context_length": 1_000_000}
        return None

    monkeypatch.setattr("harness.models_dev.lookup_model", lookup)
    monkeypatch.setattr(
        "harness.models_dev.lookup_cost",
        lambda provider, model_id, allow_network=False: (
            ({"input": 5, "output": 25}, 0.0) if model_id == "claude-opus-5-5" else None),
    )

    records = model_fetch._fetch_provider_models(get_provider("anthropic"), "sk-ant-oat01-x")

    opus, sonnet = records
    assert opus["id"] == "claude-opus-5-5"
    assert opus["name"] == "Claude Opus 5.5"
    assert opus["context_length"] == 1_000_000
    assert opus["pricing"] == {"prompt": 5e-06, "completion": 2.5e-05}
    assert sonnet["context_length"] == 400000
    assert "pricing" not in sonnet


def test_a_new_state_loads_models_dev_for_context_and_price(state, monkeypatch):
    """A new state dir has no models.dev cache. The first live fetch must
    still give context and price, not an empty row until a later fetch."""
    import io
    import json

    from harness import models_dev

    models_dev.clear_cache_for_tests()
    registry = {"anthropic": {"models": {"claude-opus-5-5": {
        "name": "Claude Opus 5.5", "limit": {"context": 1_000_000},
        "cost": {"input": 4, "output": 20},
    }}}}
    monkeypatch.setattr(models_dev.urllib.request, "urlopen",
                        lambda req, timeout=None: io.BytesIO(json.dumps(registry).encode()))
    monkeypatch.setattr(model_fetch, "_get", lambda url, headers: {"data": [
        {"id": "claude-opus-5-5", "display_name": "Claude Opus 5.5"},
    ]})

    (opus,) = model_fetch._fetch_provider_models(get_provider("anthropic"), "sk-ant-oat01-x")

    assert opus["context_length"] == 1_000_000
    assert opus["pricing"] == {"prompt": 4e-06, "completion": 2e-05}
    models_dev.clear_cache_for_tests()
