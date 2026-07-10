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


# --- interpreter validation (Windows Store alias stubs, dead pythons) ---


def test_windowsapps_python_is_rejected_without_running_it(monkeypatch):
    def forbid_run(*args, **kwargs):
        raise AssertionError("a WindowsApps stub must be rejected by path, not executed")

    monkeypatch.setattr(wiki_backend.subprocess, "run", forbid_run)
    stub = r"C:\Users\x\AppData\Local\Microsoft\WindowsApps\python.exe"
    assert wiki_backend._python_is_usable(stub) is False


def test_python_failing_version_check_is_rejected(monkeypatch):
    monkeypatch.setattr(
        wiki_backend.subprocess, "run",
        lambda *args, **kwargs: MagicMock(returncode=9009))
    assert wiki_backend._python_is_usable("/usr/bin/python-stub") is False


def test_python_passing_version_check_is_accepted(monkeypatch):
    monkeypatch.setattr(
        wiki_backend.subprocess, "run",
        lambda *args, **kwargs: MagicMock(returncode=0))
    assert wiki_backend._python_is_usable("/usr/bin/python3") is True


def test_uvicorn_cmd_refuses_windowsapps_stub_from_path(tmp_path, monkeypatch):
    backend = str(tmp_path / "backend")
    os.makedirs(backend)
    monkeypatch.setattr(wiki_backend, "_uvicorn_importable", lambda: False)
    monkeypatch.setattr(
        wiki_backend.shutil, "which",
        lambda name: r"C:\Users\x\AppData\Local\Microsoft\WindowsApps\python.exe")
    assert wiki_backend._uvicorn_cmd(backend, 8000) is None


def test_uvicorn_cmd_prefers_current_interpreter_when_uvicorn_importable(tmp_path, monkeypatch):
    backend = str(tmp_path / "backend")
    os.makedirs(backend)
    monkeypatch.setattr(wiki_backend, "_uvicorn_importable", lambda: True)
    cmd = wiki_backend._uvicorn_cmd(backend, 8123)
    assert cmd[:3] == [wiki_backend.sys.executable, "-m", "uvicorn"]
    assert cmd[-1] == "8123"


def test_ensure_reports_no_usable_python(isolated_state, monkeypatch):
    home, _ = isolated_state
    backend = _fake_backend(str(home), with_uvicorn=False)
    monkeypatch.setenv("MARIONETTE_WIKI_DIR", backend)
    monkeypatch.delenv("MARIONETTE_NO_WIKI", raising=False)
    monkeypatch.setattr(wiki_backend, "_healthz", lambda *args, **kwargs: False)
    monkeypatch.setattr(wiki_backend, "_uvicorn_importable", lambda: False)
    monkeypatch.setattr(wiki_backend.shutil, "which", lambda name: None)
    monkeypatch.setattr(wiki_backend, "_venv_repair_attempted", True)

    result = wiki_backend.ensure_wiki_backend_running(wait_secs=0.1)

    assert result["started"] is False
    assert result["reason"] == "no usable python for uvicorn"


# --- venv repair (missing/broken venv on an existing checkout) ---


def test_missing_venv_triggers_repair_at_most_once(isolated_state, monkeypatch):
    home, _ = isolated_state
    backend = _fake_backend(str(home), with_uvicorn=False)
    monkeypatch.setenv("MARIONETTE_WIKI_DIR", backend)
    monkeypatch.delenv("MARIONETTE_NO_WIKI", raising=False)
    monkeypatch.setattr(wiki_backend, "_healthz", lambda *args, **kwargs: False)
    monkeypatch.setattr(wiki_backend, "_uvicorn_importable", lambda: False)
    monkeypatch.setattr(wiki_backend.shutil, "which", lambda name: None)
    wiki_backend._venv_repair_attempted = False
    repairs = []
    monkeypatch.setattr(
        wiki_backend, "_ensure_backend_venv",
        lambda backend_dir, log: repairs.append(backend_dir))

    wiki_backend.ensure_wiki_backend_running(wait_secs=0.1)
    wiki_backend.ensure_wiki_backend_running(wait_secs=0.1)

    assert repairs == [backend]  # second call is throttled


def test_venv_repair_builds_with_validated_base_python(tmp_path, monkeypatch):
    backend = str(tmp_path / "backend")
    os.makedirs(backend)
    with open(os.path.join(backend, "requirements.txt"), "w") as f:
        f.write("fastapi\n")
    run_calls = []
    monkeypatch.setattr(
        wiki_backend, "_run",
        lambda cmd, cwd, log: run_calls.append(cmd) or True)
    monkeypatch.setattr(wiki_backend.shutil, "which", lambda name: None)  # no uv
    monkeypatch.setattr(wiki_backend, "_python_is_usable", lambda exe: True)

    wiki_backend._ensure_backend_venv(backend, log=open(os.devnull, "ab"))

    assert run_calls[0][:3] == [wiki_backend.sys.executable, "-m", "venv"]


# --- delayed background retry after a failed spawn/health-wait ---


def test_failed_startup_schedules_retry(isolated_state, monkeypatch):
    home, _ = isolated_state
    backend = _fake_backend(str(home), with_uvicorn=True)
    monkeypatch.setenv("MARIONETTE_WIKI_DIR", backend)
    monkeypatch.delenv("MARIONETTE_NO_WIKI", raising=False)
    monkeypatch.setattr(wiki_backend, "_healthz", lambda *args, **kwargs: False)
    monkeypatch.setattr(wiki_backend, "_venv_repair_attempted", True)
    dead_proc = MagicMock()
    dead_proc.poll.return_value = 1
    monkeypatch.setattr(wiki_backend, "_spawn", lambda *args, **kwargs: dead_proc)
    scheduled = []
    monkeypatch.setattr(wiki_backend, "_schedule_retry", lambda log: scheduled.append(1))

    result = wiki_backend.ensure_wiki_backend_running(wait_secs=1.0)

    assert result["reason"] == "backend exited during startup"
    assert scheduled == [1]


def test_retry_throttle_caps_attempts(monkeypatch):
    created_timers = []

    class FakeTimer:
        def __init__(self, interval, fn):
            self.interval = interval
            self.fn = fn
            self.daemon = False
            created_timers.append(self)

        def start(self):
            pass

        def is_alive(self):
            return False  # pretend each prior retry already fired

    monkeypatch.setattr(wiki_backend.threading, "Timer", FakeTimer)
    monkeypatch.setattr(wiki_backend, "_retry_count", 0)
    monkeypatch.setattr(wiki_backend, "_retry_timer", None)

    for _ in range(10):
        wiki_backend._schedule_retry(log=open(os.devnull, "ab"))

    assert len(created_timers) == wiki_backend._MAX_RETRIES
    assert all(t.daemon for t in created_timers)
    assert all(t.interval == 30.0 for t in created_timers)


def test_pending_retry_timer_is_not_duplicated(monkeypatch):
    created_timers = []

    class FakeTimer:
        def __init__(self, interval, fn):
            self.daemon = False
            created_timers.append(self)

        def start(self):
            pass

        def is_alive(self):
            return True  # a retry is already pending

    monkeypatch.setattr(wiki_backend.threading, "Timer", FakeTimer)
    monkeypatch.setattr(wiki_backend, "_retry_count", 0)
    monkeypatch.setattr(wiki_backend, "_retry_timer", None)

    for _ in range(5):
        wiki_backend._schedule_retry(log=open(os.devnull, "ab"))

    assert len(created_timers) == 1
