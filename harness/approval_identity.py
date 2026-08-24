"""contextvars turn identity around approval decide.

Thin names for the approval path. The ContextVar lives in
:mod:`harness.turn_identity` so send-loop and decide share one turn id.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Iterator, Optional

from .turn_identity import get_turn_id, reset_turn_id, set_turn_id, turn_scope


def get_approval_turn_id() -> Optional[str]:
    """Current turn id, or None when decide is not inside a turn."""
    return get_turn_id() or None


def set_approval_turn_id(turn_id: Optional[str]) -> contextvars.Token:
    return set_turn_id(turn_id)


def reset_approval_turn_id(token: contextvars.Token) -> None:
    reset_turn_id(token)


@contextmanager
def approval_turn(turn_id: str | int | None) -> Iterator[Optional[str]]:
    """Bind an approval/send turn id. ``None``/empty clears for the block."""
    if turn_id is None or not str(turn_id).strip():
        token = set_turn_id(None)
        try:
            yield get_approval_turn_id()
        finally:
            reset_turn_id(token)
        return
    with turn_scope(str(turn_id)) as tid:
        yield tid
