"""Pilot-facing browser tools -- thin wrapper over the CDP engine.

The real browser engine lives in ``puppetmaster.browser_cdp`` (a stdlib Chrome
DevTools Protocol driver) so BOTH the interactive pilot AND agentic swarm workers
drive the SAME browser through one code path. This module just re-exports the
engine functions under the ``browser_*`` names the pilot's action dispatch calls,
keeping a stable local import surface (``from harness import browser``).

Every function returns a STRING and never raises (the engine guarantees this).
If Puppetmaster's engine isn't importable for some reason, a clear message is
returned instead of crashing the turn.
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
    return _ENGINE_ERR or None


def browser_navigate(url: str) -> str:
    return _guard() or _engine.navigate(url)


def browser_snapshot() -> str:
    return _guard() or _engine.snapshot()


def browser_click(ref: str) -> str:
    return _guard() or _engine.click(ref)


def browser_type(ref: str, text: str) -> str:
    return _guard() or _engine.type_text(ref, text)


def browser_scroll(direction: str = "down") -> str:
    return _guard() or _engine.scroll(direction)


def browser_back() -> str:
    return _guard() or _engine.back()


def browser_get_text() -> str:
    return _guard() or _engine.get_text()


def browser_screenshot(out_dir: Optional[str] = None) -> str:
    return _guard() or _engine.screenshot(out_dir)
