"""Optional split of a long tool loop into shorter segments for local models.

Off by default. A fixed step count ignores the context window: a 1M-token
local pilot was compacted at about 30k tokens, every 24 steps. Set
``HARNESS_LOCAL_TURN_SPLIT_STEPS`` to N to compact a local pilot's turn every
N provider steps. The residual keeps the user's requests verbatim
(``harness.working_set``). Cloud models never split: a mid-turn rewrite busts
their prompt cache.
"""
from __future__ import annotations

import os

DEFAULT_LOCAL_SPLIT_STEPS = 0
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
