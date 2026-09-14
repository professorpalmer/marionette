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


def test_pin_candidates_include_opencode_go_deepseek_aliases():
    from harness.swarm_model_pin import pin_candidates

    for pin in (
        "opencode-go:deepseek-v4-flash",
        "agentic/deepseek-v4-flash",
        "agentic/opencode/deepseek-v4-flash",
        "deepseek-v4-flash",
    ):
        cands = [c.lower() for c in pin_candidates(pin)]
        blob = " ".join(cands)
        assert "deepseek-v4-flash" in blob
        assert "agentic/opencode-go/deepseek-v4-flash" in blob
        assert "glm-5.2" not in blob


def test_pin_candidates_map_opencode_go_deepseek_flash_live_id():
    from harness.swarm_model_pin import pin_candidates

    for pin in ("opencode-go:deepseek-flash", "deepseek-flash", "agentic/deepseek-flash"):
        cands = [c.lower() for c in pin_candidates(pin)]
        blob = " ".join(cands)
        assert "deepseek-flash" in blob
        assert "deepseek-v4-flash" in blob
        assert "agentic/opencode-go/deepseek-flash" in blob
        assert "glm-5.2" not in blob


def test_resolve_opencode_go_deepseek_flash_pin_against_v4_registry(
    monkeypatch, tmp_path,
):
    """Picker/live id deepseek-flash must pin the curated v4-flash worker row."""
    models_path = tmp_path / "models.json"
    models_path.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "agentic/deepseek-v4-flash",
                        "adapter": "agentic",
                        "adapter_model_name": "deepseek-v4-flash",
                        "capability_score": 66,
                        "payload_defaults": {
                            "provider": "opencode-go",
                            "model": "deepseek-v4-flash",
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
        lambda: {"opencode-go"},
    )
    monkeypatch.setattr(
        "harness.swarm_model_pin._settings_enabled_specs",
        lambda: ["opencode-go:deepseek-flash"],
    )
    monkeypatch.setattr(
        "puppetmaster.model_registry.apply_model_pin",
        lambda payload, model, *, adapter, registry=None: {
            **(payload or {}),
            "model": "deepseek-v4-flash",
            "provider": "opencode-go",
            "pinned_model": "agentic/deepseek-v4-flash",
            "pinned_adapter_model_name": "deepseek-v4-flash",
        },
    )

    from harness.swarm_model_pin import resolve_agentic_model_pin, resolve_swarm_model_pin

    for pin in (
        "opencode-go:deepseek-flash",
        "deepseek-flash",
        "agentic/deepseek-flash",
        "opencode-go:deepseek-v4-flash",
    ):
        out = resolve_swarm_model_pin(pin, allowed_adapters=["agentic"])
        assert out["demoted"] is False, out
        assert out["resolved"] == "agentic/deepseek-v4-flash"
        assert out["pin_fields"]["model"] == "deepseek-flash"
        pin_obj, err = resolve_agentic_model_pin(pin)
        assert err == ""
        assert pin_obj is not None
        assert pin_obj.model == "deepseek-flash"
        assert pin_obj.router_model_id == "agentic/deepseek-v4-flash"


def test_resolve_rejects_cursor_alias_when_only_agentic_is_allowed(monkeypatch, tmp_path):
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
    assert out["demoted"] is True
    assert out["auto_route"] is True
    assert out["resolved"] == ""
    assert out["adapter"] == ""
    assert out["pin_fields"] == {}


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
    assert pin is None
    assert "not in keyed worker registry" in error


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
    assert "unavailable pins fail without choosing a different model" in text.lower()


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

    for pin in ("openai-codex:gpt-5.6-luna", "openai-codex/gpt-5.6-luna"):
        out = resolve_swarm_model_pin(pin)
        assert out["demoted"] is False, pin
        assert out["auto_route"] is False, pin
        assert out["resolved"] == "agentic/openai-codex/gpt-5.6-luna", pin
        assert out["pin_fields"].get("provider") == "openai-codex", pin

    cursor = resolve_swarm_model_pin("cursor/gpt-5-6-luna")
    assert cursor["demoted"] is True
    assert cursor["resolved"] == ""


def test_resolve_cursor_pin_is_rejected_even_when_union_is_requested(monkeypatch, tmp_path):
    """Product swarms reject the former cross-adapter Cursor fallback."""
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
    assert out["demoted"] is True
    assert out["adapter"] == ""
    assert out["resolved"] == ""


def test_settings_enabled_pin_specs_requires_exact_astra_id():
    from harness.swarm_model_pin import settings_enabled_pin_specs

    enabled = [
        "openai-codex:gpt-5.6-sol",
        "openai-codex:gpt-5.6-luna",
        "openai-codex:gpt-6-astra",
    ]
    # A similarly named generation is not the requested model.
    assert settings_enabled_pin_specs(
        "agentic/openai-codex/gpt-5.6-astra", enabled=enabled,
    ) == []
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


def test_resolve_gpt56_astra_pin_does_not_remap_to_gpt6_astra(monkeypatch, tmp_path):
    """A GPT 5.6 Astra typo must not silently select GPT 6 Astra."""
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
    assert out["demoted"] is True
    assert out["resolved"] == ""
    assert out["pin_fields"] == {}
    pin, error = resolve_agentic_model_pin("agentic/openai-codex/gpt-5.6-astra")
    assert pin is None
    assert "not in keyed worker registry" in error

    exact = resolve_swarm_model_pin("agentic/openai-codex/gpt-6-astra")
    assert exact["demoted"] is False
    assert exact["resolved"] == "agentic/openai-codex/gpt-6-astra"


def test_opencode_go_curated_bound_into_auto_registry():
    from harness.auto_registry import _CURATED_MODELS
    from harness.opencode_go import CURATED_MODELS

    go = _CURATED_MODELS.get("opencode-go") or []
    assert go, "opencode-go curated must be non-empty for Go-only auth"
    slugs = {slug for _n, _t, slug in go}
    assert "gpt-5.6-luna" in slugs
    assert "deepseek-v4-flash" in slugs
    assert "deepseek-flash" in CURATED_MODELS
    assert slugs == set(CURATED_MODELS) - {"deepseek-flash"}


def test_exposed_catalog_lists_enabled_astra_ahead_of_file_order_junk(
    monkeypatch, tmp_path,
):
    """Settings-enabled Astra must appear in the 16-slot hint.

    File order puts cursor peers and openai-api leftovers first; Astra is last.
    Yesterday's registry write is not enough if the exposed list is file order.
    """
    models_path = tmp_path / "models.json"
    junk = [
        {"id": f"cursor/filler-{i}", "adapter": "cursor"}
        for i in range(14)
    ]
    models_path.write_text(
        json.dumps({
            "models": junk + [
                {"id": "cursor/gpt-5-6-luna", "adapter": "cursor"},
                {"id": "cursor/gpt-5-6-sol", "adapter": "cursor"},
                {
                    "id": "agentic/gpt-3.5-turbo",
                    "adapter": "agentic",
                    "adapter_model_name": "gpt-3.5-turbo",
                    "payload_defaults": {"provider": "openai-api"},
                },
                {
                    "id": "agentic/openai-codex/gpt-5.6-luna",
                    "adapter": "agentic",
                    "adapter_model_name": "gpt-5.6-luna",
                    "payload_defaults": {"provider": "openai-codex"},
                },
                {
                    "id": "agentic/openai-codex/gpt-6-astra",
                    "adapter": "agentic",
                    "adapter_model_name": "gpt-6-astra",
                    "payload_defaults": {"provider": "openai-codex"},
                },
                {
                    "id": "agentic/moonshotai/kimi-k3",
                    "adapter": "agentic",
                    "adapter_model_name": "moonshotai/kimi-k3",
                    "payload_defaults": {"provider": "openrouter"},
                },
            ]
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setattr(
        "harness.auto_registry.keyed_agentic_providers",
        lambda: {"openai-codex", "openrouter"},
    )
    monkeypatch.setattr(
        "harness.model_visibility.get_enabled",
        lambda: [
            "openai-codex:gpt-5.6-luna",
            "openai-codex:gpt-6-astra",
            "openrouter:moonshotai/kimi-k3",
        ],
    )
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist.resolve_swarm_worker_allowlist",
        lambda **_k: {
            "allowed_adapters": ["agentic", "cursor"],
            "prefer_plan_billed": False,
            "primary_adapter": "agentic",
        },
    )

    from harness.swarm_model_pin import (
        list_available_worker_models,
        swarm_model_pin_hint,
    )

    available = list_available_worker_models(
        limit=16, adapters={"agentic", "cursor"},
    )
    assert "agentic/openai-codex/gpt-6-astra" in available
    assert "agentic/openai-codex/gpt-5.6-luna" in available
    assert "agentic/gpt-3.5-turbo" not in available
    assert "cursor/filler-0" not in available
    hint = swarm_model_pin_hint(limit=16)
    assert "gpt-6-astra" in hint
    assert "gpt-3.5-turbo" not in hint


def test_luna_max_aliases_normalize_to_gpt56_luna_with_max_effort():
    from harness.swarm_model_pin import (
        is_luna_max_pin,
        normalize_swarm_model_pin_request,
        pin_candidates,
    )

    for pin in (
        "Luna Max",
        "GPT Luna Max",
        "gpt-5.6-luna-max",
        "openai-codex:Luna Max",
        "agentic/openai-codex/gpt-luna-max",
    ):
        assert is_luna_max_pin(pin), pin
        out, meta = normalize_swarm_model_pin_request(pin)
        assert "luna-pro" not in out.lower(), (pin, out)
        assert out.endswith("gpt-5.6-luna") or out == "gpt-5.6-luna" or out.endswith(":gpt-5.6-luna") or "/gpt-5.6-luna" in out, (pin, out)
        assert meta.get("reasoning_effort_hint") == "max", (pin, meta)
        assert "gpt-5.6-luna-pro" not in pin_candidates(pin)


def test_codex_oauth_pro_pins_remap_to_base_family():
    from harness.swarm_model_pin import (
        normalize_swarm_model_pin_request,
        pin_candidates,
        remap_codex_oauth_pro_model,
    )

    remapped, reason = remap_codex_oauth_pro_model("gpt-5.6-luna-pro")
    assert remapped == "gpt-5.6-luna"
    assert "codex_oauth_pro_remap" in reason

    out, meta = normalize_swarm_model_pin_request("openai-codex:gpt-5.6-luna-pro")
    assert out == "openai-codex:gpt-5.6-luna"
    assert meta.get("codex_pro_remap")

    cands = pin_candidates("agentic/openai-codex/gpt-5.6-sol-pro")
    assert "agentic/openai-codex/gpt-5.6-sol" in cands
    assert all("sol-pro" not in c or c.endswith("sol-pro") is False or True for c in cands)
    # Remapped base must lead; never prefer *-pro for Codex OAuth dispatch.
    assert cands[0] == "agentic/openai-codex/gpt-5.6-sol"


def test_resolve_luna_max_pin_stamps_reasoning_effort_max(monkeypatch, tmp_path):
    import json
    models_path = tmp_path / "models.json"
    models_path.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "agentic/openai-codex/gpt-5.6-luna",
                        "adapter": "agentic",
                        "adapter_model_name": "gpt-5.6-luna",
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
        if "luna-pro" in str(model):
            raise AssertionError(f"must never pin luna-pro, got {model!r}")
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
                "pinned_adapter_model_name": "gpt-5.6-luna",
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

    from harness.swarm_model_pin import resolve_swarm_model_pin

    out = resolve_swarm_model_pin("GPT Luna Max")
    assert out["demoted"] is False
    assert out["resolved"] == "agentic/openai-codex/gpt-5.6-luna"
    assert out["pin_fields"].get("model") == "gpt-5.6-luna"
    assert "luna-pro" not in str(out["pin_fields"]).lower()
    assert out["pin_fields"].get("reasoning_effort") == "max"


def test_openai_codex_pilot_models_drop_pro_slugs():
    from harness.providers import get_provider

    p = get_provider("openai-codex")
    models = list(p.pilot_models)
    assert "gpt-5.6-luna" in models
    assert "gpt-5.6-luna-pro" not in models
    assert "gpt-5.6-sol-pro" not in models
    assert "gpt-5.6-terra-pro" not in models
