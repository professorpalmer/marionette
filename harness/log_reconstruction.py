from __future__ import annotations

"""Cheap outbound-request honesty check.

DeepSeek's agent-loop invariant: a frozen request must match
``deriveMessages()`` plus folded headers. Marionette has no
``deriveMessages``; the equivalent is the just-built
``_messages_for_provider()`` payload versus a rebuild from the already
sanitized history (``_elide_stale_reads(history[1:])``) plus the system
prompt passed to ``pilot.chat`` / ``chat_stream``.

The check must not call ``_messages_for_provider`` again — that seam
sanitizes and is counted once per dispatch. Mismatch is logged and never
fails the turn.
"""

import json
from typing import Any, Optional


def _canon_messages(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    slim = []
    for msg in messages:
        if not isinstance(msg, dict):
            slim.append(str(msg))
            continue
        row = {
            "role": msg.get("role"),
            "content": msg.get("content"),
        }
        if msg.get("tool_call_id"):
            row["tool_call_id"] = msg.get("tool_call_id")
        if msg.get("tool_calls"):
            row["tool_calls"] = msg.get("tool_calls")
        slim.append(row)
    return json.dumps(slim, ensure_ascii=False, sort_keys=True, default=str)


def _rebuild_from_history(session: Any, outbound: Any) -> Any:
    history = getattr(session, "_history", None)
    elide = getattr(session, "_elide_stale_reads", None)
    if callable(elide) and isinstance(history, list) and history:
        return elide(history[1:])
    return outbound


def check_outbound_reconstruction(
    session: Any,
    outbound: Any,
    sys_prompt: Optional[str] = None,
) -> bool:
    """Compare the about-to-send payload to a fresh history rebuild.

    Returns True when honest (or when the check itself fails). Returns False
    on a logged mismatch. Never raises into the turn.
    """
    try:
        rebuild = _rebuild_from_history(session, outbound)
        if _canon_messages(outbound) != _canon_messages(rebuild):
            try:
                from .diag import note as _diag_note

                _diag_note(
                    "log_reconstruction.messages",
                    "outbound messages diverge from sanitized history",
                )
            except Exception:
                pass
            session._last_log_reconstruction = {
                "ok": False,
                "reason": "messages",
            }
            return False

        append_only = bool(getattr(session, "_append_only", False))
        frozen = getattr(session, "_frozen_system_prompt", None)
        if (
            append_only
            and isinstance(frozen, str)
            and sys_prompt is not None
            and sys_prompt != frozen
        ):
            try:
                from .diag import note as _diag_note

                _diag_note(
                    "log_reconstruction.system",
                    "append-only system prompt diverges from the frozen header",
                )
            except Exception:
                pass
            session._last_log_reconstruction = {
                "ok": False,
                "reason": "system",
            }
            return False

        session._last_log_reconstruction = {"ok": True, "reason": ""}
        return True
    except Exception:
        try:
            session._last_log_reconstruction = {"ok": True, "reason": "skipped"}
        except Exception:
            pass
        return True
