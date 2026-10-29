from __future__ import annotations

"""Stage 3.5 runner: budget-aware multi-turn driving with a substantive findings
substrate fed back after each swarm.
"""

from dataclasses import dataclass, field
from typing import Optional

from .intent import validate_intent, parse_intent_text, IntentError, DriverIntent
from .bridge import execute_intent, BridgeResult
from .eval_substrate import eval_intent, local_substrate
from .drivers.base import Driver, SYSTEM_PROMPT
from .episode_v2 import EpisodeV2


TERMINAL_ACTIONS = ("answer", "stop")
HARD_TURN_CAP = 8   # absolute guard above any budget


def _system_for(ep: EpisodeV2) -> str:
    return SYSTEM_PROMPT + f"""

MULTI-TURN LOOP WITH A BUDGET.
You have an orchestration budget of {ep.budget} swarm step(s) for this task.
Each run_swarm consumes one step. After a swarm you will be shown its findings
digest. Rules:
  - If the findings answer the objective, emit action="stop" with a one-line
    rationale referencing what was established. Do NOT spend more budget.
  - Never re-run a swarm whose findings already concluded the objective.
  - Going over budget, or looping the same swarm, is a failure.
  - For trivial questions, action="answer" immediately (zero budget).
Emit ONLY the JSON object each turn."""


@dataclass
class TurnV2:
    index: int
    intent: Optional[DriverIntent]
    raw_text: str
    valid: bool
    error: str = ""
    executed: Optional[BridgeResult] = field(default=None, repr=False)
    tokens_out: int = 0
    latency_ms: float = 0.0


@dataclass
class TrajectoryV2:
    episode_id: str
    scenario: str
    variant: str
    model: str
    turns: list
    terminated: bool
    terminal_action: Optional[str]
    swarms_run: int
    budget: int
    over_budget: bool
    total_tokens_out: int
    total_latency_ms: float
    error: str = ""


def _feedback(ep: EpisodeV2, result: BridgeResult, steps_left: int) -> str:
    # Real PM ran (seam proven); the substantive signal is the episode's fixture.
    digest = ep.findings_substrate or (
        f"Puppetmaster produced {result.num_artifacts} artifacts "
        f"({result.artifact_types})."
    )
    return (
        f"Puppetmaster ran your swarm (job {result.job_id}).\n{digest}\n\n"
        f"Budget remaining: {steps_left} swarm step(s). "
        f"Decide the next step (stop with a grounded rationale, or run_swarm only "
        f"if genuinely unresolved)."
    )


def run_episode_v2(driver: Driver, ep: EpisodeV2) -> TrajectoryV2:
    turns: list = []
    swarms = 0
    tok = 0
    lat = 0.0
    err = ""
    terminal = None
    context = ep.prompt
    system = _system_for(ep)

    for i in range(HARD_TURN_CAP):
        resp = driver.complete(context, system=system)
        tok += resp.tokens_out
        lat += resp.latency_ms
        if resp.error:
            turns.append(TurnV2(i, None, "", False, error=resp.error,
                                tokens_out=resp.tokens_out, latency_ms=resp.latency_ms))
            err = resp.error
            break
        try:
            intent = validate_intent(parse_intent_text(resp.text))
            valid, t_err = True, ""
        except IntentError as e:
            intent, valid, t_err = None, False, str(e)
        turn = TurnV2(i, intent, resp.text, valid, error=t_err,
                      tokens_out=resp.tokens_out, latency_ms=resp.latency_ms)
        if not valid:
            turns.append(turn); break
        if intent.action in TERMINAL_ACTIONS:
            terminal = intent.action
            turns.append(turn); break
        # run_swarm
        try:
            with local_substrate():
                result = execute_intent(eval_intent(intent))
        except Exception as e:
            turn.error = f"execute: {e}"; turns.append(turn); err = turn.error; break
        turn.executed = result
        swarms += 1
        turns.append(turn)
        steps_left = ep.budget - swarms
        context = ep.prompt + "\n\n" + _feedback(ep, result, steps_left)

    return TrajectoryV2(
        episode_id=ep.id, scenario=ep.scenario, variant=ep.variant,
        model=driver.name, turns=turns,
        terminated=terminal is not None, terminal_action=terminal,
        swarms_run=swarms, budget=ep.budget, over_budget=swarms > ep.budget,
        total_tokens_out=tok, total_latency_ms=round(lat, 1), error=err,
    )
