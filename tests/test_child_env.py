"""Model-driven child processes do not inherit Marionette's credentials."""

from __future__ import annotations

import json
import subprocess
import sys

from harness.child_env import managed_secret_names, model_child_env
from harness.command_policy import run_cancellable


def _child_environ(tmp_path) -> dict:
    cmd = subprocess.list2cmdline([sys.executable, "-c", "import json,os;print(json.dumps(dict(os.environ)))"])
    output, code, status = run_cancellable(cmd, cwd=str(tmp_path), timeout=30)
    assert code == 0, (output, status)
    return json.loads(output.strip().splitlines()[-1])


def test_registry_names_cover_providers_and_pool():
    names = managed_secret_names()
    for name in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_CODEX_TOKEN",
                 "ANTHROPIC_TOKEN", "AWS_SECRET_ACCESS_KEY", "CURSOR_API_KEY"):
        assert name in names


def test_model_child_env_drops_secrets_and_identity_keeps_user_tokens():
    base = {
        "PATH": "/usr/bin",
        "OPENROUTER_API_KEY": "sk-or-test",
        "ANTHROPIC_TOKEN": "oauth-test",
        "LOCAL_MY_BOX_API_KEY": "local-test",
        "CLAUDECODE": "1",
        "CODEX_THREAD_ID": "thread-test",
        "GH_TOKEN": "user-gh",
        "HOME": "/home/u",
    }
    env = model_child_env(base)
    assert env == {"PATH": "/usr/bin", "GH_TOKEN": "user-gh", "HOME": "/home/u"}
    assert base["OPENROUTER_API_KEY"] == "sk-or-test"


def test_pass_list_keeps_named_variables():
    env = model_child_env({"OPENAI_API_KEY": "sk-test", "HARNESS_CHILD_ENV_PASS": " OPENAI_API_KEY ,X"})
    assert env["OPENAI_API_KEY"] == "sk-test"


def test_run_command_child_sees_no_provider_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("GH_TOKEN", "user-gh")
    seen = _child_environ(tmp_path)
    assert "OPENROUTER_API_KEY" not in seen
    assert "ANTHROPIC_API_KEY" not in seen
    assert "CLAUDECODE" not in seen
    assert seen["GH_TOKEN"] == "user-gh"
    assert seen.get("PATH")


def test_harness_process_keeps_its_keys(tmp_path, monkeypatch):
    import os

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    _child_environ(tmp_path)
    assert os.environ["OPENROUTER_API_KEY"] == "sk-or-test"
