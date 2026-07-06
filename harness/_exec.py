# PEP 604 unions (str | None) in signatures must stay lazy on Python 3.9.
from __future__ import annotations

import os
import re
import sys
import glob
import shutil
import subprocess

_PM_PYTHON_CACHE = None
_PM_AVAILABLE_CACHE = None
# Sentinel: None = not yet probed, "" = probed, none found, str = resolved path.
_PM_EXT_PYTHON_CACHE = None
_NODE_PATH_ENSURED = False

def _clear_puppetmaster_cache():
    global _PM_PYTHON_CACHE, _PM_AVAILABLE_CACHE, _PM_EXT_PYTHON_CACHE, _NODE_PATH_ENSURED
    _PM_PYTHON_CACHE = None
    _PM_AVAILABLE_CACHE = None
    _PM_EXT_PYTHON_CACHE = None
    _NODE_PATH_ENSURED = False


def _node_candidate_dirs() -> list[str]:
    """Directories that commonly hold a `node` binary, most-preferred first.

    Covers the harness-provisioned portable Node, standard user/system prefixes,
    and the popular version managers (nvm, fnm, n, volta). Version-manager globs
    are expanded newest-first so we pick a recent Node (CodeGraph needs the
    node:sqlite backend from Node 20+)."""
    home = os.path.expanduser("~")
    dirs = [
        os.path.join(home, ".marionette", "tools", "node"),          # portable (unix layout)
        os.path.join(home, ".marionette", "tools", "node", "bin"),
        os.path.join(home, ".local", "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/opt/local/bin",
        os.path.join(home, ".volta", "bin"),
    ]
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
        dirs.insert(0, os.path.join(local, "marionette", "tools", "node"))

    def _ver_key(path: str):
        nums = re.findall(r"(\d+)\.(\d+)\.(\d+)", path)
        return tuple(int(n) for n in nums[-1]) if nums else (0, 0, 0)

    for pattern in (
        os.path.join(home, ".nvm", "versions", "node", "*", "bin"),
        os.path.join(home, ".fnm", "node-versions", "*", "installation", "bin"),
        os.path.join(home, "Library", "Application Support", "fnm", "node-versions", "*", "installation", "bin"),
        os.path.join(home, "n", "bin"),
        "/usr/local/n/versions/node/*/bin",
    ):
        matches = sorted(glob.glob(pattern), key=_ver_key, reverse=True)
        dirs.extend(matches)
    return dirs


def _is_app_bundle_path(p: str) -> bool:
    """True when `p` lives inside a macOS `.app/Contents/` bundle.

    Used to reject a `node` binary that belongs to another application (most
    notably `/Applications/Cursor.app/Contents/Resources/app/resources/helpers/node`),
    since spawning it from our backend is a cross-app binary launch that macOS
    TCC flags with the recurring "wants to access data from other apps"
    prompt. The check is a pure string scan (case-insensitive) so it is safe
    to evaluate on any platform; on non-macOS it simply never matches real
    paths."""
    if not p:
        return False
    try:
        norm = p.replace("\\", "/").lower()
    except Exception:
        return False
    # Match a `.app/Contents/` (or trailing `.app/Contents`) segment anywhere
    # in the path. Guard against a bare ".app" filename that isn't a bundle.
    return ".app/contents/" in norm or norm.endswith(".app/contents")


def sanitized_path(path: str | None = None) -> str:
    """Return a PATH string with `.app/Contents/` entries demoted to the end.

    Generalization of the node-only fix in `_ensure_node_on_path()`. Any
    subprocess Marionette spawns should run with a PATH that does NOT prefer
    binaries living inside another app's `.app/Contents/` bundle (Cursor.app,
    VS Code.app, ...), because spawning a sibling app's bundled binary is the
    cross-app launch that triggers the macOS TCC "wants to access data from
    other apps" prompt and pulls in foreign runtimes.

    Behavior:
    - Splits `path` on `os.pathsep` (defaults to `os.environ['PATH']`).
    - Preserves relative order within kept (clean) entries and within moved
      (app-bundle) entries.
    - De-duplicates while keeping the FIRST occurrence's position.
    - Moves (does NOT delete) `.app/Contents/` entries to the tail, so anything
      that ONLY exists inside an app bundle remains reachable as a last resort.
    - Best-effort and pure: on any exception, returns the input unchanged.
    - Never raises; empty/None inputs yield an empty string.
    """
    try:
        if path is None:
            path = os.environ.get("PATH", "")
        if not path:
            return ""
        seen: set[str] = set()
        kept: list[str] = []
        moved: list[str] = []
        for entry in path.split(os.pathsep):
            if entry in seen:
                continue
            seen.add(entry)
            if _is_app_bundle_path(entry):
                moved.append(entry)
            else:
                kept.append(entry)
        return os.pathsep.join(kept + moved)
    except Exception:
        return path if isinstance(path, str) else ""


def sanitized_env(env: dict | None = None) -> dict:
    """Return a COPY of `env` with PATH replaced by `sanitized_path(env['PATH'])`.

    Never mutates the input dict or `os.environ`. Best-effort: on any failure
    the copy is returned with PATH untouched (or a plain copy of the input).
    Defaults to `os.environ` when `env` is None.
    """
    try:
        source = os.environ if env is None else env
        copy = dict(source)
        try:
            # Only rewrite PATH when the source actually had one. Passing None
            # to sanitized_path() would fall back to os.environ['PATH'], which
            # would surprise callers who intentionally handed us a PATH-less
            # env dict.
            if "PATH" in copy:
                copy["PATH"] = sanitized_path(copy["PATH"])
        except Exception:
            # Fall back to the original PATH untouched.
            pass
        return copy
    except Exception:
        # Absolute last-resort fallback: never raise from a sanitizer helper.
        try:
            return dict(env if env is not None else os.environ)
        except Exception:
            return {}


def _ensure_node_on_path() -> None:
    """Make a CLEAN `node` discoverable for CodeGraph even when the backend was
    spawned with a stripped or cross-app-polluted PATH.

    Two failure modes we defend against:

    1. Stripped PATH (Electron host launches the Python backend with
       PATH=/usr/bin:/bin:...) -- a Node installed under ~/.local/bin,
       Homebrew, or a version manager is invisible and CodeGraph (a Node CLI
       using node:sqlite) reports "unavailable"/"unsupported".
    2. Cross-app node on PATH -- the Electron host (e.g. Cursor) exposes its
       BUNDLED node at `/Applications/Cursor.app/Contents/.../helpers/node`.
       `shutil.which("node")` happily returns it, but spawning that binary
       from our process is a cross-app launch that macOS TCC flags with the
       recurring "wants to access data from other apps" prompt.

    So: only accept a `node` on PATH if it is NOT inside a `.app/Contents/`
    bundle. Otherwise, prepend the first `_node_candidate_dirs()` entry that
    (a) contains a real `node` binary and (b) is itself not inside an .app
    bundle -- preferring /opt/homebrew/bin and the other clean prefixes over
    anything Cursor.app injected. Idempotent, best-effort, never raises."""
    global _NODE_PATH_ENSURED
    if _NODE_PATH_ENSURED:
        return
    try:
        resolved = shutil.which("node")
    except Exception:
        resolved = None
    if resolved and not _is_app_bundle_path(resolved):
        # Already good: a real, non-cross-app node is on PATH. Leave PATH alone.
        _NODE_PATH_ENSURED = True
        return

    exe = "node.exe" if os.name == "nt" else "node"
    try:
        candidates = _node_candidate_dirs()
    except Exception:
        candidates = []
    for d in candidates:
        try:
            if not d or _is_app_bundle_path(d):
                continue
            node_path = os.path.join(d, exe)
            if os.path.isfile(node_path) and not _is_app_bundle_path(node_path):
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
                break
        except Exception:
            continue
    _NODE_PATH_ENSURED = True


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
    # CodeGraph (and any Node-backed worker) needs `node` on PATH; the Electron
    # host spawns us with a stripped PATH, so resolve Node before we build any
    # puppetmaster command. Idempotent + best-effort.
    _ensure_node_on_path()
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
