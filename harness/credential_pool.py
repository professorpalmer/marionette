"""Multi-credential pools for Marionette (Hermes-style, standalone).

Same-provider rotation when a key hits rate limit / plan quota / billing.
OAuth and API-key entries share one pool per provider. Design lifted from
Hermes ``agent/credential_pool.py``; Marionette-owned storage under
``~/.pmharness/state/auth_pool.json`` (or ``HARNESS_STATE_DIR``).
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from .diag import note as _diag

AUTH_TYPE_OAUTH = "oauth"
AUTH_TYPE_API_KEY = "api_key"

STATUS_OK = "ok"
STATUS_EXHAUSTED = "exhausted"
STATUS_DEAD = "dead"

STRATEGY_FILL_FIRST = "fill_first"
STRATEGY_ROUND_ROBIN = "round_robin"
STRATEGY_LEAST_USED = "least_used"
STRATEGY_RANDOM = "random"
SUPPORTED_STRATEGIES = frozenset({
    STRATEGY_FILL_FIRST,
    STRATEGY_ROUND_ROBIN,
    STRATEGY_LEAST_USED,
    STRATEGY_RANDOM,
})

EXHAUSTED_TTL_401 = 5 * 60
EXHAUSTED_TTL_429 = 60 * 60
EXHAUSTED_TTL_DEFAULT = 60 * 60

# Providers that can hold OAuth (browser / device-code) entries.
OAUTH_CAPABLE = frozenset({
    "anthropic",
    "openai-codex",
    "nous",
    "xai-oauth",
    "qwen-oauth",
    "minimax-oauth",
})

# API-key (and Cursor plan-key) pools.
API_KEY_PROVIDERS = frozenset({
    "openrouter",
    "openai",
    "anthropic",
    "cursor",
    "xai",
    "google",
    "groq",
    "deepseek",
    "mistral",
})

_PLAN_LIMIT_MARKERS = (
    "usage limit reached",
    "plan limit",
    "rate_limit_exceeded",
    "insufficient_quota",
    "quota exceeded",
    "too many requests",
)

_lock = threading.RLock()
_pools: Dict[str, "CredentialPool"] = {}
_rr_index: Dict[str, int] = {}
_strategies: Dict[str, str] = {}


def _pool_path() -> str:
    state_dir = os.environ.get("HARNESS_STATE_DIR")
    if state_dir:
        return os.path.join(state_dir, "auth_pool.json")
    return os.path.join(os.path.expanduser("~/.pmharness/state"), "auth_pool.json")


@dataclass
class PooledCredential:
    provider: str
    id: str
    label: str
    auth_type: str
    source: str
    access_token: str
    refresh_token: Optional[str] = None
    priority: int = 0
    last_status: Optional[str] = STATUS_OK
    last_status_at: Optional[float] = None
    last_error_code: Optional[int] = None
    last_error_message: Optional[str] = None
    expires_at_ms: Optional[int] = None
    request_count: int = 0
    base_url: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def runtime_token(self) -> str:
        return (self.access_token or "").strip()

    def to_public_dict(self) -> Dict[str, Any]:
        tok = self.runtime_token
        masked = ""
        if tok:
            masked = (tok[:4] + "…" + tok[-4:]) if len(tok) > 10 else "****"
        return {
            "id": self.id,
            "label": self.label,
            "auth_type": self.auth_type,
            "source": self.source,
            "priority": self.priority,
            "last_status": self.last_status or STATUS_OK,
            "last_error_code": self.last_error_code,
            "last_error_message": (self.last_error_message or "")[:200] or None,
            "request_count": self.request_count,
            "masked": masked,
            "has_refresh": bool(self.refresh_token),
        }

    def to_store_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_store_dict(cls, provider: str, payload: Dict[str, Any]) -> "PooledCredential":
        return cls(
            provider=provider,
            id=str(payload.get("id") or uuid.uuid4().hex[:8]),
            label=str(payload.get("label") or provider),
            auth_type=str(payload.get("auth_type") or AUTH_TYPE_API_KEY),
            source=str(payload.get("source") or "manual"),
            access_token=str(payload.get("access_token") or ""),
            refresh_token=payload.get("refresh_token"),
            priority=int(payload.get("priority") or 0),
            last_status=payload.get("last_status") or STATUS_OK,
            last_status_at=payload.get("last_status_at"),
            last_error_code=payload.get("last_error_code"),
            last_error_message=payload.get("last_error_message"),
            expires_at_ms=payload.get("expires_at_ms"),
            request_count=int(payload.get("request_count") or 0),
            base_url=payload.get("base_url"),
            extra=dict(payload.get("extra") or {}),
        )


def _exhausted_ttl(error_code: Optional[int]) -> int:
    if error_code == 401:
        return EXHAUSTED_TTL_401
    if error_code == 429:
        return EXHAUSTED_TTL_429
    return EXHAUSTED_TTL_DEFAULT


def is_plan_limit_message(message: str) -> bool:
    m = (message or "").lower()
    return any(s in m for s in _PLAN_LIMIT_MARKERS)


class CredentialPool:
    def __init__(self, provider: str, entries: Optional[List[PooledCredential]] = None):
        self.provider = provider
        self._entries: List[PooledCredential] = list(entries or [])

    def entries(self) -> List[PooledCredential]:
        return list(self._entries)

    def add_entry(self, entry: PooledCredential) -> PooledCredential:
        if not entry.id:
            entry.id = uuid.uuid4().hex[:8]
        if entry.priority <= 0 and self._entries:
            entry.priority = max(e.priority for e in self._entries) + 1
        self._entries.append(entry)
        _persist_all()
        return entry

    def remove_entry(self, entry_id: str) -> bool:
        before = len(self._entries)
        self._entries = [e for e in self._entries if e.id != entry_id]
        if len(self._entries) != before:
            _persist_all()
            return True
        return False

    def _healthy(self, entry: PooledCredential, now: float) -> bool:
        status = entry.last_status or STATUS_OK
        if status == STATUS_DEAD:
            return False
        if status != STATUS_EXHAUSTED:
            return True
        # Cooldown elapsed?
        started = float(entry.last_status_at or 0)
        ttl = _exhausted_ttl(entry.last_error_code)
        if started and (now - started) >= ttl:
            entry.last_status = STATUS_OK
            entry.last_error_code = None
            entry.last_error_message = None
            return True
        return False

    def select(self) -> Optional[PooledCredential]:
        now = time.time()
        healthy = [e for e in self._entries if e.runtime_token and self._healthy(e, now)]
        if not healthy:
            return None
        strategy = _strategies.get(self.provider, STRATEGY_FILL_FIRST)
        if strategy == STRATEGY_ROUND_ROBIN:
            idx = _rr_index.get(self.provider, 0) % len(healthy)
            _rr_index[self.provider] = idx + 1
            chosen = healthy[idx]
        elif strategy == STRATEGY_LEAST_USED:
            chosen = min(healthy, key=lambda e: e.request_count)
        elif strategy == STRATEGY_RANDOM:
            chosen = random.choice(healthy)
        else:
            # fill_first: lowest priority number first (insertion order via priority)
            chosen = sorted(healthy, key=lambda e: e.priority)[0]
        chosen.request_count += 1
        return chosen

    def mark_exhausted_and_rotate(
        self,
        entry_id: str,
        *,
        error_code: Optional[int] = None,
        message: str = "",
        immediate: bool = False,
    ) -> Optional[PooledCredential]:
        """Mark ``entry_id`` exhausted and return the next healthy credential."""
        now = time.time()
        for e in self._entries:
            if e.id == entry_id:
                e.last_status = STATUS_EXHAUSTED
                e.last_status_at = now
                e.last_error_code = error_code
                e.last_error_message = (message or "")[:500]
                break
        _persist_all()
        return self.select()

    def reset_cooldowns(self) -> None:
        for e in self._entries:
            if e.last_status == STATUS_EXHAUSTED:
                e.last_status = STATUS_OK
                e.last_error_code = None
                e.last_error_message = None
                e.last_status_at = None
        _persist_all()


def _load_store() -> Dict[str, Any]:
    path = _pool_path()
    if not os.path.isfile(path):
        return {"version": 1, "pools": {}, "strategies": {}}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception as e:
        _diag("credential_pool.load", e)
    return {"version": 1, "pools": {}, "strategies": {}}


def _persist_all() -> None:
    path = _pool_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        payload = {
            "version": 1,
            "pools": {
                prov: [e.to_store_dict() for e in pool.entries()]
                for prov, pool in _pools.items()
            },
            "strategies": dict(_strategies),
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        _diag("credential_pool.persist", e)


def load_pool(provider: str) -> CredentialPool:
    """Return the in-memory pool for ``provider``, loading from disk once."""
    with _lock:
        if provider in _pools:
            return _pools[provider]
        store = _load_store()
        _strategies.update({
            k: v for k, v in (store.get("strategies") or {}).items()
            if v in SUPPORTED_STRATEGIES
        })
        raw_pools = store.get("pools") or {}
        entries_raw = raw_pools.get(provider) or []
        entries = [
            PooledCredential.from_store_dict(provider, e)
            for e in entries_raw
            if isinstance(e, dict)
        ]
        pool = CredentialPool(provider, entries)
        _pools[provider] = pool
        if not entries:
            _seed_from_env_and_keys(pool)
        # Export a healthy token into process env so Session.preflight and
        # classic env-only checks see OAuth pool credentials after restart.
        _mirror_pool_token_to_env(provider, pool)
        return pool


def set_strategy(provider: str, strategy: str) -> None:
    if strategy not in SUPPORTED_STRATEGIES:
        raise ValueError(f"unsupported strategy: {strategy}")
    with _lock:
        _strategies[provider] = strategy
        _persist_all()


def get_strategy(provider: str) -> str:
    return _strategies.get(provider, STRATEGY_FILL_FIRST)


def list_pool_public(provider: str) -> Dict[str, Any]:
    pool = load_pool(provider)
    return {
        "provider": provider,
        "strategy": get_strategy(provider),
        "oauth_capable": provider in OAUTH_CAPABLE or provider == "anthropic",
        "entries": [e.to_public_dict() for e in pool.entries()],
    }


def add_api_key(
    provider: str,
    api_key: str,
    *,
    label: str = "",
) -> PooledCredential:
    key = (api_key or "").strip()
    if not key:
        raise ValueError("api_key required")
    pool = load_pool(provider)
    # Dedup: return existing entry if the same token is already pooled.
    for existing in pool.entries():
        if existing.runtime_token == key:
            return existing
    entry = PooledCredential(
        provider=provider,
        id=uuid.uuid4().hex[:8],
        label=(label or f"{provider}-{len(pool.entries()) + 1}").strip(),
        auth_type=AUTH_TYPE_API_KEY,
        source="manual",
        access_token=key,
        priority=len(pool.entries()),
    )
    with _lock:
        pool.add_entry(entry)
    return entry


def remove_entry(provider: str, entry_id: str) -> bool:
    with _lock:
        pool = load_pool(provider)
        return pool.remove_entry(entry_id)


def list_all_pools_public() -> Dict[str, Any]:
    """Public snapshot of every known pool (empty pools omitted unless seeded)."""
    out = []
    for prov in known_pool_providers():
        pub = list_pool_public(prov)
        if pub["entries"]:
            out.append(pub)
    return {"pools": out, "strategies": list(SUPPORTED_STRATEGIES)}


def add_oauth_entry(
    provider: str,
    *,
    access_token: str,
    refresh_token: Optional[str] = None,
    label: str = "",
    expires_at_ms: Optional[int] = None,
    source: str = "manual:oauth",
    extra: Optional[Dict[str, Any]] = None,
) -> PooledCredential:
    pool = load_pool(provider)
    entry = PooledCredential(
        provider=provider,
        id=uuid.uuid4().hex[:8],
        label=(label or f"{provider}-oauth-{len(pool.entries()) + 1}").strip(),
        auth_type=AUTH_TYPE_OAUTH,
        source=source,
        access_token=(access_token or "").strip(),
        refresh_token=refresh_token,
        expires_at_ms=expires_at_ms,
        priority=len(pool.entries()),
        extra=dict(extra or {}),
    )
    if not entry.access_token:
        raise ValueError("access_token required")
    with _lock:
        pool.add_entry(entry)
        _mirror_pool_token_to_env(provider, pool)
    return entry


def has_healthy_credential(provider: str) -> bool:
    """True when the pool has a usable token (does not bump request_count)."""
    with _lock:
        pool = load_pool(provider)
        now = time.time()
        return any(
            e.runtime_token and pool._healthy(e, now) for e in pool.entries()
        )


def peek_token(provider: str) -> Optional[str]:
    """Return a healthy token without marking a request (availability checks)."""
    with _lock:
        pool = load_pool(provider)
        now = time.time()
        healthy = [
            e for e in pool.entries() if e.runtime_token and pool._healthy(e, now)
        ]
        if not healthy:
            return None
        strategy = _strategies.get(provider, STRATEGY_FILL_FIRST)
        if strategy == STRATEGY_LEAST_USED:
            chosen = min(healthy, key=lambda e: e.request_count)
        else:
            chosen = sorted(healthy, key=lambda e: e.priority)[0]
        return chosen.runtime_token


def resolve_token(provider: str) -> Optional[str]:
    """Select a healthy credential token for ``provider`` (or None)."""
    with _lock:
        pool = load_pool(provider)
        entry = pool.select()
        if entry is None:
            return None
        # Persist request_count bumps periodically
        if entry.request_count % 5 == 0:
            _persist_all()
        return entry.runtime_token


def resolve_entry(provider: str) -> Optional[PooledCredential]:
    with _lock:
        return load_pool(provider).select()


def report_failure(
    provider: str,
    entry_id: str,
    *,
    status_code: Optional[int] = None,
    message: str = "",
) -> Optional[str]:
    """Mark failure and return next token if any."""
    immediate = False
    if status_code in (402,):
        immediate = True
    if status_code == 429 and is_plan_limit_message(message):
        immediate = True
    with _lock:
        pool = load_pool(provider)
        nxt = pool.mark_exhausted_and_rotate(
            entry_id,
            error_code=status_code,
            message=message,
            immediate=immediate,
        )
        return nxt.runtime_token if nxt else None


def _seed_from_env_and_keys(pool: CredentialPool) -> None:
    """Hermes-style: auto-discover a single env/keys.json credential."""
    if pool.entries():
        return
    # Keep unit tests hermetic -- fixtures set HARNESS_STATE_DIR but the
    # developer's shell may still export real provider keys.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    provider = pool.provider
    env_map = {
        "openrouter": "OPENROUTER_API_KEY",
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "cursor": "CURSOR_API_KEY",
        "xai": "XAI_API_KEY",
        "google": "GOOGLE_API_KEY",
        "groq": "GROQ_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "mistral": "MISTRAL_API_KEY",
    }
    env_name = env_map.get(provider)
    token = ""
    source = ""
    if env_name:
        token = (os.environ.get(env_name) or "").strip()
        if token:
            source = f"env:{env_name}"
    if not token:
        try:
            from .keys import get_keys_file_path
            path = get_keys_file_path()
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as f:
                    keys = json.load(f)
                raw = keys.get(provider)
                if isinstance(raw, str) and raw.strip():
                    token = raw.strip()
                    source = "keys.json"
        except Exception as e:
            _diag("credential_pool.seed_keys", e)
    if token:
        pool._entries.append(PooledCredential(
            provider=provider,
            id=uuid.uuid4().hex[:8],
            label=source or provider,
            auth_type=AUTH_TYPE_API_KEY,
            source=source or "seed",
            access_token=token,
            priority=0,
        ))
        _persist_all()


def clear_pools_for_tests() -> None:
    """Test helper: drop in-memory state (does not delete disk)."""
    with _lock:
        _pools.clear()
        _rr_index.clear()
        _strategies.clear()


_ENV_TO_PROVIDER = {
    "OPENROUTER_API_KEY": "openrouter",
    "OPENAI_API_KEY": "openai",
    "ANTHROPIC_API_KEY": "anthropic",
    "ANTHROPIC_TOKEN": "anthropic",
    "CURSOR_API_KEY": "cursor",
    "OPENAI_CODEX_TOKEN": "openai-codex",
    "XAI_API_KEY": "xai",
    "XAI_OAUTH_TOKEN": "xai-oauth",
    "NOUS_API_KEY": "nous",
    "GOOGLE_API_KEY": "google",
    "GROQ_API_KEY": "groq",
    "DEEPSEEK_API_KEY": "deepseek",
    "MISTRAL_API_KEY": "mistral",
}

# Extra pools that also satisfy a driver env (OAuth sibling ids).
# Grok SuperGrok OAuth lives in ``xai-oauth`` but the pilot uses ``XAI_API_KEY``.
_ENV_FALLBACK_PROVIDERS: Dict[str, tuple[str, ...]] = {
    "XAI_API_KEY": ("xai-oauth",),
}

# Preferred env var to populate when a pool has a healthy token (OAuth burn).
_PROVIDER_TO_ENV = {
    "openai-codex": "OPENAI_CODEX_TOKEN",
    "anthropic": "ANTHROPIC_API_KEY",
    "xai-oauth": "XAI_OAUTH_TOKEN",
    "nous": "NOUS_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "cursor": "CURSOR_API_KEY",
    "xai": "XAI_API_KEY",
}

# Additional env vars to mirror so classic pilots see OAuth tokens.
_PROVIDER_TO_ENV_EXTRAS: Dict[str, tuple[str, ...]] = {
    "xai-oauth": ("XAI_API_KEY",),
}


def provider_for_env_var(env_name: str) -> Optional[str]:
    """Map a driver ``api_key_env`` name to the primary pool provider id."""
    return _ENV_TO_PROVIDER.get((env_name or "").strip())


def providers_for_env_var(env_name: str) -> List[str]:
    """Primary + fallback pool ids that can satisfy ``env_name``."""
    key = (env_name or "").strip()
    primary = _ENV_TO_PROVIDER.get(key)
    out: List[str] = []
    if primary:
        out.append(primary)
    for extra in _ENV_FALLBACK_PROVIDERS.get(key, ()):
        if extra not in out:
            out.append(extra)
    return out


def env_var_for_provider(provider: str) -> Optional[str]:
    return _PROVIDER_TO_ENV.get((provider or "").strip())


def credential_satisfied(env_name: str) -> bool:
    """True when env has the key OR a matching credential pool is healthy."""
    if (os.environ.get(env_name) or "").strip():
        return True
    try:
        return any(has_healthy_credential(p) for p in providers_for_env_var(env_name))
    except Exception:
        return False


def peek_token_for_env(env_name: str) -> Optional[str]:
    """Peek a healthy token from any pool that satisfies ``env_name``."""
    for prov in providers_for_env_var(env_name):
        tok = peek_token(prov)
        if tok:
            return tok
    return None


def resolve_entry_for_env(env_name: str) -> Optional[PooledCredential]:
    """Select a healthy pool entry for a driver env (tries OAuth aliases)."""
    for prov in providers_for_env_var(env_name):
        entry = resolve_entry(prov)
        if entry is not None and entry.runtime_token:
            return entry
    return None


def _mirror_pool_token_to_env(provider: str, pool: Optional[CredentialPool] = None) -> None:
    """Best-effort: put a healthy pool token into process env for preflight/UI."""
    env_names = []
    primary = env_var_for_provider(provider)
    if primary:
        env_names.append(primary)
    for extra in _PROVIDER_TO_ENV_EXTRAS.get(provider, ()):
        if extra not in env_names:
            env_names.append(extra)
    if not env_names:
        return
    try:
        p = pool if pool is not None else load_pool(provider)
        now = time.time()
        for e in p.entries():
            tok = e.runtime_token
            if tok and p._healthy(e, now):
                for env_name in env_names:
                    os.environ[env_name] = tok
                return
    except Exception as e:
        _diag("credential_pool.mirror_env", e)


def known_pool_providers() -> List[str]:
    """Providers shown in Settings (union of OAuth + API-key pools)."""
    return sorted(OAUTH_CAPABLE | API_KEY_PROVIDERS)
