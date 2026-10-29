"""Non-fatal request diagnostics with explicit verification scope."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Optional

from .request_snapshot import FrozenRequest, canonical_bytes


def _canon_messages(messages: Any) -> bytes:
    if not isinstance(messages, list):
        raise TypeError('messages must be a list')
    return canonical_bytes(messages)


def _rebuild_from_history(session: Any, outbound: Any) -> Any:
    history = getattr(session, '_history', None)
    elide = getattr(session, '_wire_history', None) or getattr(session, '_elide_stale_reads', None)
    if callable(elide) and isinstance(history, list) and history:
        return elide(deepcopy(history[1:]))
    return None


def check_outbound_reconstruction(
    session: Any,
    outbound: Any,
    sys_prompt: Optional[str] = None,
    *,
    request: Optional[FrozenRequest] = None,
    request_kwargs: Optional[dict] = None,
) -> bool:
    """True only for a performed comparison; failures never raise into a turn.

    Dispatch compares against its frozen normalized source, never live history.
    The legacy history diagnostic is explicitly narrower and may mismatch after
    outbound ID/host-control adaptation; it is not used to verify dispatch.
    """
    result = {'ok': False, 'status': 'error', 'reason': 'comparison_error',
              'scope': 'history_messages', 'wire_status': 'unverified'}
    try:
        if request is not None:
            result['scope'] = 'normalized_driver_inputs'
            result['request_id'] = request.request_id
            result = request.receipt(outbound, request_kwargs)
        else:
            rebuild = _rebuild_from_history(session, outbound)
            if rebuild is None:
                result.update(status='skipped', reason='no_reconstruction')
            elif _canon_messages(outbound) != _canon_messages(rebuild):
                result.update(status='mismatch', reason='messages')
            elif (getattr(session, '_append_only', False)
                  and isinstance(getattr(session, '_frozen_system_prompt', None), str)
                  and sys_prompt is not None
                  and sys_prompt != session._frozen_system_prompt):
                result.update(status='mismatch', reason='system')
            else:
                result.update(ok=True, status='verified', reason='')
    except Exception:
        # Exception text may contain user content; record only the state.
        pass
    try:
        session._last_log_reconstruction = result
    except Exception:
        pass
    return result['ok']
