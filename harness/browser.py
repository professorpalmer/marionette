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

from typing import Optional

try:
    from puppetmaster import browser_cdp as _engine
    _ENGINE_ERR = ""
except Exception as _e:  # pragma: no cover - engine should always be importable
    _engine = None
    _ENGINE_ERR = f"browser engine unavailable: {_e}"


def _guard() -> Optional[str]:
    if _ENGINE_ERR:
        return _ENGINE_ERR
    if _engine is None:
        return "browser engine unavailable"
    return None


def _call(op_name: str, method_name: str, *args, **kwargs) -> str:
    """Run a CDP engine op; always return a string, never raise on the chat path."""
    err = _guard()
    if err:
        return err
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
