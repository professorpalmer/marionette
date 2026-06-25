from __future__ import annotations

"""Intent repair: make ANY model a usable driver, including verbose reasoning
models that wrap or prepend prose around their JSON.

Stage 2 found Kimi (a reasoning model) scored 70% single-turn purely because it
emitted chain-of-thought instead of bare JSON -- a harness gap, not a capability
ceiling. This closes that gap: on an unparseable/invalid intent, re-prompt ONCE
with a strict correction. One retry is the right ceiling -- if a model can't
emit valid JSON given the schema AND a correction, it is genuinely unfit to
drive, and we surface that honestly rather than looping.
"""

from typing import Optional

from pmharness.intent import validate_intent, parse_intent_text, IntentError, DriverIntent
from pmharness.drivers.base import Driver, DriverResponse


_REPAIR_SUFFIX = (
    "\n\nYour previous reply was not a valid DriverIntent. "
    "Reply with ONLY a single JSON object, no prose, no code fences, matching:\n"
    '{"action": "run_swarm|answer|stop", "goal": "<required for run_swarm>", '
    '"rationale": "<one line>"}\n'
    "Previous reply was:\n"
)


def drive_with_repair(
    driver: Driver, prompt: str, system: str, *, max_repairs: int = 1
) -> tuple[Optional[DriverIntent], DriverResponse, int]:
    """Call the driver; if the output is not a valid intent, re-prompt up to
    max_repairs times with a strict correction. Returns
    (intent_or_None, last_response, repairs_used). Token accounting accumulates
    across attempts so the cost of repair is visible, not hidden.
    """
    total_in = 0
    total_out = 0
    total_lat = 0.0
    last: Optional[DriverResponse] = None
    ctx = prompt

    for attempt in range(max_repairs + 1):
        resp = driver.complete(ctx, system=system)
        last = resp
        total_in += resp.tokens_in
        total_out += resp.tokens_out
        total_lat += resp.latency_ms
        if resp.error:
            # transport error -- repair can't help; surface immediately
            break
        try:
            intent = validate_intent(parse_intent_text(resp.text))
            # success -- return with accumulated accounting
            merged = DriverResponse(
                text=resp.text, tokens_in=total_in, tokens_out=total_out,
                latency_ms=total_lat, model=resp.model,
                meta={**(resp.meta or {}), "repairs_used": attempt},
            )
            return intent, merged, attempt
        except IntentError:
            if attempt < max_repairs:
                ctx = prompt + _REPAIR_SUFFIX + (resp.text or "")[:500]
            continue

    merged = DriverResponse(
        text=last.text if last else "", tokens_in=total_in, tokens_out=total_out,
        latency_ms=total_lat, model=last.model if last else "",
        error=(last.error if last and last.error else "invalid intent after repair"),
        meta={"repairs_used": max_repairs},
    )
    return None, merged, max_repairs
