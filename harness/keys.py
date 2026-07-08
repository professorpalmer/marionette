from __future__ import annotations
import os
import json
import tempfile

from .secure_files import restrict_to_owner

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
    from .providers import get_provider
    p = get_provider(reach)
    if p and p.env_vars:
        return p.env_vars[0]
    return os.environ.get("HARNESS_KEY_ENV", "") or f"{reach.upper()}_API_KEY"

def _write_keys(keys: dict):
    path = get_keys_file_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), prefix="keys_")
    try:
        with os.fdopen(tmp_fd, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(keys, f)
        os.replace(tmp_path, path)
        restrict_to_owner(path)
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
        with os.fdopen(tmp_fd, "w", encoding="utf-8", newline="") as f:
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
        if stored:
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
    keys = _read_keys()
    key = keys.get(reach, "")
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
    key = keys.get(reach)
    if key:
        env_var = get_env_var_for_reach(reach)
        os.environ[env_var] = key
    # Capture env-provided keys before scrubbing so a toggled-off provider can be
    # re-enabled later in the same session without re-pasting the key.
    snapshot_env_keys()
    # Honor explicit disconnects over any shell-exported keys.
    scrub_disconnected_env()
