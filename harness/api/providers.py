"""Provider / auth / model HTTP route bodies (peeled from ``harness.server``).

Key, OAuth, pool, catalog, and visibility JSON handlers take a
:class:`ProviderServices` (or none) so this module never imports
``harness.server`` at top level. ``server.Handler`` keeps auth/token gates
and thin path delegates. API keys and OAuth codes are never logged here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable

from ..keys import (
    clear_api_key,
    get_api_key_status,
    get_disconnected,
    provider_has_env,
    scrub_provider_env,
    set_api_key,
    set_bedrock_credentials,
    set_provider_enabled,
)
from ..providers import get_provider


@dataclass
class ProviderServices:
    """Explicit deps for provider/auth/model HTTP handlers (injected by ``server.py``)."""

    cfg: Any
    diag: Callable[..., None]
    parse_bool: Callable[[Any], bool]
    resync_driver_after_model_curation: Callable[[], dict]
    driver_provider_available: Callable[[str], bool]
    resolve_available_driver: Callable[[], None]
    rebuild_pilot_and_session: Callable[[], None]


# ---------------------------------------------------------------------------
# Models (visibility / catalog)
# ---------------------------------------------------------------------------

def post_models_toggle(body: dict, svc: ProviderServices) -> tuple[int, dict]:
    """POST /api/models/toggle — enable/disable one provider:model spec."""
    from .. import model_visibility as _mv
    spec = body.get("spec", "")
    on = svc.parse_bool(body.get("enabled", True))
    enabled = _mv.toggle(spec, on)
    sync = svc.resync_driver_after_model_curation()
    return 200, {
        "ok": True,
        "enabled": enabled,
        "driver": sync.get("driver") or svc.cfg.driver,
        "driver_changed": bool(sync.get("changed")),
    }


def post_models_set(body: dict, svc: ProviderServices) -> tuple[int, dict]:
    """POST /api/models/set — replace the enabled model set."""
    from .. import model_visibility as _mv
    enabled = _mv.set_enabled(body.get("enabled") or [])
    sync = svc.resync_driver_after_model_curation()
    return 200, {
        "ok": True,
        "enabled": enabled,
        "driver": sync.get("driver") or svc.cfg.driver,
        "driver_changed": bool(sync.get("changed")),
    }


def get_models_catalog(*, force: bool = False) -> tuple[int, dict]:
    """GET /api/models/catalog — full + available catalogs and enabled set."""
    from .. import model_visibility as _mv
    # One force pass (Cursor `agent models` is multi-second); derive the
    # keyed-only catalog from the full list so we don't spawn twice.
    all_cat = _mv.catalog(available_only=False, force=force)
    return 200, {
        "catalog": [c for c in all_cat if c.get("available")],
        "all": all_cat,
        "enabled": _mv.get_enabled(),
    }


# ---------------------------------------------------------------------------
# Providers (list / probe / key)
# ---------------------------------------------------------------------------

def get_providers() -> tuple[int, list]:
    """GET /api/providers — profiles + key/env/disconnect status (no secrets)."""
    from ..registry_wizard import PROVIDERS, get_provider_key
    disconnected = get_disconnected()
    res = []
    for p in PROVIDERS:
        status = get_api_key_status(p.name)
        res.append({
            "name": p.name,
            "display_name": getattr(p, "display_name", "") or p.name,
            "env_var": p.env_vars[0] if p.env_vars else "",
            "base_url": p.base_url,
            "has_key": (get_provider_key(p) is not None) or status["has_key"],
            "masked": status["masked"],
            "api_mode": p.api_mode,
            "has_env": provider_has_env(p.name),
            "disconnected": p.name in disconnected,
        })
    return 200, res


def post_providers_probe(body: dict) -> tuple[int, dict]:
    """POST /api/providers/probe — live model list or static fallback."""
    pname = body.get("provider", "")
    p = get_provider(pname)
    if not p:
        return 400, {"error": f"Unknown provider: {pname}"}

    from ..registry_wizard import get_provider_key, probe_provider
    key = get_provider_key(p)
    try:
        return 200, probe_provider(p, key)
    except Exception as e:
        return 200, {
            "provider": p.name,
            "models": [{"id": m} for m in p.pilot_models],
            "source": "static",
            "error": str(e),
        }


def post_providers_key(body: dict, svc: ProviderServices) -> tuple[int, dict]:
    """POST /api/providers/key — set/clear/enable/disable one provider key."""
    # Per-provider key management: set or disconnect a SPECIFIC provider's
    # key independently (e.g. turn OpenRouter off while keeping Anthropic).
    # Distinct from /api/settings, which only touches the active reach.
    # Never log api_key / bedrock credential values.
    pname = str(body.get("provider", "")).strip()
    p = get_provider(pname)
    if not p:
        return 400, {"error": f"Unknown provider: {pname}"}
    action = str(body.get("action", "")).strip().lower()
    if action in ("enable", "disable", "toggle"):
        # Non-destructive on/off for env-imported (or stored) keys. Unlike
        # 'clear', this preserves the key so the user can flip a provider
        # off and back on -- e.g. swapping a work key for a personal one.
        if action == "toggle":
            enabled = p.name in get_disconnected()
        else:
            enabled = action == "enable"
        set_provider_enabled(p.name, enabled)
        if not enabled:
            # Belt-and-suspenders: scrub in-process env immediately so
            # workers/router/puppetmaster sniffers cannot see stale AWS_* keys
            # before the next restart. set_provider_enabled already scrubs;
            # repeat after mark so any race with env re-injection loses.
            scrub_provider_env(p.name)
        # Resync agentic registry when a provider is enabled/disabled so
        # models.json is pruned without a backend restart.
        from ..auto_registry import sync_agentic_registry_safe
        sync_agentic_registry_safe()
        # Keep the active driver honest: enabling may make a better model
        # reachable; disabling may kill the current one.
        try:
            if not svc.driver_provider_available(svc.cfg.driver):
                svc.resolve_available_driver()
                svc.rebuild_pilot_and_session()
        except Exception as e:
            svc.diag("server.provider_toggle_driver_rebuild", e)
        status = get_api_key_status(p.name)
        return 200, {
            "ok": True,
            "provider": p.name,
            "enabled": enabled,
            "has_key": status["has_key"],
            "masked": status["masked"],
            "disconnected": p.name in get_disconnected(),
        }
    if action == "clear" or body.get("clear") is True:
        clear_api_key(p.name)
        # clear_api_key marks disconnected + scrubs; scrub again so any
        # in-process AWS_* / provider env left by a concurrent path is gone
        # before registry sync re-reads availability.
        scrub_provider_env(p.name)
        from ..auto_registry import sync_agentic_registry_safe
        sync_agentic_registry_safe()
        # If the active driver's provider is no longer available (we just
        # disconnected the provider backing it -- whether a 'provider:model'
        # spec OR a bare name routed through the reach), re-resolve to the
        # first available enabled model and rebuild, so the app never sits
        # on a dead driver.
        try:
            if not svc.driver_provider_available(svc.cfg.driver):
                svc.resolve_available_driver()
                svc.rebuild_pilot_and_session()
        except Exception as e:
            svc.diag("server.provider_clear_driver_rebuild", e)
    else:
        # Bedrock accepts a multi-field credential blob; other providers
        # take a single api_key string (bedrock also accepts api_key as
        # the preferred bearer-token shortcut).
        if p.name == "bedrock" and isinstance(body.get("bedrock"), dict):
            set_bedrock_credentials(body["bedrock"])
        else:
            val = str(body.get("api_key", "")).strip()
            if not val:
                return 400, {"error": "api_key required to set"}
            set_api_key(p.name, val)
        # Resync agentic registry when a provider key is set
        from ..auto_registry import sync_agentic_registry_safe
        sync_agentic_registry_safe()
    status = get_api_key_status(p.name)
    return 200, {
        "ok": True,
        "provider": p.name,
        "has_key": status["has_key"],
        "masked": status["masked"],
    }


# ---------------------------------------------------------------------------
# Auth pools
# ---------------------------------------------------------------------------

def get_auth_pools(*, provider: str = "") -> tuple[int, dict]:
    """GET /api/auth/pools — public pool summary (no raw secrets)."""
    from ..credential_pool import (
        list_all_pools_public, list_pool_public, known_pool_providers,
    )
    pname = str(provider or "").strip()
    if pname:
        return 200, list_pool_public(pname)
    return 200, {
        **list_all_pools_public(),
        "providers": known_pool_providers(),
    }


def post_auth_pools(body: dict) -> tuple[int, dict]:
    """POST /api/auth/pools — same shape as GET, provider from body."""
    from ..credential_pool import (
        list_all_pools_public, list_pool_public, known_pool_providers,
    )
    pname = str(body.get("provider", "")).strip()
    if pname:
        return 200, list_pool_public(pname)
    return 200, {
        **list_all_pools_public(),
        "providers": known_pool_providers(),
    }


def post_auth_pools_add(body: dict, svc: ProviderServices) -> tuple[int, dict]:
    """POST /api/auth/pools/add — add an api_key pool entry (no key logging)."""
    from ..credential_pool import add_api_key, list_pool_public
    pname = str(body.get("provider", "")).strip()
    auth_type = str(body.get("type", "api_key")).strip().lower()
    if not pname:
        return 400, {"error": "provider required"}
    if auth_type != "api_key":
        return 400, {
            "error": "oauth add via /api/auth/oauth/start (not yet wired); "
                     "use type=api_key for now",
        }
    key = str(body.get("api_key", "")).strip()
    label = str(body.get("label", "")).strip()
    if not key:
        return 400, {"error": "api_key required"}
    try:
        entry = add_api_key(pname, key, label=label)
    except ValueError as e:
        return 400, {"error": str(e)}
    # Mirror into keys.json so classic has_key / env paths stay honest
    # for the first (or latest) key of providers that use set_api_key.
    try:
        if pname in (
            "openrouter", "openai", "anthropic", "xai", "google",
            "groq", "deepseek", "mistral", "cursor",
        ):
            set_api_key(pname, key)
    except Exception as e:
        svc.diag("server.auth_pool_mirror_key", e)
    pub = list_pool_public(pname)
    return 200, {"ok": True, "entry_id": entry.id, **pub}


def post_auth_pools_remove(body: dict) -> tuple[int, dict]:
    """POST /api/auth/pools/remove — drop one pool entry."""
    from ..credential_pool import remove_entry, list_pool_public
    pname = str(body.get("provider", "")).strip()
    eid = str(body.get("entry_id", "")).strip()
    if not pname or not eid:
        return 400, {"error": "provider and entry_id required"}
    ok = remove_entry(pname, eid)
    return 200, {"ok": ok, **list_pool_public(pname)}


def post_auth_pools_strategy(body: dict) -> tuple[int, dict]:
    """POST /api/auth/pools/strategy — set rotation strategy for a provider."""
    from ..credential_pool import set_strategy, list_pool_public, SUPPORTED_STRATEGIES
    pname = str(body.get("provider", "")).strip()
    strategy = str(body.get("strategy", "")).strip()
    if not pname or strategy not in SUPPORTED_STRATEGIES:
        return 400, {
            "error": f"provider + strategy in {sorted(SUPPORTED_STRATEGIES)} required",
        }
    set_strategy(pname, strategy)
    return 200, {"ok": True, **list_pool_public(pname)}


def post_auth_pools_reset(body: dict) -> tuple[int, dict]:
    """POST /api/auth/pools/reset — clear cooldowns for a provider pool."""
    from ..credential_pool import load_pool, list_pool_public
    pname = str(body.get("provider", "")).strip()
    if not pname:
        return 400, {"error": "provider required"}
    load_pool(pname).reset_cooldowns()
    return 200, {"ok": True, **list_pool_public(pname)}


# ---------------------------------------------------------------------------
# OAuth device / PKCE flows
# ---------------------------------------------------------------------------

def post_auth_oauth_start(body: dict) -> tuple[int, dict]:
    """POST /api/auth/oauth/start — begin a provider OAuth login."""
    pname = str(body.get("provider", "")).strip()
    label = str(body.get("label", "")).strip()
    try:
        if pname == "openai-codex":
            from ..oauth_codex import start_codex_device_login
            res = start_codex_device_login(label=label)
        elif pname == "anthropic":
            from ..oauth_anthropic import start_anthropic_pkce_login
            res = start_anthropic_pkce_login(label=label)
        elif pname == "xai-oauth":
            from ..oauth_xai import start_xai_device_login
            res = start_xai_device_login(label=label)
        elif pname == "nous":
            from ..oauth_nous import start_nous_device_login
            res = start_nous_device_login(label=label)
        else:
            return 400, {
                "error": f"oauth start unsupported for {pname!r} "
                         "(openai-codex, anthropic, xai-oauth, nous)",
            }
    except Exception as e:
        return 400, {"error": str(e)}
    return 200, {"ok": True, **res}


def post_auth_oauth_poll(body: dict) -> tuple[int, dict]:
    """POST /api/auth/oauth/poll — poll device-code OAuth status."""
    sid = str(body.get("session_id", "")).strip()
    pname = str(body.get("provider", "")).strip()
    if not sid:
        return 400, {"error": "session_id required"}
    try:
        if pname == "xai-oauth":
            from ..oauth_xai import poll_xai_device_login
            res = poll_xai_device_login(sid)
            done_provider = "xai-oauth"
        elif pname == "nous":
            from ..oauth_nous import poll_nous_device_login
            res = poll_nous_device_login(sid)
            done_provider = "nous"
        else:
            from ..oauth_codex import poll_codex_device_login
            res = poll_codex_device_login(sid)
            done_provider = "openai-codex"
    except Exception as e:
        return 400, {"error": str(e)}
    if res.get("status") == "done":
        from ..credential_pool import list_pool_public
        res = {**res, **list_pool_public(done_provider)}
    return 200, res


def post_auth_oauth_complete(body: dict) -> tuple[int, dict]:
    """POST /api/auth/oauth/complete — Anthropic PKCE paste-the-code completion."""
    sid = str(body.get("session_id", "")).strip()
    code = str(body.get("code") or body.get("auth_code") or "").strip()
    pname = str(body.get("provider", "anthropic")).strip()
    if not sid or not code:
        return 400, {
            "error": "session_id and code required",
        }
    if pname != "anthropic":
        return 400, {
            "error": "oauth complete currently supports anthropic only",
        }
    try:
        from ..oauth_anthropic import complete_anthropic_pkce_login
        res = complete_anthropic_pkce_login(sid, code)
    except Exception as e:
        return 400, {"error": str(e)}
    if res.get("status") == "done":
        from ..credential_pool import list_pool_public
        res = {**res, **list_pool_public("anthropic")}
    return 200, res


def post_auth_oauth_cancel(body: dict) -> tuple[int, dict]:
    """POST /api/auth/oauth/cancel — abandon an in-flight OAuth session."""
    sid = str(body.get("session_id", "")).strip()
    pname = str(body.get("provider", "")).strip()
    if not sid:
        return 400, {"error": "session_id required"}
    try:
        if pname == "xai-oauth":
            from ..oauth_xai import cancel_xai_device_login
            res = cancel_xai_device_login(sid)
        elif pname == "nous":
            from ..oauth_nous import cancel_nous_device_login
            res = cancel_nous_device_login(sid)
        elif pname == "anthropic":
            from ..oauth_anthropic import cancel_anthropic_pkce_login
            res = cancel_anthropic_pkce_login(sid)
        else:
            from ..oauth_codex import cancel_codex_device_login
            res = cancel_codex_device_login(sid)
    except Exception as e:
        return 400, {"error": str(e)}
    return 200, {"ok": True, **res}


# ---------------------------------------------------------------------------
# Cursor CLI auth
# ---------------------------------------------------------------------------

def _cursor_cli_workspace(body: dict, svc: ProviderServices) -> str:
    ws = ""
    if isinstance(body, dict):
        ws = (
            body.get("workspace")
            or body.get("workspace_root")
            or body.get("repo")
            or ""
        )
    if not str(ws).strip():
        ws = getattr(svc.cfg, "repo", None) or os.environ.get("HARNESS_REPO") or ""
    return str(ws).strip()


def post_auth_cursor_cli_status(body: dict) -> tuple[int, dict]:
    """POST /api/auth/cursor-cli/status — installed/auth status (optional refresh)."""
    from ..cursor_cli_auth import get_status
    try:
        # Refresh after Sign-in / Refresh status button; otherwise
        # Settings re-opens reuse the short TTL cache.
        refresh = bool(body.get("refresh")) if isinstance(body, dict) else False
        res = get_status(refresh=refresh)
    except Exception as e:
        return 400, {"error": str(e)}
    return 200, res


def post_auth_cursor_cli_login(body: dict, svc: ProviderServices) -> tuple[int, dict]:
    """POST /api/auth/cursor-cli/login — start agent login for a workspace."""
    from ..cursor_cli_auth import start_login
    try:
        ws = _cursor_cli_workspace(body, svc)
        res = start_login(workspace=ws or None)
    except Exception as e:
        return 400, {"error": str(e)}
    status = 200 if res.get("ok") else 400
    return status, res


def post_auth_cursor_cli_trust(body: dict, svc: ProviderServices) -> tuple[int, dict]:
    """POST /api/auth/cursor-cli/trust — mark workspace trusted for agent."""
    from ..cursor_cli_auth import ensure_workspace_trusted
    try:
        ws = _cursor_cli_workspace(body, svc)
        res = ensure_workspace_trusted(ws or None)
    except Exception as e:
        return 400, {"error": str(e)}
    status = 200 if res.get("ok") else 400
    return status, res


def post_auth_cursor_cli_logout() -> tuple[int, dict]:
    """POST /api/auth/cursor-cli/logout — clear Cursor CLI auth."""
    from ..cursor_cli_auth import logout
    try:
        res = logout()
    except Exception as e:
        return 400, {"error": str(e)}
    status = 200 if res.get("ok") else 400
    return status, res


def post_auth_cursor_cli_models() -> tuple[int, dict]:
    """POST /api/auth/cursor-cli/models — list models when agent is installed."""
    from ..cursor_cli_auth import list_models, get_status
    try:
        st = get_status()
        models = list_models(live=True) if st.get("installed") else []
    except Exception as e:
        return 400, {"error": str(e)}
    return 200, {
        "ok": True,
        "models": [{"id": m} for m in models],
        "auth_kind": "cursor_account",
    }
