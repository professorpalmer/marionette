from __future__ import annotations

"""Best-effort token / cost extraction across provider usage shapes.

Drivers (Cursor ACP/CLI, Codex Responses, OpenAI-compat) surface usage under
different keys. Conversation metering calls this so a missing field falls
through to the next shape instead of silent zeros.

Cursor CLI / Anthropic report ``inputTokens`` / ``input_tokens`` as the
*uncached* prompt slice only. Cache read/write live in sibling fields. Our
``_session_cost`` formula expects ``t_in`` to be the FULL prompt total
(uncached + cache read + cache write), so we expand uncached-only reports
before returning.
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


def expand_uncached_prompt_tokens(
    tin: int, cached: int, cache_write: int
) -> Tuple[int, int, int]:
    """Rebuild full prompt total when ``tin`` is uncached-only.

    Heuristic: when cache buckets exceed reported input, the provider is using
    Cursor/Anthropic semantics (input = uncached only). OpenAI-style reports
    keep ``prompt_tokens`` as the full total with cached as a subset
    (``cached <= tin``), so we leave them alone.
    """
    tin = int(tin or 0)
    cached = int(cached or 0)
    cache_write = int(cache_write or 0)
    bucket = cached + cache_write
    if bucket > 0 and tin < bucket:
        return tin + bucket, cached, cache_write
    return tin, cached, cache_write


def _from_usage_dict(usage: dict) -> Tuple[int, int, Optional[float], int, int]:
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
    cache_write = _as_int(
        usage.get("cache_write_tokens")
        or usage.get("cache_creation_input_tokens")
        or usage.get("cacheWriteTokens")
        or usage.get("cache_write_input_tokens")
        or usage.get("tokens_cache_write")
    )
    if cache_write <= 0:
        details = (
            usage.get("prompt_tokens_details")
            or usage.get("input_tokens_details")
            or usage.get("promptTokensDetails")
        )
        if isinstance(details, dict):
            cache_write = _as_int(
                details.get("cache_write_tokens")
                or details.get("cache_creation_input_tokens")
                or details.get("cacheWriteTokens")
            )
    if cache_write <= 0:
        inp = usage.get("input") or usage.get("prompt")
        if isinstance(inp, dict):
            cache_write = _as_int(
                inp.get("cache_write_tokens")
                or inp.get("cache_creation_input_tokens")
                or inp.get("cacheWriteTokens")
            )
    tin, cached, cache_write = expand_uncached_prompt_tokens(tin, cached, cache_write)
    return tin, tout, cost, cached, cache_write


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
    # Peel usage from nested result / tokenUsage dicts (Cursor CLI wraps
    # ``{"result": {"usage": {...}}}``).
    extra = []
    for cand in list(candidates):
        if cand is blob:
            continue
        for key in ("usage", "tokenUsage", "token_usage"):
            nested = cand.get(key)
            if isinstance(nested, dict) and nested not in candidates:
                extra.append(nested)
    candidates.extend(extra)
    return candidates


def coerce_token_usage(*blobs: Any) -> Tuple[int, int, Optional[float]]:
    """Return (tokens_in, tokens_out, provider_cost_usd|None) from any blobs.

    Later blobs win for non-zero fields (ACP result often has the final usage
    after streaming updates with partial counts). ``tokens_in`` is the full
    prompt total after Cursor/Anthropic uncached-only expansion.
    """
    tin, tout, cost, _cached, _write = coerce_token_usage_detail(*blobs)
    return tin, tout, cost


def coerce_token_usage_detail(
    *blobs: Any,
) -> Tuple[int, int, Optional[float], int, int]:
    """Return (tokens_in, tokens_out, cost|None, cache_read, cache_write).

    ``tokens_in`` is the FULL prompt total (uncached + cache read + cache
    write) so StatusBar meters and ``_session_cost`` stay coherent. Cache
    buckets remain available for the cache-savings chip and write premiums.
    """
    best_in, best_out, best_cost, best_cached, best_write = 0, 0, None, 0, 0
    for blob in blobs:
        if blob is None:
            continue
        for cand in _iter_usage_candidates(blob):
            tin, tout, cost, cached, cache_write = _from_usage_dict(cand)
            if tin > 0:
                best_in = tin
            if tout > 0:
                best_out = tout
            if cost is not None:
                best_cost = cost
            if cached > 0:
                best_cached = cached
            if cache_write > 0:
                best_write = cache_write
    # Re-expand after merge: cache buckets and uncached input can arrive on
    # different blobs/candidates (streaming updates vs final result).
    best_in, best_cached, best_write = expand_uncached_prompt_tokens(
        best_in, best_cached, best_write
    )
    return best_in, best_out, best_cost, best_cached, best_write
