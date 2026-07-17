"""Characterization: build_post_json_routes / build_get_routes membership.

Locks path→handler presence for a sample of guarded GET/POST routes, that
handlers are callable, and that the auth public-path set stays aligned with
PUBLIC_GET_PATHS (static shell) rather than the JSON route tables.
Zero wire/behavior changes — table shape only.
"""
from __future__ import annotations

import inspect

import harness.api.static as static_api
import harness.http_routes as http_routes
import harness.server as srv


def _fresh_route_tables():
    srv._POST_JSON_ROUTES = None
    srv._GET_ROUTES = None
    post = srv._post_json_routes()
    get = srv._get_routes()
    return post, get


# Guarded GET paths exercised by test_handler_dispatch (token required).
_SAMPLE_GUARDED_GET = (
    "/api/memory",
    "/api/config",
    "/api/platform",
    "/api/settings",
    "/api/session/state",
    "/api/jobs",
    "/api/sessions",
    "/api/providers",
    "/api/chat/events",
)

# Guarded POST paths (all POST JSON routes require the harness token).
_SAMPLE_GUARDED_POST = (
    "/api/settings",
    "/api/sessions/relocate",
    "/api/sessions/move",
    "/api/sessions/create",
    "/api/platform",
    "/api/auth/pools",
    "/api/file/write",
    "/api/restart",
    "/api/session/interrupt",
)


def test_build_helpers_match_server_lazy_tables():
    """Direct builders and server caches must expose the same path sets."""
    post, get = _fresh_route_tables()
    svc = srv._route_services()
    assert set(http_routes.build_post_json_routes(svc)) == set(post)
    assert set(http_routes.build_get_routes(svc)) == set(get)


def test_post_json_routes_sample_membership_and_callable():
    post, _ = _fresh_route_tables()
    assert len(post) >= 90
    for path in _SAMPLE_GUARDED_POST:
        assert path in post, f"missing POST route {path}"
        handler = post[path]
        assert callable(handler), f"POST handler for {path} is not callable"
        # post_json wrappers and custom handlers are (handler, body) -> Any
        sig = inspect.signature(handler)
        assert len(sig.parameters) >= 2, f"POST handler {path} arity unexpected"


def test_get_routes_sample_membership_and_callable():
    _, get = _fresh_route_tables()
    assert len(get) >= 50
    for path in _SAMPLE_GUARDED_GET:
        assert path in get, f"missing GET route {path}"
        handler = get[path]
        assert callable(handler), f"GET handler for {path} is not callable"
        # GetHandler: (handler, parsed_url, query_dict) -> Any
        sig = inspect.signature(handler)
        assert len(sig.parameters) >= 3, f"GET handler {path} arity unexpected"


def test_public_get_paths_stay_out_of_json_route_tables():
    """Bootstrap shell paths are PUBLIC_GET_PATHS, not table-driven API routes."""
    post, get = _fresh_route_tables()
    public = static_api.PUBLIC_GET_PATHS
    assert public == frozenset({"/", "/index.html", "/app.js", "/app.css"})
    assert srv.Handler._PUBLIC_GET_PATHS is public
    for path in public:
        assert path not in get
        assert path not in post


def test_preauth_wiki_connect_not_in_get_table():
    """/api/wiki/connect is a do_GET special case before the auth gate."""
    _, get = _fresh_route_tables()
    assert "/api/wiki/connect" not in get


def test_guarded_api_sample_not_in_public_allowlist():
    """Paths that dispatch tests treat as token-gated stay off PUBLIC_GET_PATHS."""
    public = static_api.PUBLIC_GET_PATHS
    for path in _SAMPLE_GUARDED_GET:
        assert path not in public
    for path in _SAMPLE_GUARDED_POST:
        assert path not in public
