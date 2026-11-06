"""Hermetic tests for harness.os_sandbox capability, profiles, and spawn plans."""
from __future__ import annotations

import errno
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys

import pytest

from harness import os_sandbox

_SHARED_SYSTEM_TEMPS = ("/tmp", "/var/tmp")


def _norm(path: str) -> str:
    return os.path.normpath(path)


def _canon(path: str) -> str:
    return os_sandbox._canonical_path(path)


def _seatbelt_subpath(path: str) -> str:
    escaped = _canon(path).replace("\\", "\\\\").replace('"', '\\"')
    return f'(subpath "{escaped}")'


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
        assert _seatbelt_subpath(path) in profile
    assert "(allow process*)" in profile


@pytest.mark.parametrize("network", ["allow", "deny"])
def test_seatbelt_network_policy_is_explicit(network):
    profile = os_sandbox.build_seatbelt_profile(["/repo"], network=network)
    assert f"({network} network*)" in profile
    assert "(deny default)" in profile
    assert "(allow default)" not in profile


@pytest.mark.parametrize(
    "mode,network,expected",
    [("required", None, "deny"), ("auto", None, "allow"),
     ("required", "allow", "allow"), ("auto", "deny", "deny")],
)
def test_child_network_policy(mode, network, expected):
    env = {"HARNESS_OS_SANDBOX": mode}
    if network is not None:
        env["HARNESS_OS_SANDBOX_NETWORK"] = network
    assert os_sandbox.resolve_child_network_policy(env) == expected


@pytest.mark.parametrize("network", ["allow", "deny"])
def test_bwrap_network_namespace_policy(monkeypatch, network):
    monkeypatch.setattr(os_sandbox.shutil, "which", lambda name: "/usr/bin/bwrap")
    argv = os_sandbox.build_bwrap_argv(["/repo"], "true", network=network)
    assert ("--unshare-net" in argv) == (network == "deny")


def test_build_bwrap_argv_includes_bind_and_sh_c(monkeypatch):
    monkeypatch.setattr(os_sandbox.shutil, "which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)
    argv = os_sandbox.build_bwrap_argv(["/repo"], "echo hi")
    assert argv[0] == "/usr/bin/bwrap"
    assert "--ro-bind" in argv
    assert "--bind" in argv
    bind_idx = argv.index("--bind")
    assert argv[bind_idx + 1] == _canon("/repo")
    assert argv[bind_idx + 2] == _canon("/repo")
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
    assert _canon(str(tmp_path / "repo")) in paths
    assert _canon(private) in paths
    assert _canon(str(tmp_path / "state")) not in paths
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
    assert _seatbelt_subpath(str(sibling)) not in profile
    assert _seatbelt_subpath(str(tmp_path / "state")) not in profile


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
    assert _canon(str(tmp_path / "state")) not in joined
    assert _canon(private) in argv


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
    assert _canon(private).startswith(_canon(str(tmp_path / "state")))
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
    assert _seatbelt_subpath(private) in profile


def test_bwrap_argv_excludes_shared_tmp(monkeypatch, tmp_path):
    monkeypatch.setattr(os_sandbox.shutil, "which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else None)
    private = str(tmp_path / "cmd-private")
    argv = os_sandbox.build_bwrap_argv(
        [str(tmp_path / "repo"), private],
        "echo hi",
    )
    _assert_bwrap_excludes_shared_tmp(argv)
    assert _canon(private) in argv


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


@pytest.mark.parametrize(
    "platform",
    [
        pytest.param(
            "darwin",
            id="macos",
            marks=pytest.mark.skipif(
                sys.platform != "darwin" or not os_sandbox._probe_macos_sandbox_exec(),
                reason="macOS sandbox-exec not available on this host",
            ),
        ),
        pytest.param(
            "linux",
            id="linux",
            marks=pytest.mark.skipif(
                sys.platform != "linux" or not os_sandbox._probe_linux_bubblewrap(),
                reason="Linux bubblewrap not available on this host",
            ),
        ),
    ],
)
def test_integration_echo(monkeypatch, platform):
    os_sandbox.reset_probe_cache()
    monkeypatch.setenv("HARNESS_OS_SANDBOX", "auto")
    plan = os_sandbox.prepare_sandbox_spawn("echo sandbox_ok")
    assert plan is not None
    try:
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
        if plan.cleanup:
            plan.cleanup()


@pytest.fixture
def seatbelt_backend():
    if sys.platform != "darwin":
        pytest.skip("Seatbelt runtime checks require macOS")
    exe = shutil.which("sandbox-exec")
    if exe is None:
        pytest.skip("sandbox-exec is not installed")
    probe = subprocess.run(
        [exe, "-p", "(version 1)\n(allow default)", "/usr/bin/true"],
        capture_output=True, text=True, timeout=10,
    )
    if probe.returncode:
        pytest.skip(
            "Seatbelt backend cannot start (nested sandbox may prohibit sandbox_apply): "
            f"exit={probe.returncode}, stderr={probe.stderr.strip()}"
        )
    os_sandbox.reset_probe_cache()
    yield
    os_sandbox.reset_probe_cache()


_CHILD = """
import json, os, pathlib, socket, sys
port, denied_path = int(sys.argv[1]), sys.argv[2]
result = {}
try:
    with socket.create_connection(('127.0.0.1', port), timeout=3) as connection:
        connection.sendall(b'sandbox-policy')
    result['network'] = 'connected'
except OSError as exc:
    result['network'] = exc.errno
for name, path in [('cwd', pathlib.Path('allowed.txt')),
                   ('temp', pathlib.Path(os.environ['TMPDIR']) / 'allowed.txt'),
                   ('denied', pathlib.Path(denied_path))]:
    try:
        path.write_text('child write', encoding='utf-8')
        result[name] = 'written'
    except OSError as exc:
        result[name] = exc.errno
print(json.dumps(result))
"""


@pytest.mark.parametrize("path_name", ["repo", 'repo\\\" ) (allow default) ;\nquoted'], ids=["plain", "quoted"])
@pytest.mark.parametrize(
    "mode,network,allowed",
    [("required", "allow", True), ("required", "deny", False),
     ("required", None, False), ("auto", None, True)],
    ids=["explicit-allow", "explicit-deny", "required-default", "auto-default"],
)
def test_macos_runtime_policy(seatbelt_backend, tmp_path, path_name, mode, network, allowed):
    cwd = tmp_path / path_name
    cwd.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    denied = state / "sessions.db"
    denied.write_text("unchanged", encoding="utf-8")
    env = dict(os.environ, HARNESS_OS_SANDBOX=mode, HARNESS_STATE_DIR=str(state))
    env.pop("HARNESS_OS_SANDBOX_NETWORK", None)
    if network is not None:
        env["HARNESS_OS_SANDBOX_NETWORK"] = network

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        server.settimeout(3)
        port = server.getsockname()[1]
        # Establish that the same child interpreter can reach this owned listener.
        baseline = subprocess.run(
            [sys.executable, "-c", "import socket,sys; socket.create_connection(('127.0.0.1', int(sys.argv[1])), timeout=3).close()", str(port)],
            capture_output=True, text=True, timeout=10,
        )
        assert baseline.returncode == 0, baseline.stderr
        connection, _ = server.accept()
        connection.close()

        command = shlex.join([sys.executable, "-c", _CHILD, str(port), str(denied)])
        plan = os_sandbox.prepare_sandbox_spawn(command, cwd=str(cwd), env=env)
        assert plan is not None
        try:
            result = subprocess.run(
                plan.command, shell=True, cwd=cwd,
                env=dict(env, **(plan.child_env or {})),
                capture_output=True, text=True, timeout=15,
            )
            assert result.returncode == 0, result.stderr
            observed = json.loads(result.stdout)
            assert observed["cwd"] == "written", observed
            assert observed["temp"] == "written", observed
            assert observed["denied"] in (errno.EPERM, errno.EACCES), observed
            assert denied.read_text(encoding="utf-8") == "unchanged"
            if allowed:
                assert observed["network"] == "connected", observed
                connection, _ = server.accept()
                with connection:
                    connection.settimeout(3)
                    assert connection.recv(64) == b"sandbox-policy"
            else:
                assert observed["network"] in (errno.EPERM, errno.EACCES), observed
        finally:
            if plan.cleanup:
                plan.cleanup()
