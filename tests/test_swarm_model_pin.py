from __future__ import annotations

"""Swarm model pins resolve against the keyed agentic catalog (or demote)."""

import json


def test_pin_candidates_remap_cursor_luna_to_opencode_dots():
    from harness.swarm_model_pin import pin_candidates

    cands = pin_candidates("cursor/gpt-5-6-luna")
    assert "cursor/gpt-5-6-luna" in cands
    assert "gpt-5-6-luna" in cands
    assert "gpt-5.6-luna" in cands
    assert "agentic/gpt-5.6-luna" in cands


def test_resolve_demotes_unknown_pin_to_auto_route(monkeypatch, tmp_path):
    models_path = tmp_path / "models.json"
    models_path.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "agentic/gpt-5.6-luna",
                        "adapter": "agentic",
                        "adapter_model_name": "gpt-5.6-luna",
                        "capability_score": 85,
                        "payload_defaults": {"provider": "opencode-go"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setattr(
        "harness.auto_registry.ensure_keyed_provider_registry_health",
        lambda: {"ready": True},
    )
    monkeypatch.setattr(
        "harness.auto_registry.keyed_agentic_providers",
        lambda: {"opencode-go"},
    )

    def _fake_pin(payload, model, *, adapter, registry=None):
        if adapter != "agentic":
            return {**(payload or {}), "model": model}
        if model in ("gpt-5.6-luna", "agentic/gpt-5.6-luna"):
            return {
                **(payload or {}),
                "model": "gpt-5.6-luna",
                "provider": "opencode-go",
                "pinned_model": "agentic/gpt-5.6-luna",
            }
        return {**(payload or {}), "model": model}

    monkeypatch.setattr(
        "puppetmaster.model_registry.apply_model_pin", _fake_pin
    )
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist.resolve_swarm_worker_allowlist",
        lambda **_k: {
            "allowed_adapters": ["agentic"],
            "prefer_plan_billed": False,
            "primary_adapter": "agentic",
        },
    )

    from harness.swarm_model_pin import resolve_swarm_model_pin

    out = resolve_swarm_model_pin("cursor/gpt-5-6-luna")
    assert out["demoted"] is False
    assert out["auto_route"] is False
    assert out["resolved"] == "agentic/gpt-5.6-luna"
    assert out["adapter"] == "agentic"
    assert out["pin_fields"].get("provider") == "opencode-go"


def test_resolve_unknown_pin_demotes_instead_of_raising(monkeypatch, tmp_path):
    models_path = tmp_path / "models.json"
    models_path.write_text(json.dumps({"models": []}), encoding="utf-8")
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setattr(
        "harness.auto_registry.ensure_keyed_provider_registry_health",
        lambda: {"ready": False},
    )
    monkeypatch.setattr(
        "harness.auto_registry.keyed_agentic_providers",
        lambda: set(),
    )
    monkeypatch.setattr(
        "puppetmaster.model_registry.apply_model_pin",
        lambda payload, model, *, adapter, registry=None: {
            **(payload or {}), "model": model,
        },
    )
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist.resolve_swarm_worker_allowlist",
        lambda **_k: {
            "allowed_adapters": ["agentic"],
            "prefer_plan_billed": False,
            "primary_adapter": "agentic",
        },
    )

    from harness.swarm_model_pin import resolve_swarm_model_pin

    out = resolve_swarm_model_pin("not-a-real-model")
    assert out["demoted"] is True
    assert out["auto_route"] is True
    assert out["pin_fields"] == {}
    assert out["resolved"] == ""


def test_resolve_direct_openrouter_agentic_pin_strict(monkeypatch, tmp_path):
    models_path = tmp_path / "models.json"
    models_path.write_text(json.dumps({"models": []}), encoding="utf-8")
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setattr(
        "harness.auto_registry.ensure_keyed_provider_registry_health",
        lambda: {"ready": True},
    )
    monkeypatch.setattr(
        "harness.auto_registry.keyed_agentic_providers",
        lambda: {"openrouter"},
    )
    monkeypatch.setattr(
        "puppetmaster.model_registry.apply_model_pin",
        lambda payload, model, *, adapter, registry=None: {
            **(payload or {}),
            "model": model,
        },
    )

    from harness.swarm_model_pin import resolve_agentic_model_pin

    pin, error = resolve_agentic_model_pin("openrouter/stealth/ox-alpha")
    assert error == ""
    assert pin is not None
    assert pin.provider == "openrouter"
    assert pin.model == "stealth/ox-alpha"
    assert pin.router_model_id == "agentic/openrouter/stealth/ox-alpha"
    assert pin.payload_fields()["auto_route"] is False
    assert pin.payload_fields()["allowed_adapters"] == ["agentic"]


def test_resolve_direct_agentic_pin_requires_keyed_provider(monkeypatch, tmp_path):
    models_path = tmp_path / "models.json"
    models_path.write_text(json.dumps({"models": []}), encoding="utf-8")
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setattr(
        "harness.auto_registry.ensure_keyed_provider_registry_health",
        lambda: {"ready": False},
    )
    monkeypatch.setattr(
        "harness.auto_registry.keyed_agentic_providers",
        lambda: set(),
    )
    monkeypatch.setattr(
        "puppetmaster.model_registry.apply_model_pin",
        lambda payload, model, *, adapter, registry=None: {
            **(payload or {}),
            "model": model,
        },
    )

    from harness.swarm_model_pin import resolve_agentic_model_pin

    pin, error = resolve_agentic_model_pin("openrouter/stealth/ox-alpha")
    assert pin is None
    assert "not in keyed worker registry" in error


def test_run_swarm_model_description_mentions_live_catalog(monkeypatch):
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist.resolve_swarm_worker_allowlist",
        lambda **_k: {
            "allowed_adapters": ["agentic"],
            "prefer_plan_billed": False,
            "primary_adapter": "agentic",
        },
    )
    monkeypatch.setattr(
        "harness.swarm_model_pin.list_available_worker_models",
        lambda limit=16, adapters=None: [
            "agentic/gpt-5.6-luna", "agentic/deepseek-v4-flash",
        ],
    )
    from harness.pilot import _run_swarm_model_pin_description

    text = _run_swarm_model_pin_description()
    assert "agentic/gpt-5.6-luna" in text
    assert "remap" in text.lower()


def test_implement_and_parallel_tool_schemas_expose_model_pin():
    from harness.pilot import build_tools_schema

    tools = {
        row["function"]["name"]: row["function"]
        for row in build_tools_schema()
    }
    assert "model" in tools["run_implement"]["parameters"]["properties"]
    assert "model" in tools["run_parallel"]["parameters"]["properties"]


def test_implement_and_parallel_wire_model_pin_accept_nested_arguments():
    from harness.pilot import from_wire

    implement = from_wire(
        "run_implement",
        {
            "arguments": {
                "goal": "fix it",
                "model": "openrouter/stealth/ox-alpha",
            },
        },
    )
    parallel = from_wire(
        "run_parallel",
        {
            "arguments": {
                "goals": ["one", "two"],
                "model": "openrouter/stealth/ox-alpha",
            },
        },
    )
    assert implement.model == "openrouter/stealth/ox-alpha"
    assert parallel.model == "openrouter/stealth/ox-alpha"


def test_pin_candidates_include_openai_codex_colon_form():
    from harness.swarm_model_pin import pin_candidates

    cands = pin_candidates("openai-codex:gpt-5.6-luna")
    assert "gpt-5.6-luna" in cands
    assert "agentic/gpt-5.6-luna" in cands
    assert "agentic/openai-codex/gpt-5.6-luna" in cands


def test_resolve_openai_codex_colon_pin_to_namespaced_row(monkeypatch, tmp_path):
    models_path = tmp_path / "models.json"
    models_path.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "agentic/openai-codex/gpt-5.6-luna",
                        "adapter": "agentic",
                        "adapter_model_name": "gpt-5.6-luna",
                        "capability_score": 90,
                        "billing": "plan",
                        "payload_defaults": {
                            "provider": "openai-codex",
                            "model": "gpt-5.6-luna",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setattr(
        "harness.auto_registry.ensure_keyed_provider_registry_health",
        lambda: {"ready": True},
    )
    monkeypatch.setattr(
        "harness.auto_registry.keyed_agentic_providers",
        lambda: {"openai-codex"},
    )

    def _fake_pin(payload, model, *, adapter, registry=None):
        if adapter != "agentic":
            return {**(payload or {}), "model": model}
        if model in (
            "gpt-5.6-luna",
            "agentic/gpt-5.6-luna",
            "openai-codex/gpt-5.6-luna",
            "agentic/openai-codex/gpt-5.6-luna",
        ):
            return {
                **(payload or {}),
                "model": "gpt-5.6-luna",
                "provider": "openai-codex",
                "pinned_model": "agentic/openai-codex/gpt-5.6-luna",
            }
        return {**(payload or {}), "model": model}

    monkeypatch.setattr(
        "puppetmaster.model_registry.apply_model_pin", _fake_pin
    )
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist.resolve_swarm_worker_allowlist",
        lambda **_k: {
            "allowed_adapters": ["agentic"],
            "prefer_plan_billed": False,
            "primary_adapter": "agentic",
        },
    )

    from harness.swarm_model_pin import resolve_swarm_model_pin

    for pin in (
        "openai-codex:gpt-5.6-luna",
        "codex/gpt-5.6-luna",
        "cursor/gpt-5-6-luna",
    ):
        out = resolve_swarm_model_pin(pin)
        assert out["demoted"] is False, pin
        assert out["auto_route"] is False, pin
        assert out["resolved"] == "agentic/openai-codex/gpt-5.6-luna", pin
        assert out["pin_fields"].get("provider") == "openai-codex", pin


def test_resolve_cursor_pin_across_adapter_union(monkeypatch, tmp_path):
    """Cursor Grok pins resolve on the cursor adapter when agentic lacks the id."""
    models_path = tmp_path / "models.json"
    models_path.write_text(json.dumps({"models": []}), encoding="utf-8")
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setattr(
        "harness.auto_registry.ensure_keyed_provider_registry_health",
        lambda: {"ready": True},
    )

    def _fake_pin(payload, model, *, adapter, registry=None):
        if adapter == "cursor" and model in ("grok-4-5", "cursor/grok-4-5"):
            return {
                **(payload or {}),
                "model": "grok-4-5",
                "pinned_model": "cursor/grok-4-5",
                "pinned_adapter_model_name": "grok-4-5",
            }
        return {**(payload or {}), "model": model}

    monkeypatch.setattr(
        "puppetmaster.model_registry.apply_model_pin", _fake_pin
    )

    from harness.swarm_model_pin import resolve_swarm_model_pin

    out = resolve_swarm_model_pin(
        "cursor/grok-4-5", allowed_adapters=["agentic", "cursor"],
    )
    assert out["demoted"] is False
    assert out["adapter"] == "cursor"
    assert out["resolved"] == "cursor/grok-4-5"


def test_settings_enabled_pin_specs_maps_astra_generation_not_sol():
    from harness.swarm_model_pin import settings_enabled_pin_specs

    enabled = [
        "openai-codex:gpt-5.6-sol",
        "openai-codex:gpt-5.6-luna",
        "openai-codex:gpt-6-astra",
    ]
    assert settings_enabled_pin_specs(
        "agentic/openai-codex/gpt-5.6-astra", enabled=enabled,
    ) == ["openai-codex:gpt-6-astra"]
    assert settings_enabled_pin_specs(
        "openai-codex:gpt-6-astra", enabled=enabled,
    ) == ["openai-codex:gpt-6-astra"]
    assert settings_enabled_pin_specs(
        "agentic/openai-codex/gpt-5.6-sol", enabled=enabled,
    ) == ["openai-codex:gpt-5.6-sol"]
    assert settings_enabled_pin_specs("agentic/openai-codex/gpt-5.6-astra", enabled=[
        "openai-codex:gpt-5.6-sol",
        "openai-codex:gpt-5.6-luna",
    ]) == []


def test_resolve_gpt56_astra_pin_to_enabled_gpt6_astra(monkeypatch, tmp_path):
    """Pilot 'GPT 5.6 Astra' pins must resolve to the Settings-enabled wire id."""
    models_path = tmp_path / "models.json"
    models_path.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "agentic/openai-codex/gpt-5.6-sol",
                        "adapter": "agentic",
                        "adapter_model_name": "gpt-5.6-sol",
                        "payload_defaults": {"provider": "openai-codex"},
                    },
                    {
                        "id": "agentic/openai-codex/gpt-6-astra",
                        "adapter": "agentic",
                        "adapter_model_name": "gpt-6-astra",
                        "payload_defaults": {"provider": "openai-codex"},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setattr(
        "harness.auto_registry.ensure_keyed_provider_registry_health",
        lambda: {"ready": True},
    )
    monkeypatch.setattr(
        "harness.auto_registry.keyed_agentic_providers",
        lambda: {"openai-codex"},
    )
    monkeypatch.setattr(
        "harness.model_visibility.get_enabled",
        lambda: [
            "openai-codex:gpt-5.6-sol",
            "openai-codex:gpt-6-astra",
        ],
    )

    def _fake_pin(payload, model, *, adapter, registry=None):
        if adapter != "agentic":
            return {**(payload or {}), "model": model}
        if model in (
            "gpt-6-astra",
            "openai-codex/gpt-6-astra",
            "agentic/openai-codex/gpt-6-astra",
        ):
            return {
                **(payload or {}),
                "model": "gpt-6-astra",
                "provider": "openai-codex",
                "pinned_model": "agentic/openai-codex/gpt-6-astra",
                "pinned_adapter_model_name": "gpt-6-astra",
            }
        if model in (
            "gpt-5.6-sol",
            "openai-codex/gpt-5.6-sol",
            "agentic/openai-codex/gpt-5.6-sol",
        ):
            return {
                **(payload or {}),
                "model": "gpt-5.6-sol",
                "provider": "openai-codex",
                "pinned_model": "agentic/openai-codex/gpt-5.6-sol",
                "pinned_adapter_model_name": "gpt-5.6-sol",
            }
        return {**(payload or {}), "model": model}

    monkeypatch.setattr("puppetmaster.model_registry.apply_model_pin", _fake_pin)
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist.resolve_swarm_worker_allowlist",
        lambda **_k: {
            "allowed_adapters": ["agentic"],
            "prefer_plan_billed": False,
            "primary_adapter": "agentic",
        },
    )

    from harness.swarm_model_pin import resolve_agentic_model_pin, resolve_swarm_model_pin

    out = resolve_swarm_model_pin("agentic/openai-codex/gpt-5.6-astra")
    assert out["demoted"] is False
    assert out["resolved"] == "agentic/openai-codex/gpt-6-astra"
    assert out["pin_fields"].get("provider") == "openai-codex"
    pin, error = resolve_agentic_model_pin("agentic/openai-codex/gpt-5.6-astra")
    assert error == ""
    assert pin is not None
    assert pin.model == "gpt-6-astra"
    assert pin.router_model_id == "agentic/openai-codex/gpt-6-astra"


def test_opencode_go_curated_bound_into_auto_registry():
    from harness.auto_registry import _CURATED_MODELS
    from harness.opencode_go import CURATED_MODELS

    go = _CURATED_MODELS.get("opencode-go") or []
    assert go, "opencode-go curated must be non-empty for Go-only auth"
    slugs = {slug for _n, _t, slug in go}
    assert "gpt-5.6-luna" in slugs
    assert "deepseek-v4-flash" in slugs
    assert slugs == set(CURATED_MODELS)
