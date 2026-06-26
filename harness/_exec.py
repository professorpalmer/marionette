import os
import sys
import shutil
import subprocess

_PM_PYTHON_CACHE = None
_PM_AVAILABLE_CACHE = None

def _clear_puppetmaster_cache():
    global _PM_PYTHON_CACHE, _PM_AVAILABLE_CACHE
    _PM_PYTHON_CACHE = None
    _PM_AVAILABLE_CACHE = None

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
        return [sys.executable, "pm-exec", *args]

    pm_script = shutil.which("puppetmaster")
    if pm_script:
        return [pm_script, *args]
    else:
        return [_puppetmaster_python(), "-m", "puppetmaster", *args]
