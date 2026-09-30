"""Exact per-provider rate cards and the one pricing formula.

A rate card is what THIS provider charges for THIS model, per million tokens
of each class: uncached input, cache read, cache write (5 minute and 1 hour),
output, with context-length tiers when the provider publishes them. Sources,
best first:

1. models.dev, keyed by the provider actually called (OpenCode Go's rate for
   its own mimo, not OpenRouter's), with cache and tier prices;
2. the harness catalog / OpenRouter live price map (input and output only);
3. nothing: the call is recorded as unpriced. There is no default rate, so an
   unknown price never turns into an invented number.

The rate in force is captured on every ledger row (``RateCard.to_dict``), so a
later price change never reprices history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence, Tuple

# Harness provider id -> the models.dev provider that publishes its prices.
# Plans are priced at their vendor's list rate for the list-price equivalent.
_RATE_PROVIDER = {
    "gemini": "google",
    "openai-codex": "openai",
    "claude-code": "anthropic",
    "bedrock": "amazon-bedrock",
}

# Anthropic publishes 1h cache writes at 2x input and 5m writes at 1.25x;
# used only when a provider reports 1h writes without publishing that rate.
_CACHE_WRITE_1H_MULTIPLIER = 2.0


@dataclass(frozen=True)
class RateCard:
    """USD per million tokens. ``cache_read``/``cache_write`` None = billed as input."""

    input: float
    output: float
    cache_read: Optional[float] = None
    cache_write: Optional[float] = None
    cache_write_1h: Optional[float] = None
    # (prompt tokens above which it applies, card) ascending.
    tiers: Tuple[Tuple[int, "RateCard"], ...] = field(default=())

    def for_prompt(self, prompt_tokens: int) -> "RateCard":
        card = self
        for threshold, tier in self.tiers:
            if prompt_tokens > threshold:
                card = tier
        return card

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"input": self.input, "output": self.output}
        for name in ("cache_read", "cache_write", "cache_write_1h"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        if self.tiers:
            out["tiers"] = [{"above": t, **card.to_dict()} for t, card in self.tiers]
        return out


@dataclass(frozen=True)
class TokenClasses:
    """One call's tokens, split the way providers bill them."""

    input_uncached: int
    cache_read: int
    cache_write_5m: int
    cache_write_1h: int
    output: int

    @property
    def prompt(self) -> int:
        return self.input_uncached + self.cache_read + self.cache_write_5m + self.cache_write_1h

    @classmethod
    def split(
        cls,
        *,
        tokens_in: int,
        tokens_out: int,
        cache_read: int,
        cache_write: int,
        cache_write_5m: int = 0,
        cache_write_1h: int = 0,
    ) -> "TokenClasses":
        """``tokens_in`` is the whole prompt (cache reads and writes included).
        Writes without a 5m/1h split from the provider bill as 5m writes."""
        if cache_write_5m or cache_write_1h:
            write_5m, write_1h = cache_write_5m, cache_write_1h
        else:
            write_5m, write_1h = cache_write, 0
        # Some providers report the same tokens as both read and written
        # (OpenRouter's Gemini cache: read == write == prompt). Classes may
        # never add up to more than the prompt: writes take what reads left.
        room = max(0, tokens_in - cache_read)
        write_1h = min(write_1h, room)
        write_5m = min(write_5m, room - write_1h)
        uncached = max(0, tokens_in - cache_read - write_5m - write_1h)
        return cls(uncached, cache_read, write_5m, write_1h, max(0, tokens_out))


def price(tokens: TokenClasses, card: RateCard) -> float:
    """USD for one call at ``card``. Reasoning is billed inside output tokens."""
    rate = card.for_prompt(tokens.prompt)
    cache_read = rate.input if rate.cache_read is None else rate.cache_read
    write_5m = rate.input if rate.cache_write is None else rate.cache_write
    write_1h = rate.cache_write_1h if rate.cache_write_1h is not None else rate.input * _CACHE_WRITE_1H_MULTIPLIER
    usd_per_mtok = (
        tokens.input_uncached * rate.input
        + tokens.cache_read * cache_read
        + tokens.cache_write_5m * write_5m
        + tokens.cache_write_1h * write_1h
        + tokens.output * rate.output
    )
    return usd_per_mtok / 1_000_000.0


def _num(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out >= 0 else None


def card_from_models_dev(cost: dict) -> Optional[RateCard]:
    base_in, base_out = _num(cost.get("input")), _num(cost.get("output"))
    if base_in is None or base_out is None:
        return None
    tiers = []
    for tier in cost.get("tiers") or ():
        size = ((tier or {}).get("tier") or {}).get("size")
        t_in, t_out = _num((tier or {}).get("input")), _num((tier or {}).get("output"))
        if isinstance(size, (int, float)) and t_in is not None and t_out is not None:
            tiers.append((int(size), RateCard(
                t_in, t_out, _num(tier.get("cache_read")), _num(tier.get("cache_write")),
            )))
    if not tiers and isinstance(cost.get("context_over_200k"), dict):
        over = cost["context_over_200k"]
        t_in, t_out = _num(over.get("input")), _num(over.get("output"))
        if t_in is not None and t_out is not None:
            tiers.append((200_000, RateCard(t_in, t_out, _num(over.get("cache_read")), _num(over.get("cache_write")))))
    return RateCard(
        base_in, base_out, _num(cost.get("cache_read")), _num(cost.get("cache_write")),
        tiers=tuple(sorted(tiers, key=lambda t: t[0])),
    )


def _equivalent_ids(provider: str, model: str) -> tuple:
    """Other ids the provider serves this model under (the rate card may list
    only one of them), from the provider module that owns the aliases."""
    if provider == "opencode-go":
        try:
            from harness.opencode_go import flash_model_keys

            return tuple(sorted(flash_model_keys(model) - {model}))
        except Exception:
            return ()
    return ()


@dataclass(frozen=True)
class RateLookup:
    card: Optional[RateCard]
    source: Optional[str]
    version: Optional[float]


def rate_card(provider: str, model: str, *, candidates: Sequence[str] = ()) -> RateLookup:
    """The rate in force for ``model`` at ``provider`` right now. Never raises."""
    ids = [m for m in (model, *candidates, *_equivalent_ids(provider, model)) if m]
    try:
        from harness.models_dev import lookup_cost

        rate_provider = _RATE_PROVIDER.get(provider, provider)
        for mid in ids:
            hit = lookup_cost(rate_provider, mid)
            if hit:
                card = card_from_models_dev(hit[0])
                if card:
                    return RateLookup(card, "models.dev", hit[1] or None)
    except Exception:
        pass
    try:
        from pmharness.registry import price_with_source

        for spec in ([f"{provider}:{m}" for m in ids] + ids):
            pin, pout, src = price_with_source(spec)
            if pin is not None and pout is not None:
                return RateLookup(RateCard(float(pin), float(pout)), f"catalog:{src}", None)
    except Exception:
        pass
    return RateLookup(None, None, None)
