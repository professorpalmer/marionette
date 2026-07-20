from __future__ import annotations

"""Best-effort token / cost extraction across provider usage shapes.

Drivers (Cursor ACP/CLI, Codex Responses, OpenAI-compat) surface usage under
different keys. Conversation metering calls this so a missing field falls
through to the next shape instead of silent zeros.
"""

from typing import Any, Optional, Tuple


def _as_int(val: Any) -> int:
    try:
        if val is None:
            return 0
        n = int(val)
        return n if n > 0 else 0
    except (TypeError, ValueError):
        return 0


def _as_cost(val: Any) -> Optional[float]:
    try:
        if val is None:
            return None
        n = float(val)
        if n != n or n < 0.0:  # NaN / negative
            return None
        return n
    except (TypeError, ValueError):
        return None


def _from_usage_dict(usage: dict) -> Tuple[int, int, Optional[float], int]:
    tin = _as_int(
        usage.get("input_tokens")
        or usage.get("prompt_tokens")
        or usage.get("inputTokens")
        or usage.get("promptTokens")
        or usage.get("tokens_in")
    )
    tout = _as_int(
        usage.get("output_tokens")
        or usage.get("completion_tokens")
        or usage.get("outputTokens")
        or usage.get("completionTokens")
        or usage.get("tokens_out")
    )
    # Nested shapes: {input: {tokens: N}, output: {tokens: N}}
    if tin <= 0:
        inp = usage.get("input") or usage.get("prompt")
        if isinstance(inp, dict):
            tin = _as_int(inp.get("tokens") or inp.get("token_count"))
        elif isinstance(inp, (int, float)):
            tin = _as_int(inp)
    if tout <= 0:
        out = usage.get("output") or usage.get("completion")
        if isinstance(out, dict):
            tout = _as_int(out.get("tokens") or out.get("token_count"))
        elif isinstance(out, (int, float)):
            tout = _as_int(out)
    cost = None
    for key in (
        "cost",
        "total_cost",
        "totalCost",
        "cost_usd",
        "costUsd",
        "provider_cost_usd",
    ):
        cost = _as_cost(usage.get(key))
        if cost is not None:
            break
    cached = _as_int(
        usage.get("cache_read_tokens")
        or usage.get("cache_read_input_tokens")
        or usage.get("cacheReadTokens")
        or usage.get("cached_tokens")
        or usage.get("cachedTokens")
        or usage.get("tokens_cached")
    )
    if cached <= 0:
        # OpenAI-style: prompt_tokens_details.cached_tokens
        details = (
            usage.get("prompt_tokens_details")
            or usage.get("input_tokens_details")
            or usage.get("promptTokensDetails")
        )
        if isinstance(details, dict):
            cached = _as_int(
                details.get("cached_tokens")
                or details.get("cache_read_tokens")
                or details.get("cachedTokens")
            )
    if cached <= 0:
        inp = usage.get("input") or usage.get("prompt")
        if isinstance(inp, dict):
            cached = _as_int(
                inp.get("cached_tokens")
                or inp.get("cache_read_tokens")
                or inp.get("cacheReadTokens")
            )
    return tin, tout, cost, cached


def _iter_usage_candidates(blob: Any) -> list:
    if not isinstance(blob, dict):
        return []
    candidates = [blob]
    for key in ("usage", "tokenUsage", "token_usage", "tokens", "result"):
        nested = blob.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)
    # ACP session/update nest
    inner = blob.get("update")
    if isinstance(inner, dict):
        candidates.append(inner)
        for key in ("usage", "tokenUsage", "token_usage"):
            nested = inner.get(key)
            if isinstance(nested, dict):
                candidates.append(nested)
    return candidates


def coerce_token_usage(*blobs: Any) -> Tuple[int, int, Optional[float]]:
    """Return (tokens_in, tokens_out, provider_cost_usd|None) from any blobs.

    Later blobs win for non-zero fields (ACP result often has the final usage
    after streaming updates with partial counts).
    """
    tin, tout, cost, _cached = coerce_token_usage_detail(*blobs)
    return tin, tout, cost


def coerce_token_usage_detail(
    *blobs: Any,
) -> Tuple[int, int, Optional[float], int]:
    """Return (tokens_in, tokens_out, provider_cost_usd|None, cache_read_tokens).

    Cursor CLI/ACP often report cache hits under ``cached_tokens`` /
    ``cache_read_input_tokens``; without this the StatusBar cache meter
    stays at zero for plan-billed CLI pilots while API pilots populate it.
    """
    best_in, best_out, best_cost, best_cached = 0, 0, None, 0
    for blob in blobs:
        if blob is None:
            continue
        for cand in _iter_usage_candidates(blob):
            tin, tout, cost, cached = _from_usage_dict(cand)
            if tin > 0:
                best_in = tin
            if tout > 0:
                best_out = tout
            if cost is not None:
                best_cost = cost
            if cached > 0:
                best_cached = cached
    return best_in, best_out, best_cost, best_cached
