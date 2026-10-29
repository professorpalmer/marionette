from __future__ import annotations

"""Driver eval swarms run on Puppetmaster's local deterministic substrate.

The eval measures driving, not working (AGENTS.md). The product bridge refuses
the local substrate unless HARNESS_ALLOW_DEMO_SWARM=1, and it routes swarms to
real workers (agentic keys, or the plan-billed Cursor adapter) whenever a repo
is in play: the process-wide HARNESS_REPO, or a "repo" field in the driver's own
intent JSON. Without these guards a live driver eval either has every swarm
refused (swarms_run stays 0 and a good driver scores as premature) or spends
real worker calls. Both runners wrap execute_intent with them.
"""

import contextlib
import dataclasses
import os
from typing import Iterator

_FLAG = "HARNESS_ALLOW_DEMO_SWARM"
_REPO = "HARNESS_REPO"


@contextlib.contextmanager
def local_substrate() -> Iterator[None]:
    """Opt into the local substrate and drop the repo pointer for one eval swarm; restore both after."""
    saved = {k: os.environ.get(k) for k in (_FLAG, _REPO)}
    os.environ[_FLAG] = "1"
    os.environ.pop(_REPO, None)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def eval_intent(intent):
    """The intent without a driver-supplied repo (it would pin real workers to that path)."""
    if getattr(intent, "repo", None):
        return dataclasses.replace(intent, repo=None)
    return intent
