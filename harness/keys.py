from __future__ import annotations
import os
import json
import tempfile
from typing import Optional

from .secure_files import restrict_to_owner
from .diag import note as _diag

_KEYS_FILE = os.path.join(os.path.expanduser("~/.pmharness"), "keys.json")

def get_keys_file_path() -> str:
    state_dir = os.environ.get("HARNESS_STATE_DIR")
    if state_dir:
        p = os.path.join(state_dir, "keys.json")
        # MIGRATION: earlier builds wrote keys.json to ~/.pmharness/keys.json.
        # Once HARNESS_STATE_DIR anchored to ~/.pmharness/state, upgraded installs
        # with keys only in the parent directory appeared keyless until re-entered.
        if not os.path.exists(p) and os.path.exists(_KEYS_FILE):
            return _KEYS_FILE
        return p
    return _KEYS_FILE

def get_env_var_for_reach(reach: str) -> str:
    if reach == "openrouter":
        return "OPENROUTER_API_KEY"
    if reach == "bedrock":
        # Preferred simple path; access-key auth uses multiple env vars instead.
        return "AWS_BEARER_TOKEN_BEDROCK"
    from .providers import get_provider
    p = get_provider(reach)
    if p and p.env_vars:
        return p.env_vars[0]
    return os.environ.get("HARNESS_KEY_ENV", "") or f"{reach.upper()}_API_KEY"


# AWS Bedrock BYOK: multi-field credentials stored under keys.json["bedrock"]
# as a dict (not a single string). Preferred auth is AWS_BEARER_TOKEN_BEDROCK;
# alternatively AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY (+ optional session).
BEDROCK_SECRET_FIELDS = (
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
)
BEDROCK_CONFIG_FIELDS = (
    "AWS_REGION",
    "BEDROCK_REGION",
    "BEDROCK_MODEL_ID",
)
BEDROCK_ENV_FIELDS = BEDROCK_SECRET_FIELDS + BEDROCK_CONFIG_FIELDS


def _normalize_bedrock_creds(raw) -> dict:
    """Coerce a keys.json bedrock value into a flat env-field dict."""
    if not isinstance(raw, dict):
        # Legacy / accidental string: treat as bearer token.
        if isinstance(raw, str) and raw.strip():
            return {"AWS_BEARER_TOKEN_BEDROCK": raw.strip()}
        return {}
    out = {}
    for field in BEDROCK_ENV_FIELDS:
        val = raw.get(field)
        if isinstance(val, str) and val.strip():
            out[field] = val.strip()
    return out


def bedrock_auth_present(creds: Optional[dict] = None) -> bool:
    """True when bearer token OR (access key + secret) is available.

    When ``creds`` is None, reads the live process environment (and does not
    fall back to the keyfile — callers that need stored+env should merge first).
    """
    if creds is None:
        bearer = (os.environ.get("AWS_BEARER_TOKEN_BEDROCK") or "").strip()
        if bearer:
            return True
        access = (os.environ.get("AWS_ACCESS_KEY_ID") or "").strip()
        secret = (os.environ.get("AWS_SECRET_ACCESS_KEY") or "").strip()
        return bool(access and secret)
    bearer = (creds.get("AWS_BEARER_TOKEN_BEDROCK") or "").strip()
    if bearer:
        return True
    access = (creds.get("AWS_ACCESS_KEY_ID") or "").strip()
    secret = (creds.get("AWS_SECRET_ACCESS_KEY") or "").strip()
    return bool(access and secret)


def bedrock_credential_token() -> Optional[str]:
    """Opaque credential string for Provider.key() / masking, or None."""
    if "bedrock" in get_disconnected():
        return None
    bearer = (os.environ.get("AWS_BEARER_TOKEN_BEDROCK") or "").strip()
    if bearer:
        return bearer
    access = (os.environ.get("AWS_ACCESS_KEY_ID") or "").strip()
    secret = (os.environ.get("AWS_SECRET_ACCESS_KEY") or "").strip()
    if access and secret:
        return access
    # Fall back to stored keyfile (startup may not have injected yet).
    stored = _normalize_bedrock_creds(_read_keys().get("bedrock"))
    if bedrock_auth_present(stored):
        return (stored.get("AWS_BEARER_TOKEN_BEDROCK")
                or stored.get("AWS_ACCESS_KEY_ID")
                or None)
    return None


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "...."
    return "...." + value[-4:]


def get_bedrock_status() -> dict:
    """Settings/doctor status for Bedrock BYOK (never returns raw secrets)."""
    disconnected = "bedrock" in get_disconnected()
    stored = _normalize_bedrock_creds(_read_keys().get("bedrock"))
    # Live env wins for presence when not disconnected; stored fills gaps for
    # the UI when env was scrubbed or not yet injected.
    effective = dict(stored)
    if not disconnected:
        for field in BEDROCK_ENV_FIELDS:
            env_val = (os.environ.get(field) or "").strip()
            if env_val:
                effective[field] = env_val
    configured = (not disconnected) and bedrock_auth_present(effective)
    auth_mode = ""
    masked = ""
    if configured:
        if (effective.get("AWS_BEARER_TOKEN_BEDROCK") or "").strip():
            auth_mode = "bearer"
            masked = _mask_secret(effective["AWS_BEARER_TOKEN_BEDROCK"])
        else:
            auth_mode = "access_key"
            masked = _mask_secret(effective.get("AWS_ACCESS_KEY_ID", ""))
    return {
        "configured": configured,
        "has_key": configured,
        "auth_mode": auth_mode,
        "masked": masked if configured else "",
        "has_bearer": bool((effective.get("AWS_BEARER_TOKEN_BEDROCK") or "").strip()) and not disconnected,
        "has_access_key": bool(
            (effective.get("AWS_ACCESS_KEY_ID") or "").strip()
            and (effective.get("AWS_SECRET_ACCESS_KEY") or "").strip()
        ) and not disconnected,
        "has_session_token": bool((effective.get("AWS_SESSION_TOKEN") or "").strip()) and not disconnected,
        "region": (effective.get("AWS_REGION") or effective.get("BEDROCK_REGION") or ""),
        "aws_region": effective.get("AWS_REGION", ""),
        "bedrock_region": effective.get("BEDROCK_REGION", ""),
        "model_id": effective.get("BEDROCK_MODEL_ID", ""),
        "disconnected": disconnected,
    }


def _apply_bedrock_to_env(creds: dict, *, clear_absent_secrets: bool = True) -> None:
    """Inject Bedrock fields into os.environ.

    Secret fields absent from ``creds`` are scrubbed (so a saved bearer-only
    snapshot does not leave a stale access key in the process). Region/model
    config is set when present; absent config fields are left alone so a
    shell-exported ``AWS_REGION`` survives a bearer-only save.
    """
    if not isinstance(creds, dict):
        creds = {}
    for field in BEDROCK_SECRET_FIELDS:
        val = (creds.get(field) or "").strip()
        if val:
            os.environ[field] = val
        elif clear_absent_secrets and field in os.environ:
            del os.environ[field]
    for field in BEDROCK_CONFIG_FIELDS:
        val = (creds.get(field) or "").strip()
        if val:
            os.environ[field] = val
        elif field in creds and field in os.environ:
            # Explicit empty in the stored snapshot clears a previously saved value.
            del os.environ[field]
    # Optional default inference-profile also helps Claude Code / PM paths that
    # read ANTHROPIC_MODEL when Bedrock billing is enabled.
    model = (creds.get("BEDROCK_MODEL_ID") or "").strip()
    if model:
        os.environ.setdefault("ANTHROPIC_MODEL", model)


def set_bedrock_credentials(fields: dict) -> dict:
    """Persist Bedrock BYOK fields and inject them into the process env.

    Empty string values clear that field. Omitted keys keep the previous stored
    value (partial update). Pass clear=True via clear_bedrock_credentials().
    """
    keys = _read_keys()
    cur = _normalize_bedrock_creds(keys.get("bedrock"))
    if not isinstance(fields, dict):
        fields = {}
    cleared_config: list[str] = []
    for field in BEDROCK_ENV_FIELDS:
        if field not in fields:
            continue
        val = fields.get(field)
        if val is None:
            continue
        text = str(val).strip()
        if text:
            cur[field] = text
        else:
            cur.pop(field, None)
            if field in BEDROCK_CONFIG_FIELDS:
                cleared_config.append(field)
    if cur:
        keys["bedrock"] = cur
    elif "bedrock" in keys:
        del keys["bedrock"]
    _write_keys(keys)
    unmark_disconnected("bedrock")
    _apply_bedrock_to_env(cur)
    for field in cleared_config:
        os.environ.pop(field, None)
    return get_bedrock_status()


def clear_bedrock_credentials() -> dict:
    """Remove stored Bedrock credentials and scrub related env vars."""
    keys = _read_keys()
    if "bedrock" in keys:
        del keys["bedrock"]
        _write_keys(keys)
    _scrub_provider_env("bedrock")
    mark_disconnected("bedrock")
    return get_bedrock_status()

def _write_keys(keys: dict):
    path = get_keys_file_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), prefix="keys_")
    try:
        with os.fdopen(tmp_fd, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(keys, f)
        os.replace(tmp_path, path)
        if not restrict_to_owner(path):
            _diag("secure_files.restrict_failed", msg=path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise

def _read_keys() -> dict:
    path = get_keys_file_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except Exception as exc:
        # A corrupted keys file must not crash callers, but silently treating
        # it as "no keys" made every provider look disconnected with no trail.
        from .diag import note
        note("keys.read_keys", exc, msg=f"unreadable keys file at {path}")
        return {}

_DISCONNECTED_FILE = os.path.join(os.path.expanduser("~/.pmharness"), "disconnected.json")


def _disconnected_file_path() -> str:
    state_dir = os.environ.get("HARNESS_STATE_DIR")
    if state_dir:
        p = os.path.join(state_dir, "disconnected.json")
        if not os.path.exists(p) and os.path.exists(_DISCONNECTED_FILE):
            return _DISCONNECTED_FILE
        return p
    return _DISCONNECTED_FILE


def get_disconnected() -> set:
    """Providers the user EXPLICITLY disconnected. Authoritative over the
    environment: even when the user's shell exports e.g. OPENROUTER_API_KEY
    (re-injected by the desktop app's login-shell env capture), a provider in
    this set is treated as disconnected and its env vars are scrubbed. Lets a
    deliberate disconnect survive app restarts instead of silently reconnecting."""
    path = _disconnected_file_path()
    if not os.path.exists(path):
        return set()
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        return set(data) if isinstance(data, list) else set()
    except Exception:
        return set()


def _write_disconnected(names: set) -> None:
    path = _disconnected_file_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), prefix="disc_")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(sorted(names), f)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# Snapshot of provider keys seen in the environment (shell-exported and
# login-shell-captured) BEFORE any disconnect scrub. Lets a provider that is
# "imported via env" be toggled off (scrubbed from os.environ so workers and
# the router stop using it) and back on WITHOUT losing the value mid-session --
# the point being painless swapping between, say, a work key and a personal one.
_ENV_KEY_CACHE: dict[str, dict[str, str]] = {}


def snapshot_env_keys() -> None:
    """Record each provider's currently-present env-var values into the cache.

    Idempotent and additive: only non-empty values are captured, and a later
    scrub never erases the cache, so a re-enable can restore the original value.
    """
    try:
        from .providers import PROVIDERS
    except Exception:
        return
    for p in PROVIDERS:
        if p.name == "bedrock":
            for ev in BEDROCK_ENV_FIELDS:
                val = os.environ.get(ev)
                if val:
                    _ENV_KEY_CACHE.setdefault(p.name, {})[ev] = val
            continue
        for ev in (p.env_vars or []):
            val = os.environ.get(ev)
            if val:
                _ENV_KEY_CACHE.setdefault(p.name, {})[ev] = val


def provider_has_env(reach: str) -> bool:
    """True when this provider has a key sourced from the environment.

    Checks both the live environment and the pre-scrub cache, so a provider
    that was toggled off (its env var scrubbed) still reports as env-backed --
    that is exactly the state where the on/off toggle must remain available.
    """
    if _ENV_KEY_CACHE.get(reach):
        return True
    if reach == "bedrock":
        return bedrock_auth_present()
    from .providers import get_provider
    p = get_provider(reach)
    for ev in ((p.env_vars if p else None) or []):
        if os.environ.get(ev):
            return True
    return False


def set_provider_enabled(reach: str, enabled: bool) -> None:
    """Enable/disable a provider without destroying its key.

    Disable: mark disconnected + scrub its env vars (cached first) so no worker
    or router call can use it. Enable: clear the disconnect flag and restore the
    key into the environment -- from the stored keyfile if present, else from the
    pre-scrub env cache. Persistent across restarts via disconnected.json.
    """
    if enabled:
        unmark_disconnected(reach)
        stored = _read_keys().get(reach, "")
        if reach == "bedrock":
            creds = _normalize_bedrock_creds(stored)
            if creds:
                _apply_bedrock_to_env(creds)
            else:
                for ev, val in _ENV_KEY_CACHE.get(reach, {}).items():
                    os.environ[ev] = val
        elif stored and isinstance(stored, str):
            os.environ[get_env_var_for_reach(reach)] = stored
        else:
            for ev, val in _ENV_KEY_CACHE.get(reach, {}).items():
                os.environ[ev] = val
    else:
        snapshot_env_keys()
        mark_disconnected(reach)
        _scrub_provider_env(reach)


def mark_disconnected(reach: str) -> None:
    names = get_disconnected()
    names.add(reach)
    _write_disconnected(names)


def unmark_disconnected(reach: str) -> None:
    names = get_disconnected()
    if reach in names:
        names.discard(reach)
        _write_disconnected(names)


def _scrub_provider_env(reach: str) -> None:
    """Remove a provider's env vars from os.environ (so a shell-exported key
    cannot make a deliberately-disconnected provider appear available)."""
    from .providers import get_provider
    p = get_provider(reach)
    if reach == "bedrock" or (p and getattr(p, "api_mode", "") == "bedrock"):
        vars_to_clear = list(BEDROCK_ENV_FIELDS)
    else:
        vars_to_clear = list(p.env_vars) if p and p.env_vars else []
        env_var = get_env_var_for_reach(reach)
        if env_var not in vars_to_clear:
            vars_to_clear.append(env_var)
    for ev in vars_to_clear:
        if ev in os.environ:
            del os.environ[ev]


def scrub_disconnected_env() -> None:
    """Scrub env vars for every disconnected provider. Called at startup AFTER
    the login-shell env is merged in, so explicit disconnects win over the
    shell environment."""
    for name in get_disconnected():
        _scrub_provider_env(name)


def get_api_key_status(reach: str) -> dict:
    # An explicitly-disconnected provider always reports no key, even if a key is
    # still stored or shell-exported -- the disconnect is authoritative.
    if reach in get_disconnected():
        return {"has_key": False, "masked": ""}
    if reach == "bedrock":
        st = get_bedrock_status()
        return {"has_key": st["has_key"], "masked": st["masked"]}
    keys = _read_keys()
    key = keys.get(reach, "")
    if isinstance(key, dict):
        # Non-bedrock structured entries are not single-string API keys.
        return {"has_key": False, "masked": ""}
    if not key:
        return {"has_key": False, "masked": ""}
    # Never reveal any portion of a short key; only show last 4 of a sufficiently
    # long one. A short/garbage key is fully masked rather than echoed back.
    if len(key) <= 8:
        masked = "...."
    else:
        masked = "...." + key[-4:]
    return {"has_key": True, "masked": masked}

def set_api_key(reach: str, value: str):
    if reach == "bedrock":
        # Simple path: a single pasted value is the preferred bearer token.
        if value:
            set_bedrock_credentials({"AWS_BEARER_TOKEN_BEDROCK": value})
        else:
            clear_bedrock_credentials()
        return
    keys = _read_keys()
    if value:
        keys[reach] = value
        _write_keys(keys)
        env_var = get_env_var_for_reach(reach)
        os.environ[env_var] = value
        # Reconnecting clears the explicit-disconnect flag.
        unmark_disconnected(reach)
    else:
        clear_api_key(reach)

def clear_api_key(reach: str):
    if reach == "bedrock":
        clear_bedrock_credentials()
        return
    keys = _read_keys()
    if reach in keys:
        del keys[reach]
        _write_keys(keys)
    _scrub_provider_env(reach)
    # Record the disconnect so it survives restarts even when the user's shell
    # exports this provider's key (which the login-shell env capture re-injects).
    mark_disconnected(reach)

def load_api_keys_on_startup(reach: str):
    _keyfile = os.environ.get("HARNESS_KEY_FILE", "")
    if _keyfile and os.path.exists(_keyfile):
        _envvar = get_env_var_for_reach(reach)
        if _envvar:
            try:
                with open(_keyfile, encoding="utf-8", errors="replace") as _kf:
                    os.environ[_envvar] = _kf.read().strip()
            except Exception:
                pass
    keys = _read_keys()
    # Inject every stored provider credential so pilots/workers see them after
    # restart — not only the active reach.
    for name, value in keys.items():
        if name == "bedrock":
            creds = _normalize_bedrock_creds(value)
            if creds:
                _apply_bedrock_to_env(creds)
            continue
        if isinstance(value, str) and value.strip():
            env_var = get_env_var_for_reach(name)
            if env_var:
                os.environ[env_var] = value
    # Capture env-provided keys before scrubbing so a toggled-off provider can be
    # re-enabled later in the same session without re-pasting the key.
    snapshot_env_keys()
    # Honor explicit disconnects over any shell-exported keys.
    scrub_disconnected_env()
