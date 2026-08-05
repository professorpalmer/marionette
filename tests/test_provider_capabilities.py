from __future__ import annotations

"""Full-stack vs pilot-only capability map."""


def test_cursor_cli_is_pilot_only():
    from harness.provider_capabilities import worker_capability

    assert worker_capability("cursor-cli") == "pilot_only"


def test_openai_codex_and_nous_are_full_stack():
    from harness.provider_capabilities import worker_capability

    assert worker_capability("openai-codex") == "full_stack"
    assert worker_capability("nous") == "full_stack"
    assert worker_capability("minimax") == "full_stack"
    assert worker_capability("nvidia") == "full_stack"


def test_cursor_api_key_pool_is_platform_worker():
    from harness.provider_capabilities import worker_capability

    assert worker_capability("cursor") == "platform_worker"


def test_annotate_provider_row_stamps_labels():
    from harness.provider_capabilities import annotate_provider_row

    row = annotate_provider_row({"name": "cursor-cli", "display_name": "Cursor CLI"})
    assert row["worker_capability"] == "pilot_only"
    assert row["worker_capability_label"] == "Pilot only"
    assert "workers" in row["worker_capability_hint"].lower()


def test_product_worker_adapter_falls_back_to_cursor(monkeypatch):
    monkeypatch.setattr(
        "harness.auto_registry.keyed_agentic_providers",
        lambda: set(),
    )
    monkeypatch.setattr(
        "harness.provider_capabilities.cursor_platform_workers_ready",
        lambda env=None: True,
    )
    monkeypatch.setattr(
        "harness.swarm_adapter.resolve_bridge_swarm_adapter",
        lambda configured=None, repo_cwd="": "agentic",
    )
    from harness.swarm_worker_route import resolve_product_worker_adapter

    assert resolve_product_worker_adapter() == "cursor"


def test_product_worker_adapter_prefers_keyed_agentic(monkeypatch):
    monkeypatch.setattr(
        "harness.auto_registry.keyed_agentic_providers",
        lambda: {"openrouter"},
    )
    monkeypatch.setattr(
        "harness.provider_capabilities.cursor_platform_workers_ready",
        lambda env=None: True,
    )
    monkeypatch.setattr(
        "harness.swarm_adapter.resolve_bridge_swarm_adapter",
        lambda configured=None, repo_cwd="": "agentic",
    )
    from harness.swarm_worker_route import resolve_product_worker_adapter

    assert resolve_product_worker_adapter() == "agentic"
