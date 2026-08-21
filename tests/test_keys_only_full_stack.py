from __future__ import annotations

"""Keys-only product contract: one Full stack Settings key, no Cursor install.

Hermetic — no network, no cursor/claude binary, no CURSOR_API_KEY.
"""

import json
from unittest.mock import patch

from harness.config import HarnessConfig
from harness.edit_engines import select_edit_engine, workers_ready, pilot_keys_ready
from harness.provider_capabilities import worker_capability
from harness.swarm_worker_allowlist import resolve_swarm_worker_allowlist


def _clear_platform_env(monkeypatch):
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.delenv("HARNESS_EDIT_ENGINE", raising=False)
    for k in (
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
        "OPENROUTER_API_KEY", "OPENCODE_GO_API_KEY", "OPENAI_CODEX_TOKEN",
        "AWS_BEARER_TOKEN_BEDROCK", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
        "DEEPSEEK_API_KEY", "XAI_API_KEY", "ZAI_API_KEY", "GLM_API_KEY",
        "MINIMAX_API_KEY", "NVIDIA_API_KEY", "NOUS_API_KEY", "HERMES_API_KEY",
    ):
        monkeypatch.delenv(k, raising=False)


def _only(*names):
    keyed = set(names)

    def _get_provider_key(provider):
        return ("fake-key-" + provider.name) if provider.name in keyed else None

    return _get_provider_key


def test_full_stack_map_covers_settings_keys():
    for name in (
        "openrouter", "anthropic", "openai", "gemini", "openai-codex",
        "opencode-go", "opencode-zen", "nous", "bedrock", "minimax", "nvidia",
    ):
        assert worker_capability(name) == "full_stack", name
    assert worker_capability("cursor-cli") == "pilot_only"
    assert worker_capability("cursor") == "platform_worker"


def test_openrouter_only_selects_agentic_and_allowlist(monkeypatch, tmp_path):
    _clear_platform_env(monkeypatch)
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr(
        "harness.edit_engines.cursor_platform_available", lambda: False
    )
    monkeypatch.setattr(
        "harness.edit_engines.agentic_available", lambda: True
    )
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist._agentic_eligible", lambda: True
    )
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist._cursor_platform_ready", lambda: False
    )
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist._platform_locked_adapters",
        lambda: frozenset({"agentic"}),
    )
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist._enabled_or_visible_specs",
        lambda: ["openrouter:moonshotai/kimi-k3"],
    )

    cfg = HarnessConfig(repo=str(tmp_path), driver="openrouter:moonshotai/kimi-k3")
    assert select_edit_engine(cfg) == "agentic"
    out = resolve_swarm_worker_allowlist()
    assert out["allowed_adapters"] == ["agentic"]
    assert out["prefer_plan_billed"] is False
    assert out["primary_adapter"] == "agentic"


def test_codex_oauth_only_is_full_stack_workers(monkeypatch, tmp_path):
    _clear_platform_env(monkeypatch)
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("harness.edit_engines.cursor_platform_available", lambda: False)
    monkeypatch.setattr("harness.registry_wizard.get_provider_key", _only("openai-codex"))
    monkeypatch.setattr(
        "harness.keys.get_api_key_status",
        lambda name: {"has_key": name == "openai-codex", "masked": "****"},
    )
    monkeypatch.setattr("harness.keys.get_disconnected", lambda: set())
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist._agentic_eligible", lambda: True
    )
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist._cursor_platform_ready", lambda: False
    )
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist._platform_locked_adapters",
        lambda: frozenset({"agentic"}),
    )
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist._enabled_or_visible_specs",
        lambda: ["openai-codex:gpt-5.4"],
    )

    assert workers_ready() is True
    assert pilot_keys_ready() is True
    out = resolve_swarm_worker_allowlist()
    assert out["allowed_adapters"] == ["agentic"]
    assert out["prefer_plan_billed"] is False


def test_cursor_cli_only_is_not_workers_ready(monkeypatch, tmp_path):
    _clear_platform_env(monkeypatch)
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("harness.edit_engines.cursor_platform_available", lambda: False)
    monkeypatch.setattr("harness.registry_wizard.get_provider_key", _only("cursor-cli"))
    monkeypatch.setattr(
        "harness.keys.get_api_key_status",
        lambda name: {"has_key": name == "cursor-cli", "masked": "****"},
    )
    monkeypatch.setattr("harness.keys.get_disconnected", lambda: set())
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist._agentic_eligible", lambda: False
    )
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist._cursor_platform_ready", lambda: False
    )
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist._platform_locked_adapters",
        lambda: frozenset({"agentic"}),
    )
    monkeypatch.setattr(
        "harness.swarm_worker_allowlist._enabled_or_visible_specs",
        lambda: ["cursor-cli:cursor-grok-4.5-high"],
    )

    assert workers_ready() is False
    assert pilot_keys_ready() is True
    out = resolve_swarm_worker_allowlist()
    assert "cursor" not in out["allowed_adapters"]
    assert out["prefer_plan_billed"] is False


def test_stale_cursor_cli_toggles_do_not_starve_openrouter_catalog(monkeypatch, tmp_path):
    models_path = tmp_path / "models.json"
    vis_store = tmp_path / "visibility.json"
    vis_store.write_text(json.dumps({
        "enabled": ["cursor-cli:cursor-grok-4.5-high", "cursor-cli:composer-2.5"],
    }), encoding="utf-8")
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))
    monkeypatch.setenv("HARNESS_LIVE_PRICES", "0")
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        "harness.model_visibility._store_path", lambda: str(vis_store),
    )
    _clear_platform_env(monkeypatch)

    live = ["moonshotai/kimi-k3", "z-ai/glm-5.2", "xiaomi/mimo-v2-flash"]

    with patch("harness.registry_wizard.get_provider_key", _only("openrouter")), \
         patch("harness.keys.get_disconnected", lambda: set()), \
         patch("harness.model_fetch.fetch_models", lambda *_a, **_k: list(live)):
        from harness.auto_registry import ensure_keyed_provider_registry_health
        report = ensure_keyed_provider_registry_health()

    assert report["ready"] is True
    assert "openrouter" in report["providers"]
    models = json.loads(models_path.read_text(encoding="utf-8"))["models"]
    agentic = [m for m in models if m.get("adapter") == "agentic"]
    assert agentic
    assert {m["payload_defaults"]["provider"] for m in agentic} == {"openrouter"}
    ids = {m["id"] for m in agentic}
    assert any("kimi" in mid or "glm" in mid or "deepseek" in mid for mid in ids)
    assert not any("mimo" in mid for mid in ids)


def test_get_config_exposes_split_readiness(monkeypatch):
    from types import SimpleNamespace
    import threading
    from harness.api.settings import SettingsServices, get_config

    monkeypatch.setattr(
        "harness.edit_engines.select_edit_engine", lambda cfg: "agentic", raising=False
    )
    monkeypatch.setattr(
        "harness.edit_engines.workers_ready", lambda: True, raising=False
    )
    monkeypatch.setattr(
        "harness.edit_engines.pilot_keys_ready", lambda: True, raising=False
    )
    monkeypatch.setattr(
        "harness.reasoning_effort.current_reasoning_effort",
        lambda: "low",
        raising=False,
    )
    busy = threading.Lock()
    svc = SettingsServices(
        cfg=SimpleNamespace(
            driver="openrouter:moonshotai/kimi-k3",
            reach="openrouter",
            budget=3,
            repo="/r",
            swarm_adapter="agentic",
        ),
        get_pilot=lambda: SimpleNamespace(_busy=busy),
        get_session=lambda: SimpleNamespace(state_dir="/state", preflight=lambda: {"ok": True}),
        parse_bool=lambda v: bool(v),
        set_api_key=lambda *_a: None,
        clear_api_key=lambda *_a: None,
        rebuild_pilot_and_session=lambda: None,
        available_pilots=lambda: ["openrouter:moonshotai/kimi-k3"],
        save_workspace_driver=lambda *_a: None,
        persist_env_setting=lambda *_a: None,
        get_settings_dict=lambda: {},
    )
    code, payload = get_config(svc)
    assert code == 200
    assert payload["workers_ready"] is True
    assert payload["pilot_ready"] is True
    assert payload["agentic_ready"] is True
    assert payload["edit_engine"] == "agentic"
