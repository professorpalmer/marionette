from __future__ import annotations

import json

from harness import hooks as hk
from harness.workspace_hook_trust import (
    STALE_DIGEST,
    TRUSTED,
    evaluate_hook_trust,
    hook_command_digest,
    remember_hooks,
)


def test_save_hooks_trusts_current_digest(tmp_path, monkeypatch):
    hooks_path = str(tmp_path / "hooks.json")
    monkeypatch.setattr(hk, "_HOOKS_JSON", hooks_path)
    hook = {
        "id": "h-trust",
        "event": "preRun",
        "command": ["echo", "ok"],
        "enabled": True,
    }
    hk.save_hooks([hook])
    assert evaluate_hook_trust(hook, hooks_json=hooks_path) == TRUSTED


def test_tampered_hook_command_is_stale_digest(tmp_path, monkeypatch):
    hooks_path = str(tmp_path / "hooks.json")
    monkeypatch.setattr(hk, "_HOOKS_JSON", hooks_path)
    hook = {
        "id": "h-trust",
        "event": "preRun",
        "command": ["echo", "ok"],
        "enabled": True,
    }
    hk.save_hooks([hook])
    tampered = dict(hook)
    tampered["command"] = ["rm", "-rf", "/"]
    assert hook_command_digest(tampered) != hook_command_digest(hook)
    assert evaluate_hook_trust(tampered, hooks_json=hooks_path) == STALE_DIGEST


def test_run_hooks_skips_stale_digest(tmp_path, monkeypatch):
    hooks_path = str(tmp_path / "hooks.json")
    monkeypatch.setattr(hk, "_HOOKS_JSON", hooks_path)
    hook = {
        "id": "h-trust",
        "event": "preRun",
        "command": ["echo", "ok"],
        "enabled": True,
    }
    hk.save_hooks([hook])
    payload = json.loads((tmp_path / "hooks.json").read_text(encoding="utf-8"))
    payload["hooks"][0]["command"] = ["echo", "pwned"]
    (tmp_path / "hooks.json").write_text(json.dumps(payload), encoding="utf-8")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(hk.subprocess, "run", fake_run)
    outcomes = hk.run_hooks("preRun", {})
    assert outcomes == [{"id": "h-trust", "status": STALE_DIGEST}]
    assert calls == []


def test_resave_clears_stale_digest(tmp_path, monkeypatch):
    hooks_path = str(tmp_path / "hooks.json")
    monkeypatch.setattr(hk, "_HOOKS_JSON", hooks_path)
    first = {
        "id": "h-trust",
        "event": "preRun",
        "command": ["echo", "ok"],
        "enabled": True,
    }
    hk.save_hooks([first])
    second = dict(first)
    second["command"] = ["echo", "next"]
    remember_hooks(hooks_path, [second])
    assert evaluate_hook_trust(second, hooks_json=hooks_path) == TRUSTED
