from __future__ import annotations

"""Stage 4 runner: budget-aware multi-turn with TURN-INDEXED findings substrate.
The substrate for turn N comes from episode.substrates[N], so the signal evolves
(inconclusive -> conclusive). A driver that genuinely READS the findings will
continue while inconclusive and stop when concluded; a pattern-follower won't.
"""

import time, uuid
from dataclasses import dataclass, field
from typing import Optional

from pmharness.intent import validate_intent, parse_intent_text, IntentError
from pmharness.bridge import execute_intent
from pmharness.eval_substrate import eval_intent, local_substrate
from pmharness.drivers.base import Driver, SYSTEM_PROMPT
from pmharness.episode_v2_runner import TrajectoryV2, TurnV2
from pmharness.episode_s4 import EpisodeS4
from harness.repair import drive_with_repair

HARD_CAP = 8


def _system(ep: EpisodeS4) -> str:
    return SYSTEM_PROMPT + f"""

MULTI-TURN LOOP, budget {ep.budget} swarm step(s). After each swarm you see its
findings. CRITICAL: read the findings. If they say more investigation is needed,
run another swarm narrowed to the open question. If they say a conclusion is
reached, STOP with a grounded rationale. Do not stop while findings are
inconclusive; do not keep swarming once they conclude. Emit ONLY the JSON object."""


def run_episode_s4(driver: Driver, ep: EpisodeS4) -> TrajectoryV2:
    turns = []; swarms = 0; tok = 0.0; lat = 0.0; err = ""; terminal = None
    context = ep.prompt
    system = _system(ep)
    for i in range(HARD_CAP):
        intent, resp, repairs = drive_with_repair(driver, context, system)
        tok += resp.tokens_out; lat += resp.latency_ms
        if intent is None:
            turns.append(TurnV2(i, None, resp.text, False, error=resp.error or "invalid",
                                tokens_out=resp.tokens_out, latency_ms=resp.latency_ms))
            err = resp.error or "invalid"; break
        turn = TurnV2(i, intent, resp.text, True, tokens_out=resp.tokens_out, latency_ms=resp.latency_ms)
        if intent.action in ("answer", "stop"):
            terminal = intent.action; turns.append(turn); break
        if swarms >= ep.budget:
            turns.append(turn); break
        try:
            with local_substrate():
                result = execute_intent(eval_intent(intent))
        except Exception as e:
            turn.error = f"execute: {e}"; turns.append(turn); err = turn.error; break
        turn.executed = result; turns.append(turn)
        # turn-indexed substrate: the signal for THIS swarm
        idx = min(swarms, len(ep.substrates) - 1) if ep.substrates else 0
        digest = ep.substrates[idx] if ep.substrates else (
            f"{result.num_artifacts} artifacts ({result.artifact_types}).")
        swarms += 1
        steps_left = ep.budget - swarms
        context = (f"{ep.prompt}\n\nPuppetmaster ran your swarm (job {result.job_id}).\n"
                   f"{digest}\n\nBudget remaining: {steps_left} step(s). Decide: stop "
                   f"(if findings concluded) or run_swarm narrowed (if inconclusive).")
    return TrajectoryV2(ep.id, ep.scenario, "n4", driver.name, turns,
                        terminated=terminal is not None, terminal_action=terminal,
                        swarms_run=swarms, budget=ep.budget, over_budget=swarms > ep.budget,
                        total_tokens_out=int(tok), total_latency_ms=round(lat, 1), error=err)


def new_s4_run_id() -> str:
    return f"s4_{int(time.time())}_{uuid.uuid4().hex[:6]}"
