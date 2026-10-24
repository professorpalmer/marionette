"""Connector secret presence. Secret values are never stored here.

The value of a submitted secret goes to two places only: the Electron main
vault (OS key storage through ``safeStorage``), which injects it at backend
start, and this backend's environment, which applies it at once. This module
records only that a secret is present, per profile (HARNESS_STATE_DIR /
~/.pmharness). Version 1 stored the value with a key file next to it; reading
a version 1 store removes the values and the key file.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
import time
from typing import Any, MutableMapping, Optional

from .secure_files import restrict_to_owner

_LOCK = threading.Lock()
_CONNECTOR_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_FIELD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
STORE_VERSION = 2

_ENV_BINDINGS: dict[tuple[str, str], list[tuple[str, Optional[str]]]] = {
    ("pypi", "token"): [
        ("TWINE_USERNAME", "__token__"),
        ("TWINE_PASSWORD", None),
        ("PYPI_TOKEN", None),
    ],
    ("portable-llm-wiki", "WIKI_OWNER_TOKEN"): [
        ("WIKI_OWNER_TOKEN", None),
    ],
    ("slack", "token"): [
        ("SLACK_BOT_TOKEN", None),
        ("SLACK_TOKEN", None),
    ],
}

_SECRET_ENV_NAMES = frozenset(
    name
    for pairs in _ENV_BINDINGS.values()
    for name, _lit in pairs
    if _lit is None
)


def _state_dir() -> str:
    explicit = (os.environ.get("HARNESS_STATE_DIR") or "").strip()
    if explicit:
        return os.path.abspath(explicit)
    return os.path.abspath(os.path.expanduser("~/.pmharness"))


def vault_path() -> str:
    return os.path.join(_state_dir(), "secret-vault.json")


def legacy_key_path() -> str:
    return os.path.join(_state_dir(), "secret-vault.key")


def normalize_connector(value: str) -> str:
    return (value or "").strip().lower()


def normalize_field(value: str) -> str:
    return (value or "").strip()


def normalize_agent_id(value: str) -> str:
    text = (value or "").strip()
    return text or "default"


def validate_secret_ref(connector: str, field: str) -> Optional[str]:
    if not _CONNECTOR_RE.fullmatch(connector):
        return "connector must be a short lowercase id"
    if not _FIELD_RE.fullmatch(field):
        return "field must be a token/key name"
    return None


def _empty_store() -> dict:
    return {"v": STORE_VERSION, "agents": {}}


def _presence_only(store: dict) -> tuple[dict, bool]:
    """Return the store with every field reduced to a presence row.

    The flag is True when the input held anything other than presence rows
    (a version 1 value blob), so the caller must rewrite the file.
    """
    changed = store.get("v") != STORE_VERSION
    agents: dict = {}
    for agent_id, by_agent in (store.get("agents") or {}).items():
        if not isinstance(by_agent, dict):
            changed = True
            continue
        for connector, fields in by_agent.items():
            if not isinstance(fields, dict):
                changed = True
                continue
            for field, row in fields.items():
                if not row:
                    changed = True
                    continue
                kept = {"present": True, "at": row.get("at") if isinstance(row, dict) else None}
                if row != kept:
                    changed = True
                agents.setdefault(agent_id, {}).setdefault(connector, {})[field] = kept
    return {"v": STORE_VERSION, "agents": agents}, changed


def _destroy_legacy_key() -> None:
    path = legacy_key_path()
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    try:
        with open(path, "r+b") as fh:
            fh.write(secrets.token_bytes(max(size, 32)))
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        pass  # Unlink still removes the name; the overwrite only shortens its life on disk.
    try:
        os.unlink(path)
    except OSError:
        pass


def _read_store() -> dict:
    """Presence store. A version 1 store is converted and its key removed."""
    path = vault_path()
    if not os.path.exists(path):
        _destroy_legacy_key()
        return _empty_store()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return _empty_store()
    if not isinstance(data, dict) or not isinstance(data.get("agents"), dict):
        return _empty_store()
    store, changed = _presence_only(data)
    if changed:
        _write_store(store)
    _destroy_legacy_key()
    return store


def _write_store(store: dict) -> None:
    path = vault_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix="sv_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(store, fh, indent=2)
        os.replace(tmp, path)
        restrict_to_owner(path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


def put_secret(
    agent_id: str,
    connector: str,
    field: str,
    value: str,
    environ: Optional[MutableMapping[str, str]] = None,
) -> dict[str, Any]:
    """Record presence and apply the value to ``environ`` (default: this process)."""
    agent_id = normalize_agent_id(agent_id)
    connector = normalize_connector(connector)
    field = normalize_field(field)
    err = validate_secret_ref(connector, field)
    if err:
        raise ValueError(err)
    secret = (value or "").strip()
    if not secret:
        raise ValueError("secret value is required")
    with _LOCK:
        store = _read_store()
        by_conn = store["agents"].setdefault(agent_id, {}).setdefault(connector, {})
        by_conn[field] = {"present": True, "at": time.time()}
        _write_store(store)
    target = os.environ if environ is None else environ
    for env_name, env_val in env_bindings(connector, field, secret).items():
        target[env_name] = env_val
    return presence(agent_id, connector, field)


def delete_secret(agent_id: str, connector: str, field: str) -> dict[str, Any]:
    agent_id = normalize_agent_id(agent_id)
    connector = normalize_connector(connector)
    field = normalize_field(field)
    with _LOCK:
        store = _read_store()
        agents = store["agents"]
        by_agent = agents.get(agent_id) or {}
        by_conn = by_agent.get(connector) or {}
        by_conn.pop(field, None)
        if connector in by_agent and not by_conn:
            by_agent.pop(connector, None)
        if agent_id in agents and not by_agent:
            agents.pop(agent_id, None)
        _write_store(store)
    return presence(agent_id, connector, field)


def presence(agent_id: str, connector: str = "", field: str = "") -> dict[str, Any]:
    agent_id = normalize_agent_id(agent_id)
    connector = normalize_connector(connector)
    field = normalize_field(field)
    with _LOCK:
        by_agent = _read_store()["agents"].get(agent_id) or {}
    if connector and field:
        present = bool((by_agent.get(connector) or {}).get(field))
        return {
            "agent_id": agent_id,
            "connector": connector,
            "field": field,
            "state": "present" if present else "missing",
            "present": present,
        }
    connectors: dict[str, dict[str, str]] = {}
    for conn, fields in by_agent.items():
        if connector and conn != connector:
            continue
        row = {name: "present" for name in fields}
        if row:
            connectors[conn] = row
    return {"agent_id": agent_id, "connectors": connectors}


def env_bindings(connector: str, field: str, value: str) -> dict[str, str]:
    connector = normalize_connector(connector)
    field = normalize_field(field)
    out: dict[str, str] = {}
    for name, lit in _ENV_BINDINGS.get((connector, field), []):
        out[name] = value if lit is None else lit
    generic = f"{connector.upper().replace('-', '_')}_{field.upper()}"
    out.setdefault(generic, value)
    return out


def presence_payload(connector: str, field: str, provided: bool) -> dict[str, Any]:
    return {
        "provided": bool(provided),
        "connector": normalize_connector(connector),
        "field": normalize_field(field),
    }


def redact_secret_text(text: str, extra_values: Optional[list[str]] = None) -> str:
    if not text:
        return text
    out = text
    names = "|".join(sorted(_SECRET_ENV_NAMES | {
        "TWINE_PASSWORD", "PYPI_TOKEN", "WIKI_OWNER_TOKEN", "SLACK_BOT_TOKEN", "SLACK_TOKEN",
    }))
    out = re.sub(
        rf"(?i)\b(?:{names})\s*[=:]\s*\S+",
        lambda m: m.group(0).split("=")[0].split(":")[0] + "=REDACTED",
        out,
    )
    for raw in extra_values or []:
        val = (raw or "").strip()
        if len(val) >= 8:
            out = out.replace(val, "REDACTED")
    return out


def parse_secret_request_payload(raw: Any) -> Optional[dict[str, str]]:
    if not isinstance(raw, dict):
        return None
    body = raw.get("secret") if raw.get("type") in ("secret-request", "secret_request") else raw
    if not isinstance(body, dict):
        body = raw
    label = str(body.get("label") or "").strip()
    connector = normalize_connector(str(body.get("connector") or ""))
    field = normalize_field(str(body.get("field") or ""))
    description = str(body.get("description") or "").strip()
    if not label or not connector or not field:
        return None
    if validate_secret_ref(connector, field):
        return None
    return {
        "label": label,
        "connector": connector,
        "field": field,
        "description": description,
    }


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def extract_secret_request_message(text: str) -> tuple[str, Optional[dict[str, str]]]:
    if not text or "secret" not in text:
        return text, None
    candidates = []
    for match in _JSON_FENCE_RE.finditer(text):
        candidates.append(match.group(1))
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        candidates.append(stripped)
    for blob in candidates:
        try:
            raw = json.loads(blob)
        except Exception:
            continue
        parsed = parse_secret_request_payload(raw)
        if parsed:
            cleaned = text.replace(blob, "").replace("```json", "").replace("```", "")
            cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
            return cleaned, parsed
    return text, None
