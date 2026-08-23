"""Secret-request HTTP handlers. Values are never logged or listed."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Union

from ..secret_vault import (
    delete_secret,
    normalize_agent_id,
    normalize_connector,
    normalize_field,
    parse_secret_request_payload,
    presence,
    presence_payload,
    put_secret,
    validate_secret_ref,
)

JsonPayload = Union[dict, list]


@dataclass
class SecretServices:
    get_runners: Callable[[], Any]


def _agent_id(body: dict, svc: SecretServices | None = None) -> str:
    session_id = str(body.get("session_id") or body.get("agent_id") or "").strip()
    if session_id:
        return normalize_agent_id(session_id)
    if svc is not None:
        runners = svc.get_runners()
        if hasattr(runners, "values"):
            for runner in runners.values():
                sid = str(getattr(runner, "harness_session_id", "") or "").strip()
                if sid:
                    return normalize_agent_id(sid)
    return "default"


def _runner(svc: SecretServices, session_id: str):
    return svc.get_runners().get(session_id)


def _qs_get(qs: dict, key: str) -> str:
    val = qs.get(key, "")
    if isinstance(val, list):
        return str(val[0] if val else "")
    return str(val or "")


def get_secrets_presence(qs: dict, svc: SecretServices) -> tuple[int, JsonPayload]:
    """GET /api/secrets/presence — present|missing only."""
    agent_id = normalize_agent_id(_qs_get(qs, "session_id") or _qs_get(qs, "agent_id"))
    connector = normalize_connector(_qs_get(qs, "connector"))
    field = normalize_field(_qs_get(qs, "field"))
    return 200, presence(agent_id, connector, field)


def post_secrets_submit(body: dict, svc: SecretServices) -> tuple[int, JsonPayload]:
    """POST /api/secrets/submit — store value, resume presence only."""
    session_id = str(body.get("session_id") or "").strip()
    connector = normalize_connector(str(body.get("connector") or ""))
    field = normalize_field(str(body.get("field") or ""))
    value = str(body.get("value") or "")
    err = validate_secret_ref(connector, field)
    if err:
        return 400, {"error": err}
    if not value.strip():
        return 400, {"error": "value is required"}
    agent_id = _agent_id(body, svc)
    try:
        row = put_secret(agent_id, connector, field, value)
    except ValueError as exc:
        return 400, {"error": str(exc)}
    # Drop the raw value immediately so later exception text cannot leak it.
    body["value"] = ""
    resume = False
    if session_id:
        runner = _runner(svc, session_id)
        decide = getattr(runner, "decide_secret_request", None)
        if callable(decide):
            decide(connector=connector, field=field, provided=True)
            resume = True
    payload = presence_payload(connector, field, True)
    payload.update({"ok": True, "state": row.get("state"), "resume": resume})
    return 200, payload


def post_secrets_dismiss(body: dict, svc: SecretServices) -> tuple[int, JsonPayload]:
    """POST /api/secrets/dismiss — decline; do not re-ask in the same breath."""
    session_id = str(body.get("session_id") or "").strip()
    connector = normalize_connector(str(body.get("connector") or ""))
    field = normalize_field(str(body.get("field") or ""))
    err = validate_secret_ref(connector, field)
    if err:
        return 400, {"error": err}
    if session_id:
        runner = _runner(svc, session_id)
        decide = getattr(runner, "decide_secret_request", None)
        if callable(decide):
            decide(connector=connector, field=field, provided=False)
    payload = presence_payload(connector, field, False)
    payload.update({"ok": True, "resume": False})
    return 200, payload


def post_secrets_emit_check(body: dict) -> tuple[int, JsonPayload]:
    """Test helper / validation: parse a structured secret-request payload."""
    parsed = parse_secret_request_payload(body)
    if not parsed:
        return 400, {"error": "invalid secret-request"}
    return 200, {"ok": True, "secret": parsed}


# Silence unused json import if a future handler needs dumps; keep for tests.
_ = json
_ = delete_secret
