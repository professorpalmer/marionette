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

Optional modality buckets (reasoning, image, cached detail, encrypted/opaque)
are extract-only: they never change tin/tout/cost/cache totals and are never
priced or folded into billed spend.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple


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


def _provider_int(val: Any) -> Optional[int]:
    """Return a non-negative integer modality count; None when absent/malformed."""
    if val is None:
        return None
    if isinstance(val, bool):
        return None
    if isinstance(val, float):
        if val != val or val in (float("inf"), float("-inf")):
            return None
        if not val.is_integer():
            return None
        n = int(val)
    elif isinstance(val, int):
        n = val
    else:
        try:
            n = int(val)
        except (TypeError, ValueError):
            return None
    return n if n >= 0 else None


@dataclass(frozen=True)
class ModalityBucket:
    """Extract-only modality count with explicit reporting basis."""

    basis: str = "absent"  # "provider" | "absent"
    count: Optional[int] = None

    @classmethod
    def absent(cls) -> ModalityBucket:
        return cls(basis="absent", count=None)

    @classmethod
    def from_provider(cls, val: Any) -> Optional[ModalityBucket]:
        n = _provider_int(val)
        if n is None:
            return None
        return cls(basis="provider", count=n)


def _first_provider_bucket(*candidates: Any) -> ModalityBucket:
    for val in candidates:
        bucket = ModalityBucket.from_provider(val)
        if bucket is not None:
            return bucket
    return ModalityBucket.absent()


def _merge_modality(prev: ModalityBucket, new: ModalityBucket) -> ModalityBucket:
    if new.basis == "provider":
        return new
    return prev


def _details_dict(usage: dict, *keys: str) -> dict:
    for key in keys:
        raw = usage.get(key)
        if isinstance(raw, dict):
            return raw
    return {}


def _modalities_from_usage_dict(usage: dict) -> Dict[str, ModalityBucket]:
    usage = usage or {}
    prompt_details = _details_dict(
        usage, "prompt_tokens_details", "promptTokensDetails"
    )
    input_details = _details_dict(
        usage, "input_tokens_details", "inputTokensDetails"
    )
    completion_details = _details_dict(
        usage, "completion_tokens_details", "completionTokensDetails"
    )
    output_details = _details_dict(
        usage, "output_tokens_details", "outputTokensDetails"
    )
    return {
        "reasoning_tokens": _first_provider_bucket(
            completion_details.get("reasoning_tokens"),
            output_details.get("reasoning_tokens"),
            usage.get("reasoning_tokens"),
            usage.get("reasoningTokens"),
            usage.get("reasoning_output_tokens"),
            usage.get("reasoningOutputTokens"),
        ),
        "image_tokens": _first_provider_bucket(
            prompt_details.get("image_tokens"),
            input_details.get("image_tokens"),
            usage.get("image_tokens"),
            usage.get("imageTokens"),
        ),
        "cached_tokens_detail": _first_provider_bucket(
            prompt_details.get("cached_tokens"),
            input_details.get("cached_tokens"),
            prompt_details.get("cache_read_tokens"),
            input_details.get("cache_read_tokens"),
        ),
        "encrypted_opaque_tokens": _first_provider_bucket(
            input_details.get("encrypted_content_tokens"),
            prompt_details.get("encrypted_content_tokens"),
            output_details.get("encrypted_content_tokens"),
            usage.get("encrypted_content_tokens"),
            usage.get("encryptedContentTokens"),
            usage.get("opaque_content_tokens"),
            usage.get("opaqueContentTokens"),
        ),
    }


@dataclass
class TokenUsageDetail:
    """Full usage record: billed totals plus optional modality buckets."""

    tokens_in: int = 0
    tokens_out: int = 0
    cost: Optional[float] = None
    cache_read: int = 0
    cache_write: int = 0
    reasoning_tokens: ModalityBucket = field(default_factory=ModalityBucket.absent)
    image_tokens: ModalityBucket = field(default_factory=ModalityBucket.absent)
    cached_tokens_detail: ModalityBucket = field(default_factory=ModalityBucket.absent)
    encrypted_opaque_tokens: ModalityBucket = field(
        default_factory=ModalityBucket.absent
    )

    def as_tuple(self) -> Tuple[int, int, Optional[float], int, int]:
        return (
            self.tokens_in,
            self.tokens_out,
            self.cost,
            self.cache_read,
            self.cache_write,
        )

    def modality_dict(self) -> Dict[str, Any]:
        """Provider-reported modality fields for meta/API passthrough.

        Absent buckets are omitted rather than serialized as zero.
        """
        out: Dict[str, Any] = {}
        for key, bucket in (
            ("reasoning_tokens", self.reasoning_tokens),
            ("image_tokens", self.image_tokens),
            ("cached_tokens_detail", self.cached_tokens_detail),
            ("encrypted_opaque_tokens", self.encrypted_opaque_tokens),
        ):
            if bucket.basis == "provider":
                out[key] = bucket.count
                out[f"{key}_basis"] = "provider"
        return out


def attach_modality_fields(target: dict, detail: TokenUsageDetail) -> None:
    """Merge extract-only modality buckets into a driver meta/out dict."""
    target.update(detail.modality_dict())


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


def coerce_token_usage_record(*blobs: Any) -> TokenUsageDetail:
    """Return full usage detail including optional modality buckets."""
    detail = TokenUsageDetail()
    for blob in blobs:
        if blob is None:
            continue
        for cand in _iter_usage_candidates(blob):
            tin, tout, cost, cached, cache_write = _from_usage_dict(cand)
            if tin > 0:
                detail.tokens_in = tin
            if tout > 0:
                detail.tokens_out = tout
            if cost is not None:
                detail.cost = cost
            if cached > 0:
                detail.cache_read = cached
            if cache_write > 0:
                detail.cache_write = cache_write
            mods = _modalities_from_usage_dict(cand)
            detail.reasoning_tokens = _merge_modality(
                detail.reasoning_tokens, mods["reasoning_tokens"]
            )
            detail.image_tokens = _merge_modality(
                detail.image_tokens, mods["image_tokens"]
            )
            detail.cached_tokens_detail = _merge_modality(
                detail.cached_tokens_detail, mods["cached_tokens_detail"]
            )
            detail.encrypted_opaque_tokens = _merge_modality(
                detail.encrypted_opaque_tokens, mods["encrypted_opaque_tokens"]
            )
    detail.tokens_in, detail.cache_read, detail.cache_write = (
        expand_uncached_prompt_tokens(
            detail.tokens_in, detail.cache_read, detail.cache_write
        )
    )
    return detail


def coerce_token_usage(*blobs: Any) -> Tuple[int, int, Optional[float]]:
    """Return (tokens_in, tokens_out, provider_cost_usd|None) from any blobs.

    Later blobs win for non-zero fields (ACP result often has the final usage
    after streaming updates with partial counts). ``tokens_in`` is the full
    prompt total after Cursor/Anthropic uncached-only expansion.
    """
    detail = coerce_token_usage_record(*blobs)
    return detail.tokens_in, detail.tokens_out, detail.cost


def coerce_token_usage_detail(
    *blobs: Any,
) -> Tuple[int, int, Optional[float], int, int]:
    """Return (tokens_in, tokens_out, cost|None, cache_read, cache_write).

    ``tokens_in`` is the FULL prompt total (uncached + cache read + cache
    write) so StatusBar meters and ``_session_cost`` stay coherent. Cache
    buckets remain available for the cache-savings chip and write premiums.

    Optional modality buckets are available via ``coerce_token_usage_record``.
    """
    return coerce_token_usage_record(*blobs).as_tuple()
