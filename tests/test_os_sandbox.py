"""Hermetic tests for harness.os_sandbox capability, profiles, and spawn plans."""
from __future__ import annotations

import os
import sys

import pytest

from harness import os_sandbox

_SHARED_SYSTEM_TEMPS = ("/tmp", "/var/tmp")


def _norm(path: str) -> str:
    return os.path.normpath(path)


def _assert_not_exact_shared_temp(path: str) -> None:
    normalized = _norm(path)
    shared = {_norm(p) for p in _SHARED_SYSTEM_TEMPS}
    assert normalized not in shared


def _assert_profile_excludes_shared_tmp(profile: str) -> None:
    shared = {_norm(p) for p in _SHARED_SYSTEM_TEMPS}
    for line in profile.splitlines():
        line = line.strip()
        if not line.startswith("(subpath "):
            continue
        quoted = line[len("(subpath ") :].rstrip(")").strip().strip('"')
        assert _norm(quoted) not in shared


def _assert_bwrap_excludes_shared_tmp(argv: list[str]) -> None:
    shared = {_norm(p) for p in _SHARED_SYSTEM_TEMPS}
    idx = 0
    while idx < len(argv):
        if argv[idx] == "--bind" and idx + 2 < len(argv):
            assert _norm(argv[idx + 1]) not in shared
            idx += 3
            continue
        idx += 1


@pytest.fixture(autouse=True)
def _reset_probe_cache():
    os_sandbox.reset_probe_cache()
    yield
    os_sandbox.reset_probe_cache()


def test_resolve_os_sandbox_mode_defaults_off(monkeypatch):
    monkeypatch.delenv("HARNESS_OS_SANDBOX", raising=False)
    assert os_sandbox.resolve_os_sandbox_mode() == "off"


@pytest.mark.parametrize("value", ["auto", "required", "OFF", " Auto "])
def test_resolve_os_sandbox_mode_valid_values(monkeypatch, value):
    monkeypatch.setenv("HARNESS_OS_SANDBOX", value)
    assert os_sandbox.resolve_os_sandbox_mode() == value.strip().lower()


def test_resolve_os_sandbox_mode_invalid_falls_back_to_off(monkeypatch):
    monkeypatch.setenv("HARNESS_OS_SANDBOX", "maybe")
    assert os_sandbox.resolve_os_sandbox_mode() == "off"


def test_build_seatbelt_profile_confines_writes():
    paths = ["/tmp/work", "/var/tmp"]
    profile = os_sandbox.build_seatbelt_profile(paths)
    assert "(deny default)" in profile
    assert "(allow file-read*)" in profile
    for path in paths:
        canonical = os.path.normcase(os.path.realpath(os.path.abspath(path)))
        assert f'(subpath "{canonical}")' in profile
    assert "(allow process*)" in profile


def test_build_bwrap_argv_includes_bind_and_sh_c(monkeypatch):
    monkeypatch.setattr(os_sandbox.shutil, "which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)
    argv = os_sandbox.build_bwrap_argv(["/repo"], "echo hi")
    assert argv[0] == "/usr/bin/bwrap"
    assert "--ro-bind" in argv
    assert "--bind" in argv
    bind_idx = argv.index("--bind")
    assert argv[bind_idx + 1] == "/repo"
    assert argv[bind_idx + 2] == "/repo"
    sh_idx = argv.index("sh")
    assert argv[sh_idx : sh_idx + 3] == ["sh", "-c", "echo hi"]


def test_prepare_sandbox_spawn_off_returns_none(monkeypatch):
    monkeypatch.setenv("HARNESS_OS_SANDBOX", "off")
    assert os_sandbox.prepare_sandbox_spawn("echo hi") is None


def test_prepare_sandbox_spawn_auto_unavailable_returns_none(monkeypatch):
    monkeypatch.setenv("HARNESS_OS_SANDBOX", "auto")
    monkeypatch.setattr(
        os_sandbox,
        "detect_sandbox_capability",
        lambda *a, **k: os_sandbox.SandboxCapability("test", None, False),
    )
    assert os_sandbox.prepare_sandbox_spawn("echo hi") is None


def test_prepare_sandbox_spawn_required_unavailable_raises(monkeypatch):
    monkeypatch.setenv("HARNESS_OS_SANDBOX", "required")
    monkeypatch.setattr(
        os_sandbox,
        "detect_sandbox_capability",
        lambda *a, **k: os_sandbox.SandboxCapability("test", None, False),
    )
    with pytest.raises(os_sandbox.SandboxRequiredUnavailable) as exc:
        os_sandbox.prepare_sandbox_spawn("echo hi")
    assert "required" in exc.value.message.lower()


def test_prepare_sandbox_spawn_macos_wraps_command(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_OS_SANDBOX", "auto")
    monkeypatch.setattr(
        os_sandbox,
        "detect_sandbox_capability",
        lambda *a, **k: os_sandbox.SandboxCapability("macos", "sandbox-exec", True),
    )
    monkeypatch.setattr(os_sandbox.shutil, "which", lambda name: "/usr/bin/sandbox-exec" if name == "sandbox-exec" else None)

    plan = os_sandbox.prepare_sandbox_spawn("echo hello", cwd=str(tmp_path))
    assert plan is not None
    assert "sandbox-exec" in plan.command
    assert "sh -c" in plan.command or "sh" in plan.command
    assert "echo hello" in plan.command
    assert plan.cleanup is not None
    plan.cleanup()


def test_prepare_sandbox_spawn_linux_wraps_command(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_OS_SANDBOX", "auto")
    monkeypatch.setattr(
        os_sandbox,
        "detect_sandbox_capability",
        lambda *a, **k: os_sandbox.SandboxCapability(
            "linux", "bubblewrap", True, landlock_capable=True
        ),
    )
    monkeypatch.setattr(os_sandbox.shutil, "which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)

    plan = os_sandbox.prepare_sandbox_spawn("echo hello", cwd=str(tmp_path))
    assert plan is not None
    assert "bwrap" in plan.command
    assert "echo hello" in plan.command


def test_writable_paths_include_cwd_and_private_temp_only(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    private = str(tmp_path / "private-temp")
    os.makedirs(private, exist_ok=True)
    paths = os_sandbox._writable_paths(
        str(tmp_path / "repo"),
        os.environ,
        private_temp=private,
    )
    assert os.path.abspath(str(tmp_path / "repo")) in paths
    assert os.path.abspath(private) in paths
    assert os.path.abspath(str(tmp_path / "state")) not in paths
    for path in paths:
        _assert_not_exact_shared_temp(path)


def test_sibling_state_files_denied_by_seatbelt_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    os.makedirs(tmp_path / "state", exist_ok=True)
    sibling = tmp_path / "state" / "sessions.db"
    sibling.write_text("secret\n", encoding="utf-8")
    private = str(tmp_path / "cmd-private")
    os.makedirs(private, exist_ok=True)
    writable = os_sandbox._writable_paths(
        str(tmp_path / "repo"),
        os.environ,
        private_temp=private,
    )
    profile = os_sandbox.build_seatbelt_profile(writable)
    assert str(sibling) not in profile
    assert str(tmp_path / "state") not in profile


def test_sibling_state_files_denied_by_bwrap_argv(monkeypatch, tmp_path):
    monkeypatch.setattr(os_sandbox.shutil, "which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    private = str(tmp_path / "cmd-private")
    writable = os_sandbox._writable_paths(
        str(tmp_path / "repo"),
        os.environ,
        private_temp=private,
    )
    argv = os_sandbox.build_bwrap_argv(writable, "echo hi")
    joined = " ".join(argv)
    assert str(tmp_path / "state") not in joined
    assert private in joined


def test_prepare_sandbox_spawn_sets_private_temp_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_OS_SANDBOX", "auto")
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        os_sandbox,
        "detect_sandbox_capability",
        lambda *a, **k: os_sandbox.SandboxCapability("macos", "sandbox-exec", True),
    )
    monkeypatch.setattr(os_sandbox.shutil, "which", lambda name: "/usr/bin/sandbox-exec" if name == "sandbox-exec" else None)

    plan = os_sandbox.prepare_sandbox_spawn("echo hello", cwd=str(tmp_path / "repo"))
    assert plan is not None
    assert plan.child_env is not None
    private = plan.child_env["TMPDIR"]
    assert private.startswith(str(tmp_path / "state"))
    assert os.path.isdir(private)
    _assert_not_exact_shared_temp(private)
    assert plan.cleanup is not None
    plan.cleanup()
    assert not os.path.isdir(private)


def test_active_profile_excludes_shared_tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    private = str(tmp_path / "cmd-private")
    os.makedirs(private, exist_ok=True)
    writable = os_sandbox._writable_paths(
        str(tmp_path / "repo"),
        os.environ,
        private_temp=private,
    )
    profile = os_sandbox.build_seatbelt_profile(writable)
    _assert_profile_excludes_shared_tmp(profile)
    assert private.replace("\\", "\\\\") in profile or private in profile


def test_bwrap_argv_excludes_shared_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr(os_sandbox.shutil, "which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)
    private = str(tmp_path / "cmd-private")
    argv = os_sandbox.build_bwrap_argv(
        [str(tmp_path / "repo"), private],
        "echo hi",
    )
    _assert_bwrap_excludes_shared_tmp(argv)
    assert private in argv


def test_detect_sandbox_capability_windows_is_unavailable(monkeypatch):
    monkeypatch.setattr(os_sandbox.sys, "platform", "win32")
    cap = os_sandbox.detect_sandbox_capability(force_refresh=True)
    assert cap.platform == "windows"
    assert cap.available is False
    assert cap.backend is None


def test_landlock_reported_on_linux(monkeypatch):
    monkeypatch.setattr(os_sandbox.sys, "platform", "linux")
    monkeypatch.setattr(os_sandbox, "_probe_linux_bubblewrap", lambda: False)
    monkeypatch.setattr(os_sandbox, "_landlock_capable", lambda: True)
    cap = os_sandbox.detect_sandbox_capability(force_refresh=True)
    assert cap.landlock_capable is True
    assert cap.available is False


@pytest.mark.skipif(
    sys.platform != "darwin" or not os_sandbox._probe_macos_sandbox_exec(),
    reason="macOS sandbox-exec not available on this host",
)
def test_macos_integration_echo():
    os_sandbox.reset_probe_cache()
    prev = os.environ.get("HARNESS_OS_SANDBOX")
    os.environ["HARNESS_OS_SANDBOX"] = "auto"
    plan = None
    try:
        plan = os_sandbox.prepare_sandbox_spawn("echo sandbox_ok")
        assert plan is not None
        import subprocess

        result = subprocess.run(
            plan.command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert "sandbox_ok" in result.stdout
    finally:
        if prev is None:
            os.environ.pop("HARNESS_OS_SANDBOX", None)
        else:
            os.environ["HARNESS_OS_SANDBOX"] = prev
        if plan is not None and plan.cleanup:
            plan.cleanup()


@pytest.mark.skipif(
    sys.platform != "linux" or not os_sandbox._probe_linux_bubblewrap(),
    reason="Linux bubblewrap not available on this host",
)
def test_linux_integration_echo():
    os_sandbox.reset_probe_cache()
    prev = os.environ.get("HARNESS_OS_SANDBOX")
    os.environ["HARNESS_OS_SANDBOX"] = "auto"
    plan = None
    try:
        plan = os_sandbox.prepare_sandbox_spawn("echo sandbox_ok")
        assert plan is not None
        import subprocess

        result = subprocess.run(
            plan.command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert "sandbox_ok" in result.stdout
    finally:
        if prev is None:
            os.environ.pop("HARNESS_OS_SANDBOX", None)
        else:
            os.environ["HARNESS_OS_SANDBOX"] = prev
        if plan is not None and plan.cleanup:
            plan.cleanup()
