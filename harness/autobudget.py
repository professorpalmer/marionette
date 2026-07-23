from __future__ import annotations

"""AutoBudget: the safety governor for Fully-Auto (unattended) mode.

Unattended autonomy = "allow all" with no human in the loop. That is exactly
where runaway token spend and confused loops happen (we watched qwen grind 7
swarms on bad substrate while supervised). So the governor is built and tested
BEFORE the autonomy it guards -- the brakes go in before the engine.

Three hard ceilings + a killswitch + a tripwire:
  - max_tokens     : cumulative driver tokens_out across the run
  - max_seconds    : wall-clock since the run started
  - max_swarms     : total swarms dispatched
  - killswitch     : a stop-file path; if it exists, halt immediately (the user
                     can `touch` it from anywhere to stop an overnight run)
  - max_idle_steps : consecutive pilot steps with no NEW findings -> stall halt
                     (stops a confused loop burning budget on nothing)

check() returns None to proceed or a string reason to HALT. The governor never
trusts the model to stop itself; it is enforced by the loop around the model.
"""

import os
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AutoBudget:
    # Tree-wide ceiling for full-auto / ambient governors. 50k starved even a
    # single native analysis worker; keep room for multi-worker swarms.
    max_tokens: int = 500_000
    max_seconds: int = 3600          # 1 hour default
    max_swarms: int = 20
    max_idle_steps: int = 3          # consecutive no-new-finding steps before halt
    killswitch_path: str = ""        # touch this file to stop a run

    # live counters (mutated by the loop)
    tokens_used: int = field(default=0)
    swarms_used: int = field(default=0)
    idle_steps: int = field(default=0)
    started_at: float = field(default_factory=time.time)
    _halted_reason: Optional[str] = field(default=None)

    # Shared-ceiling propagation. When a governor spawns a sub-agent tree
    # (pilot -> swarm -> worker), nesting must NOT reset the ceiling: each
    # child links back to the parent so its spend rolls up into the SAME
    # cumulative counters. Otherwise a deep spawn tree blows the overall
    # ceiling because every level starts a fresh 0/max budget.
    #
    # ``parent`` is not part of ``__eq__`` / ``repr`` so budgets stay simple
    # value objects for the existing tests, and it is excluded from the
    # dataclass-generated fields' comparison to avoid reference cycles in repr.
    parent: Optional["AutoBudget"] = field(
        default=None, repr=False, compare=False
    )

    def start(self) -> "AutoBudget":
        self.started_at = time.time()
        self.tokens_used = 0
        self.swarms_used = 0
        self.idle_steps = 0
        self._halted_reason = None
        return self

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at

    def add_tokens(self, n: int) -> None:
        amount = max(0, int(n or 0))
        self.tokens_used += amount
        # Roll spend up the spawn tree so nested sub-agents share ONE ceiling
        # that never resets. Walk parents defensively (a mis-wired cycle would
        # otherwise loop forever).
        node = self.parent
        seen = {id(self)}
        while node is not None and id(node) not in seen:
            node.tokens_used += amount
            seen.add(id(node))
            node = node.parent

    def add_swarm(self) -> None:
        self.swarms_used += 1
        node = self.parent
        seen = {id(self)}
        while node is not None and id(node) not in seen:
            node.swarms_used += 1
            seen.add(id(node))
            node = node.parent

    def child(self) -> "AutoBudget":
        """Return a budget that shares THIS budget's ceiling for a spawned
        sub-agent. Threading the child down a pilot->swarm->worker tree keeps
        the whole tree under one cumulative cap that never resets per level
        (the arcgentica bounded_submit_action pattern).

        The child forwards its ``add_tokens`` / ``add_swarm`` to this parent,
        so the parent's ``tokens_used`` / ``swarms_used`` (and therefore
        ``check()``) see the child's spend. The child inherits the parent's
        ceilings and killswitch, and its ``check()`` also honours the parent's
        already-tripped halt. Its own local counters start at the parent's
        current totals so the child sees spend that happened before it existed
        (a second sequential worker sees the first worker's spend).
        """
        c = AutoBudget(
            max_tokens=self.max_tokens,
            max_seconds=self.max_seconds,
            max_swarms=self.max_swarms,
            max_idle_steps=self.max_idle_steps,
            killswitch_path=self.killswitch_path,
            parent=self,
        )
        # Share the parent's clock and current totals so the child inherits the
        # tree-wide position (elapsed time and already-spent tokens/swarms).
        c.started_at = self.started_at
        c.tokens_used = self.tokens_used
        c.swarms_used = self.swarms_used
        return c

    def note_findings(self, new_count: int) -> None:
        """Track stall: a step that produced no new findings increments idle."""
        if new_count > 0:
            self.idle_steps = 0
        else:
            self.idle_steps += 1

    def killed(self) -> bool:
        return bool(self.killswitch_path) and os.path.exists(self.killswitch_path)

    def check(self) -> Optional[str]:
        """Return a HALT reason, or None to proceed. Checked every loop step."""
        if self._halted_reason:
            return self._halted_reason
        # A child honours its parent's already-tripped halt: once any node in
        # the spawn tree hits the shared ceiling, the whole tree stops.
        if self.parent is not None:
            node = self.parent
            seen = {id(self)}
            while node is not None and id(node) not in seen:
                if node._halted_reason:
                    return self._halt(node._halted_reason)
                seen.add(id(node))
                node = node.parent
        if self.killed():
            return self._halt(f"killswitch tripped ({self.killswitch_path})")
        if self.tokens_used >= self.max_tokens:
            return self._halt(f"token ceiling reached ({self.tokens_used}/{self.max_tokens})")
        if self.elapsed >= self.max_seconds:
            return self._halt(f"time ceiling reached ({int(self.elapsed)}s/{self.max_seconds}s)")
        if self.swarms_used >= self.max_swarms:
            return self._halt(f"swarm ceiling reached ({self.swarms_used}/{self.max_swarms})")
        if self.idle_steps >= self.max_idle_steps:
            return self._halt(f"stall: {self.idle_steps} steps with no new findings")
        return None

    def _halt(self, reason: str) -> str:
        self._halted_reason = reason
        return reason

    def snapshot(self) -> dict:
        return {
            "tokens_used": self.tokens_used, "max_tokens": self.max_tokens,
            "swarms_used": self.swarms_used, "max_swarms": self.max_swarms,
            "elapsed_s": int(self.elapsed), "max_seconds": self.max_seconds,
            "idle_steps": self.idle_steps, "max_idle_steps": self.max_idle_steps,
            "halted": self._halted_reason,
        }

    @classmethod
    def from_env(cls) -> "AutoBudget":
        def _i(name, default):
            try:
                return int(os.environ.get(name, "").strip() or default)
            except ValueError:
                return default
        return cls(
            max_tokens=_i("HARNESS_AUTO_MAX_TOKENS", 500_000),
            max_seconds=_i("HARNESS_AUTO_MAX_SECONDS", 3600),
            max_swarms=_i("HARNESS_AUTO_MAX_SWARMS", 20),
            max_idle_steps=_i("HARNESS_AUTO_MAX_IDLE", 3),
            killswitch_path=os.environ.get("HARNESS_AUTO_KILLSWITCH", "").strip(),
        )
