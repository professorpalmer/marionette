"""Which account a call billed, and how that account is billed.

Billing is a property of the account, not of the model: the same Kimi is
metered through OpenRouter and included in an OpenCode Go subscription. It is
declared once on each provider profile (``Provider.billing``); a local model
server is ``local``; any other endpoint is metered at a price the ledger may
not know (recorded as unpriced, never as free).
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

METERED = "metered"
PLAN = "plan"
LOCAL = "local"
BILLINGS = (METERED, PLAN, LOCAL)


@dataclass(frozen=True)
class Route:
    provider: str
    account: str
    billing: str
    model: str


def _loopback(base_url: str) -> bool:
    host = (urlsplit(base_url or "").hostname or "").strip("[]").lower()
    if host in ("localhost",) or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _split_spec(driver_name: str, model: str) -> tuple[str, str]:
    """('provider', 'model') from a driver named 'provider:model' or 'provider'."""
    name = (driver_name or "").strip()
    if ":" in name:
        prefix, rest = name.split(":", 1)
        return prefix.strip().lower(), (model or rest).strip()
    return name.lower(), (model or "").strip()


def resolve_route(driver_name: str, model: str, base_url: str) -> Route:
    prefix, model_id = _split_spec(driver_name, model)
    host = (urlsplit(base_url or "").hostname or "").lower()
    if prefix == "local" or (base_url and _loopback(base_url)):
        if base_url and not _loopback(base_url):
            # A "local" pilot pointed at someone else's server is not free.
            return Route(host or "local", host or "local", METERED, model_id)
        return Route("local", host or "local", LOCAL, model_id)
    provider = _provider(prefix) or _provider_for_url(base_url)
    if provider is not None:
        return Route(provider.name, provider.name, provider.billing, model_id)
    return Route(host or prefix or "unknown", host or prefix or "unknown", METERED, model_id)


def _provider_for_url(base_url: str):
    """The provider profile whose API ``base_url`` serves this call: the
    longest profile base URL it starts with, so providers sharing a host
    (OpenCode Go under opencode.ai/zen/go, Zen under opencode.ai/zen) stay
    apart. For sidecars and other callers that know an endpoint only."""
    url = (base_url or "").rstrip("/").lower()
    if not url:
        return None
    try:
        from harness.providers import PROVIDERS

        best, best_len = None, 0
        for provider in PROVIDERS:
            root = (provider.base_url or "").rstrip("/").lower()
            if root and (url == root or url.startswith(root + "/")) and len(root) > best_len:
                best, best_len = provider, len(root)
        return best
    except Exception:
        return None


def _provider(name: str):
    if not name:
        return None
    try:
        from harness.providers import get_provider

        return get_provider(name)
    except Exception:
        return None


def billing_for_spec(spec: str) -> Optional[str]:
    """Billing of the account a pilot spec ('provider:model') bills, or None."""
    provider = _provider(_split_spec(spec, "")[0])
    return provider.billing if provider is not None else None
