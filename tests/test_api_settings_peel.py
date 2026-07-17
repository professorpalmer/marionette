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
        "harness.edit_engines.agentic_available", lambda: False, raising=False
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
    assert get_settings(svc)[1]["budget"] == 3


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
        },
        svc,
    )
    assert code == 200
    assert cfg.budget == 10
    assert pilot._auto_distill is True
    env = dict(calls["persist"])
    assert env["HARNESS_COMMAND_TIMEOUT"] == "0"
    assert env["HARNESS_MAX_PILOT_STEPS"] == "0"


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
