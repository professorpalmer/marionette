"""The views every cost surface reads. Pure functions over ledger rows.

Headline semantics (the same everywhere in the UI):

- ``spent_usd``: cash that left the wallet. Metered calls plus any plan
  overage. ``spent_exact`` is True only when every contributing row carries
  the provider's reported cost.
- ``plan``: usage included in subscriptions: tokens, calls, and what it would
  have cost at list price (``list_usd``). Never added to spend.
- ``local``: calls to the user's own model servers ($0).
- ``unpriced_calls``: metered calls with no reported cost and no published
  rate. Surfaced, never counted as $0.

Breakdowns group the same rows, so their totals always equal the headline.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from .accounts import LOCAL, METERED, PLAN
from .store import UsageEvent


def _bucket() -> dict:
    return {
        "calls": 0, "input_uncached": 0, "cache_read": 0, "cache_write": 0, "output": 0,
        "cash_usd": 0.0, "list_usd": 0.0, "list_unpriced_calls": 0, "unpriced_calls": 0,
        "reported_calls": 0,
    }


def _add(b: dict, e: UsageEvent) -> None:
    b["calls"] += 1
    b["input_uncached"] += e.input_uncached
    b["cache_read"] += e.cache_read
    b["cache_write"] += e.cache_write_5m + e.cache_write_1h
    b["output"] += e.output
    if e.cash_usd is not None:
        b["cash_usd"] += e.cash_usd
    if e.cost_basis == "unpriced":
        b["unpriced_calls"] += 1
    if e.cost_basis == "reported":
        b["reported_calls"] += 1
    if e.list_usd is not None:
        b["list_usd"] += e.list_usd
    else:
        b["list_unpriced_calls"] += 1


def _finish(b: dict) -> dict:
    prompt = b["input_uncached"] + b["cache_read"] + b["cache_write"]
    b["tokens"] = prompt + b["output"]
    b["cache_hit"] = (b["cache_read"] / prompt) if prompt else None
    b["cash_usd"] = round(b["cash_usd"], 6)
    b["list_usd"] = round(b["list_usd"], 6)
    return b


def summarize(events: Iterable[UsageEvent]) -> dict:
    rows = list(events)
    total, plan, local = _bucket(), _bucket(), _bucket()
    spend_rows = 0
    spend_reported = 0
    by_route: dict[tuple, dict] = defaultdict(_bucket)
    by_purpose: dict[str, dict] = defaultdict(_bucket)
    by_job: dict[str, dict] = defaultdict(_bucket)
    for e in rows:
        _add(total, e)
        _add(by_route[(e.billing, e.provider, e.model)], e)
        _add(by_purpose[e.purpose], e)
        if e.job_id:
            _add(by_job[e.job_id], e)
        if e.billing == PLAN:
            _add(plan, e)
        elif e.billing == LOCAL:
            _add(local, e)
        is_spend = e.billing == METERED or (e.billing == PLAN and e.cost_basis == "reported")
        if is_spend and e.cash_usd is not None:
            spend_rows += 1
            spend_reported += e.cost_basis == "reported"

    spent = round(sum(e.cash_usd for e in rows if e.cash_usd is not None and e.billing != LOCAL), 6)
    totals = _finish(total)
    return {
        "calls": len(rows),
        "spent_usd": spent,
        "spent_exact": spend_rows > 0 and spend_rows == spend_reported,
        "spent_confidence": (
            "none" if spend_rows == 0
            else "reported" if spend_rows == spend_reported
            else "computed" if spend_reported == 0 else "mixed"
        ),
        "unpriced_calls": total["unpriced_calls"],
        "tokens": totals["tokens"],
        "cache_hit": totals["cache_hit"],
        "plan": _finish(plan),
        "local": _finish(local),
        "by_route": [
            {"billing": k[0], "provider": k[1], "model": k[2], **_finish(v)}
            for k, v in sorted(by_route.items(), key=lambda kv: -kv[1]["cash_usd"] - kv[1]["list_usd"])
        ],
        "by_purpose": {k: _finish(v) for k, v in sorted(by_purpose.items())},
        "by_job": {k: _finish(v) for k, v in sorted(by_job.items())},
        "since": min((e.recorded_at for e in rows), default=None),
    }


def session_summary(state_dir: str, session_id: str) -> dict:
    from .store import ledger_for

    return summarize(ledger_for(state_dir).events(session_id=session_id))


def period_spend(state_dir: str, account: str, since: float) -> float:
    """Known cash spent on ``account`` since ``since`` (unpriced calls excluded)."""
    from .store import ledger_for

    rows = ledger_for(state_dir).events(account=account, since=since)
    return round(sum(e.cash_usd or 0.0 for e in rows), 6)


def ledger_view(state_dir: str, session_id: str, job_reports: Iterable[tuple] = ()) -> dict:
    """One session's accounting: its model calls plus its swarm jobs' workers."""
    from .store import ledger_for
    from .swarm import swarm_events

    events = ledger_for(state_dir).events(session_id=session_id)
    events += list(swarm_events(session_id, job_reports))
    view = summarize(events)
    view["session_id"] = session_id
    return view
