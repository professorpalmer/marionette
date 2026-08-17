"""Read-only GET /api/session/performance peel.

Returns durable per-provider-step TTFT/TPS receipts for a known harness
session. Does not touch ``/api/session/state``. Auth is applied by the
table-driven GET gate before this handler runs.

Unknown session ids are 404. Sessions outside the active workspace
visibility scope (``session_visible_for_workspace``) are 403. Blank
``state_dir`` fails closed to an empty receipt list — never a tempfile
fallback.
"""

from __future__ import annotations

from typing import Optional, Set, Union

from harness.api.session_control import SessionControlServices
from harness.sessions import session_visible_for_workspace
from harness.stream_performance_store import (
    MAX_RECEIPTS_PER_SESSION,
    StreamPerformanceReceiptStore,
    safe_session_id,
)

JsonPayload = Union[dict, list]


def _qs_first(qs: dict, key: str) -> str:
    return (qs.get(key, [""])[0] or "").strip()


def _parse_limit(raw: str, *, default: int, cap: int) -> int:
    text = (raw or "").strip()
    if not text:
        return default
    try:
        n = int(text)
    except (TypeError, ValueError):
        return default
    if n < 1:
        return 1
    if n > cap:
        return cap
    return n


def _state_dir(svc: SessionControlServices) -> str:
    cfg = getattr(svc, "cfg", None)
    return str(getattr(cfg, "state_dir", None) or "").strip()


def _workspace_root(svc: SessionControlServices) -> str:
    cfg = getattr(svc, "cfg", None)
    return str(
        getattr(cfg, "repo", None) or getattr(cfg, "workspace_root", None) or ""
    ).strip()


def _is_current_session(svc: SessionControlServices, session_id: str) -> bool:
    try:
        if svc.get_sessions is not None:
            sessions = svc.get_sessions()
            active = str(getattr(sessions, "active", None) or "").strip()
            if active and session_id == active:
                return True
    except Exception:
        pass
    try:
        pilot = svc.get_pilot()
        pid = str(getattr(pilot, "harness_session_id", None) or "").strip()
        if pid and session_id == pid:
            return True
    except Exception:
        pass
    return False


def _session_rows(svc: SessionControlServices) -> list:
    rows: list = []
    sessions = None
    try:
        if svc.get_sessions is not None:
            sessions = svc.get_sessions()
    except Exception:
        sessions = None
    if sessions is None:
        return rows
    rows_fn = getattr(sessions, "rows", None)
    if not callable(rows_fn):
        return rows
    try:
        for row in rows_fn() or []:
            if isinstance(row, dict):
                rows.append(row)
    except Exception:
        return rows
    return rows


def _lookup_session_row(svc: SessionControlServices, session_id: str) -> Optional[dict]:
    for row in _session_rows(svc):
        if str(row.get("id") or "").strip() == session_id:
            return row
    return None


def _known_session_ids(svc: SessionControlServices) -> Set[str]:
    known: Set[str] = set()
    sessions = None
    try:
        if svc.get_sessions is not None:
            sessions = svc.get_sessions()
    except Exception:
        sessions = None
    if sessions is not None:
        try:
            active = str(getattr(sessions, "active", None) or "").strip()
            if active:
                known.add(active)
        except Exception:
            pass
        for row in _session_rows(svc):
            rid = str(row.get("id") or "").strip()
            if rid:
                known.add(rid)
    try:
        pilot = svc.get_pilot()
        pid = str(getattr(pilot, "harness_session_id", None) or "").strip()
        if pid:
            known.add(pid)
    except Exception:
        pass
    return known


def _resolve_session_id(qs: dict, svc: SessionControlServices) -> tuple[str, Optional[tuple[int, dict]]]:
    requested = _qs_first(qs, "session_id")
    if not requested:
        sessions = None
        try:
            if svc.get_sessions is not None:
                sessions = svc.get_sessions()
        except Exception:
            sessions = None
        if sessions is not None:
            requested = str(getattr(sessions, "active", None) or "").strip()
        if not requested:
            try:
                pilot = svc.get_pilot()
                requested = str(getattr(pilot, "harness_session_id", None) or "").strip()
            except Exception:
                requested = ""
    if not requested:
        return "", (400, {"error": "missing session id"})
    # Membership uses the caller-supplied id (stripped). Sanitizing first
    # would turn ``../known`` into a known row and open a traversal alias.
    if requested not in _known_session_ids(svc):
        return requested, (404, {"error": "unknown session"})
    if not safe_session_id(requested):
        return requested, (404, {"error": "unknown session"})
    workspace_root = _workspace_root(svc)
    if workspace_root and not _is_current_session(svc, requested):
        row = _lookup_session_row(svc, requested)
        if row is not None and not session_visible_for_workspace(
            row, workspace_root, _state_dir(svc),
        ):
            return requested, (403, {"error": "session not visible in active workspace"})
    return requested, None


def get_session_performance(
    qs: dict, svc: SessionControlServices
) -> tuple[int, JsonPayload]:
    """GET /api/session/performance?session_id=&limit=."""
    qs = qs or {}
    session_id, err = _resolve_session_id(qs, svc)
    if err is not None:
        return err
    limit = _parse_limit(
        _qs_first(qs, "limit"),
        default=MAX_RECEIPTS_PER_SESSION,
        cap=MAX_RECEIPTS_PER_SESSION,
    )
    state_dir = _state_dir(svc)
    if not state_dir:
        return 200, {
            "ok": True,
            "session_id": session_id,
            "receipts": [],
            "count": 0,
        }
    try:
        store = StreamPerformanceReceiptStore(state_dir)
        receipts = store.list_receipts(session_id, limit=limit)
    except Exception:
        receipts = []
    return 200, {
        "ok": True,
        "session_id": session_id,
        "receipts": receipts,
        "count": len(receipts),
    }
