"""Turn identity ContextVar for attributing allowlist writes to a send/decide.

Mirrors ``harness.correlation``: stdlib contextvars only, no new framework.
``send()`` binds a fresh turn id for the duration of a user turn; approval
``decide`` reuses the current id when already set (same context) or binds a
fresh one for HTTP/operator decisions that arrive off the send thread.
"""

from __future__ import annotations

import contextvars
import uuid
from contextlib import contextmanager
from typing import Iterator, Optional

_TURN_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "harness_turn_id",
    default=None,
)


def new_turn_id() -> str:
    return str(uuid.uuid4())


def get_turn_id() -> str:
    cur = _TURN_ID.get()
    return cur if cur else ""


def set_turn_id(turn_id: Optional[str]) -> contextvars.Token:
    """Bind ``turn_id``. Empty/None clears the ContextVar (no auto-mint)."""
    raw = str(turn_id or "").strip()
    return _TURN_ID.set(raw if raw else None)


def reset_turn_id(token: contextvars.Token) -> None:
    _TURN_ID.reset(token)


def resolve_turn_id(incoming: Optional[str] = None) -> str:
    """Prefer an explicit id, else the bound ContextVar, else a fresh uuid."""
    raw = str(incoming or "").strip()
    if raw:
        return raw
    cur = _TURN_ID.get()
    return cur if cur else new_turn_id()


@contextmanager
def turn_scope(turn_id: Optional[str] = None) -> Iterator[str]:
    """Bind a turn id for the duration of a send or approval decide."""
    tid = resolve_turn_id(turn_id)
    token = set_turn_id(tid)
    try:
        yield tid
    finally:
        reset_turn_id(token)
