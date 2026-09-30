"""Turn each reported model call into one ledger row."""

from __future__ import annotations

import uuid
from typing import Callable, Optional

from pmharness.drivers.metering import ProviderCall, register_sink

from .accounts import LOCAL, METERED, PLAN, resolve_route
from .rates import TokenClasses, price, rate_card
from .store import UsageEvent, UsageLedger


def event_from_call(call: ProviderCall, *, rates=rate_card) -> UsageEvent:
    route = resolve_route(call.driver, call.model, call.base_url)
    tokens = TokenClasses.split(
        tokens_in=call.tokens_in,
        tokens_out=call.tokens_out,
        cache_read=call.cache_read_tokens,
        cache_write=call.cache_write_tokens,
        cache_write_5m=call.cache_write_5m_tokens,
        cache_write_1h=call.cache_write_1h_tokens,
    )
    served = call.served_model if call.served_model and call.served_model != route.model else None
    lookup = rates(route.provider, route.model, candidates=(served,) if served else ())
    list_usd = price(tokens, lookup.card) if lookup.card else None
    reported = call.provider_cost_usd

    if route.billing == LOCAL:
        cash, basis = 0.0, "local"
    elif route.billing == PLAN:
        # A plan's usage is included; a charge the provider reports is overage.
        cash, basis = (reported, "reported") if reported else (0.0, "included")
    elif reported is not None:
        cash, basis = reported, "reported"
    elif list_usd is not None:
        cash, basis = list_usd, "computed"
    else:
        cash, basis = None, "unpriced"

    attr = call.attribution
    return UsageEvent(
        event_id=str(uuid.uuid4()),
        recorded_at=call.ended_at,
        session_id=attr.get("session_id") or None,
        turn=attr.get("turn"),
        purpose=str(attr.get("purpose") or "unattributed"),
        job_id=attr.get("job_id") or None,
        provider=route.provider,
        account=route.account,
        billing=route.billing if route.billing in (METERED, PLAN, LOCAL) else METERED,
        model=route.model or call.model,
        served_model=served,
        input_uncached=tokens.input_uncached,
        cache_read=tokens.cache_read,
        cache_write_5m=tokens.cache_write_5m,
        cache_write_1h=tokens.cache_write_1h,
        output=tokens.output,
        reasoning=call.reasoning_tokens,
        token_basis=call.token_basis,
        cash_usd=cash,
        list_usd=list_usd,
        cost_basis=basis,
        provider_cost_usd=reported,
        rate_source=lookup.source,
        rate_version=lookup.version,
        rates=lookup.card.to_dict() if lookup.card else None,
        error=bool(call.error),
    )


def install(ledger: UsageLedger) -> Callable[[], None]:
    """Record every model call this process makes. Returns an uninstall function."""

    def sink(call: ProviderCall) -> None:
        if not (call.tokens_in or call.tokens_out or call.provider_cost_usd):
            return  # nothing was consumed (e.g. a refused request)
        ledger.append(event_from_call(call))

    return register_sink(sink)


_installed: Optional[Callable[[], None]] = None


def install_for_state_dir(state_dir: str) -> None:
    """Idempotent process-wide install (server boot)."""
    global _installed
    from .store import ledger_for

    if _installed is None:
        _installed = install(ledger_for(state_dir))
