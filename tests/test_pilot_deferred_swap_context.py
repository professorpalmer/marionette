"""Deferred pilot swap must fail closed when OpenRouter metadata is unknown."""
import pytest


def test_deferred_swap_preserves_driver_on_unknown_openrouter_context(monkeypatch):
    from harness.api.pilot import PilotServices, swap_pilot

    class BusyLock:
        def locked(self):
            return True

    class BusyPilot:
        _busy = BusyLock()
        config = type("Cfg", (), {"driver": "openrouter:anthropic/claude-sonnet-4"})()

    cfg = type("Cfg", (), {"driver": "openrouter:anthropic/claude-sonnet-4", "repo": "/tmp"})()
    calls = {"apply": 0, "saved": None}

    def apply_context():
        calls["apply"] += 1
        if cfg.driver == "deepseek/deepseek-v4-unknown":
            raise ValueError("OpenRouter metadata unavailable for 'deepseek/deepseek-v4-unknown'")

    def save_workspace_driver(repo, model):
        calls["saved"] = model

    svc = PilotServices(
        cfg=cfg,
        get_pilot=lambda: BusyPilot(),
        apply_model_context_window=apply_context,
        save_workspace_driver=save_workspace_driver,
        perform_pilot_swap=lambda model: None,
    )
    status, body = swap_pilot("deepseek/deepseek-v4-unknown", svc)
    assert status == 500
    assert "metadata unavailable" in body["error"]
    assert cfg.driver == "openrouter:anthropic/claude-sonnet-4"
    assert calls["saved"] is None
