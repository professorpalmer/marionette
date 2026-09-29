"""Setting or clearing a provider key drops that provider's cached model catalog."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import harness.api.providers as providers_api
from harness import model_fetch


def _svc():
    return providers_api.ProviderServices(
        cfg=SimpleNamespace(driver="openai/gpt-x"),
        diag=lambda *_a, **_k: None,
        parse_bool=bool,
        resync_driver_after_model_curation=lambda: {},
        driver_provider_available=lambda _d: True,
        resolve_available_driver=lambda: None,
        rebuild_pilot_and_session=lambda: None,
    )


@pytest.fixture
def stubbed(monkeypatch, tmp_path):
    monkeypatch.setattr(
        model_fetch, "_cache_path", lambda: str(tmp_path / "models_cache.json"),
    )
    monkeypatch.setattr(providers_api, "set_api_key", lambda *_a: None)
    monkeypatch.setattr(providers_api, "clear_api_key", lambda *_a: None)
    monkeypatch.setattr(providers_api, "scrub_provider_env", lambda *_a: None)
    monkeypatch.setattr(
        providers_api, "get_api_key_status",
        lambda _n: {"has_key": True, "masked": "sk-...1"},
    )
    import harness.auto_registry as ar
    monkeypatch.setattr(ar, "sync_agentic_registry_safe", lambda: None)
    monkeypatch.setitem(model_fetch._MEM, "openai", ["old-org-model"])
    monkeypatch.setitem(model_fetch._MEM, "anthropic", ["keep-me"])


@pytest.mark.parametrize("body", [
    {"provider": "openai", "api_key": "sk-new"},
    {"provider": "openai", "action": "clear"},
])
def test_key_change_drops_only_that_provider_catalog(stubbed, body):
    status, _ = providers_api.post_providers_key(body, _svc())
    assert status == 200
    assert "openai" not in model_fetch._MEM
    assert model_fetch._MEM["anthropic"] == ["keep-me"]
