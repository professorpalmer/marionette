"""Request correlation IDs for harness HTTP handlers and swallowed-failure logs.

Each inbound request gets a stable correlation id (from X-Correlation-Id when
present, otherwise a fresh uuid4). The id threads through diag.note and the
quiet server access log so support can quote one line across renderer + backend.
"""

from __future__ import annotations

import contextvars
import uuid
from contextlib import contextmanager
from typing import Iterator, Optional

_CORRELATION_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "harness_correlation_id",
    default=None,
)


def new_correlation_id() -> str:
    return str(uuid.uuid4())


def get_correlation_id() -> str:
    cur = _CORRELATION_ID.get()
    return cur if cur else ""


def set_correlation_id(correlation_id: str) -> contextvars.Token:
    return _CORRELATION_ID.set(str(correlation_id or "").strip() or new_correlation_id())


def reset_correlation_id(token: contextvars.Token) -> None:
    _CORRELATION_ID.reset(token)


def resolve_correlation_id(incoming: Optional[str]) -> str:
    raw = str(incoming or "").strip()
    return raw if raw else new_correlation_id()


@contextmanager
def correlation_scope(correlation_id: Optional[str] = None) -> Iterator[str]:
    """Bind a correlation id for the duration of a request or test."""
    cid = resolve_correlation_id(correlation_id)
    token = set_correlation_id(cid)
    try:
        yield cid
    finally:
        reset_correlation_id(token)
