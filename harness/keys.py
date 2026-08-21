from __future__ import annotations
import os
import re
import json
import tempfile
from typing import Optional

from .secure_files import restrict_to_owner
from .diag import note as _diag

# Doctor / hermetic fixtures sometimes write obvious fake tokens into keys.json.
# Those must never mark a provider "configured" for registry seeding or routing.
_PLACEHOLDER_CREDENTIAL_RE = re.compile(
    r"(?i)^(doctor|test|dummy|placeholder|example)[-_]"
)


def is_placeholder_credential(value: Optional[str]) -> bool:
    """True for obvious test/doctor/dummy/placeholder/example credential strings.

    Matches values whose first segment is a known fake prefix followed by
    ``-`` or ``_`` (case-insensitive), e.g. ``doctor-bearer-token-1``.
    """
    text = (value or "").strip()
    if not text:
        return False
    return _PLACEHOLDER_CREDENTIAL_RE.match(text) is not None

_KEYS_FILE = os.path.join(os.path.expanduser("~/.pmharness"), "keys.json")

def get_keys_file_path() -> str:
    """The canonical keys.json Marionette reads and WRITES.

    Always the state-dir file when ``HARNESS_STATE_DIR`` is set. Legacy keys
    from the pre-state-dir layout are folded in on read by
    :func:`legacy_keys_file_path` / :func:`_read_keys` instead of redirecting
    writes back into the old location.
    """
    state_dir = os.environ.get("HARNESS_STATE_DIR")
    if state_dir:
        return os.path.join(state_dir, "keys.json")
    return _KEYS_FILE


def _state_dir_under_product_home(state_dir: str) -> bool:
    """True when ``state_dir`` is exactly ``~/.pmharness`` or a subdirectory.

    Uses ``home_root + os.sep`` boundary matching so sibling paths like
    ``~/.pmharness_shadow/state`` never qualify as in-tree.
    """
    try:
        home_root = os.path.normcase(
            os.path.abspath(os.path.expanduser("~/.pmharness"))
        )
        abs_state = os.path.normcase(os.path.abspath(state_dir))
        return abs_state == home_root or abs_state.startswith(home_root + os.sep)
    except Exception:
        return False


def legacy_keys_file_path() -> str:
    """The pre-state-dir ``~/.pmharness/keys.json``, or "" when not applicable.

    Earlier builds wrote keys.json directly to ``~/.pmharness``. Once
    ``HARNESS_STATE_DIR`` anchored to ``~/.pmharness/state``, upgraded installs
    with keys only in the parent directory appeared keyless until re-entered.
    Only real harness homes see this fallback — never ephemeral test / temp
    state dirs (same rule as disconnected.json), so tests cannot read the
    developer's actual credentials.
    """
    state_dir = os.environ.get("HARNESS_STATE_DIR")
    if not state_dir:
        return ""
    if not os.path.exists(_KEYS_FILE):
        return ""
    if not _state_dir_under_product_home(state_dir):
        return ""
    try:
        abs_state = os.path.normcase(os.path.abspath(state_dir))
        if abs_state == os.path.normcase(os.path.dirname(os.path.abspath(_KEYS_FILE))):
            # State dir IS the legacy home; get_keys_file_path already points there.
            return ""
    except Exception:
        return ""
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


def _bedrock_secret_fields_usable(creds: dict) -> bool:
    """True when ``creds`` has a non-placeholder bearer or access-key pair."""
    bearer = (creds.get("AWS_BEARER_TOKEN_BEDROCK") or "").strip()
    if bearer:
        return not is_placeholder_credential(bearer)
    access = (creds.get("AWS_ACCESS_KEY_ID") or "").strip()
    secret = (creds.get("AWS_SECRET_ACCESS_KEY") or "").strip()
    if access and secret:
        return not (
            is_placeholder_credential(access)
            or is_placeholder_credential(secret)
        )
    return False


def bedrock_auth_present(creds: Optional[dict] = None) -> bool:
    """True when bearer token OR (access key + secret) is available.

    Placeholder / doctor / test tokens do not count. When ``creds`` is None,
    reads the live process environment (and does not fall back to the keyfile
    — callers that need stored+env should merge first via
    :func:`resolve_usable_bedrock_credentials`).
    """
    if creds is None:
        return _bedrock_secret_fields_usable({
            "AWS_BEARER_TOKEN_BEDROCK": os.environ.get("AWS_BEARER_TOKEN_BEDROCK", ""),
            "AWS_ACCESS_KEY_ID": os.environ.get("AWS_ACCESS_KEY_ID", ""),
            "AWS_SECRET_ACCESS_KEY": os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
        })
    return _bedrock_secret_fields_usable(creds)


def resolve_usable_bedrock_credentials() -> Optional[dict]:
    """Single source of truth: usable Bedrock auth material, or None.

    Returns None when:
    - ``bedrock`` is in :func:`get_disconnected`
    - credentials are obvious test/doctor placeholders
    - neither a bearer token nor (access key + secret) is present

    Merges process env over the stored keyfile so startup-before-inject and
    live Settings updates both resolve consistently.
    """
    if "bedrock" in get_disconnected():
        return None
    effective = _normalize_bedrock_creds(_read_keys().get("bedrock"))
    for field in BEDROCK_ENV_FIELDS:
        env_val = (os.environ.get(field) or "").strip()
        if env_val:
            effective[field] = env_val
    if not _bedrock_secret_fields_usable(effective):
        return None
    return effective


def bedrock_credential_token() -> Optional[str]:
    """Opaque credential string for Provider.key() / masking, or None.

    Honors disconnect, rejects placeholders, and requires real auth material
    (bearer, or access key + secret). All Bedrock "is configured?" checks
    should go through this or :func:`resolve_usable_bedrock_credentials`.
    """
    creds = resolve_usable_bedrock_credentials()
    if not creds:
        return None
    return (
        (creds.get("AWS_BEARER_TOKEN_BEDROCK") or "").strip()
        or (creds.get("AWS_ACCESS_KEY_ID") or "").strip()
        or None
    )


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
    snapshot does not leave a stale access key in the process). Obvious
    doctor/test/placeholder secrets are never injected (and clear any stale
    value) so Puppetmaster env sniffers cannot treat them as live auth.
    Region/model config is set when present; absent config fields are left
    alone so a shell-exported ``AWS_REGION`` survives a bearer-only save.
    """
    if not isinstance(creds, dict):
        creds = {}
    for field in BEDROCK_SECRET_FIELDS:
        val = (creds.get(field) or "").strip()
        if val and not is_placeholder_credential(val):
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

def _read_keys_file(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except Exception as exc:
        # A corrupted keys file must not crash callers, but silently treating
        # it as "no keys" made every provider look disconnected with no trail.
        from .diag import note
        note("keys.read_keys", exc, msg=f"unreadable keys file at {path}")
        return {}
    return data if isinstance(data, dict) else {}


def _read_keys() -> dict:
    """Every stored provider credential, legacy folded under the state file.

    The merge is PER PROVIDER: an upgraded install whose legacy keys.json holds
    openrouter while the state file holds anthropic must see both. The state
    file wins any provider present in both, so re-entering a key in Settings
    always takes effect.
    """
    state = _read_keys_file(get_keys_file_path())
    legacy = _read_keys_file(legacy_keys_file_path())
    if not legacy:
        return state
    merged = dict(legacy)
    merged.update(state)
    return merged


def migrate_legacy_keys_into_state() -> list:
    """Copy legacy-only providers into the state keys file. Returns their names.

    One-way and non-destructive: providers already in the state file keep their
    stored value, so this can run on every startup without clobbering a key the
    user re-entered in Settings.
    """
    legacy_path = legacy_keys_file_path()
    if not legacy_path:
        return []
    legacy = _read_keys_file(legacy_path)
    if not legacy:
        return []
    state = _read_keys_file(get_keys_file_path())
    migrated = sorted(name for name in legacy if name not in state)
    if not migrated:
        return []
    merged = dict(legacy)
    merged.update(state)
    try:
        _write_keys(merged)
    except Exception as exc:
        _diag("keys.migrate_legacy", exc)
        return []
    _diag("keys.migrate_legacy", msg=f"migrated={','.join(migrated)}")
    return migrated

_DISCONNECTED_FILE = os.path.join(os.path.expanduser("~/.pmharness"), "disconnected.json")


def _disconnected_file_path() -> str:
    state_dir = os.environ.get("HARNESS_STATE_DIR")
    if state_dir:
        p = os.path.join(state_dir, "disconnected.json")
        if os.path.exists(p):
            return p
        # Legacy layout: disconnected.json lived under ~/.pmharness/ while
        # state moved to ~/.pmharness/state. Only fall back for real harness
        # homes — never for ephemeral test / temp state dirs.
        if _state_dir_under_product_home(state_dir) and os.path.exists(_DISCONNECTED_FILE):
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
            if reach == "zai":
                try:
                    from .providers import ensure_zai_worker_base_url
                    ensure_zai_worker_base_url()
                except Exception:
                    pass
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


def scrub_provider_env(reach: str) -> None:
    """Remove a provider's env vars from os.environ (so a shell-exported key
    cannot make a deliberately-disconnected provider appear available).

    For bedrock this clears AWS_BEARER_TOKEN_BEDROCK, AWS_ACCESS_KEY_ID,
    AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN, and region/model config fields.
    """
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


# Back-compat alias (internal callers / older tests).
_scrub_provider_env = scrub_provider_env


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
    if key and is_placeholder_credential(key):
        return {"has_key": False, "masked": ""}
    if not key:
        # Credential-pool OAuth / multi-key entries count as configured.
        try:
            from .credential_pool import has_healthy_credential, peek_token, list_pool_public
            # openai-codex / xai-oauth use their own pool ids; classic "xai"
            # reach also accepts SuperGrok OAuth in the xai-oauth pool.
            pool_names = [reach]
            if reach == "xai":
                pool_names.append("xai-oauth")
            for pool_name in pool_names:
                if has_healthy_credential(pool_name):
                    tok = peek_token(pool_name) or ""
                    if len(tok) <= 8:
                        return {"has_key": True, "masked": "...."}
                    return {"has_key": True, "masked": "...." + tok[-4:]}
            # Anthropic OAuth lives in anthropic pool; also check aliases
            pub = list_pool_public(reach)
            if pub.get("entries"):
                e0 = pub["entries"][0]
                return {"has_key": True, "masked": e0.get("masked") or "...."}
        except Exception:
            pass
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
        if is_placeholder_credential(value):
            # Persist for inspection but do not inject into the live process.
            if env_var in os.environ:
                del os.environ[env_var]
        else:
            os.environ[env_var] = value
            if reach == "zai":
                try:
                    from .providers import ensure_zai_worker_base_url
                    ensure_zai_worker_base_url()
                except Exception:
                    pass
            # Reconnecting clears the explicit-disconnect flag.
            unmark_disconnected(reach)
            # Keep the credential pool in sync so multi-key rotation can include
            # keys pasted via the classic Settings form.
            try:
                from .credential_pool import add_api_key
                add_api_key(reach, value, label=f"{reach}-settings")
            except Exception:
                pass
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
    # Drop the pre-scrub cache so Settings stops treating this as env-backed
    # and can show a paste field for a replacement key.
    _ENV_KEY_CACHE.pop(reach, None)
    # Record the disconnect so it survives restarts even when the user's shell
    # exports this provider's key (which the login-shell env capture re-injects).
    mark_disconnected(reach)

# Providers whose shell-exported keys may be persisted into keys.json for the
# next cold start. openai-codex uses OPENAI_CODEX_TOKEN (OAuth) and is handled
# by the credential pool / OAuth flow — not this env-key scoop list. cursor-cli
# is login-state, not an API key file.
_PERSISTABLE_ENV_PROVIDERS = (
    "openrouter",
    "openai",
    "anthropic",
    "gemini",
    "deepseek",
    "zai",
    "xai",
    "opencode-go",
    "opencode-zen",
    "cursor",
    "google",
    "groq",
    "mistral",
)


def persist_env_api_keys() -> list[str]:
    """Copy shell-exported API keys into keys.json when the store has none.

    Fresh desktop installs often inherit ``OPENROUTER_API_KEY`` (etc.) from the
    login-shell env Electron merges in, while ``keys.json`` is still empty. The
    in-process env is enough for *this* session, but the next Finder launch can
    miss those vars — agentic swarms then fail with "no usable provider
    credential" and SESSION COST never accumulates routing/cache savings.

    Never overwrites a stored key, never persists placeholders, and never
    resurrects an explicitly disconnected provider.
    """
    disconnected = get_disconnected()
    keys = _read_keys()
    imported: list[str] = []
    for name in _PERSISTABLE_ENV_PROVIDERS:
        if name in disconnected:
            continue
        existing = keys.get(name)
        if isinstance(existing, str) and existing.strip() and not is_placeholder_credential(existing):
            continue
        env_var = get_env_var_for_reach(name)
        if not env_var:
            continue
        value = (os.environ.get(env_var) or "").strip()
        # Zen prefers its own key, then OPENCODE_API_KEY. The Go subscription
        # key may authorize Zen at lookup time but must not be copied into
        # Zen's stored Settings identity.
        if name == "opencode-zen" and (
            not value or is_placeholder_credential(value)
        ):
            value = (os.environ.get("OPENCODE_API_KEY") or "").strip()
        if not value or is_placeholder_credential(value):
            continue
        keys[name] = value
        imported.append(name)
    if imported:
        try:
            _write_keys(keys)
            _diag("keys.persist_env_api_keys", msg=f"imported={','.join(imported)}")
        except Exception as exc:
            _diag("keys.persist_env_api_keys", exc)
            return []
    return imported


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
    # Fold pre-state-dir keys into the canonical file before anything reads it,
    # so an upgraded install stops depending on the legacy path to stay keyed.
    try:
        migrate_legacy_keys_into_state()
    except Exception as exc:
        _diag("keys.migrate_legacy_on_startup", exc)
    # Capture login-shell / Electron-merged keys into durable store first so
    # agentic registry sync (and the next cold start) see them.
    try:
        persist_env_api_keys()
    except Exception as exc:
        _diag("keys.persist_env_on_startup", exc)
    keys = _read_keys()
    # Inject every stored provider credential so pilots/workers see them after
    # restart — not only the active reach. Skip obvious placeholders so a stale
    # doctor/test token in keys.json cannot seed the agentic catalog.
    for name, value in keys.items():
        if name == "bedrock":
            creds = _normalize_bedrock_creds(value)
            if creds and _bedrock_secret_fields_usable(creds):
                _apply_bedrock_to_env(creds)
            continue
        if isinstance(value, str) and value.strip():
            if is_placeholder_credential(value):
                continue
            env_var = get_env_var_for_reach(name)
            if env_var:
                os.environ[env_var] = value
    if any(
        isinstance(value, str) and value.strip() and not is_placeholder_credential(value)
        for name, value in keys.items()
        if name == "zai"
    ):
        try:
            from .providers import ensure_zai_worker_base_url
            ensure_zai_worker_base_url()
        except Exception:
            pass
    # Capture env-provided keys before scrubbing so a toggled-off provider can be
    # re-enabled later in the same session without re-pasting the key.
    snapshot_env_keys()
    # Honor explicit disconnects over any shell-exported keys.
    scrub_disconnected_env()
