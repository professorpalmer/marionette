"""Pilot-facing browser tools -- thin wrapper over the CDP engine.

The real browser engine lives in ``puppetmaster.browser_cdp`` (a stdlib Chrome
DevTools Protocol driver) so BOTH the interactive pilot AND agentic swarm workers
drive the SAME browser through one code path. This module just re-exports the
engine functions under the ``browser_*`` names the pilot's action dispatch calls,
keeping a stable local import surface (``from harness import browser``).

Every function returns a STRING and never raises. If Puppetmaster's engine isn't
importable, or a CDP call fails unexpectedly, a calm error message is returned
instead of crashing the chat turn.
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from typing import Optional

try:
    from puppetmaster import browser_cdp as _engine
    _ENGINE_ERR = ""
except Exception as _e:  # pragma: no cover - engine should always be importable
    _engine = None
    _ENGINE_ERR = f"browser engine unavailable: {_e}"

# macOS bundle names, probed under both /Applications (system-wide) and
# ~/Applications (per-user installs, which is where Chrome lands for anyone
# without admin rights and where Beta/Dev/Canary channels usually live).
_MACOS_BROWSER_BUNDLES = (
    ("Google Chrome.app", "Google Chrome"),
    ("Google Chrome Beta.app", "Google Chrome Beta"),
    ("Google Chrome Dev.app", "Google Chrome Dev"),
    ("Google Chrome Canary.app", "Google Chrome Canary"),
    ("Chromium.app", "Chromium"),
)

# PATH names, probed after the bundles. Keeps parity with
# puppetmaster.browser_cdp._CHROME_CANDIDATES.
_CHROME_PATH_NAMES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "chrome",
)


def _macos_bundle_candidates() -> tuple:
    if sys.platform != "darwin":
        return ()
    roots = ("/Applications", os.path.expanduser("~/Applications"))
    return tuple(
        os.path.join(root, bundle, "Contents", "MacOS", executable)
        for root in roots
        for bundle, executable in _MACOS_BROWSER_BUNDLES
    )


def chrome_candidates() -> tuple:
    """Every standalone Chrome/Chromium path this host might have, in order."""
    return _macos_bundle_candidates() + _CHROME_PATH_NAMES


def _reject_embedded_browser(path: str) -> bool:
    normalized = os.path.normpath(path)
    components = {part.lower() for part in normalized.split(os.sep)}
    executable_name = os.path.basename(normalized).lower()
    return (
        "marionette.app" in components
        or "electron.app" in components
        or executable_name == "marionette"
        or executable_name == "electron"
        or executable_name.startswith("electron helper")
    )


def _chrome_executable_available(path: str) -> bool:
    if _reject_embedded_browser(path):
        return False
    if os.path.isfile(path) and os.access(path, os.X_OK):
        return True
    return bool(shutil.which(path))


def _find_standalone_chrome() -> Optional[str]:
    configured = os.environ.get("PM_BROWSER_CHROME", "").strip()
    if configured:
        return configured if _chrome_executable_available(configured) else None
    for candidate in chrome_candidates():
        if _chrome_executable_available(candidate):
            return candidate
    return None


# Filesystem/PATH probing across ~14 candidates is too expensive to repeat for
# every turn's tool schema, but must not go stale when the user installs Chrome
# or edits PM_BROWSER_CHROME. Cache per configured-executable value with a short
# TTL: an env change swaps the cache key and is visible immediately, while a
# fresh install shows up on the next refresh after the TTL.
# Value shape: cache_key -> (monotonic_ts, resolved_path | None)
_CHROME_PROBE_TTL_SECONDS = 30.0
_chrome_probe_cache: dict = {}


def _probe_standalone_chrome(*, refresh: bool = False) -> Optional[str]:
    """Resolve standalone Chrome once; reuse the cached path within the TTL."""
    if _ENGINE_ERR or _engine is None:
        return None
    key = os.environ.get("PM_BROWSER_CHROME", "").strip()
    now = time.monotonic()
    cached = _chrome_probe_cache.get(key)
    if cached is not None and not refresh and (now - cached[0]) < _CHROME_PROBE_TTL_SECONDS:
        return cached[1]
    path = _find_standalone_chrome()
    _chrome_probe_cache.clear()
    _chrome_probe_cache[key] = (now, path)
    return path


def standalone_browser_available(*, refresh: bool = False) -> bool:
    """True when a real Chrome/Chromium (never Electron) can be driven via CDP.

    Cheap enough to call on every tool-schema refresh. ``refresh=True`` forces a
    re-probe for callers that just changed the browser configuration.
    """
    return _probe_standalone_chrome(refresh=refresh) is not None


def standalone_chrome_path(*, refresh: bool = False) -> Optional[str]:
    """Resolved standalone Chrome/Chromium path, or None when unavailable.

    Same Electron-rejection rules and probe cache as
    ``standalone_browser_available`` — never re-runs ``_find_standalone_chrome``
    after a cache hit. ``refresh=True`` forces a fresh probe for readiness UI
    after installs or ``PM_BROWSER_CHROME`` edits.
    """
    return _probe_standalone_chrome(refresh=refresh)


def _guard() -> Optional[str]:
    if _ENGINE_ERR:
        return _ENGINE_ERR
    if _engine is None:
        return "browser engine unavailable"
    if getattr(_engine, "__name__", "") == "puppetmaster.browser_cdp":
        configured = os.environ.get("PM_BROWSER_CHROME", "").strip()
        if configured:
            if _reject_embedded_browser(configured):
                return (
                    "browser unavailable: PM_BROWSER_CHROME must point to a "
                    "standalone Chrome/Chromium executable, not Marionette/Electron"
                )
            if not _chrome_executable_available(configured):
                return (
                    "browser unavailable: PM_BROWSER_CHROME does not name an "
                    "executable; install Chrome/Chromium or set PM_BROWSER_CHROME"
                )
        elif _find_standalone_chrome() is None:
            return (
                "browser unavailable: install Chrome/Chromium or set "
                "PM_BROWSER_CHROME to its standalone executable"
            )
    return None


def _call(op_name: str, method_name: str, *args, **kwargs) -> str:
    """Run a CDP engine op; always return a string, never raise on the chat path."""
    err = _guard()
    if err:
        return err
    try:
        from .browser_auth import ensure_shared_browser_env
        ensure_shared_browser_env()
    except Exception:
        pass
    try:
        fn = getattr(_engine, method_name)
        result = fn(*args, **kwargs)
    except Exception as e:
        return f"{op_name} failed: {type(e).__name__}: {e}"
    if result is None:
        return f"{op_name} failed: empty result from browser engine"
    return result if isinstance(result, str) else str(result)


def browser_navigate(url: str) -> str:
    return _call("navigate", "navigate", url)


def browser_snapshot() -> str:
    return _call("snapshot", "snapshot")


def browser_click(ref: str) -> str:
    return _call("click", "click", ref)


def browser_type(ref: str, text: str) -> str:
    return _call("type", "type_text", ref, text)


def browser_scroll(direction: str = "down") -> str:
    return _call("scroll", "scroll", direction)


def browser_back() -> str:
    return _call("back", "back")


def browser_get_text() -> str:
    return _call("get_text", "get_text")


def browser_screenshot(out_dir: Optional[str] = None) -> str:
    return _call("screenshot", "screenshot", out_dir)


def browser_relay_enabled() -> bool:
    """True when the opt-in Chrome extension / native-host relay is on."""
    from .browser_relay import relay_enabled

    return relay_enabled()


def browser_relay_snapshot():
    """Last recorded relay snapshot, or None. Does not drive CDP."""
    from .browser_relay import last_snapshot

    return last_snapshot()

