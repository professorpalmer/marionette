"""Auto-provision and auto-start a local Portable LLM Wiki backend so the wiki
panel works out of the box on a fresh install -- no manual terminal, on any OS.

Marionette is a *client* of the wiki (it reads WIKI_API_BASE / WIKI_OWNER_TOKEN).
On startup we make sure a local wiki backend is running and that Marionette holds
its owner token:

  1. If a wiki backend is already answering /healthz, do nothing.
  2. Otherwise look for an existing backend checkout to launch (discovery order
     below).
  3. Otherwise PROVISION one: shallow-clone the public wiki repo into a managed
     directory, create its venv + install backend deps (uv preferred, stdlib
     venv fallback), generate an OWNER_TOKEN, write backend/.env pointed at the
     bundled demo wiki, and persist the token to Marionette's wiki.json so the
     client authenticates as owner.

Everything here is pure Python over `git` + `uv` (both guaranteed by every
installer), so it is identical across macOS, Windows, and Linux -- universality
comes for free with no per-OS installer surgery.

Discovery order for an existing backend dir:
  1. $MARIONETTE_WIKI_DIR (explicit override)
  2. ~/portable-llm-wiki/backend (a developer checkout)
  3. the managed clone under $MARIONETTE_WIKI_HOME (default ~/.marionette/wiki)

Opt out entirely with MARIONETTE_NO_WIKI=1. The backend is spawned detached so
it survives Marionette backend respawns and stays available to other clients
(e.g. the Cursor wiki MCP). A prior instance is detected via /healthz so we
never double-start.
"""
from __future__ import annotations

import importlib.util
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from urllib.parse import urlparse

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_WIKI_REPO_URL = "https://github.com/professorpalmer/portable-llm-wiki.git"
_DEFAULT_BASE = "http://127.0.0.1:8000"
_IS_WINDOWS = os.name == "nt"

_started_proc = None
_ensure_lock = threading.Lock()

# Best-effort self-healing is throttled per process: the venv repair runs at
# most once, and a failed spawn/health-wait schedules at most _MAX_RETRIES
# delayed retries so a broken environment never spins forever.
_MAX_RETRIES = 3
_retry_count = 0
_retry_timer = None
_venv_repair_attempted = False


def _opted_out() -> bool:
    return os.environ.get("MARIONETTE_NO_WIKI", "").strip().lower() in {"1", "true", "yes"}


def _wiki_base() -> str:
    base = (
        os.environ.get("WIKI_API_BASE")
        or os.environ.get("HARNESS_WIKI_URL")
        or ""
    ).strip().rstrip("/")
    return base or _DEFAULT_BASE


def _is_local(base: str) -> bool:
    try:
        return (urlparse(base).hostname or "") in _LOCAL_HOSTS
    except Exception:
        return False


def _healthz(base: str, timeout: float = 2.0) -> bool:
    for path in ("/healthz", "/health"):
        try:
            with urllib.request.urlopen(base + path, timeout=timeout) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            continue
    return False


def _managed_home() -> str:
    override = os.environ.get("MARIONETTE_WIKI_HOME", "").strip()
    return override or os.path.expanduser(os.path.join("~", ".marionette", "wiki"))


def _is_backend_dir(path: str) -> bool:
    return bool(path) and os.path.isfile(os.path.join(path, "app", "main.py"))


def _find_existing_backend_dir() -> str | None:
    candidates = []
    override = os.environ.get("MARIONETTE_WIKI_DIR", "").strip()
    if override:
        candidates.append(override)
    candidates.append(os.path.expanduser(os.path.join("~", "portable-llm-wiki", "backend")))
    candidates.append(os.path.join(_managed_home(), "backend"))
    for path in candidates:
        if _is_backend_dir(path):
            return path
    return None


def _venv_bin(venv_dir: str, name: str) -> str:
    """Path to an executable inside a venv, OS-aware (Scripts/*.exe on Windows)."""
    if _IS_WINDOWS:
        return os.path.join(venv_dir, "Scripts", name + ".exe")
    return os.path.join(venv_dir, "bin", name)


def _log_handle():
    try:
        log_path = os.path.expanduser(os.path.join("~", ".pmharness", "wiki-backend.log"))
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        return open(log_path, "ab", buffering=0)
    except Exception:
        return subprocess.DEVNULL


def _log_line(log, message: str) -> None:
    """Best-effort line to the wiki-backend log; log may be DEVNULL (an int)."""
    try:
        if hasattr(log, "write"):
            log.write(("[marionette] " + message.rstrip() + "\n").encode("utf-8", "replace"))
    except Exception:
        pass


def _python_is_usable(exe: str) -> bool:
    """Reject interpreters that cannot actually run.

    On Windows, `shutil.which('python')` often resolves to the WindowsApps
    execution-alias stub, which prints "Python was not found; run without
    arguments to install from the Microsoft Store" and exits immediately --
    spawning uvicorn with it produces an endlessly dead backend. Filter those
    out by path, then confirm the interpreter answers `--version`.
    """
    if not exe:
        return False
    if "windowsapps" in exe.lower():
        return False
    try:
        result = subprocess.run(
            [exe, "--version"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def _validated_which_python() -> str | None:
    for name in ("python3", "python"):
        exe = shutil.which(name)
        if exe and _python_is_usable(exe):
            return exe
    return None


def _uvicorn_importable() -> bool:
    try:
        return importlib.util.find_spec("uvicorn") is not None
    except Exception:
        return False


def _run(cmd: list[str], cwd: str | None, log) -> bool:
    try:
        subprocess.run(
            cmd, cwd=cwd, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            check=True,
        )
        return True
    except Exception:
        return False


def _read_env_token(env_file: str) -> str:
    try:
        with open(env_file, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("OWNER_TOKEN="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def _ensure_backend_venv(backend_dir: str, log) -> None:
    """Create the backend venv and install its deps (uv preferred, stdlib venv
    fallback). Idempotent and best-effort: shared by first-time provisioning
    and by the repair path when an existing checkout's venv is missing/broken."""
    venv_dir = os.path.join(backend_dir, ".venv")
    venv_py = _venv_bin(venv_dir, "python")
    requirements = os.path.join(backend_dir, "requirements.txt")
    uv = shutil.which("uv")
    if os.path.isfile(_venv_bin(venv_dir, "uvicorn")):
        return
    if uv:
        _run([uv, "venv", venv_dir], backend_dir, log)
    if not os.path.isfile(venv_py):
        base_py = sys.executable if _python_is_usable(sys.executable) else _validated_which_python()
        if base_py:
            _run([base_py, "-m", "venv", venv_dir], backend_dir, log)
    if os.path.isfile(requirements):
        installed = False
        if uv and os.path.isfile(venv_py):
            installed = _run(
                [uv, "pip", "install", "--python", venv_py, "-r", requirements],
                backend_dir, log)
        if not installed:
            pip = _venv_bin(venv_dir, "pip")
            if os.path.isfile(pip):
                _run([pip, "install", "-r", requirements], backend_dir, log)


def _repair_backend_venv_once(backend_dir: str, log) -> None:
    """Best-effort rebuild of a missing/broken backend venv, at most once per
    process so a hopeless environment doesn't reinstall on every ensure call."""
    global _venv_repair_attempted
    if _venv_repair_attempted:
        return
    _venv_repair_attempted = True
    _log_line(log, f"wiki backend venv missing/broken in {backend_dir}; attempting repair")
    try:
        _ensure_backend_venv(backend_dir, log)
    except Exception as exc:
        _log_line(log, f"wiki backend venv repair failed: {exc}")


def _provision_wiki(log) -> str | None:
    """Clone + set up a managed wiki backend. Idempotent. Returns backend dir or None."""
    home = _managed_home()
    backend_dir = os.path.join(home, "backend")

    git = shutil.which("git")
    if not git:
        return None

    # 1. Clone (shallow) if the checkout is missing.
    if not _is_backend_dir(backend_dir):
        try:
            os.makedirs(os.path.dirname(home), exist_ok=True)
        except Exception:
            pass
        if os.path.isdir(home) and not os.listdir(home):
            try:
                os.rmdir(home)
            except Exception:
                pass
        if not os.path.isdir(home):
            if not _run([git, "clone", "--depth", "1", _WIKI_REPO_URL, home], None, log):
                return None
        if not _is_backend_dir(backend_dir):
            return None

    # 2. venv + backend deps (uv preferred, stdlib venv fallback). Idempotent.
    _ensure_backend_venv(backend_dir, log)

    # 3. backend/.env with a generated OWNER_TOKEN, pointed at the bundled demo.
    env_file = os.path.join(backend_dir, ".env")
    token = _read_env_token(env_file)
    if not token:
        token = secrets.token_hex(32)
        wiki_root = os.path.join(home, "wiki-demo")
        if not os.path.isdir(wiki_root):
            wiki_root = home
        try:
            with open(env_file, "w", encoding="utf-8") as f:
                f.write(
                    "# Generated by Marionette wiki auto-provision\n"
                    f"WIKI_ROOT={wiki_root}\n"
                    f"OWNER_TOKEN={token}\n"
                    "DEFAULT_TIER=private\n"
                    "CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000\n"
                    "PUBLIC_BASE_URL=http://localhost:8000\n"
                )
        except Exception:
            return backend_dir

    # 4. Register the token so Marionette's WikiClient authenticates as owner.
    if token:
        try:
            from .wiki_config import set_wiki_config
            set_wiki_config(api_base=_DEFAULT_BASE, owner_token=token)
        except Exception:
            pass

    return backend_dir if _is_backend_dir(backend_dir) else None


def _uvicorn_cmd(backend_dir: str, port: int) -> list[str] | None:
    """Pick the best way to launch uvicorn, most-reliable first:

      1. the backend venv's uvicorn executable
      2. the backend venv's python -m uvicorn
      3. this process's interpreter, if uvicorn is importable here
      4. a validated python3/python from PATH (WindowsApps stubs rejected)

    Returns None when nothing usable exists rather than spawning a dead stub.
    """
    args = ["app.main:app", "--host", "127.0.0.1", "--port", str(port)]
    venv_dir = os.path.join(backend_dir, ".venv")
    venv_uvicorn = _venv_bin(venv_dir, "uvicorn")
    if os.path.isfile(venv_uvicorn):
        return [venv_uvicorn] + args
    venv_py = _venv_bin(venv_dir, "python")
    if os.path.isfile(venv_py) and _python_is_usable(venv_py):
        return [venv_py, "-m", "uvicorn"] + args
    if _uvicorn_importable() and _python_is_usable(sys.executable):
        return [sys.executable, "-m", "uvicorn"] + args
    which_py = _validated_which_python()
    if which_py:
        return [which_py, "-m", "uvicorn"] + args
    return None


def _spawn(cmd: list[str], cwd: str, log):
    kwargs = dict(cwd=cwd, stdout=log, stderr=log, stdin=subprocess.DEVNULL)
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:
        # Detach from Marionette's process group so it survives respawns,
        # without flashing a console window on every launch. Deliberately NOT
        # DETACHED_PROCESS: it is mutually exclusive with CREATE_NO_WINDOW at
        # the CreateProcess level, and combining them made Windows Terminal
        # open a visible window for uvicorn. CREATE_NO_WINDOW alone gives the
        # child its own hidden console, which is detachment enough.
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "replace")
    return subprocess.Popen(cmd, **kwargs)


def _schedule_retry(log) -> None:
    """After a failed spawn/health-wait, retry once in the background.

    A single delayed daemon Timer (30s), capped at _MAX_RETRIES per process, so
    a transiently broken environment (e.g. venv mid-repair) self-heals without
    ever spinning forever.
    """
    global _retry_count, _retry_timer
    if _retry_count >= _MAX_RETRIES:
        return
    if _retry_timer is not None and _retry_timer.is_alive():
        return
    _retry_count += 1
    _log_line(log, f"wiki backend start failed; retry {_retry_count}/{_MAX_RETRIES} in 30s")
    _retry_timer = threading.Timer(30.0, lambda: ensure_wiki_backend_running())
    _retry_timer.daemon = True
    _retry_timer.start()


def ensure_wiki_backend_running(wait_secs: float = 90.0, allow_provision: bool = True) -> dict:
    """Ensure a local wiki backend is up, provisioning one if needed.

    Returns a small status dict; never raises. Safe to call repeatedly.
    """
    global _started_proc
    with _ensure_lock:
        if _opted_out():
            return {"started": False, "reason": "opted out (MARIONETTE_NO_WIKI)"}

        base = _wiki_base()
        if not _is_local(base):
            return {"started": False, "reason": "wiki configured at a remote URL"}
        if _healthz(base):
            return {"started": False, "reason": "already running"}

        log = _log_handle()

        backend_dir = _find_existing_backend_dir()
        if not backend_dir and allow_provision:
            backend_dir = _provision_wiki(log)
        if not backend_dir:
            return {"started": False, "reason": "no wiki backend available"}

        if not os.path.isfile(_venv_bin(os.path.join(backend_dir, ".venv"), "python")):
            _repair_backend_venv_once(backend_dir, log)

        port = urlparse(base).port or 8000
        cmd = _uvicorn_cmd(backend_dir, port)
        if not cmd:
            _log_line(log, "no usable python for uvicorn; not spawning")
            return {"started": False, "reason": "no usable python for uvicorn"}

        try:
            _started_proc = _spawn(cmd, backend_dir, log)
        except Exception as exc:
            _schedule_retry(log)
            return {"started": False, "reason": f"spawn failed: {exc}"}

        deadline = time.monotonic() + wait_secs
        while time.monotonic() < deadline:
            if _healthz(base, timeout=1.5):
                return {"started": True, "reason": "backend up",
                        "dir": backend_dir, "port": port}
            if _started_proc.poll() is not None:
                _schedule_retry(log)
                return {"started": False, "reason": "backend exited during startup"}
            time.sleep(0.5)
        _schedule_retry(log)
        return {"started": False, "reason": "timeout waiting for /healthz"}


def ensure_wiki_backend_async() -> None:
    """Run ensure_wiki_backend_running on a daemon thread so startup never blocks
    on cloning/installing/health-waiting."""
    threading.Thread(
        target=lambda: ensure_wiki_backend_running(),
        daemon=True,
    ).start()
