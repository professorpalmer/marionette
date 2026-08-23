"""Session-side secret-request cards. Display never stores the secret."""

from __future__ import annotations

import json
from typing import Any, Optional

from .secret_vault import (
    get_secret,
    parse_secret_request_payload,
    presence,
    presence_payload,
)


def secret_ref_key(connector: str, field: str) -> str:
    return f"{connector}::{field}"


def display_secret_request_row(pending: dict, *, status: str) -> dict:
    return {
        "type": "secret_request",
        "id": pending.get("id") or secret_ref_key(pending.get("connector") or "", pending.get("field") or ""),
        "label": pending.get("label") or "",
        "connector": pending.get("connector") or "",
        "field": pending.get("field") or "",
        "description": pending.get("description") or "",
        "status": status,
    }


def upsert_display_secret_request(session: Any, pending: dict, *, status: str) -> None:
    row = display_secret_request_row(pending, status=status)
    display = getattr(session, "_display_transcript", None)
    if display is None:
        session._display_transcript = [row]
        return
    key = secret_ref_key(row["connector"], row["field"])
    for i, existing in enumerate(display):
        if (
            isinstance(existing, dict)
            and existing.get("type") == "secret_request"
            and secret_ref_key(existing.get("connector") or "", existing.get("field") or "") == key
        ):
            display[i] = row
            return
    display.append(row)


def register_pending_secret_request(session: Any, payload: dict) -> Optional[dict]:
    parsed = parse_secret_request_payload(payload) or payload
    label = str(parsed.get("label") or "").strip()
    connector = str(parsed.get("connector") or "").strip()
    field = str(parsed.get("field") or "").strip()
    description = str(parsed.get("description") or "").strip()
    if not (label and connector and field):
        return None
    agent_id = str(getattr(session, "harness_session_id", "") or "").strip() or "default"
    pending = {
        "id": secret_ref_key(connector, field),
        "label": label,
        "connector": connector,
        "field": field,
        "description": description,
        "session_id": agent_id,
    }
    if getattr(session, "_pending_secret_requests", None) is None:
        session._pending_secret_requests = {}
    session._pending_secret_requests[secret_ref_key(connector, field)] = pending
    upsert_display_secret_request(session, pending, status="pending")
    return pending


def decide_secret_request(
    session: Any,
    *,
    connector: str,
    field: str,
    provided: bool,
) -> Optional[dict]:
    key = secret_ref_key(connector, field)
    pending_map = getattr(session, "_pending_secret_requests", None) or {}
    pending = pending_map.get(key)
    if pending is None:
        pending = {
            "id": key,
            "label": field,
            "connector": connector,
            "field": field,
            "description": "",
            "session_id": getattr(session, "harness_session_id", "") or "default",
        }
    pending_map.pop(key, None)
    session._pending_secret_requests = pending_map
    if not provided:
        declined = getattr(session, "_declined_secret_requests", None)
        if declined is None:
            declined = set()
            session._declined_secret_requests = declined
        declined.add(key)
    upsert_display_secret_request(
        session, pending, status="saved" if provided else "declined",
    )
    payload = presence_payload(connector, field, provided)
    history = getattr(session, "_history", None)
    if isinstance(history, list):
        history.append({
            "role": "user",
            "content": json.dumps(payload, separators=(",", ":")),
            "secret_request_resume": True,
        })
    return pending


def declined_this_breath(session: Any, connector: str, field: str) -> bool:
    declined = getattr(session, "_declined_secret_requests", None) or set()
    return secret_ref_key(connector, field) in declined


def already_present(session: Any, connector: str, field: str) -> bool:
    agent_id = str(getattr(session, "harness_session_id", "") or "").strip() or "default"
    if presence(agent_id, connector, field).get("present"):
        return True
    return bool(get_secret(agent_id, connector, field))
