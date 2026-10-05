from __future__ import annotations

"""Per-call sizing reminder: solo until the projected falloff, then hand off.

The pilot's session todos are the plan; tasks prefixed ``[parallel]`` are
independent units. Puppetmaster's sizing gate projects the solo finish from
measured pace and context fill, and on a projected overrun this provider
asks the pilot, once per turn, to hand the remaining independent units to a
run_flow map. Request-only: nothing is written to history.
"""

import time
from typing import Any, Optional

from .system_reminder import SystemReminder, default_registry

_DONE = ("completed", "abandoned")


def _plan(session: Any) -> list:
    units = []
    for phase in getattr(session, "_todo_phases", None) or []:
        for task in getattr(phase, "tasks", None) or []:
            status = "done" if getattr(task, "status", "") in _DONE else "pending"
            units.append({"content": str(getattr(task, "content", "") or ""), "status": status})
    return units


def _context_frac(session: Any) -> float:
    try:
        window = int(getattr(getattr(session, "config", None), "max_context_tokens", 0) or 0)
        used = int(getattr(session, "_last_prompt_tokens", 0) or 0)
        return used / window if window > 0 and used > 0 else 0.0
    except Exception:
        return 0.0


def sizing_provider(session: Any) -> Optional[SystemReminder]:
    if getattr(getattr(session, "config", None), "no_delegation", False):
        return None
    started = getattr(session, "_busy_since", None)
    if not started or getattr(session, "_sizing_advised_turn", None) == started:
        return None
    plan = _plan(session)
    if sum(item["content"].lstrip().lower().startswith("[parallel]") for item in plan) < 2:
        return None
    from pmharness.bridge import sizing_decision

    model = str(getattr(getattr(session, "pilot", None), "model", "") or "")
    decision = sizing_decision(plan, elapsed_s=time.monotonic() - started,
                               context_frac=_context_frac(session), model=model)
    if not decision or not decision.get("advice"):
        return None
    session._sizing_advised_turn = started
    session._last_sizing_decision = decision
    return SystemReminder(provider_id="sizing", text=decision["advice"])


default_registry().register(sizing_provider)
