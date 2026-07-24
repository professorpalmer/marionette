"""Session / job cost math and catalog price resolution.

Owns prompt-cache multipliers, deterministic ``_session_cost`` / ``_job_cost``
helpers, cost-source labels, and active-runner price resolution. Boot meters
and swarm accounting live in sibling modules; ``harness.api.cost`` re-exports
the historical surface.
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

# Prompt-cache FALLBACK multipliers (used only when the provider did not return
# usage.cost). OpenRouter billed USD is preferred whenever present.
# Anthropic/Bedrock published rates: reads ~0.1x, 5m writes 1.25x, 1h writes 2x.
# OpenAI/Gemini implicit cache is usually read-only (write bucket stays 0).
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0
# Undifferentiated cache_write_tokens (no TTL split) billed at the 5m write rate.
CACHE_WRITE_MULTIPLIER = CACHE_WRITE_5M_MULTIPLIER

# Wire values for price/cost provenance (backward-compatible additions).
# live = OpenRouter /models rates; static = eval catalog; default = hardcoded
# 0.5/2.0 fallback; provider = billed usage.cost (cost_source, not price ladder).
PRICE_SOURCE_LIVE = "live"
PRICE_SOURCE_STATIC = "static"
PRICE_SOURCE_DEFAULT = "default"
CACHE_SAVINGS_CATALOG = "catalog"
CACHE_SAVINGS_CAPPED = "capped"
CACHE_SAVINGS_UNKNOWN = "unknown"


def _normalize_price_source(src: Optional[str]) -> str:
    """Map registry source labels onto the public wire vocabulary.

    ``live_alias`` collapses to ``live``; ``catalog`` becomes ``static``.
    Unresolved / missing → ``default``.
    """
    if not src:
        return PRICE_SOURCE_DEFAULT
    key = str(src).strip().lower()
    if key in ("live", "live_alias"):
        return PRICE_SOURCE_LIVE
    if key in ("catalog", "static"):
        return PRICE_SOURCE_STATIC
    if key == "default":
        return PRICE_SOURCE_DEFAULT
    return PRICE_SOURCE_DEFAULT


def _spend_is_estimated(cost_source: str, price_source: str = "") -> bool:
    """True when the dollar figure is not a full provider receipt.

    Provider-billed spend stays non-estimated even if display rates fell back
    to defaults (those rates are unused for the billed total). Default rates
    with a catalog/estimate path always mark the amount estimated.
    """
    src = (cost_source or "").strip().lower()
    if src == "provider":
        return False
    if (price_source or "").strip().lower() == PRICE_SOURCE_DEFAULT:
        return True
    return src != "provider"


def _session_cost(
    t_in: float,
    t_out: float,
    cached: float,
    price_in: float,
    price_out: float,
    cache_write: float = 0.0,
    cache_write_5m: float = 0.0,
    cache_write_1h: float = 0.0,
) -> float:
    """Deterministic session cost from tokens + per-Mtok prices.

    ``t_in`` is the FULL prompt token total (uncached + cache read + cache
    write). Cache-read / cache-write buckets are peeled out of that total and
    billed at their multipliers; the remainder is full-price input. Falls back
    to pricing the combined total at ``price_out`` when no in/out split is
    available (completion dominates cost, so this is the least-wrong
    single-rate estimate)."""
    t_in = float(t_in or 0.0)
    t_out = float(t_out or 0.0)
    cached = max(0.0, float(cached or 0.0))
    w5 = max(0.0, float(cache_write_5m or 0.0))
    w1 = max(0.0, float(cache_write_1h or 0.0))
    w_u = max(0.0, float(cache_write or 0.0))
    if w5 or w1:
        split = w5 + w1
        # Prefer TTL splits; drop overlapping undifferentiated write totals.
        if w_u <= split + 0.5:
            w_u = 0.0
        else:
            w_u = max(0.0, w_u - split)
    if t_in or t_out or cached or w5 or w1 or w_u:
        cached = min(cached, t_in)
        remain = max(0.0, t_in - cached)
        w1 = min(w1, remain)
        remain -= w1
        w5 = min(w5, remain)
        remain -= w5
        w_u = min(w_u, remain)
        remain -= w_u
        uncached_in = remain
        return (
            (uncached_in / 1.0e6) * price_in
            + (cached / 1.0e6) * price_in * CACHE_READ_MULTIPLIER
            + (w5 / 1.0e6) * price_in * CACHE_WRITE_5M_MULTIPLIER
            + (w1 / 1.0e6) * price_in * CACHE_WRITE_1H_MULTIPLIER
            + (w_u / 1.0e6) * price_in * CACHE_WRITE_MULTIPLIER
            + (t_out / 1.0e6) * price_out
        )
    # No split tracked: price the combined total at the output rate.
    total = t_in + t_out
    return (total / 1.0e6) * price_out


def _pilot_write_buckets(pilot: Any) -> tuple:
    """Return (cache_write, write_5m, write_1h) meters for a pilot-like object."""
    return (
        int(getattr(pilot, "_tokens_cache_write", 0) or 0),
        int(getattr(pilot, "_tokens_cache_write_5m", 0) or 0),
        int(getattr(pilot, "_tokens_cache_write_1h", 0) or 0),
    )


def _session_cost_split(pilot: Any, price_in: float, price_out: float) -> float:
    """Session cost that prices PILOT tokens at the pilot rate and ADDS
    delegated-worker dollars (already priced at each worker's own model rate).

    Worker tokens are folded into the pilot's _tokens_* meters for display, but
    pricing them at the pilot rate under-reports cost when a worker ran on a
    pricier model (e.g. opus at $5/$25 vs a cheap pilot). So we subtract the
    worker token split from the pilot-priced portion and add _worker_cost_usd.

    When the pilot accumulated OpenRouter (or similar) ``usage.cost`` into
    ``_provider_cost_usd``, that billed USD is ground truth for the covered
    token slice; any remaining uncovered pilot tokens fall back to the
    cache-aware catalog estimate. getattr defaults keep OLD sessions (no
    worker / provider split) identical to before."""
    t_in = int(getattr(pilot, "_tokens_in", 0) or 0)
    t_out = int(getattr(pilot, "_tokens_out", 0) or 0)
    t_cached = int(getattr(pilot, "_tokens_cached", 0) or 0)
    t_write, t_write_5m, t_write_1h = _pilot_write_buckets(pilot)
    w_in = int(getattr(pilot, "_worker_tokens_in", 0) or 0)
    w_out = int(getattr(pilot, "_worker_tokens_out", 0) or 0)
    w_cost = float(getattr(pilot, "_worker_cost_usd", 0.0) or 0.0)
    provider_cost = float(getattr(pilot, "_provider_cost_usd", 0.0) or 0.0)
    billed_in = int(getattr(pilot, "_provider_billed_tokens_in", 0) or 0)
    billed_out = int(getattr(pilot, "_provider_billed_tokens_out", 0) or 0)
    billed_cached = int(getattr(pilot, "_provider_billed_tokens_cached", 0) or 0)
    billed_write = int(getattr(pilot, "_provider_billed_tokens_cache_write", 0) or 0)
    billed_write_5m = int(getattr(pilot, "_provider_billed_tokens_cache_write_5m", 0) or 0)
    billed_write_1h = int(getattr(pilot, "_provider_billed_tokens_cache_write_1h", 0) or 0)
    pilot_in = max(0, t_in - w_in)
    pilot_out = max(0, t_out - w_out)
    # Cached / write tokens are subsets of pilot input; clamp so discounts /
    # premiums never exceed the pilot input we are actually pricing here.
    pilot_cached = max(0, min(t_cached, pilot_in))
    pilot_write = max(0, min(t_write, pilot_in))
    pilot_write_5m = max(0, min(t_write_5m, pilot_in))
    pilot_write_1h = max(0, min(t_write_1h, pilot_in))
    if billed_in > 0 or billed_out > 0:
        rem_in = max(0, pilot_in - billed_in)
        rem_out = max(0, pilot_out - billed_out)
        rem_cached = max(0, min(max(0, pilot_cached - billed_cached), rem_in))
        rem_write = max(0, min(max(0, pilot_write - billed_write), rem_in))
        rem_w5 = max(0, min(max(0, pilot_write_5m - billed_write_5m), rem_in))
        rem_w1 = max(0, min(max(0, pilot_write_1h - billed_write_1h), rem_in))
        return (
            provider_cost
            + _session_cost(
                rem_in, rem_out, rem_cached, price_in, price_out,
                cache_write=rem_write,
                cache_write_5m=rem_w5,
                cache_write_1h=rem_w1,
            )
            + w_cost
        )
    return (
        _session_cost(
            pilot_in, pilot_out, pilot_cached, price_in, price_out,
            cache_write=pilot_write,
            cache_write_5m=pilot_write_5m,
            cache_write_1h=pilot_write_1h,
        )
        + w_cost
    )


def _cache_savings_gross(cached: float, price_in: float) -> float:
    """Uncapped catalog/list-price value of prompt-cache hits.

    Always ``tokens_cached/1e6 * price_in * (1 - CACHE_READ_MULTIPLIER)``.
    Unlike :func:`_cache_savings_with_basis`, this never clamps to provider
    spend — the UI uses it so displayed cache value keeps growing with
    cached tokens while the reconciled field stays provider-safe.
    """
    raw = (float(cached or 0.0) / 1.0e6) * float(price_in or 0.0) * (
        1.0 - CACHE_READ_MULTIPLIER
    )
    return raw if raw > 0 else 0.0


def _cache_savings(
    cached: float,
    price_in: float,
    provider_cost_usd: Optional[float] = None,
) -> float:
    """USD saved by billing ``cached`` prompt tokens at the cache-read discount
    instead of the full input price (catalog-rate fallback estimate).

    When ``provider_cost_usd`` is supplied the claim is capped at that
    provider-grounded spend (OpenRouter ``usage.cost`` is already net of
    cache). Pass ``None`` when spend is only estimated — callers must not
    substitute estimated session totals as a cap. A present-but-non-positive
    provider receipt means savings are unknown — return 0 rather than
    inventing catalog dollars on top of a net bill. Cache-write premiums
    are a cost, not a saving."""
    usd, _basis = _cache_savings_with_basis(
        cached, price_in, provider_cost_usd=provider_cost_usd
    )
    return usd


def _cache_savings_with_basis(
    cached: float,
    price_in: float,
    provider_cost_usd: Optional[float] = None,
) -> Tuple[float, str]:
    """Return ``(savings_usd, basis)`` where basis is catalog | capped | unknown."""
    raw = _cache_savings_gross(cached, price_in)
    if raw <= 0:
        return 0.0, CACHE_SAVINGS_CATALOG
    if provider_cost_usd is None:
        return raw, CACHE_SAVINGS_CATALOG
    try:
        prov = float(provider_cost_usd)
    except (TypeError, ValueError):
        return 0.0, CACHE_SAVINGS_UNKNOWN
    if prov <= 0:
        # Provider path is authoritative but net spend is unknown / zero —
        # do not claim independent catalog savings above it.
        return 0.0, CACHE_SAVINGS_UNKNOWN
    if raw > prov:
        return prov, CACHE_SAVINGS_CAPPED
    return raw, CACHE_SAVINGS_CATALOG


def _cost_source_label(pilot_like: Any) -> str:
    """How pilot spend was derived: provider | mixed | estimated | plan_estimated."""
    billed_in = int(getattr(pilot_like, "_provider_billed_tokens_in", 0) or 0)
    billed_out = int(getattr(pilot_like, "_provider_billed_tokens_out", 0) or 0)
    if billed_in <= 0 and billed_out <= 0:
        if getattr(pilot_like, "_plan_billing", False):
            return "plan_estimated"
        return "estimated"
    t_in = int(getattr(pilot_like, "_tokens_in", 0) or 0)
    t_out = int(getattr(pilot_like, "_tokens_out", 0) or 0)
    w_in = int(getattr(pilot_like, "_worker_tokens_in", 0) or 0)
    w_out = int(getattr(pilot_like, "_worker_tokens_out", 0) or 0)
    pilot_in = max(0, t_in - w_in)
    pilot_out = max(0, t_out - w_out)
    if billed_in >= pilot_in and billed_out >= pilot_out:
        return "provider"
    return "mixed"


def _job_cost(tokens_in: float, tokens_out: float, tokens_total: float,
              price_in: float, price_out: float) -> float:
    """Deterministic per-job cost. Uses the real in/out split when the job
    carries it; otherwise prices the single ``tokens`` total at ``price_out``
    (completion tokens dominate cost, matching the session fallback) rather than
    a naive 50/50 blend that mis-prices output-heavy jobs.

    Unsplit totals are estimates — callers should surface ``estimated`` via
    :func:`_job_cost_is_unsplit` rather than inventing a fixed in/out ratio."""
    if tokens_in or tokens_out:
        return ((float(tokens_in) / 1.0e6) * price_in
                + (float(tokens_out) / 1.0e6) * price_out)
    return (float(tokens_total) / 1.0e6) * price_out


def _job_cost_is_unsplit(
    tokens_in: float, tokens_out: float, tokens_total: float
) -> bool:
    """True when cost came from a combined token total (no in/out split)."""
    if tokens_in or tokens_out:
        return False
    return float(tokens_total or 0.0) > 0.0


def _log_price_fallback(where: str, err: BaseException) -> None:
    """Surface silent 0.5/2.0 default pricing so registry misconfig is visible."""
    try:
        import logging

        logging.getLogger("harness.cost").warning(
            "price resolve fell back to default 0.5/2.0 MTok (%s): %s: %s",
            where,
            type(err).__name__,
            err,
        )
    except Exception:
        pass


def _resolve_active_prices_with_source() -> tuple:
    """Per-Mtok (price_in, price_out, price_source) for the active driver."""
    from .cost import _cfg

    try:
        from pmharness.registry import resolve_price, price_with_source

        price_in, price_out = resolve_price(_cfg().driver)
        raw_in, raw_out, src = price_with_source(_cfg().driver)
        return (
            float(price_in),
            float(price_out),
            _normalize_price_source(
                None if raw_in is None or raw_out is None else src
            ),
        )
    except Exception as exc:
        _log_price_fallback("active", exc)
        return 0.5, 2.0, PRICE_SOURCE_DEFAULT


def _resolve_active_prices() -> tuple:
    """Per-Mtok (price_in, price_out) for the active driver; safe defaults on failure."""
    pin, pout, _src = _resolve_active_prices_with_source()
    return pin, pout


def _resolve_prices_for_runner_with_source(runner: Any) -> tuple:
    """Per-Mtok (price_in, price_out, price_source) for a runner's bound driver."""
    from .cost import _server_attr

    try:
        cfg = getattr(runner, "config", None)
        driver = getattr(cfg, "driver", None) if cfg is not None else None
        if driver:
            from pmharness.registry import resolve_price, price_with_source

            price_in, price_out = resolve_price(driver)
            raw_in, raw_out, src = price_with_source(driver)
            return (
                float(price_in),
                float(price_out),
                _normalize_price_source(
                    None if raw_in is None or raw_out is None else src
                ),
            )
    except Exception as exc:
        _log_price_fallback("runner", exc)
    resolve_active = _server_attr(
        "_resolve_active_prices_with_source", _resolve_active_prices_with_source
    )
    return resolve_active()


def _resolve_prices_for_runner(runner: Any) -> tuple:
    """Per-Mtok prices for a runner's bound driver (fallback: active / defaults).

    Idle swap may have already retargeted ``_cfg().driver`` before rebuild; price
    historical meters from the runner's frozen ``config.driver`` when present.
    """
    pin, pout, _src = _resolve_prices_for_runner_with_source(runner)
    return pin, pout
