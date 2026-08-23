"""One lightweight place for failures we deliberately swallow.

The rig degrades gracefully in a lot of spots -- a fetch fails, we fall back to
cache; a cache write fails, we carry on. That resilience is correct, but
``except Exception: pass`` also makes the *reason* vanish, which turns "why is my
model list empty?" into an unanswerable question. This module keeps the graceful
fallback while making the discarded cause inspectable, without pulling in a
logging framework or changing any control flow.

Stdlib-only. Never raises: diagnostics must not become a new failure mode.
"""
from __future__ import annotations

import logging
import os

_logger: logging.Logger | None = None


def _diag_dir() -> str:
    # Honor the test/state override so diagnostics don't leak into a real home
    # during tests; otherwise land next to the other pmharness state.
    state_dir = os.environ.get("HARNESS_STATE_DIR")
    return state_dir if state_dir else os.path.join(os.path.expanduser("~"), ".pmharness")


def _get_logger() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger
    lg = logging.getLogger("pmharness.diag")
    lg.propagate = False
    if not lg.handlers:
        try:
            base = _diag_dir()
            os.makedirs(base, exist_ok=True)
            handler: logging.Handler = logging.FileHandler(
                os.path.join(base, "diagnostics.log")
            )
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(message)s")
            )
            lg.addHandler(handler)
            lg.setLevel(logging.INFO)
        except Exception:
            # Can't open the file (read-only home, permissions) -> swallow, never
            # let diagnostics itself break the caller.
            lg.addHandler(logging.NullHandler())
    _logger = lg
    return lg


def note(where: str, exc: BaseException | None = None, msg: str = "") -> None:
    """Record a swallowed failure. ``where`` is a stable call-site label so the
    log is greppable; ``exc`` (if given) is rendered with repr for the real
    cause. Best-effort -- any failure here is itself swallowed."""
    try:
        from .correlation import get_correlation_id

        cid = get_correlation_id()
        prefix = f"[{cid}] " if cid else ""
        logger = _get_logger()
        if exc is not None:
            logger.warning("%s%s: %s%r", prefix, where, (msg + " " if msg else ""), exc)
        elif msg:
            logger.info("%s%s: %s", prefix, where, msg)
    except Exception:
        pass
