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


def _from_usage_dict(usage: dict) -> Tuple[int, int, Optional[float]]:
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
    return tin, tout, cost


def coerce_token_usage(*blobs: Any) -> Tuple[int, int, Optional[float]]:
    """Return (tokens_in, tokens_out, provider_cost_usd|None) from any blobs.

    Later blobs win for non-zero fields (ACP result often has the final usage
    after streaming updates with partial counts).
    """
    best_in, best_out, best_cost = 0, 0, None
    for blob in blobs:
        if blob is None:
            continue
        candidates = []
        if isinstance(blob, dict):
            candidates.append(blob)
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
        for cand in candidates:
            tin, tout, cost = _from_usage_dict(cand)
            if tin > 0:
                best_in = tin
            if tout > 0:
                best_out = tout
            if cost is not None:
                best_cost = cost
    return best_in, best_out, best_cost
