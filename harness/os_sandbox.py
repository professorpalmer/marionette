"""Opt-in OS-level sandbox wrapper for shell command execution.

Thin spawn prefix only — confines filesystem *writes* for run_command via
platform sandboxes (macOS sandbox-exec / Linux bubblewrap). Does not sandbox
in-process file editors (hash_edit / write_file). Stdlib-only; Python 3.9+.

Controlled by HARNESS_OS_SANDBOX: off (default) | auto | required.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Callable, Mapping, Optional

_VALID_MODES = frozenset({"off", "auto", "required"})

_PROBE_CACHE: Optional["SandboxCapability"] = None


class SandboxRequiredUnavailable(Exception):
    """Raised when HARNESS_OS_SANDBOX=required but no backend is available."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class SandboxCapability:
    platform: str
    backend: Optional[str]
    available: bool
    landlock_capable: bool = False


@dataclass(frozen=True)
class SandboxSpawnPlan:
    command: str
    cleanup: Optional[Callable[[], None]] = None
    child_env: Optional[dict[str, str]] = None


def resolve_os_sandbox_mode(env: Mapping[str, str] | None = None) -> str:
    """Return off|auto|required. Unset or invalid values default to off."""
    env = env if env is not None else os.environ
    raw = (env.get("HARNESS_OS_SANDBOX") or "off").strip().lower()
    if raw in _VALID_MODES:
        return raw
    return "off"


def reset_probe_cache() -> None:
    """Clear cached capability probe (for hermetic tests)."""
    global _PROBE_CACHE
    _PROBE_CACHE = None


def _canonical_path(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(path))))


def _resolve_state_dir(env: Mapping[str, str]) -> str:
    raw = (env.get("HARNESS_STATE_DIR") or "").strip()
    return _canonical_path(raw or "~/.pmharness/state")


def _sandbox_temp_root(cwd: str | None, env: Mapping[str, str]) -> str:
    """Private per-command temp parent under state or cwd — never shared /tmp."""
    resolved_cwd = _canonical_path(cwd or os.getcwd())
    state_dir = _resolve_state_dir(env)
    for candidate in (state_dir, resolved_cwd):
        try:
            root = os.path.join(candidate, ".harness-sandbox-temp")
            os.makedirs(root, exist_ok=True)
            return root
        except OSError:
            continue
    root = os.path.join(resolved_cwd, ".harness-sandbox-temp")
    os.makedirs(root, exist_ok=True)
    return root


def _make_private_temp_dir(cwd: str | None, env: Mapping[str, str]) -> str:
    root = _sandbox_temp_root(cwd, env)
    return _canonical_path(tempfile.mkdtemp(prefix="cmd-", dir=root))


def _writable_paths(
    cwd: str | None,
    env: Mapping[str, str],
    *,
    private_temp: str,
) -> list[str]:
    canonical_cwd = _canonical_path(cwd or os.getcwd())
    temp_parent = _canonical_path(os.path.dirname(private_temp))
    result: list[str] = []
    for candidate in (cwd or os.getcwd(), private_temp):
        path = _canonical_path(candidate)
        try:
            allowed = (os.path.commonpath([path, canonical_cwd]) == canonical_cwd or
                       os.path.commonpath([path, temp_parent]) == temp_parent)
        except ValueError:
            allowed = False
        if not allowed:
            raise SandboxRequiredUnavailable("OS sandbox writable path resolves outside allowed canonical roots")
        if path not in result:
            result.append(path)
    return result


def resolve_child_network_policy(env: Mapping[str, str] | None = None, *, mode: str | None = None) -> str:
    values = env if env is not None else os.environ
    selected = mode or resolve_os_sandbox_mode(values)
    raw = (values.get("HARNESS_OS_SANDBOX_NETWORK") or "").strip().lower()
    if raw in {"deny", "allow"}:
        return raw
    return "deny" if selected == "required" else "allow"


def _landlock_capable() -> bool:
    if sys.platform != "linux":
        return False
    return os.path.isdir("/sys/kernel/security/landlock")


def _probe_macos_sandbox_exec() -> bool:
    exe = shutil.which("sandbox-exec")
    if not exe:
        return False
    try:
        result = subprocess.run(
            [exe, "-p", "(version 1)\n(allow default)", "true"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _probe_linux_bubblewrap() -> bool:
    exe = shutil.which("bwrap")
    if not exe:
        return False
    try:
        result = subprocess.run(
            [
                exe,
                "--ro-bind",
                "/",
                "/",
                "--dev",
                "/dev",
                "--proc",
                "/proc",
                "--die-with-parent",
                "true",
            ],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def detect_sandbox_capability(
    env: Mapping[str, str] | None = None,
    *,
    force_refresh: bool = False,
) -> SandboxCapability:
    """Detect whether an OS sandbox backend is available on this host."""
    global _PROBE_CACHE
    if _PROBE_CACHE is not None and not force_refresh:
        return _PROBE_CACHE

    if sys.platform == "darwin":
        available = _probe_macos_sandbox_exec()
        cap = SandboxCapability(
            platform="macos",
            backend="sandbox-exec" if available else None,
            available=available,
        )
    elif sys.platform.startswith("linux"):
        available = _probe_linux_bubblewrap()
        cap = SandboxCapability(
            platform="linux",
            backend="bubblewrap" if available else None,
            available=available,
            landlock_capable=_landlock_capable(),
        )
    else:
        cap = SandboxCapability(
            platform="windows" if sys.platform == "win32" else sys.platform,
            backend=None,
            available=False,
        )

    _PROBE_CACHE = cap
    return cap


def build_seatbelt_profile(writable_paths: list[str], *, network: str = "allow") -> str:
    """Build a minimal Seatbelt profile confining writes to the given paths."""
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process*)",
        "(allow signal (target self))",
        "(allow sysctl-read)",
        "(allow file-read*)",
        "(allow file-write*",
    ]
    for path in writable_paths:
        escaped = path.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'    (subpath "{escaped}")')
    lines.append(")")
    return "\n".join(lines) + "\n"


def build_bwrap_argv(writable_paths: list[str], command: str, *, network: str = "allow") -> list[str]:
    """Build a bubblewrap argv prefix ending in sh -c <command>."""
    exe = shutil.which("bwrap")
    if not exe:
        raise FileNotFoundError("bwrap not found")

    argv: list[str] = [
        exe,
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--die-with-parent",
    ]
    for path in writable_paths:
        argv.extend(["--bind", path, path])
    argv.extend(["sh", "-c", command])
    return argv


def _wrap_argv_as_shell_command(argv: list[str]) -> str:
    """Turn an argv list into a string safe for Popen(..., shell=True)."""
    return " ".join(shlex.quote(part) for part in argv)


def _macos_spawn_plan(
    command: str,
    writable_paths: list[str],
    *,
    child_env: Mapping[str, str],
    network: str = "allow",
) -> SandboxSpawnPlan:
    exe = shutil.which("sandbox-exec")
    if not exe:
        raise SandboxRequiredUnavailable(
            "OS sandbox is required (HARNESS_OS_SANDBOX=required) but "
            "sandbox-exec is not available on this macOS host."
        )

    profile_text = build_seatbelt_profile(writable_paths, network=network)
    fd, profile_path = tempfile.mkstemp(suffix=".sb", prefix="harness_sandbox_", dir=child_env["TMPDIR"])
    os.close(fd)
    with open(profile_path, "w", encoding="utf-8") as fh:
        fh.write(profile_text)

    def _cleanup() -> None:
        try:
            os.unlink(profile_path)
        except OSError:
            pass

    wrapped = _wrap_argv_as_shell_command(
        [exe, "-f", profile_path, "sh", "-c", command]
    )
    return SandboxSpawnPlan(
        command=wrapped,
        cleanup=_cleanup,
        child_env=dict(child_env),
    )


def _linux_spawn_plan(
    command: str,
    writable_paths: list[str],
    *,
    child_env: Mapping[str, str],
) -> SandboxSpawnPlan:
    try:
        argv = build_bwrap_argv(writable_paths, command)
    except FileNotFoundError:
        raise SandboxRequiredUnavailable(
            "OS sandbox is required (HARNESS_OS_SANDBOX=required) but "
            "bubblewrap (bwrap) is not available on this Linux host."
        ) from None
    return SandboxSpawnPlan(
        command=_wrap_argv_as_shell_command(argv),
        child_env=dict(child_env),
    )


def _chain_cleanup(*cleanups: Callable[[], None] | None) -> Callable[[], None]:
    def _run() -> None:
        for fn in cleanups:
            if fn is not None:
                try:
                    fn()
                except Exception:
                    pass

    return _run


def prepare_sandbox_spawn(
    command: str,
    *,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
) -> SandboxSpawnPlan | None:
    """Return a spawn plan when sandboxing applies, else None.

    Raises SandboxRequiredUnavailable when mode=required but no backend exists.
    """
    env_map: Mapping[str, str] = env if env is not None else os.environ
    mode = resolve_os_sandbox_mode(env_map)
    if mode == "off":
        return None

    cap = detect_sandbox_capability(env_map)
    if not cap.available:
        if mode == "required":
            if sys.platform == "win32":
                msg = (
                    "OS sandbox is required (HARNESS_OS_SANDBOX=required) but "
                    "no supported OS sandbox is available on Windows."
                )
            elif sys.platform == "darwin":
                msg = (
                    "OS sandbox is required (HARNESS_OS_SANDBOX=required) but "
                    "sandbox-exec is not available on this macOS host."
                )
            elif sys.platform.startswith("linux"):
                msg = (
                    "OS sandbox is required (HARNESS_OS_SANDBOX=required) but "
                    "bubblewrap (bwrap) is not available on this Linux host."
                )
            else:
                msg = (
                    "OS sandbox is required (HARNESS_OS_SANDBOX=required) but "
                    "no supported OS sandbox is available on this platform."
                )
            raise SandboxRequiredUnavailable(msg)
        return None

    private_temp = _make_private_temp_dir(cwd, env_map)

    def _cleanup_private_temp() -> None:
        try:
            shutil.rmtree(private_temp, ignore_errors=True)
        except Exception:
            pass

    child_env = {
        "TMPDIR": private_temp,
        "TEMP": private_temp,
        "TMP": private_temp,
    }
    writable = _writable_paths(cwd, env_map, private_temp=private_temp)
    if cap.platform == "macos":
        plan = _macos_spawn_plan(command, writable, child_env=child_env)
    elif cap.platform == "linux":
        plan = _linux_spawn_plan(command, writable, child_env=child_env)
    else:
        _cleanup_private_temp()
        return None
    return SandboxSpawnPlan(
        command=plan.command,
        cleanup=_chain_cleanup(plan.cleanup, _cleanup_private_temp),
        child_env=child_env,
    )
