from __future__ import annotations
import os
import json
import tempfile

_KEYS_FILE = os.path.join(os.path.expanduser("~/.pmharness"), "keys.json")

def get_keys_file_path() -> str:
    state_dir = os.environ.get("HARNESS_STATE_DIR")
    if state_dir:
        return os.path.join(state_dir, "keys.json")
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
        with os.fdopen(tmp_fd, 'w') as f:
            json.dump(keys, f)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
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
        with open(path, 'r') as f:
            return json.load(f)
    except Exception:
        return {}

def get_api_key_status(reach: str) -> dict:
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
    else:
        clear_api_key(reach)

def clear_api_key(reach: str):
    keys = _read_keys()
    if reach in keys:
        del keys[reach]
        _write_keys(keys)
    from .providers import get_provider
    p = get_provider(reach)
    vars_to_clear = list(p.env_vars) if p and p.env_vars else []
    env_var = get_env_var_for_reach(reach)
    if env_var not in vars_to_clear:
        vars_to_clear.append(env_var)
    for ev in vars_to_clear:
        if ev in os.environ:
            del os.environ[ev]

def load_api_keys_on_startup(reach: str):
    _keyfile = os.environ.get("HARNESS_KEY_FILE", "")
    if _keyfile and os.path.exists(_keyfile):
        _envvar = get_env_var_for_reach(reach)
        if _envvar:
            try:
                with open(_keyfile) as _kf:
                    os.environ[_envvar] = _kf.read().strip()
            except Exception:
                pass
    keys = _read_keys()
    key = keys.get(reach)
    if key:
        env_var = get_env_var_for_reach(reach)
        os.environ[env_var] = key
