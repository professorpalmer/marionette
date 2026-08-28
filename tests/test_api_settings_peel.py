"""Characterization tests for settings/config API peel."""
from __future__ import annotations

import threading
from types import SimpleNamespace

from harness.api.settings import (
    SettingsServices,
    get_config,
    get_settings,
    post_settings,
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


def test_get_config_and_settings(monkeypatch):
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
    assert get_settings(svc)[1]["budget"] == 3


def test_get_config_includes_dynamic_model_labels(monkeypatch):
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


def test_post_settings_budget_and_flags(monkeypatch):
    monkeypatch.setattr(
        "harness.auto_registry.sync_agentic_registry_safe", lambda: None
    )
    svc, cfg, pilot, calls = _svc()
    code, payload = post_settings(
        {
            "budget": 10,
            "auto_distill": True,
            "commandTimeout": "off",
            "maxPilotSteps": "unlimited",
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
    assert env["HARNESS_PILOT_TOOL_BUDGET"] == "50"
    assert env["HARNESS_WORKER_TOKEN_BUDGET"] == "50000"


def test_post_settings_pilot_tool_budget_unlimited(monkeypatch):
    monkeypatch.setattr(
        "harness.auto_registry.sync_agentic_registry_safe", lambda: None
    )
    svc, _, _, calls = _svc()
    code, _ = post_settings({"pilotToolBudget": "unlimited"}, svc)
    assert code == 200
    assert dict(calls["persist"])["HARNESS_PILOT_TOOL_BUDGET"] == "0"


def test_post_settings_bad_pilot_tool_budget():
    svc, _, _, _ = _svc()
    assert post_settings({"pilotToolBudget": "nope"}, svc)[0] == 400


def test_post_settings_negative_pilot_tool_budget():
    svc, _, _, _ = _svc()
    assert post_settings({"pilotToolBudget": "-1"}, svc)[0] == 400


def test_post_settings_bad_worker_token_budget():
    svc, _, _, _ = _svc()
    assert post_settings({"workerTokenBudget": "nope"}, svc)[0] == 400


def test_post_settings_compaction_residual_hybrid(monkeypatch):
    monkeypatch.setattr(
        "harness.auto_registry.sync_agentic_registry_safe", lambda: None
    )
    svc, _, _, calls = _svc()
    code, _ = post_settings({"compactionResidual": "hybrid"}, svc)
    assert code == 200
    assert dict(calls["persist"])["HARNESS_COMPACTION_RESIDUAL"] == "hybrid"


def test_post_settings_compaction_residual_catalog():
    svc, _, _, calls = _svc()
    code, _ = post_settings({"compactionResidual": "catalog"}, svc)
    assert code == 200
    assert dict(calls["persist"])["HARNESS_COMPACTION_RESIDUAL"] == "catalog"


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


def test_post_settings_bad_budget():
    svc, _, _, _ = _svc()
    assert post_settings({"budget": "x"}, svc)[0] == 400


def test_post_settings_busy_on_key(monkeypatch):
    monkeypatch.setattr(
        "harness.auto_registry.sync_agentic_registry_safe", lambda: None
    )
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



def test_get_config_includes_recorded_key_bootstrap_issues(monkeypatch):
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
