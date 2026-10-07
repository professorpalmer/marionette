"""Split a long tool loop into shorter segments for local models.

A local model loses track of its task deep in one long turn: the serving
rig's review found a 50-call turn decaying where 25-call turns stay strong.
Every ``local_turn_split_steps`` provider steps, the loop compacts the turn's
earlier tool rounds (the catalog residual keeps the user's request and the
recent tail), so each segment starts inside the depth where the model is
reliable. Cloud models keep one unbroken turn: a mid-turn rewrite busts
their prompt cache, and they do not decay at this depth.
"""
from __future__ import annotations

import os

DEFAULT_LOCAL_SPLIT_STEPS = 24
SPLIT_ENV = "HARNESS_LOCAL_TURN_SPLIT_STEPS"


def local_turn_split_steps(session) -> int:
    """Steps per segment for this session's pilot; 0 means never split."""
    from .compaction_mixin import session_uses_local_pilot

    if not session_uses_local_pilot(session):
        return 0
    raw = (os.environ.get(SPLIT_ENV) or "").strip()
    try:
        steps = int(raw) if raw else DEFAULT_LOCAL_SPLIT_STEPS
    except ValueError:
        steps = DEFAULT_LOCAL_SPLIT_STEPS
    return max(steps, 0)


def split_due(step: int, every: int) -> bool:
    return every > 0 and step > 0 and step % every == 0
