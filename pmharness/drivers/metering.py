"""Every finished model call, reported once, as a fact.

Drivers answer ``chat`` / ``complete`` / ``chat_stream`` with a
``DriverResponse`` whose usage is already normalized (``tokens_in`` is the
whole prompt, cache reads and writes included; see ``token_usage``). The
``@metered`` class decorator reports that response to registered sinks as a
``ProviderCall`` so the harness can keep a ledger of what was spent. Drivers
never know what the call cost or who it was for: pricing, plans and session
attribution are the harness's job.

- Only the outermost metered call reports: a driver whose ``chat`` delegates to
  its own ``chat_stream`` (or a mixture-of-agents driver calling other drivers)
  is one call, not several.
- Attribution (session, turn, purpose, job) comes from the driver's own
  ``metering_tags`` (a session tags its pilot once, so every call it makes is
  attributed on any thread) overlaid by the ambient ``attribution(...)``
  block, which follows work into threads that copy the context.
- A sink can never break a model call: failures are swallowed.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional

_METERED_METHODS = ("chat", "complete", "chat_stream")

_depth: contextvars.ContextVar[int] = contextvars.ContextVar("pm_metering_depth", default=0)
_attribution: contextvars.ContextVar[Mapping[str, Any]] = contextvars.ContextVar(
    "pm_metering_attribution", default={}
)

_sinks: List[Callable[["ProviderCall"], None]] = []
_sinks_lock = threading.Lock()


@dataclass(frozen=True)
class ProviderCall:
    """One completed model call, as the provider reported it."""

    driver: str
    model: str
    served_model: str
    base_url: str
    started_at: float
    ended_at: float
    tokens_in: int
    tokens_out: int
    cache_read_tokens: int
    cache_write_tokens: int
    cache_write_5m_tokens: int
    cache_write_1h_tokens: int
    reasoning_tokens: int
    token_basis: str
    provider_cost_usd: Optional[float]
    billing_hint: str
    error: Optional[str]
    attribution: Mapping[str, Any] = field(default_factory=dict)


def register_sink(sink: Callable[[ProviderCall], None]) -> Callable[[], None]:
    """Receive every outermost ProviderCall. Returns an unregister function."""
    with _sinks_lock:
        _sinks.append(sink)

    def unregister() -> None:
        with _sinks_lock:
            if sink in _sinks:
                _sinks.remove(sink)

    return unregister


@contextlib.contextmanager
def attribution(**fields: Any) -> Iterator[None]:
    """Tag every model call made inside this block (merged over outer tags)."""
    merged = dict(_attribution.get())
    merged.update({k: v for k, v in fields.items() if v is not None})
    token = _attribution.set(merged)
    try:
        yield
    finally:
        _attribution.reset(token)


def current_attribution() -> Dict[str, Any]:
    return dict(_attribution.get())


def _call_attribution(driver: Any) -> Dict[str, Any]:
    """The driver's own tags (``metering_tags``: a mapping, or a callable
    returning one, read at call time) overlaid by the ambient attribution."""
    tags = getattr(driver, "metering_tags", None)
    try:
        tags = tags() if callable(tags) else tags
    except Exception:
        tags = None
    merged = dict(tags) if isinstance(tags, Mapping) else {}
    merged.update(current_attribution())
    return {k: v for k, v in merged.items() if v is not None}


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _cost(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out >= 0.0 else None


def provider_call_from_response(driver: Any, response: Any, started_at: float) -> ProviderCall:
    meta = getattr(response, "meta", None) or {}
    tokens_in = _int(getattr(response, "tokens_in", 0))
    basis = str(meta.get("token_basis") or ("provider" if tokens_in else "absent"))
    return ProviderCall(
        driver=str(getattr(driver, "name", "") or driver.__class__.__name__),
        model=str(getattr(driver, "model", "") or getattr(response, "model", "") or ""),
        served_model=str(meta.get("served_model") or getattr(response, "model", "") or ""),
        base_url=str(getattr(driver, "base_url", "") or ""),
        started_at=started_at,
        ended_at=time.time(),
        tokens_in=tokens_in,
        tokens_out=_int(getattr(response, "tokens_out", 0)),
        cache_read_tokens=_int(meta.get("cache_read_tokens")),
        cache_write_tokens=_int(meta.get("cache_write_tokens")),
        cache_write_5m_tokens=_int(meta.get("cache_write_5m_tokens")),
        cache_write_1h_tokens=_int(meta.get("cache_write_1h_tokens")),
        reasoning_tokens=_int(meta.get("reasoning_tokens")),
        token_basis=basis,
        provider_cost_usd=_cost(meta.get("provider_cost_usd")),
        billing_hint=str(meta.get("billing") or ""),
        error=getattr(response, "error", None) or None,
        attribution=_call_attribution(driver),
    )


def report_usage(
    *,
    name: str,
    model: str,
    base_url: str,
    usage: Any,
    started_at: float,
    owner: Any = None,
    **attribution_fields: Any,
) -> None:
    """Report a model call made outside a driver's chat path (raw provider
    ``usage`` blob, OpenAI or Anthropic shaped). ``owner`` lends its
    ``metering_tags``, as for a driver's own calls. Never raises."""
    try:
        from .token_usage import coerce_token_usage_record

        detail = coerce_token_usage_record(usage)
        if not (detail.tokens_in or detail.tokens_out or detail.cost):
            return
        attr = _call_attribution(owner) if owner is not None else current_attribution()
        attr.update({k: v for k, v in attribution_fields.items() if v is not None})
        _report(ProviderCall(
            driver=name, model=model, served_model="", base_url=base_url,
            started_at=started_at, ended_at=time.time(),
            tokens_in=detail.tokens_in, tokens_out=detail.tokens_out,
            cache_read_tokens=detail.cache_read, cache_write_tokens=detail.cache_write,
            cache_write_5m_tokens=0, cache_write_1h_tokens=0, reasoning_tokens=0,
            token_basis="provider", provider_cost_usd=_cost(detail.cost),
            billing_hint="", error=None, attribution=attr,
        ))
    except Exception:
        pass


def _report(call: ProviderCall) -> None:
    with _sinks_lock:
        sinks = list(_sinks)
    for sink in sinks:
        try:
            sink(call)
        except Exception:
            pass


def _wrap(method: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(method)
    def metered_method(self: Any, *args: Any, **kwargs: Any) -> Any:
        token = _depth.set(_depth.get() + 1)
        started = time.time()
        try:
            response = method(self, *args, **kwargs)
        finally:
            _depth.reset(token)
        if _depth.get() == 0 and response is not None and hasattr(response, "tokens_in"):
            try:
                _report(provider_call_from_response(self, response, started))
            except Exception:
                pass
        return response

    metered_method.__metered__ = True  # type: ignore[attr-defined]
    return metered_method


def metered(cls: type) -> type:
    """Class decorator: report each outermost chat/complete/chat_stream result.

    Methods are wrapped on the class, so instances keep their type (callers
    that check ``isinstance`` or ``type(...) is`` are unaffected).
    """
    for name in _METERED_METHODS:
        method = cls.__dict__.get(name)
        if callable(method) and not getattr(method, "__metered__", False):
            setattr(cls, name, _wrap(method))
    return cls
