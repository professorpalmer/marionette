"""Swarm workers' spend as ledger rows.

Workers run in Puppetmaster processes, which keep their own priced per-task
ledger (the job cost report). Each task becomes one row here so a session's
views cover pilot and workers alike. Puppetmaster knows how each worker was
billed (plan or API) and prices API tasks from its registry; a task it could
not price stays unpriced, never $0.
"""

from __future__ import annotations

from typing import Any, Iterable, Iterator, Optional, Tuple

from .accounts import METERED, PLAN
from .rates import TokenClasses
from .store import UsageEvent


def _num(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out >= 0 else None


def _provider_of(model_id: str) -> str:
    """Registry ids look like 'agentic/moonshotai/kimi-k3' or 'cursor/grok-4-6'."""
    return (model_id.split("/", 1)[0] if "/" in model_id else model_id) or "worker"


def swarm_events(session_id: str, job_reports: Iterable[Tuple[str, dict]]) -> Iterator[UsageEvent]:
    for job_id, report in job_reports:
        actual = (report or {}).get("actual_cost") or {}
        for index, task in enumerate(actual.get("tasks") or ()):
            model = str(task.get("model_id") or "unknown")
            plan = str(task.get("billing") or "").lower() == PLAN
            tokens = TokenClasses.split(
                tokens_in=int(task.get("tokens_in") or 0),
                tokens_out=int(task.get("tokens_out") or 0),
                cache_read=int(task.get("cache_read_tokens") or 0),
                cache_write=int(task.get("cache_write_tokens") or 0),
            )
            marginal = _num(task.get("marginal_cost_usd"))
            if plan:
                cash, basis = 0.0, "included"
            elif marginal is not None and task.get("priced", True):
                cash, basis = marginal, "computed"
            else:
                cash, basis = None, "unpriced"
            yield UsageEvent(
                event_id=f"pm:{job_id}:{task.get('task_id') or index}",
                recorded_at=0.0,
                session_id=session_id,
                turn=None,
                purpose="swarm",
                job_id=job_id,
                provider=_provider_of(model),
                account=_provider_of(model),
                billing=PLAN if plan else METERED,
                model=model,
                served_model=None,
                input_uncached=tokens.input_uncached,
                cache_read=tokens.cache_read,
                cache_write_5m=tokens.cache_write_5m,
                cache_write_1h=tokens.cache_write_1h,
                output=tokens.output,
                reasoning=0,
                token_basis="estimated" if task.get("tokens_estimated") else "provider",
                cash_usd=cash,
                list_usd=_num(task.get("api_equivalent_cost_usd")),
                cost_basis=basis,
                provider_cost_usd=None,
                rate_source="puppetmaster",
                rate_version=None,
                rates=None,
            )
