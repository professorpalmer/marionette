"""Shared Chrome auth session for the interactive harness.

GrokBot and Hermes keep one visible browser with a durable profile so the
user can complete login/Cloudflare and later workers reuse that session.
This module publishes the ``PM_BROWSER_*`` env contract that
``puppetmaster.browser_cdp`` reads:

- ``PM_BROWSER_USER_DATA_DIR`` -- durable profile (cookies stay on disk)
- ``PM_BROWSER_CDP_PORT`` -- shared debugging port; workers attach
- ``PM_BROWSER_HEADED`` -- visible window for interactive login

Default on for the desktop app; off in CI and pytest unless
``HARNESS_BROWSER_AUTH=1``. Never logs cookies or passwords.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

AUTH_ENV = "HARNESS_BROWSER_AUTH"
HEADED_ENV = "HARNESS_BROWSER_HEADED"
DEFAULT_CDP_PORT = "9333"
_TRUTHY = ("1", "true", "yes", "on")
_FALSY = ("0", "false", "no", "off")


def default_profile_dir() -> str:
    return str(Path.home() / ".pmharness" / "browser-profile")


def _is_ci_or_pytest() -> bool:
    if (os.environ.get("CI") or "").strip().lower() in _TRUTHY:
        return True
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    return False


def auth_env_enabled() -> bool:
    raw = (os.environ.get(AUTH_ENV) or "").strip().lower()
    if raw in _FALSY:
        return False
    if raw in _TRUTHY:
        return True
    return not _is_ci_or_pytest()


def headed_enabled() -> bool:
    raw = (os.environ.get(HEADED_ENV) or "").strip().lower()
    if raw in _FALSY:
        return False
    if raw in _TRUTHY:
        return True
    return False


def ensure_shared_browser_env(*, headed: Optional[bool] = None) -> dict:
    """Idempotently publish the shared CDP env. Returns the applied map.

    Best-effort: never raises. The harness process also marks itself as the
    Chrome janitor so worker atexit does not kill the shared window.
    """
    applied: dict = {}
    try:
        if not auth_env_enabled() and headed is None:
            return applied
        if headed is True:
            os.environ["PM_BROWSER_HEADED"] = "1"
        elif headed is False:
            os.environ["PM_BROWSER_HEADED"] = "0"
        elif headed_enabled() and "PM_BROWSER_HEADED" not in os.environ:
            os.environ["PM_BROWSER_HEADED"] = "1"
        if not (os.environ.get("PM_BROWSER_USER_DATA_DIR") or "").strip():
            path = default_profile_dir()
            Path(path).mkdir(parents=True, exist_ok=True)
            os.environ["PM_BROWSER_USER_DATA_DIR"] = path
        if not (os.environ.get("PM_BROWSER_CDP_PORT") or "").strip():
            os.environ["PM_BROWSER_CDP_PORT"] = DEFAULT_CDP_PORT
        applied = {
            "headed": os.environ.get("PM_BROWSER_HEADED", ""),
            "user_data_dir": os.environ.get("PM_BROWSER_USER_DATA_DIR", ""),
            "cdp_port": os.environ.get("PM_BROWSER_CDP_PORT", ""),
        }
        try:
            from puppetmaster import browser_cdp as engine
            setter = getattr(engine, "set_janitor", None)
            if callable(setter):
                setter(True)
        except Exception:
            pass
    except Exception:
        return applied
    return applied


def browser_auth_handoff(url: str) -> str:
    """Open ``url`` in the shared visible Chrome. Never return cookies."""
    target = (url or "").strip()
    if not target:
        return "auth handoff failed: url is required"
    ensure_shared_browser_env(headed=True)
    try:
        from . import browser as _browser
    except Exception as e:
        return "auth handoff failed: %s" % e
    engine = getattr(_browser, "_engine", None)
    if engine is not None:
        fn = getattr(engine, "auth_handoff", None)
        if callable(fn):
            try:
                result = fn(target)
            except Exception as e:
                return "auth handoff failed: %s: %s" % (type(e).__name__, e)
            if result is None:
                return "auth handoff failed: empty result from browser engine"
            return result if isinstance(result, str) else str(result)
    # Older puppetmaster-ai: env is published; navigate still uses the shared
    # profile/port even if the window stays headless until the engine is updated.
    nav = _browser.browser_navigate(target)
    port = os.environ.get("PM_BROWSER_CDP_PORT", DEFAULT_CDP_PORT)
    profile = os.environ.get("PM_BROWSER_USER_DATA_DIR", default_profile_dir())
    return (
        "%s\n\nAuth handoff: a harness Chrome session is open at that URL "
        "(port=%s, profile=%s). Complete login or Cloudflare in the visible "
        "window if one appeared. Do not paste passwords or cookies into chat. "
        "Workers attach to this same CDP port."
        % (nav, port, profile)
    )
