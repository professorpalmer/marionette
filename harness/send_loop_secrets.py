"""Peel secret-request turn endings out of _send_locked_inner."""
from __future__ import annotations

from typing import Any, Iterator, Optional


def peel_secret_request_message(text: str) -> tuple[str, Optional[dict]]:
    if not text:
        return text, None
    from .secret_vault import extract_secret_request_message
    return extract_secret_request_message(text)


def iter_extracted_secret_turn(
    session: Any,
    extracted: Optional[dict],
    turn: Any,
    *,
    user_message: str,
    step: int,
    swarms: int,
    turn_prose: list,
    turn_findings: list,
    last_classified: Any,
) -> Iterator[Any]:
    """Yield a secret-request card and end the turn, or yield nothing."""
    if not extracted:
        return
    if any(getattr(a, "kind", "") == "request_secret" for a in turn.actions):
        return
    from .conversation import ConvEvent
    from .secret_request import already_present, declined_this_breath, register_pending_secret_request
    from .send_loop_phases import classified_finish_kwargs, finalize_assistant_turn
    from .terminal_cause import TERMINAL_NATURAL

    connector = extracted.get("connector") or ""
    field = extracted.get("field") or ""
    if already_present(session, connector, field) or declined_this_breath(session, connector, field):
        return
    pending = register_pending_secret_request(session, extracted)
    yield ConvEvent("secret_request", {
        **(pending or extracted),
        "session_id": getattr(session, "harness_session_id", "") or "default",
        "ends_turn": True,
    })
    session._sanitize_tool_pairs()
    yield from finalize_assistant_turn(
        session, user_message=user_message, step=step,
        swarms=swarms, turn_prose=turn_prose,
        turn_findings=turn_findings,
        extra={"secret_request": True},
        stop_cause=TERMINAL_NATURAL,
        **classified_finish_kwargs(last_classified),
    )
    return True


def iter_secret_action_turn(
    session: Any,
    disposition: str,
    *,
    user_message: str,
    step: int,
    swarms: int,
    turn_prose: list,
    turn_findings: list,
    last_classified: Any,
) -> Iterator[Any]:
    if disposition != "secret_request":
        return
    from .send_loop_phases import classified_finish_kwargs, finalize_assistant_turn
    from .terminal_cause import TERMINAL_NATURAL

    session._sanitize_tool_pairs()
    yield from finalize_assistant_turn(
        session, user_message=user_message, step=step,
        swarms=swarms, turn_prose=turn_prose,
        turn_findings=turn_findings,
        extra={"secret_request": True},
        stop_cause=TERMINAL_NATURAL,
        **classified_finish_kwargs(last_classified),
    )
    return True
