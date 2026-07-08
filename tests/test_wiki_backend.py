"""Tests for the local wiki backend auto-provision/auto-start (harness.wiki_backend).

These avoid the network by faking an already-cloned checkout under a temp
MARIONETTE_WIKI_HOME and a dummy venv uvicorn, so we exercise the provisioning
side effects (backend/.env generation, owner-token registration) and the
OS-aware helpers without cloning or installing anything.
"""
from __future__ import annotations

import os
import subprocess
from unittest.mock import MagicMock

import pytest

from harness import wiki_backend
from harness import wiki_config


def _fake_backend(home: str, *, with_uvicorn: bool = True, env_body: str | None = None) -> str:
    backend = os.path.join(home, "backend")
    os.makedirs(os.path.join(backend, "app"), exist_ok=True)
    with open(os.path.join(backend, "app", "main.py"), "w") as f:
        f.write("app = object()\n")
    if with_uvicorn:
        venv_bin = os.path.dirname(wiki_backend._venv_bin(os.path.join(backend, ".venv"), "uvicorn"))
        os.makedirs(venv_bin, exist_ok=True)
        open(wiki_backend._venv_bin(os.path.join(backend, ".venv"), "uvicorn"), "w").close()
    if env_body is not None:
        with open(os.path.join(backend, ".env"), "w") as f:
            f.write(env_body)
    # a bundled demo dir so WIKI_ROOT points somewhere real
    os.makedirs(os.path.join(home, "wiki-demo"), exist_ok=True)
    return backend


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    home = tmp_path / "wiki"
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("MARIONETTE_WIKI_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    monkeypatch.delenv("MARIONETTE_WIKI_DIR", raising=False)
    # Tracked by monkeypatch so set_wiki_config's direct os.environ writes are
    # restored on teardown and don't leak into other tests.
    monkeypatch.setenv("WIKI_API_BASE", "")
    monkeypatch.setenv("WIKI_OWNER_TOKEN", "")
    return home, state


def test_provision_generates_env_and_registers_owner_token(isolated_state, monkeypatch):
    home, _ = isolated_state
    _fake_backend(str(home))  # pretend the repo is already cloned + venv set up

    backend_dir = wiki_backend._provision_wiki(log=open(os.devnull, "ab"))

    assert backend_dir == os.path.join(str(home), "backend")
    env_file = os.path.join(backend_dir, ".env")
    assert os.path.isfile(env_file)
    token = wiki_backend._read_env_token(env_file)
    assert len(token) == 64  # secrets.token_hex(32)
    # The generated token is registered so the client authenticates as owner.
    cfg = wiki_config.get_wiki_config()
    assert cfg["has_token"] is True
    assert cfg["api_base"] == wiki_backend._DEFAULT_BASE
    assert os.environ.get("WIKI_OWNER_TOKEN") == token


def test_provision_reuses_existing_owner_token(isolated_state):
    home, _ = isolated_state
    existing = "a" * 64
    _fake_backend(str(home), env_body=f"WIKI_ROOT=/x\nOWNER_TOKEN={existing}\n")

    backend_dir = wiki_backend._provision_wiki(log=open(os.devnull, "ab"))

    assert wiki_backend._read_env_token(os.path.join(backend_dir, ".env")) == existing


def test_opt_out_skips_everything(monkeypatch):
    monkeypatch.setenv("MARIONETTE_NO_WIKI", "1")
    result = wiki_backend.ensure_wiki_backend_running(wait_secs=0.1)
    assert result["started"] is False
    assert "opted out" in result["reason"]


def test_remote_wiki_is_not_auto_started(monkeypatch):
    monkeypatch.delenv("MARIONETTE_NO_WIKI", raising=False)
    monkeypatch.setenv("WIKI_API_BASE", "https://wiki.example.com")
    result = wiki_backend.ensure_wiki_backend_running(wait_secs=0.1)
    assert result["started"] is False
    assert "remote" in result["reason"]


def test_venv_bin_is_os_aware(monkeypatch):
    monkeypatch.setattr(wiki_backend, "_IS_WINDOWS", False)
    assert wiki_backend._venv_bin("/v", "uvicorn").endswith(os.path.join("bin", "uvicorn"))
    monkeypatch.setattr(wiki_backend, "_IS_WINDOWS", True)
    assert wiki_backend._venv_bin("/v", "uvicorn").endswith(os.path.join("Scripts", "uvicorn.exe"))


def test_find_existing_backend_dir_prefers_override(isolated_state, tmp_path, monkeypatch):
    override = tmp_path / "custom"
    os.makedirs(os.path.join(str(override), "app"))
    open(os.path.join(str(override), "app", "main.py"), "w").close()
    monkeypatch.setenv("MARIONETTE_WIKI_DIR", str(override))
    assert wiki_backend._find_existing_backend_dir() == str(override)


def test_spawn_uses_platform_process_group_kwargs(monkeypatch):
    popen_calls = []

    def fake_popen(cmd, **kwargs):
        popen_calls.append((cmd, kwargs))
        return MagicMock()

    monkeypatch.setattr(wiki_backend.subprocess, "Popen", fake_popen)
    log = open(os.devnull, "ab")
    wiki_backend._spawn(["uvicorn"], os.getcwd(), log)
    assert popen_calls
    _, kwargs = popen_calls[0]
    if os.name == "posix":
        assert kwargs.get("start_new_session") is True
        assert "creationflags" not in kwargs
    else:
        assert "start_new_session" not in kwargs
        assert kwargs["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
