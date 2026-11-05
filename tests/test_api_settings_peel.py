"""Characterization tests for settings/config API peel."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from harness.api.settings import (
    SettingsServices,
    get_config,
    get_settings,
    post_settings,
)


@pytest.fixture(autouse=True)
def _no_registry_sync(monkeypatch):
    monkeypatch.setattr(
        "harness.auto_registry.sync_agentic_registry_safe", lambda: None
    )


@pytest.fixture
def _offline_config(monkeypatch):
    monkeypatch.setattr(
        "harness.edit_engines.select_edit_engine", lambda cfg: "native", raising=False
    )
    monkeypatch.setattr(
        "harness.edit_engines.pilot_keys_ready", lambda: False, raising=False
    )
    monkeypatch.setattr(
        "harness.edit_engines.workers_ready", lambda: False, raising=False
    )
    monkeypatch.setattr(
        "harness.reasoning_effort.current_reasoning_effort",
        lambda: "low",
        raising=False,
    )


def _svc(**overrides):
    busy = threading.Lock()
    pilot = SimpleNamespace(
        _busy=busy,
        _auto_distill=False,
        _review_edits_before_apply=False,
        _auto_command_guard=True,
    )
    session = SimpleNamespace(state_dir="/state", preflight=lambda: {"ok": True})
    cfg = SimpleNamespace(
        driver="anthropic:claude-opus-4-8",
        reach="anthropic",
        budget=3,
        repo="/r",
        swarm_adapter="cursor",
        auto_verify=True,
        verify_command="",
    )
    calls = {"rebuild": 0, "persist": [], "keys": []}
    base = dict(
        cfg=cfg,
        get_pilot=lambda: pilot,
        get_session=lambda: session,
        parse_bool=lambda v: bool(v) if not isinstance(v, str) else v.lower() in ("1", "true", "yes"),
        set_api_key=lambda reach, val: calls["keys"].append(("set", reach, val)),
        clear_api_key=lambda reach: calls["keys"].append(("clear", reach)),
        rebuild_pilot_and_session=lambda: calls.__setitem__("rebuild", calls["rebuild"] + 1),
        available_pilots=lambda: ["anthropic:claude-opus-4-8"],
        save_workspace_driver=lambda repo, driver: None,
        persist_env_setting=lambda k, v: calls["persist"].append((k, v)),
        get_settings_dict=lambda: {"driver": cfg.driver, "budget": cfg.budget},
    )
    base.update(overrides)
    return SettingsServices(**base), cfg, pilot, calls


def test_get_config_and_settings(_offline_config):
    svc, cfg, _, _ = _svc()
    code, payload = get_config(svc)
    assert code == 200
    assert payload["driver"] == cfg.driver
    assert payload["models"] == ["anthropic:claude-opus-4-8"]
    assert payload["workers_ready"] is False
    assert payload["pilot_ready"] is False
    assert payload["agentic_ready"] is False
    assert isinstance(payload.get("model_labels"), dict)
    assert isinstance(payload.get("key_bootstrap_issues"), list)
    assert payload.get("swarm_reasoning_effort") in (
        "none", "low", "medium", "high", "xhigh", "max",
    )
    assert get_settings(svc)[1]["budget"] == 3


def test_get_config_includes_dynamic_model_labels(monkeypatch, _offline_config):
    seen = {}

    def fake_labels(specs, force=False):
        seen["specs"] = list(specs)
        seen["force"] = force
        return {
            "opencode-zen:big-pickle": "Big Pickle",
            "opencode-zen:mimo-v2.5-free": "MiMo-V2.5 Free",
        }

    monkeypatch.setattr(
        "harness.model_visibility.picker_model_labels",
        fake_labels,
        raising=False,
    )
    svc, _, _, _ = _svc(
        available_pilots=lambda: [
            "opencode-zen:big-pickle",
            "opencode-zen:mimo-v2.5-free",
        ],
    )
    code, payload = get_config(svc)
    assert code == 200
    assert payload["models"] == [
        "opencode-zen:big-pickle",
        "opencode-zen:mimo-v2.5-free",
    ]
    assert payload["model_labels"]["opencode-zen:big-pickle"] == "Big Pickle"
    assert payload["model_labels"]["opencode-zen:mimo-v2.5-free"] == "MiMo-V2.5 Free"
    assert seen["force"] is False
    assert seen["specs"] == payload["models"]


def test_get_config_includes_reasoning_support_for_models_and_current_driver():
    svc, cfg, _, _ = _svc(
        available_pilots=lambda: ["openai-codex:gpt-5.6-luna"],
    )
    cfg.driver = "local:Qwen/Qwen3-8B"

    code, payload = get_config(svc)

    assert code == 200
    assert payload["models"] == ["openai-codex:gpt-5.6-luna"]
    assert payload["reasoning_support"] == {
        "openai-codex:gpt-5.6-luna": True,
        "local:Qwen/Qwen3-8B": False,
    }


def test_get_config_retains_reasoning_support_when_model_labels_fail(monkeypatch):
    def unavailable_labels(*args, **kwargs):
        raise RuntimeError("catalog labels unavailable")

    monkeypatch.setattr("harness.model_visibility.picker_model_labels", unavailable_labels)
    monkeypatch.setenv("HARNESS_CODEX_REASONING_EFFORT", "high")
    svc, cfg, _, calls = _svc(available_pilots=lambda: [])
    cfg.driver = "local:Qwen/Qwen3-8B"

    code, payload = get_config(svc)

    assert code == 200
    assert payload["model_labels"] == {}
    assert payload["reasoning_support"] == {cfg.driver: False}
    assert payload["reasoning_effort"] == "high"
    assert calls["persist"] == []


def test_post_settings_budget_and_flags():
    svc, cfg, pilot, calls = _svc()
    code, payload = post_settings(
        {
            "budget": 10,
            "auto_distill": True,
            "commandTimeout": "off",
            "maxPilotSteps": "unlimited",
            "maxOutputTokens": "off",
            "pilotToolBudget": "50",
            "workerTokenBudget": "50000",
        },
        svc,
    )
    assert code == 200
    assert cfg.budget == 10
    assert pilot._auto_distill is True
    env = dict(calls["persist"])
    assert env["HARNESS_COMMAND_TIMEOUT"] == "0"
    assert env["HARNESS_MAX_PILOT_STEPS"] == "0"
    assert env["HARNESS_MAX_TOKENS"] == "0"
    assert env["HARNESS_PILOT_TOOL_BUDGET"] == "50"
    assert env["HARNESS_WORKER_TOKEN_BUDGET"] == "50000"


@pytest.mark.parametrize(
    ("key", "value", "env", "expected"),
    [
        pytest.param("pilotToolBudget", "unlimited", "HARNESS_PILOT_TOOL_BUDGET", "0",
                     id="pilot_tool_budget_unlimited"),
        pytest.param("maxOutputTokens", "16000", "HARNESS_MAX_TOKENS", "16000",
                     id="max_output_tokens_positive"),
        pytest.param("workerTokenBudget", "0", "HARNESS_WORKER_TOKEN_BUDGET", "0",
                     id="worker_token_budget_zero_is_unlimited"),
        pytest.param("workerTokenBudget", "unlimited", "HARNESS_WORKER_TOKEN_BUDGET", "0",
                     id="worker_token_budget_unlimited"),
        pytest.param("workerTokenBudget", "1", "HARNESS_WORKER_TOKEN_BUDGET", "250000",
                     id="worker_token_budget_one_resets_to_default"),
        pytest.param("workerTokenBudget", "39999", "HARNESS_WORKER_TOKEN_BUDGET", "250000",
                     id="worker_token_budget_below_floor_resets_to_default"),
        pytest.param("workerTokenBudget", "40000", "HARNESS_WORKER_TOKEN_BUDGET", "40000",
                     id="worker_token_budget_floor_accepted"),
        pytest.param("autoMaxTokens", "unlimited", "HARNESS_AUTO_MAX_TOKENS", "0",
                     id="auto_max_tokens_unlimited"),
        pytest.param("autoMaxTokens", "2000000", "HARNESS_AUTO_MAX_TOKENS", "2000000",
                     id="auto_max_tokens_positive"),
        pytest.param("compactionResidual", "hybrid", "HARNESS_COMPACTION_RESIDUAL", "hybrid",
                     id="compaction_residual_hybrid"),
        pytest.param("compactionResidual", "catalog", "HARNESS_COMPACTION_RESIDUAL", "catalog",
                     id="compaction_residual_catalog"),
        pytest.param("swarm_reasoning_effort", "high", "HARNESS_SWARM_REASONING_EFFORT", "high",
                     id="swarm_reasoning_effort"),
    ],
)
def test_post_settings_persists_env(key, value, env, expected):
    svc, _, _, calls = _svc()
    code, _ = post_settings({key: value}, svc)
    assert code == 200
    assert dict(calls["persist"])[env] == expected


@pytest.mark.parametrize(
    ("key", "value"),
    [
        pytest.param("budget", "x", id="budget"),
        pytest.param("maxOutputTokens", "nope", id="max_output_tokens_text"),
        pytest.param("maxOutputTokens", "-5", id="max_output_tokens_negative"),
        pytest.param("pilotToolBudget", "nope", id="pilot_tool_budget_text"),
        pytest.param("pilotToolBudget", "-1", id="pilot_tool_budget_negative"),
        pytest.param("workerTokenBudget", "nope", id="worker_token_budget_text"),
        pytest.param("autoMaxTokens", "nope", id="auto_max_tokens_text"),
        pytest.param("autoMaxTokens", "-1", id="auto_max_tokens_negative"),
    ],
)
def test_post_settings_rejects_bad_value(key, value):
    svc, _, _, _ = _svc()
    assert post_settings({key: value}, svc)[0] == 400


def test_post_settings_jev_enabled_persists_off_by_default():
    svc, _, _, calls = _svc()
    code, _ = post_settings({"jev_enabled": True}, svc)
    assert code == 200
    assert dict(calls["persist"])["HARNESS_JEV"] == "1"
    code, _ = post_settings({"jev_enabled": False}, svc)
    assert code == 200
    assert dict(calls["persist"])["HARNESS_JEV"] == "0"


def test_post_settings_compaction_residual_rejects_off():
    svc, _, _, calls = _svc()
    code, payload = post_settings({"compactionResidual": "off"}, svc)
    assert code == 400
    assert calls["persist"] == []
    assert payload["error"] == "Invalid compactionResidual"


def test_post_settings_browser_real_profile_roundtrip(monkeypatch):
    import os

    cleaned = []
    monkeypatch.setattr(
        "harness.browser_real_profile.cleanup_real_profile_snapshots",
        lambda: cleaned.append(True),
    )
    monkeypatch.setenv("PM_BROWSER_USER_DATA_DIR", "/tmp/isolated-profile")
    svc, _, _, calls = _svc()
    code, _ = post_settings({"browserRealProfile": True}, svc)
    assert code == 200
    assert dict(calls["persist"])["HARNESS_BROWSER_REAL_PROFILE"] == "1"
    assert cleaned == []
    assert "PM_BROWSER_USER_DATA_DIR" not in os.environ

    code, _ = post_settings({"browserRealProfile": False}, svc)
    assert code == 200
    assert dict(calls["persist"])["HARNESS_BROWSER_REAL_PROFILE"] == "0"
    assert cleaned == [True]


def test_post_settings_busy_on_key():
    busy = threading.Lock()
    busy.acquire()
    svc, _, _, _ = _svc(
        get_pilot=lambda: SimpleNamespace(
            _busy=busy,
            _auto_distill=False,
            _review_edits_before_apply=False,
            _auto_command_guard=True,
        )
    )
    code, payload = post_settings({"api_key": "sk-test"}, svc)
    assert code == 409
    busy.release()


def test_post_settings_unknown_driver(monkeypatch):
    monkeypatch.setattr(
        "harness.model_visibility.catalog",
        lambda available_only=True: [],
        raising=False,
    )
    svc, _, _, _ = _svc()
    code, payload = post_settings({"driver": "nope:model"}, svc)
    assert code == 400
    assert "Unknown" in payload["error"]


def test_get_config_includes_recorded_key_bootstrap_issues(_offline_config):
    from harness.keys import record_key_bootstrap_issue, get_key_bootstrap_issues

    # Isolate from other tests that may have recorded issues.
    get_key_bootstrap_issues()
    from harness import keys as K
    K._KEY_BOOTSTRAP_ISSUES.clear()
    record_key_bootstrap_issue("migrate_legacy", OSError("no space"))
    svc, _, _, _ = _svc()
    code, payload = get_config(svc)
    assert code == 200
    assert payload["key_bootstrap_issues"] == [
        {"step": "migrate_legacy", "message": "OSError: no space"}
    ]
    K._KEY_BOOTSTRAP_ISSUES.clear()
