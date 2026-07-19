"""Wiki HTTP route bodies and helpers (peeled from ``harness.server``).

Cache/nonce state and JSON/HTML response builders live here so this module
never imports ``harness.server`` at top level. ``server.Handler`` keeps auth
gates (``_guard``, token, ``_host_ok``) and thin path delegates; tests keep
using historical ``harness.server._wiki_*`` names via re-exports.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote as _quote

from ..wiki_config import (
    clear_wiki_config,
    get_wiki_config,
    is_hosted_portablellm_base,
    set_wiki_config,
)


# ---------------------------------------------------------------------------
# Shared constants / in-process state (re-exported from server for tests)
# ---------------------------------------------------------------------------

WIKI_NEEDS_AUTH_HINT = (
    "Connected at public tier only. Paste your personal LLM URL or owner token "
    "in Settings → Wiki Graph (portablellm.wiki Owner console)."
)

# Short-TTL cache for the /api/wiki/graph payload. Each fetch is an HTTP round
# trip to the wiki host (up to an 8s timeout when slow/unreachable), and the
# wiki graph changes rarely, so a brief cache removes the repeated stall on the
# panel without making the data meaningfully stale.
# Key includes a token fingerprint so saving a token after a public-tier fetch
# cannot keep serving the stale public payload for the TTL window.
wiki_graph_cache: dict = {}  # cache_key -> (monotonic_expiry, payload_dict)
WIKI_GRAPH_TTL = 60.0  # seconds

# One-shot nonces for loopback wiki handoff (avoids marionette:// on Windows,
# which the OS routes to the Microsoft Store when the protocol is unregistered).
wiki_connect_nonces: dict = {}  # nonce -> monotonic_expiry
WIKI_CONNECT_NONCE_TTL = 900.0  # 15 minutes


@dataclass
class WikiServices:
    """Explicit deps for wiki HTTP handlers (injected by ``server.py``)."""

    cfg: Any
    get_pilot: Callable[[], Any]


# ---------------------------------------------------------------------------
# Helpers (historical server names re-exported with leading underscore)
# ---------------------------------------------------------------------------

def wiki_cache_key(client) -> str:
    base = getattr(client, "base_url", "") or ""
    tok = getattr(client, "token", "") or ""
    th = hashlib.sha256(tok.encode("utf-8")).hexdigest()[:16] if tok else "none"
    return "%s|%s" % (base, th)


def clear_wiki_graph_cache() -> None:
    wiki_graph_cache.clear()


def mint_wiki_connect_nonce() -> str:
    # Drop expired entries opportunistically.
    now = time.monotonic()
    for k, exp in list(wiki_connect_nonces.items()):
        if exp <= now:
            wiki_connect_nonces.pop(k, None)
    nonce = secrets.token_urlsafe(24)
    wiki_connect_nonces[nonce] = now + WIKI_CONNECT_NONCE_TTL
    return nonce


def consume_wiki_connect_nonce(nonce: str) -> bool:
    if not nonce:
        return False
    exp = wiki_connect_nonces.pop(nonce, None)
    return exp is not None and exp > time.monotonic()


def wiki_status_extras(client, graph_res=None) -> dict:
    """Extra wiki status fields: needs_auth, viewer_tier, page_count, hint."""
    extras = {}
    base = getattr(client, "base_url", "") or ""
    has_token = bool(getattr(client, "token", "") or "")
    if not has_token:
        try:
            has_token = bool(get_wiki_config().get("has_token"))
        except Exception:
            has_token = False
    meta = {}
    try:
        meta = client.manifest_meta() or {}
    except Exception:
        meta = {}
    page_count = meta.get("page_count")
    if page_count is None and isinstance(graph_res, dict):
        page_count = len(graph_res.get("nodes") or [])
    if page_count is not None:
        extras["page_count"] = page_count
    viewer_tier = meta.get("viewer_tier")
    viewer_is_owner = meta.get("viewer_is_owner")
    if viewer_tier is not None:
        extras["viewer_tier"] = viewer_tier
    if viewer_is_owner is not None:
        extras["viewer_is_owner"] = viewer_is_owner
    needs_auth = False
    # Personal LLM URLs mint private-tier *share* tokens — viewer_is_owner is
    # False for those and must NOT force needs_auth. Only missing token or an
    # actual public-tier response means the connection is incomplete.
    if is_hosted_portablellm_base(base):
        if not has_token:
            needs_auth = True
        elif (viewer_tier or "").lower() == "public":
            needs_auth = True
    if needs_auth:
        extras["status"] = "needs_auth"
        if has_token and (viewer_tier or "").lower() == "public":
            extras["hint"] = (
                "Token is saved but the wiki still returns public tier. "
                "Disconnect, then Connect again (or paste a fresh personal LLM URL)."
            )
        else:
            extras["hint"] = WIKI_NEEDS_AUTH_HINT
        extras["needs_owner_token"] = True
    return extras


def _wiki_unreachable(err: str) -> bool:
    err_l = str(err or "").lower()
    return any(t in err_l for t in (
        "connection refused", "refused", "timed out", "timeout",
        "name or service not known", "nodename nor servname",
        "failed to establish", "max retries", "cannot connect",
        "connection error", "urlopen error", "getaddrinfo",
        "no route to host", "network is unreachable", "[errno",
    ))


def _make_wiki_client(svc: WikiServices):
    from ..wiki import WikiClient
    try:
        return WikiClient(base_url=svc.cfg.wiki_url or "", timeout=8), None
    except Exception as e:
        return None, str(e)


# ---------------------------------------------------------------------------
# Route bodies — return (status, payload) or (status, body, content_type)
# ---------------------------------------------------------------------------

def handle_wiki_connect(qs: dict) -> tuple[int, str, str]:
    """Apply wiki config from a loopback handoff (nonce + personal LLM URL).

    This route is intentionally *pre-auth*: the browser reaches it via a
    plain navigation (popout/window handoff) and cannot reliably attach
    `X-Harness-Token` headers to the GET request.

    The caller must already enforce loopback Host, and this handler enforces
    nonce strictness: it consumes the one-shot nonce before saving any config
    so replays and stale/nonces always fail.

    Returns HTML bodies (success and failure) with the historical status
    codes.
    """
    nonce = (qs.get("nonce") or [""])[0]
    raw_url = (qs.get("url") or [""])[0]
    api_base = (qs.get("api_base") or [""])[0]
    token = (qs.get("token") or [""])[0]
    if not consume_wiki_connect_nonce(nonce):
        html = (
            "<!doctype html><html><head><meta charset=utf-8>"
            "<title>Wiki connect failed</title></head><body style='"
            "font-family:system-ui;background:#111;color:#eee;padding:2rem'>"
            "<h1>Link expired</h1>"
            "<p>Open Connect again from Marionette State → Wiki.</p>"
            "</body></html>"
        )
        return 403, html, "text/html"
    try:
        if raw_url:
            res = set_wiki_config(api_base=raw_url, owner_token=None)
        elif api_base or token:
            res = set_wiki_config(
                api_base=api_base or None,
                owner_token=token or None,
            )
        else:
            html = (
                "<!doctype html><html><head><meta charset=utf-8>"
                "<title>Wiki connect failed</title></head><body style='"
                "font-family:system-ui;background:#111;color:#eee;padding:2rem'>"
                "<h1>Missing wiki URL</h1>"
                "<p>No personal LLM URL was provided.</p>"
                "</body></html>"
            )
            return 400, html, "text/html"
    except Exception as e:
        html = (
            "<!doctype html><html><head><meta charset=utf-8>"
            "<title>Wiki connect failed</title></head><body style='"
            "font-family:system-ui;background:#111;color:#eee;padding:2rem'>"
            "<h1>Could not save</h1><p>%s</p></body></html>"
        ) % (str(e).replace("<", "&lt;")[:200],)
        return 500, html, "text/html"
    clear_wiki_graph_cache()
    base = (res or {}).get("api_base") or ""
    html = (
        "<!doctype html><html><head><meta charset=utf-8>"
        "<title>Wiki linked</title></head><body style='"
        "font-family:system-ui;background:#111;color:#eee;padding:2rem'>"
        "<h1>Wiki linked</h1>"
        "<p>Marionette saved your portable LLM wiki connection.</p>"
        "<p style='color:#888;font-size:12px;word-break:break-all'>%s</p>"
        "<p>You can close this window.</p>"
        "</body></html>"
    ) % (base.replace("<", "&lt;"),)
    return 200, html, "text/html"


def post_wiki_ingest_prepared(body: dict, svc: WikiServices) -> tuple[int, dict]:
    """One-click approve: file locally-orchestrated pages into the wiki."""
    pages = body.get("pages") or []
    count = svc.get_pilot().ingest_prepared_pages(pages)
    # Same cache bust as connect/disconnect -- ingested pages change the
    # tenant graph/status the UI polls.
    clear_wiki_graph_cache()
    return 200, {"ok": count > 0, "ingested": count}


def post_wiki_config(body: dict) -> tuple[int, dict]:
    api_base = body.get("api_base")
    owner_token = body.get("owner_token")
    res = set_wiki_config(
        api_base=api_base if api_base is not None else None,
        owner_token=owner_token if owner_token is not None else None,
    )
    clear_wiki_graph_cache()
    return 200, res


def post_wiki_disconnect() -> tuple[int, dict]:
    res = clear_wiki_config()
    clear_wiki_graph_cache()
    return 200, res


def post_wiki_handoff(host: str) -> tuple[int, dict]:
    """Mint a one-shot nonce and return a loopback setup URL.

    Caller must already validate ``host`` with ``_host_ok``.
    """
    nonce = mint_wiki_connect_nonce()
    return_url = "http://%s/api/wiki/connect" % host
    setup_url = (
        "https://portablellm.wiki/connect/marionette"
        "?client=marionette"
        "&return=%s"
        "&nonce=%s"
    ) % (_quote(return_url, safe=""), _quote(nonce, safe=""))
    return 200, {
        "ok": True,
        "nonce": nonce,
        "return_url": return_url,
        "setup_url": setup_url,
    }


def get_wiki_config_payload() -> tuple[int, dict]:
    return 200, get_wiki_config()


def get_wiki_graph(svc: WikiServices) -> tuple[int, dict]:
    """WikiClient graph payload for the State pane (auth already applied)."""
    client, _client_err = _make_wiki_client(svc)
    if client is None or not client.base_url:
        return 200, {
            "configured": False,
            "status": "not_configured",
            "nodes": [],
            "edges": [],
            "base_url": "",
        }
    ck = wiki_cache_key(client)
    cached = wiki_graph_cache.get(ck)
    if cached and cached[0] > time.monotonic():
        return 200, cached[1]
    try:
        res = client.graph()
    except Exception as e:
        res = {"error": f"Unexpected error: {str(e)}", "nodes": [], "edges": []}
    if res.get("error"):
        # Distinguish "wiki host unreachable / not actually set up" from a real
        # API error. An unreachable host (connection refused, DNS failure, timeout)
        # should look like NOT CONNECTED -- neutral -- not a scary red ERROR, so a
        # user who never set up a wiki is not confused by a broken-looking panel.
        unreachable = _wiki_unreachable(res.get("error", ""))
        # If the wiki was NEVER configured (no base_url/token), an
        # unreachable result is just "not set up" -> neutral. But if a
        # base_url IS configured, a transient failure must NOT wipe the
        # connection -- keep configured + base_url and report a retryable
        # error so Refresh recovers instead of showing "not connected".
        is_configured = bool(client.base_url)
        if unreachable and not is_configured:
            return 200, {
                "configured": False,
                "status": "not_configured",
                "nodes": [],
                "edges": [],
                "base_url": "",
            }
        return 200, {
            "configured": True,
            "status": "error",
            "nodes": [],
            "edges": [],
            "error": ("Wiki temporarily unreachable -- click Refresh to retry."
                      if unreachable else res["error"]),
            "retryable": True,
            "base_url": client.base_url,
        }
    payload = {
        "configured": True,
        "status": "ok",
        "nodes": res.get("nodes") or [],
        "edges": res.get("edges") or [],
        "base_url": client.base_url,
    }
    payload.update(wiki_status_extras(client, res))
    wiki_graph_cache[wiki_cache_key(client)] = (
        time.monotonic() + WIKI_GRAPH_TTL, payload)
    return 200, payload


def get_wiki_status(svc: WikiServices) -> tuple[int, dict]:
    """Lightweight summary for the State pane strip — counts only.

    Reuses the same graph cache as ``/api/wiki/graph``.
    """
    client, _ = _make_wiki_client(svc)
    if client is None or not client.base_url:
        return 200, {
            "configured": False,
            "status": "not_configured",
            "page_count": 0,
            "link_count": 0,
            "base_url": "",
        }
    ck = wiki_cache_key(client)
    cached_entry = wiki_graph_cache.get(ck)
    if cached_entry and cached_entry[0] > time.monotonic():
        cached = cached_entry[1]
        page_count = cached.get("page_count")
        if page_count is None:
            page_count = len(cached.get("nodes") or [])
        return 200, {
            "configured": cached.get("configured", True),
            "status": cached.get("status", "ok"),
            "page_count": page_count,
            "link_count": len(cached.get("edges") or []),
            "error": cached.get("error"),
            "retryable": cached.get("retryable"),
            "base_url": cached.get("base_url") or client.base_url,
            "hint": cached.get("hint"),
            "viewer_tier": cached.get("viewer_tier"),
            "viewer_is_owner": cached.get("viewer_is_owner"),
            "needs_owner_token": cached.get("needs_owner_token"),
        }
    try:
        res = client.graph()
    except Exception as e:
        res = {"error": f"Unexpected error: {str(e)}", "nodes": [], "edges": []}
    if res.get("error"):
        unreachable = _wiki_unreachable(res.get("error", ""))
        is_configured = bool(client.base_url)
        if unreachable and not is_configured:
            return 200, {
                "configured": False,
                "status": "not_configured",
                "page_count": 0,
                "link_count": 0,
                "base_url": "",
            }
        return 200, {
            "configured": True,
            "status": "error",
            "page_count": 0,
            "link_count": 0,
            "error": ("Wiki temporarily unreachable -- click Refresh to retry."
                      if unreachable else res["error"]),
            "retryable": True,
            "base_url": client.base_url,
        }
    nodes = res.get("nodes") or []
    edges = res.get("edges") or []
    extras = wiki_status_extras(client, res)
    payload = {
        "configured": True,
        "status": "ok",
        "nodes": nodes,
        "edges": edges,
        "base_url": client.base_url,
    }
    payload.update(extras)
    wiki_graph_cache[wiki_cache_key(client)] = (
        time.monotonic() + WIKI_GRAPH_TTL, payload)
    page_count = extras.get("page_count")
    if page_count is None:
        page_count = len(nodes)
    return 200, {
        "configured": True,
        "status": payload.get("status", "ok"),
        "page_count": page_count,
        "link_count": len(edges),
        "base_url": client.base_url,
        "hint": extras.get("hint"),
        "viewer_tier": extras.get("viewer_tier"),
        "viewer_is_owner": extras.get("viewer_is_owner"),
        "needs_owner_token": extras.get("needs_owner_token"),
    }
