"""Auth pool / OAuth / Cursor CLI HTTP route bodies.

These handlers already lived in :mod:`harness.api.providers` after the first
API peel. This module re-exports them under an ``auth`` ownership name so
``server.Handler`` dispatches auth routes through ``harness.api.auth`` (matching
the audit peel plan) without duplicating credential logic.
"""

from __future__ import annotations

from .providers import (  # noqa: F401 — re-export surface for Handler
    ProviderServices,
    get_auth_pools,
    post_auth_cursor_cli_login,
    post_auth_cursor_cli_logout,
    post_auth_cursor_cli_models,
    post_auth_cursor_cli_status,
    post_auth_cursor_cli_trust,
    post_auth_oauth_cancel,
    post_auth_oauth_complete,
    post_auth_oauth_poll,
    post_auth_oauth_start,
    post_auth_pools,
    post_auth_pools_add,
    post_auth_pools_remove,
    post_auth_pools_reset,
    post_auth_pools_strategy,
)

__all__ = [
    "ProviderServices",
    "get_auth_pools",
    "post_auth_pools",
    "post_auth_pools_add",
    "post_auth_pools_remove",
    "post_auth_pools_strategy",
    "post_auth_pools_reset",
    "post_auth_oauth_start",
    "post_auth_oauth_poll",
    "post_auth_oauth_complete",
    "post_auth_oauth_cancel",
    "post_auth_cursor_cli_status",
    "post_auth_cursor_cli_login",
    "post_auth_cursor_cli_trust",
    "post_auth_cursor_cli_logout",
    "post_auth_cursor_cli_models",
]
