import os
import sys
import shutil
import subprocess

_PM_PYTHON_CACHE = None
_PM_AVAILABLE_CACHE = None
# Sentinel: None = not yet probed, "" = probed, none found, str = resolved path.
_PM_EXT_PYTHON_CACHE = None

def _clear_puppetmaster_cache():
    global _PM_PYTHON_CACHE, _PM_AVAILABLE_CACHE, _PM_EXT_PYTHON_CACHE
    _PM_PYTHON_CACHE = None
    _PM_AVAILABLE_CACHE = None
    _PM_EXT_PYTHON_CACHE = None


def _external_puppetmaster_python() -> str:
    """A real (non-frozen) Python that can import puppetmaster, or "" if none.

    Used only when the app is FROZEN, to decide how to run Puppetmaster/harness
    workers. Re-entering the frozen binary via `pm-exec` runs those workers from
    the PyInstaller PYZ snapshot, which has been observed in the field to
    (a) fail an implement worker's worktree packaging with "zlib incorrect
    header check" and (b) import a STALE harness.worker (missing WorkerResult) --
    both because the snapshot's stdlib/module graph is not the live installed
    source. Running through a real external interpreter instead executes the live
    installed puppetmaster + harness (editable venv / pyenv / system) with a
    working stdlib. Candidates, in priority order: the PMHARNESS_PYTHON override
    (the target repo's venv, set by the Electron host), then python3/python on
    PATH. Each candidate must actually import puppetmaster to be accepted."""
    global _PM_EXT_PYTHON_CACHE
    if _PM_EXT_PYTHON_CACHE is not None:
        return _PM_EXT_PYTHON_CACHE

    candidates = []
    env_py = os.environ.get("PMHARNESS_PYTHON")
    if env_py:
        candidates.append(env_py)
    for name in ("python3", "python"):
        resolved = shutil.which(name)
        if resolved:
            candidates.append(resolved)

    for py in candidates:
        # An absolute path must exist; a PATH-resolved name already does.
        if os.path.isabs(py) and not os.path.exists(py):
            continue
        try:
            res = subprocess.run(
                [py, "-c", "import puppetmaster"], capture_output=True, timeout=5
            )
            if res.returncode == 0:
                _PM_EXT_PYTHON_CACHE = py
                return py
        except Exception:
            pass

    _PM_EXT_PYTHON_CACHE = ""
    return ""

def _puppetmaster_python() -> str:
    global _PM_PYTHON_CACHE
    if _PM_PYTHON_CACHE is not None:
        return _PM_PYTHON_CACHE

    # 1. env override: os.environ.get("PMHARNESS_PYTHON") if set and exists.
    env_py = os.environ.get("PMHARNESS_PYTHON")
    if env_py and os.path.exists(env_py):
        _PM_PYTHON_CACHE = env_py
        return env_py

    # 2. If sys.executable is NOT frozen (not PyInstaller binary) and looks like python -> use sys.executable
    is_frozen = getattr(sys, "frozen", False)
    basename = os.path.basename(sys.executable).lower()
    looks_like_python = ("python" in basename)

    if not is_frozen and looks_like_python:
        _PM_PYTHON_CACHE = sys.executable
        return sys.executable

    # 3. If frozen: search for a real python that has puppetmaster importable.
    # Try in order: common interpreters: python3, python
    for py in ["python3", "python"]:
        py_path = shutil.which(py)
        if py_path:
            try:
                res = subprocess.run([py_path, "-c", "import puppetmaster"], capture_output=True, timeout=5)
                if res.returncode == 0:
                    _PM_PYTHON_CACHE = py_path
                    return py_path
            except Exception:
                pass

    # 4. Fallback: return sys.executable
    _PM_PYTHON_CACHE = sys.executable
    return sys.executable

def _puppetmaster_available() -> bool:
    global _PM_AVAILABLE_CACHE
    if _PM_AVAILABLE_CACHE is not None:
        return _PM_AVAILABLE_CACHE

    is_frozen = getattr(sys, "frozen", False)
    if is_frozen:
        try:
            import puppetmaster
            _PM_AVAILABLE_CACHE = True
            return True
        except ImportError:
            _PM_AVAILABLE_CACHE = False
            return False

    # 1. env override: os.environ.get("PMHARNESS_PYTHON") if set and exists.
    env_py = os.environ.get("PMHARNESS_PYTHON")
    if env_py and os.path.exists(env_py):
        _PM_AVAILABLE_CACHE = True
        return True

    # 2. If sys.executable is NOT frozen and looks like python:
    is_frozen = getattr(sys, "frozen", False)
    basename = os.path.basename(sys.executable).lower()
    looks_like_python = ("python" in basename)

    if not is_frozen and looks_like_python:
        try:
            import puppetmaster
            _PM_AVAILABLE_CACHE = True
            return True
        except ImportError:
            # Let's also check if it runs with sys.executable via subprocess
            try:
                res = subprocess.run([sys.executable, "-c", "import puppetmaster"], capture_output=True, timeout=5)
                if res.returncode == 0:
                    _PM_AVAILABLE_CACHE = True
                    return True
            except Exception:
                pass

    # 3. Check for puppetmaster console script
    pm_script = shutil.which("puppetmaster")
    if pm_script:
        try:
            subprocess.run([pm_script, "--help"], capture_output=True, timeout=5)
            _PM_AVAILABLE_CACHE = True
            return True
        except Exception:
            pass

    # 4. Search for python3/python that has puppetmaster
    for py in ["python3", "python"]:
        py_path = shutil.which(py)
        if py_path:
            try:
                res = subprocess.run([py_path, "-c", "import puppetmaster"], capture_output=True, timeout=5)
                if res.returncode == 0:
                    _PM_AVAILABLE_CACHE = True
                    return True
            except Exception:
                pass

    _PM_AVAILABLE_CACHE = False
    return False

def _puppetmaster_cmd(*args) -> list[str]:
    is_frozen = getattr(sys, "frozen", False)
    if is_frozen:
        # Prefer a real external interpreter running the LIVE installed source
        # over re-entering the frozen PYZ snapshot (see
        # _external_puppetmaster_python for why the snapshot breaks worktree
        # packaging + imports a stale harness.worker). Fall back to the
        # self-contained `pm-exec` re-entry only for a pure-DMG install with no
        # external Python that can import puppetmaster.
        ext = _external_puppetmaster_python()
        if ext:
            return [ext, "-m", "puppetmaster", *args]
        return [sys.executable, "pm-exec", *args]

    # Prefer the interpreter running THIS backend when it can import puppetmaster
    # (the source-run case: the repo's .venv/bin/python). This is guaranteed
    # consistent with the running process and avoids PATH `puppetmaster` shims --
    # notably pyenv shims, which resolve to a different Python that lacks
    # puppetmaster and exit 127 ("pyenv: puppetmaster: command not found"),
    # making codegraph/swarm calls fail and the panel show "unsupported".
    try:
        import importlib.util
        if importlib.util.find_spec("puppetmaster") is not None:
            return [sys.executable, "-m", "puppetmaster", *args]
    except Exception:
        pass

    pm_script = shutil.which("puppetmaster")
    if pm_script:
        return [pm_script, *args]
    return [_puppetmaster_python(), "-m", "puppetmaster", *args]
