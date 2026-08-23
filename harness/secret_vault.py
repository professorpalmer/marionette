"""Host-held connector secrets. Values never return to the model or renderer.

Storage is per-profile (HARNESS_STATE_DIR / ~/.pmharness), encrypted at rest
with a machine-local key file (owner-only). Presence is present|missing.
Subprocess injection happens in this process (or Electron main) via env.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
from typing import Any, Optional

from .secure_files import restrict_to_owner

_LOCK = threading.Lock()
_CONNECTOR_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_FIELD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")

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


def _key_path() -> str:
    return os.path.join(_state_dir(), "secret-vault.key")


def _load_key() -> bytes:
    path = _key_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        with open(path, "rb") as fh:
            data = fh.read()
        if len(data) >= 32:
            return data[:32]
    key = secrets.token_bytes(32)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix="svk_")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        os.replace(tmp, path)
        restrict_to_owner(path)
    except Exception:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise
    return key


def _crypt(data: bytes, key: bytes, nonce: bytes) -> bytes:
    out = bytearray(len(data))
    block = b""
    for i, byte in enumerate(data):
        if i % 32 == 0:
            block = hashlib.sha256(key + nonce + i.to_bytes(8, "big")).digest()
        out[i] = byte ^ block[i % 32]
    return bytes(out)


def _seal(plain: str) -> dict[str, str]:
    key = _load_key()
    nonce = secrets.token_bytes(16)
    raw = _crypt(plain.encode("utf-8"), key, nonce)
    return {"n": nonce.hex(), "c": raw.hex()}


def _unseal(blob: Any) -> str:
    if not isinstance(blob, dict):
        return ""
    try:
        nonce = bytes.fromhex(blob.get("n") or "")
        raw = bytes.fromhex(blob.get("c") or "")
    except ValueError:
        return ""
    try:
        return _crypt(raw, _load_key(), nonce).decode("utf-8")
    except Exception:
        return ""


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
    return {"v": 1, "agents": {}}


def _read_store() -> dict:
    path = vault_path()
    if not os.path.exists(path):
        return _empty_store()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("agents"), dict):
            return data
    except Exception:
        pass
    return _empty_store()


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


def put_secret(agent_id: str, connector: str, field: str, value: str) -> dict[str, Any]:
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
        agents = store.setdefault("agents", {})
        by_agent = agents.setdefault(agent_id, {})
        by_conn = by_agent.setdefault(connector, {})
        by_conn[field] = _seal(secret)
        _write_store(store)
    apply_to_environ(agent_id)
    return presence(agent_id, connector, field)


def delete_secret(agent_id: str, connector: str, field: str) -> dict[str, Any]:
    agent_id = normalize_agent_id(agent_id)
    connector = normalize_connector(connector)
    field = normalize_field(field)
    with _LOCK:
        store = _read_store()
        by_agent = store.get("agents", {}).get(agent_id) or {}
        by_conn = by_agent.get(connector) or {}
        by_conn.pop(field, None)
        if isinstance(by_agent.get(connector), dict) and not by_conn:
            by_agent.pop(connector, None)
        if not by_agent:
            store.get("agents", {}).pop(agent_id, None)
        _write_store(store)
    return presence(agent_id, connector, field)


def get_secret(agent_id: str, connector: str, field: str) -> str:
    agent_id = normalize_agent_id(agent_id)
    connector = normalize_connector(connector)
    field = normalize_field(field)
    with _LOCK:
        store = _read_store()
        blob = (
            ((store.get("agents") or {}).get(agent_id) or {})
            .get(connector) or {}
        ).get(field)
    return _unseal(blob) if blob else ""


def presence(agent_id: str, connector: str = "", field: str = "") -> dict[str, Any]:
    agent_id = normalize_agent_id(agent_id)
    connector = normalize_connector(connector)
    field = normalize_field(field)
    with _LOCK:
        store = _read_store()
        by_agent = (store.get("agents") or {}).get(agent_id) or {}
    if connector and field:
        present = bool(((by_agent.get(connector) or {}).get(field)))
        return {
            "agent_id": agent_id,
            "connector": connector,
            "field": field,
            "state": "present" if present else "missing",
            "present": present,
        }
    connectors: dict[str, dict[str, str]] = {}
    for conn, fields in by_agent.items():
        if not isinstance(fields, dict):
            continue
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


def apply_to_environ(agent_id: str, environ: Optional[dict] = None) -> dict[str, str]:
    target = environ if environ is not None else os.environ
    agent_id = normalize_agent_id(agent_id)
    injected: dict[str, str] = {}
    with _LOCK:
        store = _read_store()
        by_agent = (store.get("agents") or {}).get(agent_id) or {}
        items = []
        for conn, fields in by_agent.items():
            if not isinstance(fields, dict):
                continue
            for name, blob in fields.items():
                items.append((conn, name, blob))
    for conn, name, blob in items:
        value = _unseal(blob)
        if not value:
            continue
        for env_name, env_val in env_bindings(conn, name, value).items():
            target[env_name] = env_val
            injected[env_name] = env_val
    return injected


def subprocess_env(agent_id: str, base: Optional[dict] = None) -> dict[str, str]:
    env = dict(base if base is not None else os.environ)
    apply_to_environ(agent_id, env)
    return env


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
