"""Check the ledger against what the provider's own account says.

OpenRouter reports a key's spend for the current UTC day and month. Next to
the ledger's own total for the same period, a gap means spend the ledger did
not see (other apps on the same key, or a call Marionette failed to record).
It is shown, never silently absorbed. Cached so the UI never waits on it.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Optional

_TTL_S = 300.0
_lock = threading.Lock()
_cache: dict[str, tuple[float, Optional[dict]]] = {}


def _openrouter_key_usage(key: str) -> Optional[dict]:
    req = urllib.request.Request("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = (json.loads(resp.read()) or {}).get("data") or {}
    daily, monthly = data.get("usage_daily"), data.get("usage_monthly")
    if not isinstance(daily, (int, float)) or not isinstance(monthly, (int, float)):
        return None
    return {"day_usd": float(daily), "month_usd": float(monthly)}


def _refresh(key: str, fetch: Callable[[str], Optional[dict]], now: float) -> None:
    try:
        account = fetch(key)
    except Exception:
        account = None
    with _lock:
        _cache[key] = (now, account)


def _period_starts(now: float) -> tuple[float, float]:
    dt = datetime.fromtimestamp(now, tz=timezone.utc)
    day = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return day.timestamp(), day.replace(day=1).timestamp()


def openrouter_reconciliation(
    state_dir: str,
    *,
    key: Optional[str] = None,
    fetch: Callable[[str], Optional[dict]] = _openrouter_key_usage,
    now: Optional[float] = None,
    block: bool = False,
) -> Optional[dict]:
    """{'day': {...}, 'month': {...}} ledger vs account, or None while unknown.
    Never waits on the network unless ``block``: a stale value refreshes in
    the background and the last known one is returned."""
    if key is None:
        try:
            from harness.providers import get_provider

            key = get_provider("openrouter").key()
        except Exception:
            key = None
    if not key:
        return None
    now = time.time() if now is None else now
    with _lock:
        hit = _cache.get(key)
        stale = hit is None or now - hit[0] > _TTL_S
        if stale:
            # Mark the refresh in flight so concurrent readers do not stack it.
            _cache[key] = (now, hit[1] if hit else None)
    if stale:
        if block:
            _refresh(key, fetch, now)
        else:
            threading.Thread(target=_refresh, args=(key, fetch, now), name="ledger-reconcile", daemon=True).start()
    with _lock:
        account = (_cache.get(key) or (0.0, None))[1]
    if not account:
        return None
    from .summary import period_spend

    day_start, month_start = _period_starts(now)
    out = {}
    for period, since, provider_usd in (("day", day_start, account["day_usd"]), ("month", month_start, account["month_usd"])):
        ledger_usd = period_spend(state_dir, "openrouter", since) or 0.0
        out[period] = {
            "ledger_usd": round(ledger_usd, 6),
            "account_usd": round(provider_usd, 6),
            "unrecorded_usd": round(max(0.0, provider_usd - ledger_usd), 6),
        }
    return out
