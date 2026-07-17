"""Pilot hot-swap HTTP route body (peeled from ``harness.server``).

Owns ``GET /api/pilot`` model swap (idle rebuild or deferred stage while busy).
Auth/token gates stay on ``server.Handler``; this module never imports
``harness.server`` at top level. ``_perform_pilot_swap`` stays on the server
module (also used by idle ensure) and is injected via :class:`PilotServices`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Union


@dataclass
class PilotServices:
    """Explicit deps for pilot HTTP handlers (injected by ``server.py``)."""

    cfg: Any
    get_pilot: Callable[[], Any]
    apply_model_context_window: Callable[[], None]
    save_workspace_driver: Callable[[Any, str], None]
    perform_pilot_swap: Callable[[str], None]


JsonPayload = Union[dict, list]


def swap_pilot(model: str, svc: PilotServices) -> tuple[int, JsonPayload]:
    """Hot-swap the pilot model (or stage while a turn is busy).

    Preserves the in-flight conversation on idle rebuild via
    ``perform_pilot_swap``. Hermes-style mid-turn: while streaming we stage
    ``cfg.driver`` + workspace drivers and return ``deferred: true`` without
    touching the live pilot object.
    """
    if not model:
        return 400, {"error": "model required"}
    pilot = svc.get_pilot()
    busy = (
        getattr(pilot, "_busy", None) is not None
        and pilot._busy.locked()
    )
    if busy:
        # Stage preference only -- do not touch the live pilot object.
        svc.cfg.driver = model
        svc.apply_model_context_window()
        svc.save_workspace_driver(svc.cfg.repo, model)
        return 200, {"ok": True, "driver": model, "deferred": True}
    try:
        svc.perform_pilot_swap(model)
        return 200, {"ok": True, "driver": model, "deferred": False}
    except Exception as e:
        return 500, {"error": str(e)}


def get_pilot_swap(model: str, svc: PilotServices) -> tuple[int, JsonPayload]:
    """GET /api/pilot — hot-swap (or defer) the pilot model."""
    return swap_pilot(model, svc)
