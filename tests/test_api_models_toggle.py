from __future__ import annotations

"""Settings model toggle/set must refresh the isolated worker catalog."""

from types import SimpleNamespace

from harness.api.providers import ProviderServices, post_models_set, post_models_toggle


def _svc():
    return ProviderServices(
        cfg=SimpleNamespace(driver="openai-codex:gpt-5.6-sol"),
        diag=lambda *_a, **_k: None,
        parse_bool=lambda v: bool(v) if not isinstance(v, str) else v.lower() in (
            "1", "true", "yes", "on",
        ),
        resync_driver_after_model_curation=lambda: {
            "driver": "openai-codex:gpt-5.6-sol",
            "changed": False,
        },
        driver_provider_available=lambda _n: True,
        resolve_available_driver=lambda: None,
        rebuild_pilot_and_session=lambda: None,
    )


def test_models_toggle_syncs_agentic_worker_registry(monkeypatch):
    syncs = []
    monkeypatch.setattr(
        "harness.model_visibility.toggle",
        lambda spec, on: ["openai-codex:gpt-6-astra"] if on else [],
    )
    monkeypatch.setattr(
        "harness.auto_registry.sync_agentic_registry_safe",
        lambda force=False: syncs.append({"force": force}),
    )

    code, body = post_models_toggle(
        {"spec": "openai-codex:gpt-6-astra", "enabled": True},
        _svc(),
    )
    assert code == 200
    assert body["ok"] is True
    assert syncs, "Settings enable must sync the isolated worker catalog"


def test_models_set_syncs_agentic_worker_registry(monkeypatch):
    syncs = []
    monkeypatch.setattr(
        "harness.model_visibility.set_enabled",
        lambda specs: list(specs or []),
    )
    monkeypatch.setattr(
        "harness.auto_registry.sync_agentic_registry_safe",
        lambda force=False: syncs.append({"force": force}),
    )

    code, body = post_models_set(
        {"enabled": ["openai-codex:gpt-6-astra", "openai-codex:gpt-5.6-sol"]},
        _svc(),
    )
    assert code == 200
    assert body["ok"] is True
    assert syncs, "Settings set must sync the isolated worker catalog"
