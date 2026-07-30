"""Optional environment readiness: standalone browser + local analyzers.

These are optional prerequisites, not product failures. This module only
probes and explains — it never auto-installs or shells out to install tools.
Browser tools remain unavailable when only embedded Electron exists.
"""
from __future__ import annotations

import copy
import os
import sys
import threading
import time
from typing import Any, Dict, Optional

# Short TTL so ordinary UI polls reuse LSP/Chrome discovery; explicit
# refresh=True bypasses and replaces the entry for that cache key.
_READINESS_TTL_SECONDS = 30.0
_readiness_lock = threading.Lock()
# (workspace, browser_chrome_key) -> (monotonic_ts, payload)
_readiness_cache: Dict[tuple[str, str], tuple[float, Dict[str, Any]]] = {}


def _platform_family() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    return "linux"


def _normalize_workspace(root: Optional[str]) -> str:
    raw = (root or "").strip()
    if not raw:
        return ""
    try:
        # Preserve the filesystem's display casing in the API payload. Windows
        # cache identity is case-insensitive, but lowercasing the user-facing
        # workspace path makes an otherwise valid path look rewritten.
        return os.path.abspath(os.path.expanduser(raw))
    except Exception:
        return raw


def _browser_chrome_cache_key() -> str:
    """Fingerprint the current PM_BROWSER_CHROME value for readiness cache keys.

    Changing the env (including pointing at an embedded Electron binary) must
    not reuse a stale 30s readiness payload. Empty means unset / PATH discovery.
    """
    configured = os.environ.get("PM_BROWSER_CHROME", "").strip()
    if not configured:
        return ""
    try:
        return os.path.normcase(os.path.abspath(os.path.expanduser(configured)))
    except Exception:
        return configured


def _readiness_cache_key(workspace: str) -> tuple[str, str]:
    return (os.path.normcase(workspace), _browser_chrome_cache_key())


def browser_remedy(*, available: bool, configured: str = "") -> str:
    """Platform-aware, actionable Chrome/Chromium install guidance."""
    if available:
        return ""
    family = _platform_family()
    configured = (configured or "").strip()
    if configured:
        return (
            f"PM_BROWSER_CHROME is set to {configured!r} but that path is not a "
            "usable standalone Chrome/Chromium executable (Electron/Marionette "
            "embeds are rejected). Point it at a real Chrome/Chromium binary, "
            "or unset it and install Chrome/Chromium."
        )
    if family == "macos":
        return (
            "Install Google Chrome (or Chromium) from https://www.google.com/chrome/ "
            "(Applications or ~/Applications), or set PM_BROWSER_CHROME to its "
            "standalone executable. Marionette's embedded Electron cannot drive "
            "browser tools."
        )
    if family == "windows":
        return (
            "Install Google Chrome or Chromium, or set PM_BROWSER_CHROME to the "
            "chrome.exe path (for example "
            "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe). "
            "Embedded Electron is not used for browser tools."
        )
    return (
        "Install Chromium or Google Chrome (e.g. `sudo apt install chromium` / "
        "`chromium-browser`, or your distro's chrome package), ensure it is on "
        "PATH, or set PM_BROWSER_CHROME to the executable. Embedded Electron is "
        "not used for browser tools."
    )


def python_analyzer_remedy(*, available: bool) -> str:
    if available:
        return ""
    family = _platform_family()
    venv_bin = ".venv\\Scripts" if family == "windows" else ".venv/bin"
    return (
        "Python analyzer unavailable: install pyright on PATH "
        f"(`pip install pyright` / `npm i -g pyright`), or in the workspace "
        f"{venv_bin} / node_modules/.bin. Marionette does not auto-install."
    )


def typescript_analyzer_remedy(*, available: bool) -> str:
    if available:
        return ""
    return (
        "TypeScript analyzer unavailable: install typescript in the workspace "
        "(`npm i -D typescript`) so node_modules/.bin/tsc exists, or put tsc on "
        "PATH. Marionette does not auto-install."
    )


def _probe_environment_readiness(*, workspace: str, refresh: bool) -> Dict[str, Any]:
    from .browser import standalone_chrome_path
    from .lsp_code_intelligence import discover_lsp_tools

    configured = os.environ.get("PM_BROWSER_CHROME", "").strip()
    browser_path: Optional[str] = None
    try:
        browser_path = standalone_chrome_path(refresh=refresh)
    except Exception:
        browser_path = None
    browser_ok = bool(browser_path)

    tools = discover_lsp_tools(root=workspace or None)
    py_path = tools.python_pyright or tools.python_pyright_langserver
    ts_path = (
        tools.typescript_tsc
        or tools.typescript_tsserver
        or tools.typescript_typescript_language_server
    )
    py_ok = bool(tools.python_available)
    ts_ok = bool(tools.typescript_available)

    return {
        "browser": {
            "available": browser_ok,
            "path": browser_path,
            "remedy": browser_remedy(available=browser_ok, configured=configured),
        },
        "python_analyzer": {
            "available": py_ok,
            "path": py_path,
            "remedy": python_analyzer_remedy(available=py_ok),
        },
        "typescript_analyzer": {
            "available": ts_ok,
            "path": ts_path,
            "remedy": typescript_analyzer_remedy(available=ts_ok),
        },
        "workspace_root": workspace,
    }


def build_environment_readiness(
    *,
    root: Optional[str] = None,
    refresh: bool = False,
) -> Dict[str, Any]:
    """Return a small readiness contract for UI/API consumers.

    Ordinary callers (``refresh=False``) reuse a short cache keyed by workspace
    plus the current ``PM_BROWSER_CHROME`` fingerprint so bounded LSP discovery
    is not repeated on every mount/poll, but an env-path change cannot serve a
    stale browser readiness row. Explicit ``refresh=True`` invalidates that
    entry and re-probes.

    Shape::

        {
          "browser": {"available": bool, "path": str|None, "remedy": str},
          "python_analyzer": {"available": bool, "path": str|None, "remedy": str},
          "typescript_analyzer": {"available": bool, "path": str|None, "remedy": str},
          "workspace_root": str,
        }
    """
    workspace = _normalize_workspace(root)
    cache_key = _readiness_cache_key(workspace)
    now = time.monotonic()
    with _readiness_lock:
        if not refresh:
            cached = _readiness_cache.get(cache_key)
            if cached is not None and (now - cached[0]) < _READINESS_TTL_SECONDS:
                return copy.deepcopy(cached[1])
        elif cache_key in _readiness_cache:
            del _readiness_cache[cache_key]

    payload = _probe_environment_readiness(workspace=workspace, refresh=refresh)

    with _readiness_lock:
        _readiness_cache[cache_key] = (time.monotonic(), copy.deepcopy(payload))
    return payload


def clear_environment_readiness_cache() -> None:
    """Test helper — drop all cached readiness payloads."""
    with _readiness_lock:
        _readiness_cache.clear()
