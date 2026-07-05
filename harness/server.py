from __future__ import annotations

"""Harness web server: a local, zero-dependency-beyond-stdlib HTTP server that
serves the three-pane GUI and streams Session events over SSE. Cursor 3.0 /
Hermes style: left nav, center driver-loop conversation, right durable-state.

stdlib http.server only -- no FastAPI/uvicorn needed, keeps the harness
dependency-light and launchable anywhere.
"""

import json
import os
import time
import threading
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import secrets as _secrets
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import tempfile
import uuid

from .config import HarnessConfig
from .session import Session
from .conversation import ConversationalSession
from .mcp_manager import McpManager, CATALOG
from .skill_store import SkillStore
from .rule_store import RuleStore
from .command_store import CommandStore
from .memory_store import MemoryStore, MEMORY_CHAR_LIMIT
from . import workspaces as _ws
from .sessions import SessionStore, save_transcript, load_transcript
from .autobudget import AutoBudget
from ._exec import _puppetmaster_python, _puppetmaster_available, _puppetmaster_cmd, _ensure_node_on_path
from .diag import note as _diag


# Prompt-cache reads are billed at a steep discount vs fresh input tokens:
# Anthropic prompt caching and OpenAI/Gemini cached-input reads are ~10% of the
# normal input price. We meter cache_read_tokens separately (see
# ConversationalSession) and re-bill that slice at this multiplier instead of
# the full input rate. 0.1 is the published cache-read ratio and is
# conservative (some providers go lower), so we never *under*-count spend.
CACHE_READ_MULTIPLIER = 0.1


def _session_cost(t_in: float, t_out: float, cached: float,
                  price_in: float, price_out: float) -> float:
    """Deterministic session cost from tokens + per-Mtok prices.

    Cached prompt tokens are a subset of ``t_in`` and are billed at the
    cache-read discount rather than full input price. Falls back to pricing the
    whole total at ``price_out`` when no in/out split is available (completion
    dominates cost, so this is the least-wrong single-rate estimate)."""
    if t_in or t_out:
        cached = max(0.0, min(float(cached), float(t_in)))
        uncached_in = max(0.0, float(t_in) - cached)
        return ((uncached_in / 1.0e6) * price_in
                + (cached / 1.0e6) * price_in * CACHE_READ_MULTIPLIER
                + (float(t_out) / 1.0e6) * price_out)
    # No split tracked: price the combined total at the output rate.
    total = float(t_in) + float(t_out)
    return (total / 1.0e6) * price_out


def _cache_savings(cached: float, price_in: float) -> float:
    """USD saved by billing ``cached`` prompt tokens at the cache-read discount
    instead of the full input price."""
    return (float(cached) / 1.0e6) * price_in * (1.0 - CACHE_READ_MULTIPLIER)


def _job_cost(tokens_in: float, tokens_out: float, tokens_total: float,
              price_in: float, price_out: float) -> float:
    """Deterministic per-job cost. Uses the real in/out split when the job
    carries it; otherwise prices the single ``tokens`` total at ``price_out``
    (completion tokens dominate cost, matching the session fallback) rather than
    a naive 50/50 blend that mis-prices output-heavy jobs."""
    if tokens_in or tokens_out:
        return ((float(tokens_in) / 1.0e6) * price_in
                + (float(tokens_out) / 1.0e6) * price_out)
    return (float(tokens_total) / 1.0e6) * price_out


def _get_platform_json_path() -> str:
    override = os.environ.get("TEST_PLATFORM_JSON_PATH")
    if override:
        return override
    return os.path.expanduser("~/.puppetmaster/platform.json")


def _write_platform_json_atomic(path: str, data: dict) -> None:
    dir_path = os.path.dirname(path)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=dir_path or ".", prefix="platform_")
    try:
        with os.fdopen(tmp_fd, 'w', encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def _init_platform_lock() -> None:
    path = _get_platform_json_path()
    pdata = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                pdata = json.load(f)
        except Exception as e:
            _diag("server.platform_lock_read", e)
    if not isinstance(pdata, dict):
        pdata = {}
    
    if not os.path.exists(path) or "harness_initialized" not in pdata:
        # Standalone default: out of the box only the built-in ``agentic`` adapter
        # is enabled. It runs its own tool-use loop directly against whatever
        # provider API the user has a key for (Anthropic, OpenAI, Gemini,
        # OpenRouter, ...), so a fresh install needs NOTHING but a provider key --
        # no external agent CLI (cursor / claude / codex / hermes) installed or
        # logged in. Every CLI adapter is left OFF so Marionette stays fully
        # self-contained and vendor-neutral; any of them can still be re-enabled
        # in Settings > Platform for users who have that tooling.
        default_disabled = ["cursor", "claude-code", "codex", "openai", "hermes"]
        if "disabled" not in pdata or not isinstance(pdata["disabled"], list):
            pdata["disabled"] = default_disabled
        else:
            # Legacy platform.json missing the init marker: fold in the standalone
            # defaults (so every CLI adapter lands off) while guaranteeing the
            # built-in agentic adapter stays on.
            merged = set(pdata["disabled"]) | set(default_disabled)
            merged.discard("agentic")
            pdata["disabled"] = sorted(merged)
        pdata["harness_initialized"] = True
        try:
            _write_platform_json_atomic(path, pdata)
        except Exception as e:
            _diag("server.platform_lock_write", e)


def _seed_agentic_catalog() -> None:
    """Seed the standalone 'agentic' models into the Puppetmaster registry.

    auto_route can only pick a standalone model if one is in
    ``~/.puppetmaster/models.json``. This merges the curated agentic catalog
    (API-billed) filtered to the providers the user actually has a key for, so a
    fresh install with, say, only an Anthropic key gets exactly the Anthropic
    agentic models and nothing that would 401. Idempotent (refresh-or-add) and
    never fatal -- a swarm must never fail to start over catalog seeding.
    """
    try:
        from pathlib import Path as _Path
        from puppetmaster.model_registry import load_registry, save_registry, default_registry_path
        from puppetmaster.static_catalog import merge_curated_into_registry
        from puppetmaster.providers import available_providers

        env_path = os.environ.get("PUPPETMASTER_MODELS_PATH")
        registry_path = _Path(env_path) if env_path else default_registry_path()
        existing = load_registry(registry_path)
        merged, _report = merge_curated_into_registry(
            "agentic", "api", existing, allowed_providers=available_providers()
        )
        save_registry(merged, registry_path)
    except Exception as e:
        _diag("server.seed_agentic_catalog", e)


def _get_platform_adapters() -> dict:
    import shutil
    from .keys import get_api_key_status
    path = _get_platform_json_path()
    disabled_list = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                pdata = json.load(f)
                if isinstance(pdata, dict) and "disabled" in pdata and isinstance(pdata["disabled"], list):
                    disabled_list = pdata["disabled"]
        except Exception as e:
            _diag("server.platform_disabled_read", e)

    adapters_config = [
        {"name": "agentic", "implement_capable": True},
        {"name": "cursor", "implement_capable": True},
        {"name": "hermes", "implement_capable": True},
        {"name": "claude-code", "implement_capable": True},
        {"name": "codex", "implement_capable": True},
        {"name": "openai", "implement_capable": False}
    ]

    adapters = []
    for cfg in adapters_config:
        name = cfg["name"]
        enabled = name not in disabled_list
        
        # Best-effort availability
        if name == "agentic":
            try:
                from puppetmaster.providers import available_providers
                ready = sorted(available_providers())
            except Exception:
                ready = []
            available = bool(ready)
            note = (
                "Standalone (default). Runs directly on your provider keys -- no "
                "external CLI. "
                + (f"Ready: {', '.join(ready)}." if ready else "Add a provider key to enable.")
            )
        elif name == "hermes":
            available = ("OPENROUTER_API_KEY" in os.environ) or get_api_key_status("openrouter")["has_key"]
            note = "Hermes via OpenRouter. Uses standard API key."
        elif name == "openai":
            available = ("OPENAI_API_KEY" in os.environ) or get_api_key_status("openai")["has_key"]
            note = "OpenAI API adapter. Note: Analysis-only, cannot drive implement tasks."
        elif name == "cursor":
            available = shutil.which("cursor") is not None
            note = "Cursor editor CLI. Run swarm/implement in a Cursor workspace."
        elif name == "claude-code":
            available = shutil.which("claude") is not None
            note = "Anthropic Claude Code. Requires 'claude' npm command in path."
        elif name == "codex":
            available = shutil.which("codex") is not None
            note = "Codex agent CLI. Requires 'codex' command in path."
        else:
            available = True
            note = ""

        adapters.append({
            "name": name,
            "enabled": enabled,
            "implement_capable": cfg["implement_capable"],
            "available": available,
            "note": note
        })
    return {"adapters": adapters}


_WEB = Path(__file__).resolve().parent / "web"
# One shared session per server process (single-user local app).
_state_dir = os.environ.get("HARNESS_STATE_DIR", "")
_cfg = HarnessConfig.from_env()
def _state_home() -> str:
    """Base dir for app state files (workspace.json, token, drivers, markers).

    Honors HARNESS_STATE_DIR so the test suite -- which sets it to an isolated
    temp dir per test (tests/conftest.py::_isolate_provider_state) -- can NEVER
    read or write the developer's real ~/.pmharness. These paths used to be
    frozen to real home at import time, so importing harness.server during tests
    leaked live state: a dead pytest temp repo in workspace.json and, worse, a
    rewritten auth token. A respawned backend then held a token the renderer no
    longer knew, every request 403'd, and it read as "the backend died."
    """
    return os.environ.get("HARNESS_STATE_DIR") or os.path.expanduser("~/.pmharness")


def _workspace_json_path() -> str:
    return os.path.join(_state_home(), "workspace.json")


def _workspace_drivers_path() -> str:
    return os.path.join(_state_home(), "workspace_drivers.json")


def _save_workspace_driver(repo: str, driver: str) -> None:
    """Remember which model the user last used in a given workspace, so opening
    that dir later restores it (use opus-4-8 in repo A, gpt-5.5 in repo B, and
    each comes back correctly on switch)."""
    if not repo or not driver:
        return
    import tempfile as _tf
    # Never persist ephemeral temp dirs (test state leaks otherwise).
    if os.path.realpath(repo).startswith(os.path.realpath(_tf.gettempdir())):
        return
    path = _workspace_drivers_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {}
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
            except Exception:
                data = {}
        if not isinstance(data, dict):
            data = {}
        data[os.path.realpath(repo)] = driver
        from .registry_wizard import write_json_atomic
        write_json_atomic(path, data)
    except Exception as e:
        _diag("server.workspace_driver_write", e)


def _get_workspace_driver(repo: str):
    """The model last used in this workspace, or None if never set."""
    if not repo:
        return None
    path = _workspace_drivers_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        return data.get(os.path.realpath(repo))
    except Exception:
        return None

def _record_recent_workspace(target_repo: str) -> list:
    import json
    import os
    import tempfile as _tf
    ws_json_path = _workspace_json_path()
    try:
        os.makedirs(os.path.dirname(ws_json_path), exist_ok=True)
        recents = []
        if os.path.exists(ws_json_path):
            try:
                with open(ws_json_path) as f:
                    recents = json.load(f).get("recents", []) or []
            except Exception:
                recents = []
        # never persist temp dirs (test/ephemeral state_dirs leak otherwise)
        _tmproot = os.path.realpath(_tf.gettempdir())
        def _persistable(_pth):
            if not _pth:
                return False
            _rp = os.path.realpath(_pth)
            if "PYTEST_CURRENT_TEST" not in os.environ:
                if _rp.startswith(_tmproot) or "/var/folders/" in _rp or "/T/tmp" in _pth:
                    return False
            return os.path.isdir(_pth)
        # prepend, dedupe (keep first occurrence), drop temp/dead dirs, cap 8
        recents = [target_repo] + [r for r in recents if r != target_repo]
        recents = [r for r in recents if _persistable(r)]
        recents = recents[:8]

        # Use atomic-write
        target_dir = os.path.dirname(ws_json_path)
        fd, temp_path = _tf.mkstemp(dir=target_dir, prefix=".tmp-")
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump({"repo": target_repo, "recents": recents}, f)
            os.replace(temp_path, ws_json_path)
        except Exception as e:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            raise e

        try:
            os.chmod(ws_json_path, 0o600)
        except Exception:
            pass
        return recents
    except Exception:
        # Fallback to get recents if possible
        try:
            if os.path.exists(ws_json_path):
                with open(ws_json_path) as f:
                    return json.load(f).get("recents", []) or []
        except Exception:
            pass
        return []

def _forget_recent_workspace(forget_path: str) -> list:
    import json
    import os
    import tempfile as _tf
    ws_json_path = _workspace_json_path()
    try:
        os.makedirs(os.path.dirname(ws_json_path), exist_ok=True)
        recents = []
        repo = ""
        if os.path.exists(ws_json_path):
            try:
                with open(ws_json_path) as f:
                    data = json.load(f)
                    recents = data.get("recents", []) or []
                    repo = data.get("repo", "")
            except Exception:
                recents = []
        # never persist temp dirs (test/ephemeral state_dirs leak otherwise)
        _tmproot = os.path.realpath(_tf.gettempdir())
        def _persistable(_pth):
            if not _pth:
                return False
            _rp = os.path.realpath(_pth)
            if "PYTEST_CURRENT_TEST" not in os.environ:
                if _rp.startswith(_tmproot) or "/var/folders/" in _rp or "/T/tmp" in _pth:
                    return False
            return os.path.isdir(_pth)

        # remove forget_path
        recents = [r for r in recents if r != forget_path]
        recents = [r for r in recents if _persistable(r)]
        recents = recents[:8]

        # Use atomic-write
        target_dir = os.path.dirname(ws_json_path)
        fd, temp_path = _tf.mkstemp(dir=target_dir, prefix=".tmp-")
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump({"repo": repo, "recents": recents}, f)
            os.replace(temp_path, ws_json_path)
        except Exception as e:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            raise e

        try:
            os.chmod(ws_json_path, 0o600)
        except Exception:
            pass
        return recents
    except Exception:
        # Fallback to get recents if possible
        try:
            if os.path.exists(ws_json_path):
                with open(ws_json_path) as f:
                    return json.load(f).get("recents", []) or []
        except Exception:
            pass
        return []

_ws_boot_path = _workspace_json_path()
if not os.environ.get("HARNESS_REPO") and os.path.exists(_ws_boot_path):
    try:
        with open(_ws_boot_path, "r") as _ws_f:
            _ws_data = json.load(_ws_f)
            # Only adopt a persisted repo that still exists on disk. A stale or
            # corrupted workspace.json (e.g. a vanished dir) must not wedge boot.
            if _ws_data.get("repo") and os.path.isdir(_ws_data["repo"]):
                _cfg.repo = _ws_data["repo"]
                os.environ["HARNESS_REPO"] = _ws_data["repo"]
    except Exception as e:
        _diag("server.workspace_boot_load", e)

if _state_dir:
    _cfg.state_dir = _state_dir
elif not _cfg.state_dir:
    # DURABLE STATE (fixes swarm-history loss across restart): with no explicit
    # HARNESS_STATE_DIR and no config value, state_dir was left blank -- so the
    # session and pilot each fell back to their OWN throwaway mkdtemp(), landing
    # swarm history / transcripts / job stores in a fresh temp dir every launch
    # that nothing ever reads again. Anchor to a stable per-install dir under the
    # state home so history (swarm_local_jobs.json, transcripts, sqlite job
    # stores) survives close/reopen and backend restarts, tied to the install.
    _stable = os.path.join(_state_home(), "state")
    try:
        os.makedirs(_stable, exist_ok=True)
        _cfg.state_dir = _stable
        os.environ.setdefault("HARNESS_STATE_DIR", _stable)
    except Exception as e:
        _diag("server.stable_state_dir", e)

# Masker-safe live key: if HARNESS_KEY_FILE points at a file, load it into the
# expected env var for the chosen reach before the Session builds its driver.
from .keys import load_api_keys_on_startup, get_api_key_status, get_env_var_for_reach, set_api_key, clear_api_key
from .wiki_config import load_wiki_config_on_startup, get_wiki_config, set_wiki_config
from .wiki_backend import ensure_wiki_backend_async
load_api_keys_on_startup(_cfg.reach)
# The Electron host spawns the backend with a stripped PATH; make Node visible so
# CodeGraph (a Node CLI) works out of the box instead of reporting "unsupported".
_ensure_node_on_path()
load_wiki_config_on_startup()
# Auto-provision (clone + venv + token) and boot a local wiki backend so the
# panel works out of the box with no manual terminal. Opt out: MARIONETTE_NO_WIKI=1.
ensure_wiki_backend_async()


def _driver_provider_available(spec: str) -> bool:
    """True if the provider backing a driver spec currently has a usable key.
    A bare name (e.g. 'qwen3-coder-30b') routes through the reach provider
    (OpenRouter); a 'provider:model' spec is backed by that provider."""
    from . import providers as _prov
    if not spec:
        return False
    # Stub/offline drivers (stub-oracle-v2, etc.) run deterministically with no
    # provider key, so they are always usable and must never be swapped out by
    # startup driver resolution. Mirrors doctor.py's spec.startswith("stub").
    if spec.startswith("stub"):
        return True
    if ":" in spec:
        prov_name = spec.split(":", 1)[0]
        p = _prov.get_provider(prov_name)
        return bool(p and p.available)
    # Bare catalog name -> uses the reach provider (default openrouter).
    p = _prov.get_provider(_cfg.reach)
    return bool(p and p.available)


def _resolve_available_driver():
    """If the configured driver's provider is unavailable (e.g. OpenRouter was
    disconnected but the saved driver still routes through it), fall back to the
    first available enabled model so the app never defaults to a dead driver."""
    global _cfg
    try:
        if _driver_provider_available(_cfg.driver):
            return
        # Pick the first available pilot (enabled set, key-filtered).
        from . import model_visibility as _mv
        candidates = _mv.enabled_pilots()
        for spec in candidates:
            if _driver_provider_available(spec):
                _cfg.driver = spec
                # Recompute the context window inline (the _apply_model_context_window
                # helper is defined later in this module; avoid a forward reference).
                if "HARNESS_MAX_CONTEXT_TOKENS" not in os.environ:
                    try:
                        from pmharness.registry import context_window
                        _cfg.max_context_tokens = context_window(_cfg.driver, default=200000)
                    except Exception as e:
                        _diag("server.resolve_driver_context_window", e)
                return
    except Exception as e:
        _diag("server.resolve_available_driver", e)


_resolve_available_driver()
_session = Session(_cfg)
_pilot = ConversationalSession(_cfg)
# Session and pilot each fall back to their OWN mkdtemp() when config.state_dir
# is blank (the default), landing run_swarm's job store (pilot's state_dir) and
# the tracker's read store (session's state_dir) in two DIFFERENT temp dirs. The
# Swarm Tracker (/api/swarm/live) and Session Jobs (/api/jobs) read the session
# store, so they stayed empty even after a real swarm ran in the pilot store.
# Pin the session to the pilot's store so both read exactly where jobs are written.
_session.state_dir = _pilot.state_dir
import tempfile as _tf
_sessions = SessionStore(os.path.join(_cfg.state_dir or _tf.gettempdir(), "harness_sessions.json"))
_mcp = McpManager()
# Serialize pilot rebinds so a /api/pilot swap and a workspace-switch rebuild
# cannot interleave their history-copy/rebind steps and leave a torn _pilot.
_pilot_swap_lock = threading.Lock()
from .pty_manager import PtyManager
_pty = PtyManager()
_pilot._mcp = _mcp
_init_platform_lock()
_seed_agentic_catalog()

def _apply_model_context_window():
    """Recompute _cfg.max_context_tokens for the active driver's real window
    after a model swap. An explicit HARNESS_MAX_CONTEXT_TOKENS env override
    always wins (so a deliberate cap is never silently widened)."""
    if "HARNESS_MAX_CONTEXT_TOKENS" in os.environ:
        return
    try:
        from pmharness.registry import context_window
        _cfg.max_context_tokens = context_window(_cfg.driver, default=200000)
    except Exception as e:
        _diag("server.apply_model_context_window", e)


def _rebuild_pilot_and_session():
    """Rebuild the session + pilot for the active driver, preserving history.

    Defensive: if the configured driver cannot be built (e.g. a stale saved
    spec the catalog no longer knows), do NOT let the exception escape and
    crash the POST handler -- that left the whole app dead on workspace-open /
    session-switch. We roll back to the previous working driver and surface the
    error to the caller to show, instead of taking down the process.
    """
    global _session, _pilot, _cfg
    prev_driver = _cfg.driver
    _apply_model_context_window()
    try:
        new_session = Session(_cfg)
        new_pilot = ConversationalSession(_cfg)
    except Exception as e:
        # Roll back to the last driver that built successfully.
        _cfg.driver = prev_driver
        _apply_model_context_window()
        raise RuntimeError(
            f"could not load model {prev_driver!r}: {e}. Reverted to the "
            f"previous pilot."
        ) from e
    # Keep the tracker/jobs reads pointed at the store the pilot writes to (see
    # the pin at initial construction) across workspace/driver switches too.
    new_session.state_dir = new_pilot.state_dir
    with _pilot_swap_lock:
        old_history = _pilot._history
        old_auto_distill = getattr(_pilot, "_auto_distill", False)
        _session = new_session
        _pilot = new_pilot
        _pilot._history = old_history
        _pilot._auto_distill = old_auto_distill
        _pilot._mcp = _mcp

# Startup: Restore the active/most-recent session's transcript into _pilot
if _sessions.active:
    _startup_history = load_transcript(_cfg.state_dir or _tf.gettempdir(), _sessions.active)
    if _startup_history:
        _pilot.load_history(_startup_history)

_skills = SkillStore()
_rules = RuleStore()
_commands = CommandStore()
_memory = MemoryStore()
_UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "harness-uploads")
os.makedirs(_UPLOAD_DIR, exist_ok=True)

# Per-process auth token (defense-in-depth). Written chmod-600 so the local
# client (Electron main / served page) can read it; required on mutating
# endpoints. Origin/Host validation below is the primary anti-RCE guard.
_TOKEN = os.environ.get("HARNESS_TOKEN") or _secrets.token_hex(16)
_TOKEN_FILE = os.path.join(_state_home(), "token")
try:
    os.makedirs(os.path.dirname(_TOKEN_FILE), exist_ok=True)
    with open(_TOKEN_FILE, "w") as _tf2:
        _tf2.write(_TOKEN)
    os.chmod(_TOKEN_FILE, 0o600)
except OSError:
    pass

_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _host_ok(host_header: str) -> bool:
    """Defeat DNS-rebinding: the Host must be a literal loopback name. A rebound
    attacker domain (evil.com -> 127.0.0.1) shows its own name in Host."""
    if not host_header:
        return False
    host = host_header.rsplit(":", 1)[0] if host_header.count(":") <= 1 else host_header.rsplit(":", 1)[0]
    return host in _ALLOWED_HOSTS


def _origin_ok(origin: str) -> bool:
    """A malicious webpage sends its own Origin (https://evil.com) on cross-origin
    requests -> reject. Same-origin requests omit Origin; Electron file:// sends
    'null'. Both allowed."""
    if not origin or origin == "null":
        return True
    try:
        from urllib.parse import urlparse as _up
        h = _up(origin).hostname
        return h in _ALLOWED_HOSTS
    except Exception:
        return False


def _parse_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("1", "true", "yes", "on")
    return False


_codegraph_status = "unsupported"
_codegraph_status_reason = None

# Short-TTL cache for the /api/codegraph status payload, keyed by repo path.
# Reading codegraph status spawns a `puppetmaster codegraph status --json`
# subprocess (interpreter cold-start + DB read) on every poll, which is the
# source of the panel's load lag. The graph only changes on (re)index, so we
# serve a cached payload for a few seconds and only re-spawn when stale. The
# cache is bypassed entirely while status == "indexing" (that path never hits
# the subprocess), so a fresh index is reflected as soon as it finishes.
_codegraph_status_cache = {}  # repo -> (monotonic_expiry, payload_dict)
_CODEGRAPH_STATUS_TTL = 30.0  # seconds

# Short-TTL cache for the /api/wiki/graph payload. Each fetch is an HTTP round
# trip to the wiki host (up to an 8s timeout when slow/unreachable), and the
# wiki graph changes rarely, so a brief cache removes the repeated stall on the
# panel without making the data meaningfully stale.
_wiki_graph_cache = {}  # base_url -> (monotonic_expiry, payload_dict)
_WIKI_GRAPH_TTL = 60.0  # seconds


# Handle to the in-flight CodeGraph indexer: (repo_path, Popen). Lets status
# self-heal -- a wedged "indexing" flag can never outlive the actual job -- and
# prevents a SECOND indexer from spawning while one runs (concurrent indexers
# collide on the same SQLite and Puppetmaster fails them lock-busy, which
# manifested as the panel locking up + metrics vanishing).
_codegraph_index_proc = None  # tuple[str, subprocess.Popen] | None
_codegraph_index_lock = threading.Lock()


def _codegraph_indexed(repo_path: str) -> bool:
    """True only when a built CodeGraph DB exists for the repo.

    The `.codegraph/` directory alone is NOT proof of an index: `codegraph init`
    writes `config.json` there before any indexing, and `codegraph status` hangs
    on a config-only checkout (no DB) until it times out -- which surfaced as
    "unsupported" on fresh installs. Gate on the actual DB file so an init'd-but-
    unindexed checkout is treated as needing an index, not as ready.
    """
    try:
        return os.path.isfile(os.path.join(repo_path, ".codegraph", "codegraph.db"))
    except Exception:
        return False


def _codegraph_index_alive() -> bool:
    """True only while the tracked indexer subprocess is actually running."""
    p = _codegraph_index_proc
    if p is None:
        return False
    try:
        return p[1].poll() is None
    except Exception:
        return False


def _index_codegraph_bg(repo_path: str):
    global _codegraph_status, _codegraph_status_reason, _codegraph_status_cache
    if not _puppetmaster_available():
        _codegraph_status = "unsupported"
        _codegraph_status_reason = "puppetmaster not found -- codegraph/swarm unavailable"
        return
    global _codegraph_index_proc
    # Guard against a second indexer while one is already running -- concurrent
    # codegraph indexers collide on the same SQLite (lock-busy) and wedge the panel.
    with _codegraph_index_lock:
        if _codegraph_index_alive():
            _codegraph_status = "indexing"
            return
        _codegraph_status = "indexing"
        _codegraph_status_reason = None
        # Invalidate any cached status for this repo so the panel does not show
        # stale "ready" stats while a fresh (re)index is running.
        _codegraph_status_cache.pop(repo_path, None)
        try:
            import subprocess
            proc = subprocess.Popen(
                _puppetmaster_cmd("codegraph", "init", "--index"),
                cwd=repo_path,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            _codegraph_index_proc = (repo_path, proc)
        except Exception:
            _codegraph_status = "unsupported"
            return

    def wait_and_update():
        global _codegraph_status, _codegraph_index_proc
        try:
            proc.wait(timeout=600)  # max 10 mins
            if proc.returncode == 0:
                _codegraph_status = "ready"
            else:
                _codegraph_status = "unsupported"
        except Exception:
            _codegraph_status = "unsupported"
            try:
                proc.kill()
            except Exception:
                pass
        finally:
            # Clear the tracker so status can self-heal and a future index can run.
            with _codegraph_index_lock:
                if _codegraph_index_proc and _codegraph_index_proc[1] is proc:
                    _codegraph_index_proc = None
            _codegraph_status_cache.pop(repo_path, None)

    threading.Thread(target=wait_and_update, daemon=True).start()


def _reindex_codegraph_bg(repo_path: str):
    global _codegraph_status, _codegraph_status_reason
    if not _puppetmaster_available():
        _codegraph_status = "unsupported"
        _codegraph_status_reason = "puppetmaster not found -- codegraph/swarm unavailable"
        return
    global _codegraph_index_proc
    with _codegraph_index_lock:
        if _codegraph_index_alive():
            _codegraph_status = "indexing"
            return
        _codegraph_status = "indexing"
        _codegraph_status_reason = None
        _codegraph_status_cache.pop(repo_path, None)
        try:
            import subprocess
            proc = subprocess.Popen(
                _puppetmaster_cmd("codegraph", "index"),
                cwd=repo_path,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            _codegraph_index_proc = (repo_path, proc)
        except Exception:
            _codegraph_status = "unsupported"
            return

    def wait_and_update():
        global _codegraph_status, _codegraph_index_proc
        try:
            proc.wait(timeout=600)  # max 10 mins
            if proc.returncode == 0:
                _codegraph_status = "ready"
            else:
                _codegraph_status = "unsupported"
        except Exception:
            _codegraph_status = "unsupported"
            try:
                proc.kill()
            except Exception:
                pass
        finally:
            with _codegraph_index_lock:
                if _codegraph_index_proc and _codegraph_index_proc[1] is proc:
                    _codegraph_index_proc = None
            _codegraph_status_cache.pop(repo_path, None)

    threading.Thread(target=wait_and_update, daemon=True).start()


def _get_codegraph_status(repo_path: str) -> str:
    global _codegraph_status
    if not repo_path:
        return "unsupported"
    if not _puppetmaster_available():
        _codegraph_status = "unsupported"
        return "unsupported"
    # Self-heal: trust the "indexing" flag ONLY while the indexer subprocess is
    # actually alive. A stale flag (proc finished but the wait thread lost a
    # race, or an old global left over) must not pin the panel on "indexing"
    # forever -- fall through to the disk check below. This is the bug that
    # required a full app restart to clear.
    if _codegraph_status == "indexing":
        if _codegraph_index_alive():
            return "indexing"
        # Indexer is not running -> resolve real state from disk.
        _codegraph_status = "ready" if os.path.isdir(os.path.join(repo_path, ".codegraph")) else "unsupported"

    if os.path.isdir(os.path.join(repo_path, ".codegraph")):
        _codegraph_status = "ready"
        return "ready"
    else:
        return "unsupported"


# Debounce: never re-check staleness more than once per this interval per repo,
# so per-turn triggers cannot thrash the (CPU-heavy) reindex during rapid edits.
_codegraph_stale_check_at = {}  # repo -> monotonic timestamp of last check
_CODEGRAPH_STALE_DEBOUNCE = 20.0  # seconds


def _codegraph_is_stale(repo_path: str) -> bool:
    """True if the working tree has changed since the .codegraph index was built.

    Detects edits AND deletions: we compare the index mtime against the newest
    mtime of (a) every source FILE and (b) every DIRECTORY. Directory mtimes are
    the key to catching deletions/renames -- removing a file bumps its parent
    dir's mtime even though no surviving file looks newer (the original bug:
    deleted files left the index referencing ghosts while this returned False).
    """
    try:
        codegraph_path = os.path.join(repo_path, ".codegraph")
        if not os.path.exists(codegraph_path):
            return False
        cg_mtime = os.path.getmtime(codegraph_path)
        skip_dirs = {".git", "node_modules", ".venv", ".codegraph", "dist", "build", "__pycache__"}
        extensions = {".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".swift", ".go", ".rs"}
        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            # (b) directory mtime -- catches deletions/renames/additions in this dir
            try:
                if os.path.getmtime(root) > cg_mtime:
                    return True
            except Exception:
                pass
            # (a) source file mtime -- catches in-place edits
            for file in files:
                _, ext = os.path.splitext(file)
                if ext.lower() in extensions:
                    try:
                        if os.path.getmtime(os.path.join(root, file)) > cg_mtime:
                            return True
                    except Exception:
                        pass
    except Exception:
        pass
    return False


def _maybe_refresh_codegraph(repo_path: str, *, force: bool = False) -> None:
    """Debounced, background staleness-driven reindex. Safe to call on every turn
    and on session switch -- the debounce + the indexing-guard ensure it never
    thrashes. force=True bypasses the debounce (e.g. an explicit user action)."""
    if not repo_path:
        return
    import time as _time
    if not force:
        last = _codegraph_stale_check_at.get(repo_path, 0.0)
        if (_time.monotonic() - last) < _CODEGRAPH_STALE_DEBOUNCE:
            return
    _codegraph_stale_check_at[repo_path] = _time.monotonic()

    def worker():
        global _codegraph_status, _codegraph_status_reason
        if _codegraph_status == "indexing":
            return
        if _codegraph_is_stale(repo_path):
            _codegraph_status_reason = "files changed -- refreshing index"
            _reindex_codegraph_bg(repo_path)
    try:
        threading.Thread(target=worker, daemon=True).start()
    except Exception as e:
        _diag("server.codegraph_stale_check_thread", e)


def _strip_markdown_fences(text: str) -> str:
    text_stripped = text.strip()
    if text_stripped.startswith("```"):
        lines = text_stripped.splitlines()
        if len(lines) >= 2 and lines[0].startswith("```"):
            if lines[-1].strip() == "```":
                return "\n".join(lines[1:-1])
            else:
                return "\n".join(lines[1:])
    return text


def _parse_multipart_files(body: bytes, content_type: str) -> list:
    """Extract uploaded files from a multipart/form-data body using the stdlib
    email parser. Replaces cgi.FieldStorage, which was removed in Python 3.13.
    Returns a list of (filename, data_bytes) for every part carrying a filename.
    The body is already size-capped by the caller, so buffering it is bounded."""
    from email.parser import BytesParser
    # Synthesize the MIME header block the parser needs, then feed it the body.
    header = (b"MIME-Version: 1.0\r\nContent-Type: "
              + content_type.encode("latin-1", "replace") + b"\r\n\r\n")
    message = BytesParser().parsebytes(header + body)
    files = []
    if not message.is_multipart():
        return files
    for part in message.get_payload():
        filename = part.get_filename()
        if not filename:
            continue
        data = part.get_payload(decode=True)
        if data is None:
            continue
        files.append((filename, data))
    return files


class Handler(BaseHTTPRequestHandler):
    def handle_one_request(self):
        # A client (the Electron renderer) closing the socket mid-request --
        # navigating away, stopping a stream, swapping models -- or a handler
        # that answers early without draining the request body (e.g. a 413 for an
        # oversized upload) raises ConnectionError/TimeoutError deep in the stdlib
        # request machinery. That is benign, but on a bare ThreadingHTTPServer it
        # escapes to socketserver's default handle_error and dumps a traceback to
        # stderr. Swallow only those transport errors so a disconnect never prints
        # noise; genuine handler bugs still surface unchanged.
        try:
            super().handle_one_request()
        except (ConnectionError, TimeoutError):
            self.close_connection = True

    def log_message(self, *a):  # quiet
        pass

    def _cors(self):
        # No wildcard. Reflect the Origin only when it is a loopback origin, so a
        # cross-origin attacker page can never read responses.
        origin = self.headers.get("Origin", "")
        if origin and origin != "null" and _origin_ok(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Harness-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _guard(self) -> bool:
        """Reject cross-origin / rebound / unauthenticated requests. Returns True
        if the request should be BLOCKED (and sends the 403)."""
        if not _host_ok(self.headers.get("Host", "")):
            self._send(403, json.dumps({"error": "host not allowed"})); return True
        if not _origin_ok(self.headers.get("Origin", "")):
            self._send(403, json.dumps({"error": "origin not allowed"})); return True
        return False

    def _token_ok(self) -> bool:
        if self.headers.get("X-Harness-Token", "") == _TOKEN:
            return True
        # Accept the token as a query param too, matching do_GET's checks. The IPC
        # POST bridge sends the header, so this changes no current behavior -- it
        # removes an asymmetry where a query-token caller was rejected only on POST.
        try:
            qtok = parse_qs(urlparse(self.path).query).get("token", [""])[0]
        except Exception:
            qtok = ""
        return qtok == _TOKEN

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def do_POST(self):
        global _codegraph_status
        if self._guard():
            return
        if not self._token_ok():
            return self._send(403, json.dumps({"error": "missing or bad token"}))
        u = urlparse(self.path)
        if u.path == "/api/upload":
            return self._handle_upload()
        if u.path in ("/api/workspaces/switch", "/api/workspaces/create",
                      "/api/sessions/create", "/api/sessions/switch",
                      "/api/sessions/delete", "/api/sessions/archive", "/api/sessions/rename",
                      "/api/session/interrupt", "/api/session/compact", "/api/session/steer",
                      "/api/session/queue", "/api/session/queue/reorder",
                      "/api/session/persist", "/api/restart",
                      "/api/swarm/cancel",
                      "/api/mcp/add", "/api/mcp/remove", "/api/mcp/start",
                      "/api/mcp/stop", "/api/mcp/call",
                      "/api/skills/distill", "/api/skills/approve",
                      "/api/wiki/ingest-prepared",
                      "/api/models/toggle", "/api/models/set",
                      "/api/skills/reject", "/api/skills/archive",
                      "/api/rules/approve", "/api/rules/reject",
                      "/api/memory/add", "/api/memory/remove",
                      "/api/settings", "/api/providers/probe", "/api/providers/key", "/api/wiki/config",
                      "/api/platform", "/api/reviews/apply", "/api/reviews/dismiss",
                      "/api/registry", "/api/roles", "/api/pilot/validate",
                      "/api/worktrees/add", "/api/worktrees/remove",
                      "/api/worktrees/prune", "/api/worktrees/max",
                      "/api/hooks/add", "/api/hooks/update", "/api/hooks/remove",
                      "/api/workspace/open", "/api/workspace/forget", "/api/codegraph/reindex",
                      "/api/file/write",
                      "/api/inline-edit",
                      "/api/commands/render",
                      "/api/git/connect", "/api/git/device/poll", "/api/git/disconnect",
                      "/api/checkpoints/restore", "/api/checkpoints/snapshot",
                      "/api/terminal/create", "/api/terminal/write",
                      "/api/terminal/resize", "/api/terminal/kill"):
            # Wrap the dispatch so NO handler exception can escape to the
            # socketserver and crash the connection/process. A bad driver spec,
            # a failed rebuild, etc. now return a clean 500 the UI can show
            # instead of taking the whole backend down (the "socket hang up" /
            # "Error opening directory" crash on workspace-open/session-switch).
            try:
                return self._handle_post_json(u.path)
            except Exception as e:
                import traceback as _tb
                _tb.print_exc()
                try:
                    return self._send(500, json.dumps({"error": str(e)}))
                except Exception:
                    return
        return self._send(404, json.dumps({"error": "not found"}))

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        data = self.rfile.read(n)
        try:
            decoded = data.decode()
        except Exception as e:
            raise json.JSONDecodeError("Unicode decode error", doc="", pos=0) from e
        return json.loads(decoded or "{}")

    def _handle_post_json(self, path):
        global _pilot
        try:
            body = self._read_json()
        except json.JSONDecodeError:
            return self._send(400, json.dumps({"error": "invalid JSON"}))
        repo = _cfg.repo

        if path == "/api/reviews/apply":
            review_id = body.get("id", "").strip()
            decisions = body.get("decisions", {})
            if not review_id:
                return self._send(400, json.dumps({"error": "Missing review id"}))
            res = _pilot.apply_review(review_id, decisions)
            return self._send(200, json.dumps(res))

        if path == "/api/reviews/dismiss":
            review_id = body.get("id", "").strip()
            if not review_id:
                return self._send(400, json.dumps({"error": "Missing review id"}))
            success = _pilot.dismiss_review(review_id)
            return self._send(200, json.dumps({"ok": success}))
        if path == "/api/swarm/cancel":
            # Cooperative cancel for a swarm job. Best-effort and never raises:
            # local (provider-worker) jobs are cancelled via the per-job Event on
            # the conversation; durable store jobs are marked cancelled in the
            # store where possible. Mirrors the auth/token guard already applied to
            # every POST (see do_POST). Shape: {ok, job_id} or {ok:false,error}.
            job_id = (body.get("job_id") or "").strip()
            if not job_id:
                return self._send(400, json.dumps({"ok": False, "error": "missing job_id"}))
            # 1) Local provider-worker job on the live conversation.
            try:
                if hasattr(_pilot, "cancel_local_job") and _pilot.cancel_local_job(job_id):
                    return self._send(200, json.dumps({"ok": True, "job_id": job_id}))
            except Exception as e:
                _diag("server.swarm_cancel_local", e)
            # 2) Durable Puppetmaster store job -- best-effort mark cancelled.
            try:
                state_obj = _session.state()
                store = getattr(state_obj, "store", None)
                known = False
                try:
                    known = any(j.get("id") == job_id for j in state_obj.list_jobs())
                except Exception:
                    known = False
                if known and store is not None:
                    marked = False
                    for meth in ("cancel_job", "set_job_status", "update_job_status"):
                        fn = getattr(store, meth, None)
                        if fn is None:
                            continue
                        try:
                            if meth == "cancel_job":
                                fn(job_id)
                            else:
                                fn(job_id, "cancelled")
                            marked = True
                            break
                        except Exception:
                            continue
                    return self._send(200, json.dumps({
                        "ok": True, "job_id": job_id, "durable": True,
                        "marked": marked,
                    }))
            except Exception as e:
                _diag("server.swarm_cancel_durable", e)
            return self._send(404, json.dumps({"ok": False, "error": "unknown job_id", "job_id": job_id}))
        if path == "/api/session/persist":
            # Flush the live transcript to disk on demand. Called right before a
            # backend restart (self-edit apply) so the fresh process restores the
            # exact conversation state, including any unanswered user turn.
            try:
                if _sessions.active:
                    save_transcript(_cfg.state_dir or _tf.gettempdir(), _sessions.active, _pilot.export_transcript_data())
                return self._send(200, json.dumps({"ok": True}))
            except Exception as e:
                return self._send(500, json.dumps({"ok": False, "error": str(e)}))
        if path == "/api/restart":
            # Graceful self-restart for non-Electron callers (served browser) and
            # as a fallback path. Persist, ACK, then SIGTERM self so a supervisor
            # (Electron) respawns on the freshly-edited source. In the desktop app
            # the Electron IPC path (harness:restart) is preferred -- it also
            # reloads the renderer -- but this keeps the capability reachable over
            # HTTP for the pilot or a browser session.
            try:
                if _sessions.active:
                    save_transcript(_cfg.state_dir or _tf.gettempdir(), _sessions.active, _pilot.export_transcript_data())
            except Exception:
                pass
            self._send(200, json.dumps({"ok": True, "restarting": True}))

            def _delayed_self_terminate():
                import time as _t
                import signal as _signal
                _t.sleep(0.4)  # let the 200 flush before we exit
                try:
                    os.kill(os.getpid(), _signal.SIGTERM)
                except Exception:
                    os._exit(0)
            threading.Thread(target=_delayed_self_terminate, daemon=True).start()
            return
        if path == "/api/session/compact":
            before = _pilot._estimate_context_tokens()
            orig_tokens = getattr(_cfg, "max_context_tokens", 96000)
            _cfg.max_context_tokens = 1
            try:
                events = list(_pilot._maybe_compact_history())
            finally:
                _cfg.max_context_tokens = orig_tokens
            after = _pilot._estimate_context_tokens()
            if _sessions.active:
                save_transcript(_cfg.state_dir or _tf.gettempdir(), _sessions.active, _pilot.export_transcript_data())
            return self._send(200, json.dumps({
                "ok": True,
                "before_tokens": before,
                "after_tokens": after
            }))
        if path == "/api/checkpoints/restore":
            if not repo or not os.path.exists(repo):
                return self._send(400, json.dumps({"error": "No open workspace"}))
            checkpoint_id = body.get("id", "").strip()
            if not checkpoint_id:
                return self._send(400, json.dumps({"error": "Missing checkpoint id"}))
            from .checkpoints import CheckpointStore
            store = CheckpointStore(repo)
            result = store.restore(checkpoint_id)
            if result.get("ok"):
                return self._send(200, json.dumps(result))
            else:
                return self._send(400, json.dumps({"error": result.get("error", "Restore failed")}))

        if path == "/api/checkpoints/snapshot":
            if not repo or not os.path.exists(repo):
                return self._send(400, json.dumps({"error": "No open workspace"}))
            label = body.get("label", "").strip() or "Manual checkpoint"
            from .checkpoints import CheckpointStore
            store = CheckpointStore(repo)
            checkpoint_id = store.snapshot(label=label, trigger="manual")
            if checkpoint_id:
                return self._send(200, json.dumps({"ok": True, "id": checkpoint_id}))
            else:
                return self._send(400, json.dumps({"error": "Failed to create checkpoint snapshot"}))

        if path == "/api/codegraph/reindex":
            if not repo or not os.path.isdir(repo):
                return self._send(400, json.dumps({"error": "No open workspace"}))
            # Don't stack a second indexer on top of a running one -- concurrent
            # codegraph indexers collide on the same SQLite and wedge the panel.
            if _codegraph_index_alive():
                return self._send(200, json.dumps({"ok": True, "status": "indexing", "note": "already indexing"}))
            _reindex_codegraph_bg(repo)
            return self._send(200, json.dumps({"ok": True, "status": "indexing"}))
        if path == "/api/commands/render":
            name = body.get("name", "").strip()
            args = body.get("args", "")
            if not name:
                return self._send(400, json.dumps({"error": "Missing name parameter"}))
            rendered = _commands.render(name, args, repo=repo)
            if rendered is None:
                return self._send(404, json.dumps({"error": "unknown command"}))
            return self._send(200, json.dumps({"name": name, "prompt": rendered}))

        if path == "/api/inline-edit":
            if not repo or not os.path.exists(repo):
                return self._send(400, json.dumps({"error": "No open workspace"}))
            rel_path = body.get("path", "").strip()
            if not rel_path:
                return self._send(400, json.dumps({"error": "Missing path parameter"}))
            target_path = os.path.abspath(os.path.join(repo, rel_path))
            from .conversation import is_safe_path
            if not is_safe_path(target_path, repo):
                return self._send(400, json.dumps({"error": f"Path traversal attempt rejected: {rel_path}"}))
            
            selection = body.get("selection", "")
            instruction = body.get("instruction", "")
            prefix = body.get("prefix", "")
            suffix = body.get("suffix", "")
            language = body.get("language", "")
            
            if len(selection) > 20000:
                return self._send(400, json.dumps({"error": "Selection size exceeds 20000 characters limit"}))
            if len(prefix) > 4000:
                return self._send(400, json.dumps({"error": "Prefix size exceeds 4000 characters limit"}))
            if len(suffix) > 4000:
                return self._send(400, json.dumps({"error": "Suffix size exceeds 4000 characters limit"}))
            
            system_msg = (
                "You are a precise code-editing assistant. You rewrite ONLY the user's SELECTED code per their instruction. "
                "Output ONLY the replacement code for the selection -- no markdown fences, no explanation, no surrounding code. "
                "Preserve the surrounding indentation style. If the instruction cannot apply, output the selection unchanged."
            )
            
            task_prompt = (
                f"We are editing a file of language: {language or 'unknown'}.\n"
                f"File Path: {rel_path}\n\n"
                f"CONTEXT BEFORE THE SELECTION (Do not modify this, only use for context):\n"
                f"---BEGIN PREFIX---\n{prefix}\n---END PREFIX---\n\n"
                f"SELECTED CODE TO REWRITE:\n"
                f"---BEGIN SELECTION---\n{selection}\n---END SELECTION---\n\n"
                f"CONTEXT AFTER THE SELECTION (Do not modify this, only use for context):\n"
                f"---BEGIN SUFFIX---\n{suffix}\n---END SUFFIX---\n\n"
                f"INSTRUCTION: {instruction}\n\n"
                f"Please output ONLY the new rewritten code that will replace the SELECTED CODE TO REWRITE. "
                f"Do not output prefix context, suffix context, explanation, or markdown fences. Output the replacement code directly."
            )
            
            try:
                if not hasattr(_pilot, "pilot") or not _pilot.pilot:
                    return self._send(200, json.dumps({"ok": False, "error": "No pilot driver configured"}))
                
                resp = _pilot.pilot.complete(task_prompt, system=system_msg)
                if getattr(resp, "error", None):
                    return self._send(200, json.dumps({"ok": False, "error": resp.error}))
                
                cleaned_text = _strip_markdown_fences(resp.text)
                return self._send(200, json.dumps({"ok": True, "edit": cleaned_text}))
            except Exception as e:
                return self._send(200, json.dumps({"ok": False, "error": f"Failed during inline edit pilot execution: {str(e)}"}))

        if path == "/api/file/write":
            if not repo or not os.path.exists(repo):
                return self._send(400, json.dumps({"error": "No open workspace"}))
            rel_path = body.get("path", "").strip()
            content = body.get("content", "")
            if not rel_path:
                return self._send(400, json.dumps({"error": "Missing path parameter"}))
            target_path = os.path.abspath(os.path.join(repo, rel_path))
            from .conversation import is_safe_path
            if not is_safe_path(target_path, repo):
                return self._send(403, json.dumps({"error": f"Path traversal attempt rejected: {rel_path}"}))
            parts = rel_path.split(os.sep)
            if ".git" in parts or any(p.startswith(".git") for p in parts):
                return self._send(403, json.dumps({"error": "Access denied: .git files are restricted"}))
            try:
                try:
                    from .checkpoints import CheckpointStore
                    store = CheckpointStore(repo)
                    store.snapshot(
                        label=f"before manual edit {rel_path}",
                        trigger="manual_edit"
                    )
                except Exception as cp_err:
                    import sys
                    print(f"Checkpoint error before write: {cp_err}", file=sys.stderr)
                
                target_dir = os.path.dirname(target_path)
                os.makedirs(target_dir, exist_ok=True)
                import tempfile
                fd, temp_path = tempfile.mkstemp(dir=target_dir, prefix=".tmp-")
                try:
                    with os.fdopen(fd, 'w', encoding='utf-8') as f:
                        f.write(content)
                    os.replace(temp_path, target_path)
                except Exception as e:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                    raise e
                bytes_written = len(content.encode('utf-8'))
                return self._send(200, json.dumps({
                    "ok": True,
                    "bytes": bytes_written
                }))
            except Exception as e:
                return self._send(500, json.dumps({"error": f"Failed to write file: {e}"}))
        if path == "/api/workspace/open":
            import subprocess
            target_repo = body.get("path", "").strip()
            if not target_repo or not os.path.isdir(target_repo):
                return self._send(400, json.dumps({"error": "Path is not an existing directory"}))

            # Save outgoing conversation transcript
            if _sessions.active:
                save_transcript(_cfg.state_dir or _tf.gettempdir(), _sessions.active, _pilot.export_transcript_data())

            _cfg.repo = target_repo
            os.environ["HARNESS_REPO"] = target_repo

            # Restore the model last used in this workspace (if any + still
            # available), so each dir remembers its model across switches.
            try:
                saved_driver = _get_workspace_driver(target_repo)
                if saved_driver and saved_driver != _cfg.driver:
                    from . import model_visibility as _mv
                    avail = {row["spec"] for row in _mv.catalog(available_only=True)}
                    if saved_driver in avail or not avail:
                        _cfg.driver = saved_driver
                        _apply_model_context_window()
            except Exception as e:
                _diag("server.restore_workspace_driver", e)

            try:
                recents = _record_recent_workspace(target_repo)
            except Exception as e:
                _diag("server.record_recent_workspace", e)

            is_git = False
            branch = ""
            try:
                proc = subprocess.run(
                    ["git", "-C", target_repo, "rev-parse", "--is-inside-work-tree"],
                    capture_output=True, text=True, timeout=5
                )
                if proc.returncode == 0:
                    is_git = True
                    proc_branch = subprocess.run(
                        ["git", "-C", target_repo, "rev-parse", "--abbrev-ref", "HEAD"],
                        capture_output=True, text=True, timeout=5
                    )
                    if proc_branch.returncode == 0:
                        branch = proc_branch.stdout.strip()
            except Exception:
                pass

            # Select the target project's session instead of carrying the current one
            target_sessions = [s for s in _sessions.list() if s.get("repo") == target_repo]
            if target_sessions:
                newest_session = max(target_sessions, key=lambda s: s.get("created", 0))
                _sessions.switch(newest_session["id"])
            else:
                basename = os.path.basename(os.path.abspath(target_repo)) or "Workspace"
                _sessions.create(title=basename, repo=target_repo, branch=branch)

            _rebuild_pilot_and_session()

            # Explicitly load target session's transcript to replace the preserved history
            if _sessions.active:
                target_history = load_transcript(_cfg.state_dir or _tf.gettempdir(), _sessions.active)
                _pilot.load_history(target_history)

            has_codegraph = os.path.isdir(os.path.join(target_repo, ".codegraph"))
            if not has_codegraph:
                _index_codegraph_bg(target_repo)
            else:
                if _puppetmaster_available():
                    _codegraph_status = "ready"
                    _maybe_refresh_codegraph(target_repo)
                else:
                    _codegraph_status = "unsupported"

            return self._send(200, json.dumps({
                "ok": True,
                "repo": target_repo,
                "branch": branch,
                "is_git": is_git,
                "codegraph": _get_codegraph_status(target_repo),
                "active_session": _sessions.active
            }))

        if path == "/api/workspace/forget":
            target_repo = body.get("path", "").strip()
            if not target_repo:
                return self._send(400, json.dumps({"error": "Path is required"}))
            try:
                recents = _forget_recent_workspace(target_repo)
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))
            return self._send(200, json.dumps({
                "ok": True,
                "recents": recents
            }))

        if path == "/api/workspaces/switch":
            return self._send(200, json.dumps(_ws.switch_workspace(repo, body.get("name",""),
                              allow_dirty=_parse_bool(body.get("allow_dirty")))))
        if path == "/api/workspaces/create":
            return self._send(200, json.dumps(_ws.create_workspace(repo, body.get("name",""),
                              body.get("branch") or None)))
        if path == "/api/mcp/add":
            name = body.get("name", "")
            server = {k: body[k] for k in ("command", "args", "env", "cwd", "url", "headers") if k in body}
            _mcp.save_server(name, server)
            try:
                tools = _mcp.start_server(name)
                return self._send(200, json.dumps({"ok": True, "tools": len(tools)}))
            except Exception as e:
                return self._send(200, json.dumps({"ok": False, "error": str(e)}))
        if path == "/api/mcp/remove":
            _mcp.remove_server(body.get("name", ""))
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/mcp/start":
            try:
                tools = _mcp.start_server(body.get("name", ""))
                return self._send(200, json.dumps({"ok": True, "tools": len(tools)}))
            except Exception as e:
                return self._send(200, json.dumps({"ok": False, "error": str(e)}))
        if path == "/api/mcp/stop":
            _mcp.stop_server(body.get("name", ""))
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/mcp/call":
            args = body.get("arguments")
            if args is not None and not isinstance(args, dict):
                return self._send(400, json.dumps({"error": "arguments must be a dictionary"}))
            try:
                out = _mcp.call(body.get("tool", ""), args or {})
                return self._send(200, json.dumps({"ok": True, "result": out}))
            except Exception as e:
                return self._send(200, json.dumps({"ok": False, "error": str(e)}))
        if path == "/api/skills/distill":
            return self._send(200, json.dumps(_pilot.distill()))
        if path == "/api/wiki/ingest-prepared":
            # One-click approve: file the locally-orchestrated pages into the wiki.
            pages = body.get("pages") or []
            count = _pilot.ingest_prepared_pages(pages)
            return self._send(200, json.dumps({"ok": count > 0, "ingested": count}))
        if path == "/api/models/toggle":
            from . import model_visibility as _mv
            spec = body.get("spec", "")
            on = _parse_bool(body.get("enabled", True))
            enabled = _mv.toggle(spec, on)
            return self._send(200, json.dumps({"ok": True, "enabled": enabled}))
        if path == "/api/models/set":
            from . import model_visibility as _mv
            enabled = _mv.set_enabled(body.get("enabled") or [])
            return self._send(200, json.dumps({"ok": True, "enabled": enabled}))
        if path == "/api/skills/approve":
            sk = _skills.set_state(body.get("slug", ""), "active")
            return self._send(200, json.dumps({"ok": bool(sk)}))
        if path == "/api/skills/reject":
            _skills.set_state(body.get("slug", ""), "archived")
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/skills/archive":
            _skills.set_state(body.get("slug", ""), "archived")
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/rules/approve":
            ok = _rules.set_state(body.get("slug", ""), "active")
            return self._send(200, json.dumps({"ok": ok}))
        if path == "/api/rules/reject":
            _rules.set_state(body.get("slug", ""), "archived")
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/memory/add":
            text = body.get("text", "")
            category = body.get("category", "general")
            entry = _memory.add(text, category=category, source="user")
            return self._send(200, json.dumps({
                "id": entry.id,
                "text": entry.text,
                "category": entry.category,
                "created_at": entry.created_at,
                "source": entry.source
            }))
        if path == "/api/memory/remove":
            entry_id = body.get("id", "")
            ok = _memory.remove(entry_id)
            return self._send(200, json.dumps({"ok": ok}))
        if path == "/api/sessions/create":
            if _sessions.active:
                save_transcript(_cfg.state_dir or _tf.gettempdir(), _sessions.active, _pilot.export_transcript_data())
            title = body.get("title") or "New session"
            repo = _cfg.repo or ""
            branch = ""
            if repo and os.path.isdir(repo):
                import subprocess
                try:
                    proc = subprocess.run(
                        ["git", "-C", repo, "rev-parse", "--is-inside-work-tree"],
                        capture_output=True, text=True, timeout=5
                    )
                    if proc.returncode == 0:
                        proc_branch = subprocess.run(
                            ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
                            capture_output=True, text=True, timeout=5
                        )
                        if proc_branch.returncode == 0:
                            branch = proc_branch.stdout.strip()
                except Exception:
                    pass
            res = _sessions.create(title, repo=repo, branch=branch)
            _pilot.load_history([])
            
            from .hooks import run_hooks
            run_hooks("sessionStart", {"session_id": res.get("id", ""), "title": title})
            
            return self._send(200, json.dumps(res))
        if path == "/api/sessions/switch":
            if _sessions.active:
                save_transcript(_cfg.state_dir or _tf.gettempdir(), _sessions.active, _pilot.export_transcript_data())
            res = _sessions.switch(body.get("id",""))
            if res.get("ok") and _sessions.active:
                target_sess = None
                for s in _sessions.list():
                    if s.get("id") == _sessions.active:
                        target_sess = s
                        break
                target_repo = target_sess.get("repo", "").strip() if target_sess else ""
                
                if target_repo and os.path.isdir(target_repo) and target_repo != _cfg.repo:
                    _cfg.repo = target_repo
                    os.environ["HARNESS_REPO"] = target_repo
                    _rebuild_pilot_and_session()
                    
                    history = load_transcript(_cfg.state_dir or _tf.gettempdir(), _sessions.active)
                    _pilot.load_history(history)
                    
                    has_codegraph = os.path.isdir(os.path.join(target_repo, ".codegraph"))
                    if not has_codegraph:
                        _index_codegraph_bg(target_repo)
                    else:
                        if _puppetmaster_available():
                            _codegraph_status = "ready"
                            _maybe_refresh_codegraph(target_repo)
                        else:
                            _codegraph_status = "unsupported"
                else:
                    history = load_transcript(_cfg.state_dir or _tf.gettempdir(), _sessions.active)
                    _pilot.load_history(history)
                    
                res["repo"] = _cfg.repo
                res["codegraph"] = _get_codegraph_status(_cfg.repo) if _cfg.repo else "unsupported"
                
            return self._send(200, json.dumps(res))
        if path == "/api/sessions/delete":
            sid = body.get("session") or body.get("id") or ""
            if not sid:
                return self._send(400, json.dumps({"error": "missing session id"}))
            is_active = (_sessions.active == sid)
            
            from .hooks import run_hooks
            run_hooks("sessionEnd", {"session_id": sid})
            
            new_active = _sessions.delete(sid)
            safe_sid = "".join(c for c in sid if c.isalnum() or c in ("-", "_"))
            if safe_sid:
                state_dir = _cfg.state_dir or _tf.gettempdir()
                trans_dir = os.path.abspath(os.path.join(state_dir, "transcripts"))
                p = os.path.abspath(os.path.join(trans_dir, f"{safe_sid}.json"))
                if p.startswith(trans_dir) and os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
            if is_active:
                if new_active:
                    history = load_transcript(_cfg.state_dir or _tf.gettempdir(), new_active)
                    _pilot.load_history(history)
                else:
                    _pilot.load_history([])
            return self._send(200, json.dumps({"ok": True, "active": new_active}))
        if path == "/api/sessions/archive":
            sid = body.get("session") or body.get("id") or ""
            if not sid:
                return self._send(400, json.dumps({"error": "missing session id"}))
            archived = _parse_bool(body.get("archived"))
            _sessions.archive(sid, archived)
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/sessions/rename":
            sid = body.get("session") or body.get("id") or ""
            title = body.get("title") or ""
            if not sid:
                return self._send(400, json.dumps({"error": "missing session id"}))
            if not title:
                return self._send(400, json.dumps({"error": "missing title"}))
            ok = _sessions.rename(sid, title)
            return self._send(200, json.dumps({"ok": ok}))
        if path == "/api/session/interrupt":
            _pilot.interrupt()
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/session/steer":
            text = body.get("text", "").strip()
            images = body.get("images") or []
            if isinstance(images, str):
                images = [p for p in images.split("|") if p]
            if not text and not images:
                return self._send(400, json.dumps({"error": "missing text"}))
            if not _pilot:
                return self._send(404, json.dumps({"error": "no active session"}))
            # Route through steer_with_images so an attached screenshot is
            # transcribed into the steer text (a steer is a text injection and
            # cannot carry raw image blocks mid-turn).
            if images and hasattr(_pilot, "steer_with_images"):
                _pilot.steer_with_images(text, images)
            else:
                _pilot.enqueue_steer(text)
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/session/queue":
            # PROMPT QUEUE mutations, mirroring the auth/token guard already
            # applied to every POST. Distinct from /api/session/steer: the queue
            # is a "playlist" of FULL user prompts that each run as their own
            # complete turn once the current one finishes. Never raises.
            if not _pilot:
                return self._send(404, json.dumps({"error": "no active session"}))
            # DELETE-style body: {clear: true} clears the queue; {id: "..."}
            # removes a single item. We accept these via POST so the browser
            # transport shim (which lacks a DELETE helper) can drive both.
            if body.get("clear") is True:
                try:
                    n = _pilot.clear_prompts()
                except Exception:
                    n = 0
                return self._send(200, json.dumps({"ok": True, "cleared": n}))
            rid = (body.get("id") or "").strip() if isinstance(body.get("id"), str) else ""
            if rid:
                try:
                    ok = _pilot.remove_prompt(rid)
                except Exception:
                    ok = False
                return self._send(200, json.dumps({"ok": bool(ok), "id": rid}))
            # Otherwise: enqueue a new prompt.
            text = (body.get("text") or "").strip()
            if not text:
                return self._send(400, json.dumps({"error": "missing text"}))
            # Optional image attachments: accept a list or a '|'-joined string
            # (mirror the steer endpoint) and validate every path lives inside
            # the upload dir (mirror /api/run and /api/chat validation). A queued
            # prompt runs as its own fresh turn so it can carry real images.
            images = body.get("images") or []
            if isinstance(images, str):
                images = [p for p in images.split("|") if p]
            valid_imgs = []
            upload_dir_real = os.path.realpath(_UPLOAD_DIR)
            for p in images:
                if not p:
                    continue
                real_p = os.path.realpath(p)
                try:
                    if os.path.commonpath([upload_dir_real, real_p]) == upload_dir_real:
                        valid_imgs.append(p)
                    else:
                        return self._send(400, json.dumps({"error": f"Invalid image path: {p}"}))
                except ValueError:
                    return self._send(400, json.dumps({"error": f"Invalid image path: {p}"}))
            try:
                item = _pilot.enqueue_prompt(text, images=valid_imgs)
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))
            if not item or not item.get("id"):
                return self._send(400, json.dumps({"error": "enqueue failed"}))
            return self._send(200, json.dumps({"ok": True, "item": item}))
        if path == "/api/session/queue/reorder":
            if not _pilot:
                return self._send(404, json.dumps({"error": "no active session"}))
            ids = body.get("ids") or []
            if not isinstance(ids, list):
                return self._send(400, json.dumps({"error": "ids must be a list"}))
            try:
                items = _pilot.reorder_prompts([str(x) for x in ids])
            except Exception:
                try:
                    items = _pilot.list_prompts()
                except Exception:
                    items = []
            return self._send(200, json.dumps({"ok": True, "items": items}))
        if path == "/api/terminal/create":
            try:
                # Reap any dead PTY sessions first so exited/stuck terminals do
                # not pile up across restarts (the Restart button creates a fresh
                # session each time; the old dead ones should be cleaned up).
                _pty.reap()
                cwd = _cfg.repo or os.path.expanduser("~")
                cols = int(body.get("cols", 80)); rows = int(body.get("rows", 24))
                sess = _pty.create(cwd=cwd, cols=cols, rows=rows)
                return self._send(200, json.dumps({"id": sess.id, "cwd": sess._cwd}))
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))
        if path == "/api/terminal/write":
            sess = _pty.get(body.get("id", ""))
            if not sess:
                return self._send(404, json.dumps({"error": "no such terminal"}))
            sess.write(body.get("data", ""))
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/terminal/resize":
            sess = _pty.get(body.get("id", ""))
            if not sess:
                return self._send(404, json.dumps({"error": "no such terminal"}))
            sess.resize(int(body.get("rows", 24)), int(body.get("cols", 80)))
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/terminal/kill":
            _pty.kill(body.get("id", ""))
            return self._send(200, json.dumps({"ok": True}))
        if path == "/api/wiki/config":
            api_base = body.get("api_base")
            owner_token = body.get("owner_token")
            res = set_wiki_config(
                api_base=api_base if api_base is not None else None,
                owner_token=owner_token if owner_token is not None else None,
            )
            return self._send(200, json.dumps(res))
        if path == "/api/git/connect":
            method = body.get("method")
            if method not in ("gh", "device"):
                return self._send(400, json.dumps({"error": f"Invalid method: {method}"}))
            from .git_provision import GitProvisioner, save_connection, get_status
            prov = GitProvisioner()
            if method == "gh":
                info = prov.detect_gh()
                if not info["available"]:
                    return self._send(400, json.dumps({"error": "GitHub CLI not authenticated or not installed"}))
                token = prov.github_token()
                if not token:
                    return self._send(400, json.dumps({"error": "Could not retrieve GitHub CLI token"}))
                res = prov.provision_wiki_repo(token)
                if not res.get("ok"):
                    return self._send(500, json.dumps({"error": res.get("error", "Failed to provision repository")}))
                save_connection("gh", res["repo_full_name"], res["html_url"])
                return self._send(200, json.dumps(get_status()))
            elif method == "device":
                res = prov.device_flow_start()
                if "error" in res:
                    return self._send(500, json.dumps({"error": res["error"]}))
                return self._send(200, json.dumps(res))
        if path == "/api/git/device/poll":
            device_code = body.get("device_code")
            if not device_code:
                return self._send(400, json.dumps({"error": "Missing device_code"}))
            from .git_provision import GitProvisioner, save_connection, save_device_token, get_status
            prov = GitProvisioner()
            res = prov.device_flow_poll(None, device_code)
            if res.get("status") == "authorized":
                token = res.get("token")
                if not token:
                    return self._send(500, json.dumps({"error": "No token in authorized response"}))
                repo_res = prov.provision_wiki_repo(token)
                if not repo_res.get("ok"):
                    return self._send(500, json.dumps({"error": repo_res.get("error", "Failed to provision repository")}))
                save_device_token(token)
                save_connection("device", repo_res["repo_full_name"], repo_res["html_url"])
                return self._send(200, json.dumps(get_status()))
            elif res.get("status") == "pending":
                return self._send(200, json.dumps({"status": "pending"}))
            else:
                return self._send(400, json.dumps({"error": res.get("error", "Verification failed")}))
        if path == "/api/git/disconnect":
            from .git_provision import delete_connection, get_status
            delete_connection()
            return self._send(200, json.dumps(get_status()))
        if path == "/api/platform":
            name = body.get("name")
            enabled = body.get("enabled")
            if name not in ("agentic", "cursor", "hermes", "claude-code", "codex", "openai"):
                return self._send(400, json.dumps({"error": f"Unknown adapter: {name}"}))
            if not isinstance(enabled, bool):
                return self._send(400, json.dumps({"error": "enabled must be a boolean"}))
            
            path_file = _get_platform_json_path()
            pdata = {}
            if os.path.exists(path_file):
                try:
                    with open(path_file, "r", encoding="utf-8") as f:
                        pdata = json.load(f)
                except Exception:
                    pass
            if not isinstance(pdata, dict):
                pdata = {}
            if "disabled" not in pdata or not isinstance(pdata["disabled"], list):
                pdata["disabled"] = []
            
            disabled_list = pdata["disabled"]
            if enabled:
                pdata["disabled"] = [x for x in disabled_list if x != name]
            else:
                if name not in disabled_list:
                    pdata["disabled"] = disabled_list + [name]
            
            try:
                _write_platform_json_atomic(path_file, pdata)
            except Exception as e:
                return self._send(500, json.dumps({"error": f"Failed to save platform.json: {str(e)}"}))
            
            return self._send(200, json.dumps(_get_platform_adapters()))
        if path == "/api/settings":
            requires_rebuild = False
            if "api_key" in body or body.get("clear_api_key") is True:
                requires_rebuild = True
            driver = body.get("driver")
            if driver is not None and driver != _cfg.driver:
                requires_rebuild = True
            if requires_rebuild:
                if not _pilot._busy.acquire(blocking=False):
                    return self._send(409, json.dumps({"error": "pilot busy, try again"}))
                _pilot._busy.release()

            reach_to_use = body.get("reach", _cfg.reach)
            if "api_key" in body:
                val = str(body["api_key"]).strip()
                if val:
                    set_api_key(reach_to_use, val)
                    _rebuild_pilot_and_session()
                    # Resync agentic registry when a key is added
                    from .auto_registry import sync_agentic_registry_safe
                    sync_agentic_registry_safe()
            elif body.get("clear_api_key") is True:
                clear_api_key(reach_to_use)
                _rebuild_pilot_and_session()
                # Resync agentic registry when a key is cleared
                from .auto_registry import sync_agentic_registry_safe
                sync_agentic_registry_safe()

            driver = body.get("driver")
            if driver is not None:
                # Validate against the FULL available catalog (every model from a
                # keyed provider), not just the enabled picker subset -- a user may
                # set a driver that is valid but not currently toggled into the
                # dropdown. _available_pilots() is the curated picker list; the
                # catalog is the superset of what can actually be built.
                from . import model_visibility as _mv
                catalog_specs = {c["spec"] for c in _mv.catalog(available_only=True)}
                av = set(_available_pilots()) | catalog_specs
                if driver not in av:
                    return self._send(400, json.dumps({"error": f"Unknown or unavailable driver: {driver}"}))
                if driver != _cfg.driver:
                    try:
                        _cfg.driver = driver
                        _rebuild_pilot_and_session()
                    except Exception as e:
                        return self._send(500, json.dumps({"error": f"Failed to swap driver: {str(e)}"}))
            budget = body.get("budget")
            if budget is not None:
                try:
                    b_val = int(budget)
                    _cfg.budget = max(1, min(50, b_val))
                except (ValueError, TypeError):
                    return self._send(400, json.dumps({"error": "Invalid budget value"}))
            if "auto_distill" in body:
                ad_val = _parse_bool(body["auto_distill"])
                _pilot._auto_distill = ad_val
                os.environ["HARNESS_AUTO_DISTILL"] = "true" if ad_val else "false"
            if "reviewEditsBeforeApply" in body:
                rev_val = _parse_bool(body["reviewEditsBeforeApply"])
                _pilot._review_edits_before_apply = rev_val
                os.environ["HARNESS_REVIEW_EDITS_BEFORE_APPLY"] = "true" if rev_val else "false"
            if "autoCommandGuard" in body:
                g_val = _parse_bool(body["autoCommandGuard"])
                _pilot._auto_command_guard = g_val
                os.environ["HARNESS_AUTO_COMMAND_GUARD"] = "true" if g_val else "off"
            if "commandTimeout" in body:
                # seconds; "0"/"off"/"none" = unbounded. Validate before storing.
                raw = str(body["commandTimeout"]).strip().lower()
                if raw in ("0", "off", "none", "unbounded"):
                    os.environ["HARNESS_COMMAND_TIMEOUT"] = "0"
                else:
                    try:
                        os.environ["HARNESS_COMMAND_TIMEOUT"] = str(max(1, int(raw)))
                    except (ValueError, TypeError):
                        return self._send(400, json.dumps({"error": "Invalid commandTimeout"}))

            return self._send(200, json.dumps(_get_settings_dict()))

        if path == "/api/providers/probe":
            pname = body.get("provider", "")
            from .providers import get_provider
            p = get_provider(pname)
            if not p:
                return self._send(400, json.dumps({"error": f"Unknown provider: {pname}"}))
            
            from .registry_wizard import get_provider_key, probe_provider
            key = get_provider_key(p)
            try:
                res = probe_provider(p, key)
                return self._send(200, json.dumps(res))
            except Exception as e:
                return self._send(200, json.dumps({
                    "provider": p.name,
                    "models": [{"id": m} for m in p.pilot_models],
                    "source": "static",
                    "error": str(e)
                }))

        if path == "/api/providers/key":
            # Per-provider key management: set or disconnect a SPECIFIC provider's
            # key independently (e.g. turn OpenRouter off while keeping Anthropic).
            # Distinct from /api/settings, which only touches the active reach.
            pname = str(body.get("provider", "")).strip()
            from .providers import get_provider
            p = get_provider(pname)
            if not p:
                return self._send(400, json.dumps({"error": f"Unknown provider: {pname}"}))
            action = str(body.get("action", "")).strip().lower()
            if action in ("enable", "disable", "toggle"):
                # Non-destructive on/off for env-imported (or stored) keys. Unlike
                # 'clear', this preserves the key so the user can flip a provider
                # off and back on -- e.g. swapping a work key for a personal one.
                from .keys import set_provider_enabled, get_disconnected
                if action == "toggle":
                    enabled = p.name in get_disconnected()
                else:
                    enabled = action == "enable"
                set_provider_enabled(p.name, enabled)
                # Resync agentic registry when a provider is enabled/disabled
                from .auto_registry import sync_agentic_registry_safe
                sync_agentic_registry_safe()
                # Keep the active driver honest: enabling may make a better model
                # reachable; disabling may kill the current one.
                try:
                    if not _driver_provider_available(_cfg.driver):
                        _resolve_available_driver()
                        _rebuild_pilot_and_session()
                except Exception as e:
                    _diag("server.provider_toggle_driver_rebuild", e)
                status = get_api_key_status(p.name)
                return self._send(200, json.dumps({
                    "ok": True,
                    "provider": p.name,
                    "enabled": enabled,
                    "has_key": status["has_key"],
                    "masked": status["masked"],
                }))
            if action == "clear" or body.get("clear") is True:
                clear_api_key(p.name)
                # Resync agentic registry when a provider key is cleared
                from .auto_registry import sync_agentic_registry_safe
                sync_agentic_registry_safe()
                # If the active driver's provider is no longer available (we just
                # disconnected the provider backing it -- whether a 'provider:model'
                # spec OR a bare name routed through the reach), re-resolve to the
                # first available enabled model and rebuild, so the app never sits
                # on a dead driver.
                try:
                    if not _driver_provider_available(_cfg.driver):
                        _resolve_available_driver()
                        _rebuild_pilot_and_session()
                except Exception as e:
                    _diag("server.provider_clear_driver_rebuild", e)
            else:
                val = str(body.get("api_key", "")).strip()
                if not val:
                    return self._send(400, json.dumps({"error": "api_key required to set"}))
                set_api_key(p.name, val)
                # Resync agentic registry when a provider key is set
                from .auto_registry import sync_agentic_registry_safe
                sync_agentic_registry_safe()
            status = get_api_key_status(p.name)
            return self._send(200, json.dumps({
                "ok": True,
                "provider": p.name,
                "has_key": status["has_key"],
                "masked": status["masked"],
            }))

        if path == "/api/registry":
            models = body.get("models")
            if not isinstance(models, list):
                return self._send(400, json.dumps({"error": "models must be a list"}))
            
            validated_models = []
            for m in models:
                if not isinstance(m, dict):
                    return self._send(400, json.dumps({"error": "each model must be a dictionary"}))
                
                model_id = m.get("id")
                if not isinstance(model_id, str) or not model_id.strip():
                    return self._send(400, json.dumps({"error": "id must be a non-empty string"}))
                
                adapter = m.get("adapter")
                if not isinstance(adapter, str):
                    return self._send(400, json.dumps({"error": "adapter must be a string"}))
                
                try:
                    score = int(m.get("capability_score", 0))
                    score = max(0, min(100, score))
                except (ValueError, TypeError):
                    return self._send(400, json.dumps({"error": "capability_score must be an integer"}))
                
                m["id"] = model_id.strip()
                m["adapter"] = adapter
                m["capability_score"] = score
                validated_models.append(m)
                
            from .registry_wizard import get_models_file_path, write_json_atomic
            dest_path = get_models_file_path()
            try:
                write_json_atomic(dest_path, {"models": validated_models})
                return self._send(200, json.dumps({"ok": True, "models": validated_models}))
            except Exception as e:
                return self._send(500, json.dumps({"error": f"Failed to write registry: {str(e)}"}))

        if path == "/api/roles":
            overrides = body.get("overrides", {})
            policy = body.get("routing_policy")
            
            if not isinstance(overrides, dict):
                return self._send(400, json.dumps({"error": "overrides must be a dictionary"}))
            
            validated_overrides = {}
            from .registry_wizard import REAL_BASE_SCORES
            for role, score in overrides.items():
                if role not in REAL_BASE_SCORES:
                    return self._send(400, json.dumps({"error": f"Unknown role: {role}"}))
                try:
                    clamped_score = max(0, min(100, int(score)))
                    validated_overrides[role] = clamped_score
                except (ValueError, TypeError):
                    return self._send(400, json.dumps({"error": f"Invalid score for role {role}: {score}"}))
            
            if policy is not None:
                valid_policies = {"balanced", "cheap", "quality", "escalating"}
                if policy not in valid_policies:
                    return self._send(400, json.dumps({"error": f"Invalid policy: {policy}; expected one of {list(valid_policies)}"}))
            
            from .registry_wizard import get_routing_file_path, write_json_atomic
            dest_path = get_routing_file_path()
            current_data = {}
            if os.path.exists(dest_path):
                try:
                    with open(dest_path) as f:
                        current_data = json.load(f)
                except Exception:
                    pass
            
            current_overrides = current_data.get("overrides", {})
            current_overrides.update(validated_overrides)
            current_data["overrides"] = current_overrides
            
            if policy is not None:
                current_data["routing_policy"] = policy
            elif "routing_policy" not in current_data:
                current_data["routing_policy"] = "balanced"
                
            try:
                write_json_atomic(dest_path, current_data, chmod_mode=0o600)
                return self._send(200, json.dumps({"ok": True, "overrides": current_data["overrides"], "routing_policy": current_data["routing_policy"]}))
            except Exception as e:
                return self._send(500, json.dumps({"error": f"Failed to save roles config: {str(e)}"}))

        if path == "/api/pilot/validate":
            driver = body.get("driver")
            if not isinstance(driver, str):
                return self._send(400, json.dumps({"error": "driver must be a string"}))
                
            from .registry_wizard import validate_pilot_driver
            try:
                res = validate_pilot_driver(driver)
                return self._send(200, json.dumps(res))
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))

        if path == "/api/worktrees/add":
            from . import worktrees as _wt
            branch = body.get("branch", "").strip()
            base = body.get("base") or "HEAD"
            if not branch or branch.startswith("-") or (base and base.startswith("-")):
                return self._send(400, json.dumps({"error": "invalid branch or base name"}))
            try:
                new_wt = _wt.add_worktree(_cfg.repo, branch, base)
                _wt.cleanup_old_worktrees(_cfg.repo, _wt.get_max_worktrees())
                return self._send(200, json.dumps(new_wt))
            except ValueError as e:
                return self._send(400, json.dumps({"error": str(e)}))
            except Exception as e:
                return self._send(400, json.dumps({"error": f"Failed to add worktree: {str(e)}"}))

        if path == "/api/worktrees/remove":
            from . import worktrees as _wt
            wt_path = body.get("path", "").strip()
            force = _parse_bool(body.get("force"))
            if not wt_path:
                return self._send(400, json.dumps({"error": "missing path"}))
            try:
                _wt.remove_worktree(_cfg.repo, wt_path, force=force)
                return self._send(200, json.dumps({"ok": True}))
            except ValueError as e:
                return self._send(400, json.dumps({"error": str(e)}))
            except Exception as e:
                return self._send(400, json.dumps({"error": f"Failed to remove worktree: {str(e)}"}))

        if path == "/api/worktrees/prune":
            from . import worktrees as _wt
            try:
                _wt.prune_worktrees(_cfg.repo)
                return self._send(200, json.dumps({"ok": True}))
            except Exception as e:
                return self._send(400, json.dumps({"error": f"Failed to prune worktrees: {str(e)}"}))

        if path == "/api/worktrees/max":
            from . import worktrees as _wt
            try:
                max_val = int(body.get("max") or body.get("max_worktrees") or 25)
                _wt.set_max_worktrees(max_val)
                _wt.cleanup_old_worktrees(_cfg.repo, max_val)
                return self._send(200, json.dumps({"ok": True}))
            except (ValueError, TypeError):
                return self._send(400, json.dumps({"error": "Invalid max value"}))

        if path == "/api/hooks/add":
            from . import hooks as _hk
            event = body.get("event", "").strip()
            command = body.get("command", "").strip()
            if event not in _hk.ALLOWED_EVENTS:
                return self._send(400, json.dumps({"error": f"Invalid event. Allowed: {_hk.ALLOWED_EVENTS}"}))
            if not command:
                return self._send(400, json.dumps({"error": "Command cannot be empty"}))
            
            hooks = _hk.get_hooks()
            new_hook = {
                "id": uuid.uuid4().hex[:12],
                "event": event,
                "command": command,
                "enabled": True
            }
            hooks.append(new_hook)
            _hk.save_hooks(hooks)
            return self._send(200, json.dumps(new_hook))

        if path == "/api/hooks/update":
            from . import hooks as _hk
            hid = body.get("id", "").strip()
            if not hid:
                return self._send(400, json.dumps({"error": "missing hook id"}))
            
            hooks = _hk.get_hooks()
            hook = next((h for h in hooks if h["id"] == hid), None)
            if not hook:
                return self._send(404, json.dumps({"error": "hook not found"}))
            
            if "enabled" in body:
                hook["enabled"] = _parse_bool(body["enabled"])
            if "command" in body:
                cmd = body["command"].strip()
                if not cmd:
                    return self._send(400, json.dumps({"error": "Command cannot be empty"}))
                hook["command"] = cmd
            
            _hk.save_hooks(hooks)
            return self._send(200, json.dumps(hook))

        if path == "/api/hooks/remove":
            from . import hooks as _hk
            hid = body.get("id", "").strip()
            if not hid:
                return self._send(400, json.dumps({"error": "missing hook id"}))
            
            hooks = _hk.get_hooks()
            hooks = [h for h in hooks if h["id"] != hid]
            _hk.save_hooks(hooks)
            return self._send(200, json.dumps({"ok": True}))

        return self._send(404, json.dumps({"error": "not found"}))

    def _handle_upload(self):
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            return self._send(400, json.dumps({"error": "expected multipart/form-data"}))
        # Reject oversized bodies BEFORE parsing. Without a ceiling, a large
        # multipart POST is read straight off the socket into memory on a
        # thread-per-request server -- a cheap memory-exhaustion DoS. Cap by the
        # declared Content-Length (default 10MB, env-tunable).
        max_bytes = int(os.environ.get("HARNESS_UPLOAD_MAX_BYTES", str(10 * 1024 * 1024)))
        try:
            content_length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            content_length = 0
        if content_length <= 0:
            return self._send(400, json.dumps({"error": "missing or empty body"}))
        if content_length > max_bytes:
            return self._send(413, json.dumps({
                "error": f"upload too large: {content_length} bytes exceeds cap of {max_bytes}"
            }))
        body = self.rfile.read(content_length)
        saved = []
        for filename, data in _parse_multipart_files(body, ctype):
            ext = os.path.splitext(filename)[1].lower() or ".png"
            if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                continue
            path = os.path.join(_UPLOAD_DIR, f"{uuid.uuid4().hex}{ext}")
            with open(path, "wb") as out:
                out.write(data)
            saved.append({"path": path, "name": filename})
        return self._send(200, json.dumps({"saved": saved}))

    # GET endpoints that are intentionally public (the same-origin renderer
    # bootstrap assets, which must load BEFORE the page has the token to make
    # authenticated calls). Everything else under /api requires the token.
    _PUBLIC_GET_PATHS = frozenset({"/", "/index.html", "/app.js", "/app.css"})

    def do_GET(self):
        global _codegraph_status
        u = urlparse(self.path)
        # CENTRALIZED AUTH GATE (defense against per-handler drift): do_POST has
        # a single token check at its top, but do_GET historically required each
        # handler to re-add a copy-pasted token check -- and ~11 endpoints
        # (/api/memory, /api/config, /api/skills, /api/rules, /api/commands,
        # /api/settings, /api/platform, /api/jobs, /api/workspace, /api/mcp*)
        # were left unauthenticated, leaking durable memory/config/skills to any
        # local caller with no token. Gate every non-public path here so a newly
        # added GET endpoint is authenticated by default; the redundant
        # per-handler checks below are now harmless no-ops.
        if u.path not in self._PUBLIC_GET_PATHS:
            if self._guard():
                return
            if not self._token_ok():
                return self._send(403, json.dumps({"error": "missing or bad token"}))
        if u.path in ("/", "/index.html"):
            html = (_WEB / "index.html").read_text()
            # inject the auth token so the same-origin page can call the API
            meta = '<meta name="harness-token" content="%s">' % _TOKEN
            html = html.replace("</head>", meta + "</head>", 1) if "</head>" in html else meta + html
            return self._send(200, html, "text/html")
        if u.path == "/app.js":
            return self._send(200, (_WEB / "app.js").read_text(),
                              "application/javascript")
        if u.path == "/app.css":
            return self._send(200, (_WEB / "app.css").read_text(), "text/css")
        if u.path == "/api/git/status":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            from .git_provision import get_status
            return self._send(200, json.dumps(get_status()))
        if u.path == "/api/session/state":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            # resume_pending: the transcript ends on an unanswered user turn while
            # the pilot is idle -- i.e. a reply is owed but no turn is running. This
            # is the signal a client uses to auto-continue after a backend restart
            # (self-edit apply) so an in-flight turn is never silently dropped.
            _state = _pilot.state()
            return self._send(200, json.dumps({
                "state": _state,
                "pending_swarms": _pilot.has_pending_swarms(),
                "resume_pending": bool(_state == "idle" and _pilot.has_pending_user_turn()),
            }))
        if u.path == "/api/session/swarm-results":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            results = []
            for ev in _pilot.drain_swarm_results():
                results.append({"kind": ev.kind, "data": ev.data})
            return self._send(200, json.dumps({"results": results}))
        if u.path == "/api/session/queue":
            # PROMPT QUEUE snapshot -- the sequential "playlist" of full user
            # prompts that will each run as their own complete turn after the
            # current one finishes. Distinct from /api/session/steer (which is
            # a mid-turn interrupt on the CURRENT running turn).
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            try:
                items = _pilot.list_prompts() if _pilot else []
            except Exception:
                items = []
            return self._send(200, json.dumps({"items": items}))
        if u.path == "/api/checkpoints":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            repo = _cfg.repo
            if not repo or not os.path.exists(repo):
                return self._send(200, json.dumps([]))
            from .checkpoints import CheckpointStore
            store = CheckpointStore(repo)
            return self._send(200, json.dumps(store.list()))
        if u.path == "/api/checkpoints/diff":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            repo = _cfg.repo
            if not repo or not os.path.exists(repo):
                return self._send(400, json.dumps({"error": "No open workspace"}))
            checkpoint_id = parse_qs(u.query).get("id", [""])[0].strip()
            if not checkpoint_id:
                return self._send(400, json.dumps({"error": "Missing checkpoint id"}))
            from .checkpoints import CheckpointStore
            store = CheckpointStore(repo)
            result = store.diff(checkpoint_id)
            if result.get("ok"):
                return self._send(200, json.dumps(result))
            else:
                return self._send(400, json.dumps({"error": result.get("error", "Diff generation failed")}))
        if u.path == "/api/mcp":
            return self._send(200, json.dumps({"servers": _mcp.status(),
                "tools": [{"server": t.server, "name": t.name, "qualified": t.qualified,
                           "description": t.description} for t in _mcp.tools()]}))
        if u.path == "/api/mcp/catalog":
            return self._send(200, json.dumps({"catalog": CATALOG}))
        if u.path == "/api/commands":
            qargs = parse_qs(u.query)
            repo = qargs.get("repo", [""])[0].strip() or _cfg.repo
            cmds = _commands.list(repo=repo)
            return self._send(200, json.dumps({
                "commands": [
                    {"name": c.name, "description": c.description, "scope": c.scope}
                    for c in cmds
                ]
            }))
        if u.path == "/api/skills":
            return self._send(200, json.dumps([
                {"slug": sk.slug, "name": sk.name, "description": sk.description,
                 "state": sk.state, "source": sk.source, "used_count": sk.used_count,
                 "body": sk.body, "supersedes": getattr(sk, "supersedes", "")}
                for sk in _skills.list()]))
        if u.path == "/api/rules":
            return self._send(200, json.dumps([
                {"slug": r.slug, "text": r.text, "scope": r.scope,
                 "state": r.state, "source": r.source}
                for r in _rules.list()]))
        if u.path == "/api/memory":
            entries = _memory.list()
            return self._send(200, json.dumps({
                "memory": [
                    {"id": e.id, "text": e.text, "category": e.category,
                     "created_at": e.created_at, "source": e.source}
                    for e in entries
                ],
                "total_chars": _memory.total_chars(),
                "limit": MEMORY_CHAR_LIMIT
            }))
        if u.path == "/api/file/read":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            repo = _cfg.repo
            if not repo or not os.path.exists(repo):
                return self._send(400, json.dumps({"error": "No open workspace"}))
            rel_path = parse_qs(u.query).get("path", [""])[0].strip()
            if not rel_path:
                return self._send(400, json.dumps({"error": "Missing path parameter"}))
            full_path = os.path.abspath(os.path.join(repo, rel_path))
            from .conversation import is_safe_path
            if not is_safe_path(full_path, repo):
                return self._send(403, json.dumps({"error": "Access denied: path escapes workspace"}))
            parts = rel_path.split(os.sep)
            if ".git" in parts or any(p.startswith(".git") for p in parts):
                return self._send(403, json.dumps({"error": "Access denied: .git files are restricted"}))
            if not os.path.isfile(full_path):
                return self._send(404, json.dumps({"error": "File not found"}))
            try:
                with open(full_path, "rb") as f:
                    chunk = f.read(1024)
                    if b"\x00" in chunk:
                        return self._send(200, json.dumps({"ok": False, "error": "Cannot read binary files", "binary": True}))
            except Exception as e:
                return self._send(500, json.dumps({"error": f"Failed to check file type: {e}"}))
            try:
                file_size = os.path.getsize(full_path)
                truncated = False
                max_bytes = 1024 * 1024
                if file_size > max_bytes:
                    truncated = True
                    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read(max_bytes)
                else:
                    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                return self._send(200, json.dumps({
                    "ok": True,
                    "path": rel_path,
                    "content": content,
                    "truncated": truncated
                }))
            except Exception as e:
                return self._send(500, json.dumps({"error": f"Failed to read file: {e}"}))

        if u.path == "/api/workspace/files":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            repo = _cfg.repo
            if not repo or not os.path.isdir(repo):
                return self._send(200, json.dumps({"files": []}))
            files_list = []
            skip_dirs = {".git", "node_modules", ".venv", ".codegraph", "dist", "build", ".pytest_cache", "__pycache__", ".mypy_cache", ".ruff_cache", ".idea", ".vscode", "venv", ".next", "coverage", ".hermes", "release", "backend-dist"}
            repo_abs = os.path.abspath(repo)
            for root, dirs, files in os.walk(repo_abs):
                dirs[:] = [d for d in dirs if d not in skip_dirs]
                for f in files:
                    full_path = os.path.join(root, f)
                    rel_path = os.path.relpath(full_path, repo_abs)
                    if rel_path == "." or rel_path.startswith(".."):
                        continue
                    files_list.append(rel_path)
                    if len(files_list) >= 2000:
                        break
                if len(files_list) >= 2000:
                    break
            return self._send(200, json.dumps({"files": sorted(files_list)}))

        if u.path == "/api/workspace/symbols":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            
            repo = _cfg.repo
            cg_status = _get_codegraph_status(repo) if repo else "unsupported"
            
            if not repo or not os.path.isdir(repo):
                return self._send(200, json.dumps({"symbols": [], "status": cg_status}))
            
            try:
                import puppetmaster.codegraph as cg
                if not cg.codegraph_available() or not cg.codegraph_ready(repo):
                    return self._send(200, json.dumps({"symbols": [], "status": cg_status}))
            except Exception:
                return self._send(200, json.dumps({"symbols": [], "status": "unsupported"}))
            
            q = parse_qs(u.query).get("q", [""])[0].strip()
            if len(q) < 1:
                return self._send(200, json.dumps({"symbols": [], "status": "ready"}))
            
            try:
                import puppetmaster.codegraph as cg
                res = cg.codegraph_query(search=q, cwd=repo, limit=20)
                symbols_list = []
                if res.get("ok") and res.get("stdout"):
                    try:
                        data = json.loads(res["stdout"])
                        if isinstance(data, list):
                            for item in data:
                                node = item.get("node")
                                if not node:
                                    continue
                                name = node.get("name")
                                kind = node.get("kind")
                                file_path = node.get("filePath")
                                start_line = node.get("startLine")
                                if name and file_path and start_line is not None:
                                    symbols_list.append({
                                        "name": str(name),
                                        "kind": str(kind or "unknown"),
                                        "path": str(file_path),
                                        "line": int(start_line)
                                    })
                                if len(symbols_list) >= 20:
                                    break
                    except Exception:
                        pass
                return self._send(200, json.dumps({"symbols": symbols_list, "status": "ready"}))
            except Exception as e:
                return self._send(200, json.dumps({"symbols": [], "error": str(e), "status": cg_status}))
        if u.path == "/api/workspace":
            repo = _cfg.repo
            is_git = False
            branch = ""
            if repo and os.path.isdir(repo):
                import subprocess
                try:
                    proc = subprocess.run(
                        ["git", "-C", repo, "rev-parse", "--is-inside-work-tree"],
                        capture_output=True, text=True, timeout=5
                    )
                    if proc.returncode == 0:
                        is_git = True
                        proc_branch = subprocess.run(
                            ["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
                            capture_output=True, text=True, timeout=5
                        )
                        if proc_branch.returncode == 0:
                            branch = proc_branch.stdout.strip()
                except Exception:
                    pass
            cg_status = _get_codegraph_status(repo) if repo else "unsupported"
            recents = []
            try:
                _ws_path = _workspace_json_path()
                if os.path.exists(_ws_path):
                    with open(_ws_path) as f:
                        recents = json.load(f).get("recents", []) or []
            except Exception:
                recents = []
            # filter temp/dead dirs so ephemeral test state_dirs never show as recents
            _tmproot = os.path.realpath(_tf.gettempdir())
            recents = [
                r for r in recents
                if r and os.path.isdir(r)
                and not os.path.realpath(r).startswith(_tmproot)
                and "/var/folders/" not in os.path.realpath(r)
            ]
            return self._send(200, json.dumps({
                "repo": repo,
                "branch": branch,
                "is_git": is_git,
                "codegraph_status": cg_status,
                "recents": recents
            }))
        if u.path == "/api/models/catalog":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            from . import model_visibility as _mv
            return self._send(200, json.dumps({
                "catalog": _mv.catalog(available_only=True),
                "all": _mv.catalog(available_only=False),
                "enabled": _mv.get_enabled(),
            }))
        if u.path == "/api/codegraph":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))

            repo = _cfg.repo
            if not repo or not os.path.isdir(repo):
                return self._send(200, json.dumps({
                    "indexed": False,
                    "status": "none",
                    "nodes": None,
                    "edges": None,
                    "files": None,
                    "languages": None,
                    "last_indexed": None,
                    "repo": ""
                }))

            if not _puppetmaster_available():
                return self._send(200, json.dumps({
                    "indexed": False,
                    "status": "unsupported",
                    "reason": _codegraph_status_reason or "puppetmaster not found -- codegraph/swarm unavailable",
                    "nodes": None,
                    "edges": None,
                    "files": None,
                    "languages": None,
                    "last_indexed": None,
                    "repo": repo
                }))

            # Only report "indexing" while the indexer subprocess is actually
            # alive. If the flag is stale (job finished), fall through to the real
            # status query so the panel shows live metrics instead of nulls --
            # this is what previously stuck the panel on INDEXING until a restart.
            if _codegraph_status == "indexing" and not _codegraph_index_alive():
                _codegraph_status = "ready" if _codegraph_indexed(repo) else "unsupported"
                _codegraph_status_cache.pop(repo, None)

            if _codegraph_status == "indexing" and _codegraph_index_alive():
                last_indexed = None
                try:
                    import puppetmaster.codegraph as cg
                    mtime = cg.codegraph_index_mtime(repo)
                    if mtime:
                        import datetime
                        last_indexed = datetime.datetime.fromtimestamp(mtime).isoformat()
                except Exception:
                    try:
                        db_path = os.path.join(repo, ".codegraph", "db")
                        if not os.path.exists(db_path):
                            db_path = os.path.join(repo, ".codegraph")
                        if os.path.exists(db_path):
                            mtime = os.path.getmtime(db_path)
                            import datetime
                            last_indexed = datetime.datetime.fromtimestamp(mtime).isoformat()
                    except Exception:
                        pass

                return self._send(200, json.dumps({
                    "indexed": False,
                    "status": "indexing",
                    "reason": _codegraph_status_reason or None,
                    "nodes": None,
                    "edges": None,
                    "files": None,
                    "languages": None,
                    "last_indexed": last_indexed,
                    "repo": repo
                }))

            # No built DB yet and no indexer running: start one and report
            # "indexing" rather than shelling out to `codegraph status --json`,
            # which hangs on a config-only checkout until the 20s timeout and then
            # mis-reports "unsupported". This makes a fresh install self-heal.
            if not _codegraph_indexed(repo) and not _codegraph_index_alive():
                def _kick_index():
                    _index_codegraph_bg(repo)
                threading.Thread(target=_kick_index, daemon=True).start()
                return self._send(200, json.dumps({
                    "indexed": False,
                    "status": "indexing",
                    "reason": None,
                    "nodes": None,
                    "edges": None,
                    "files": None,
                    "languages": None,
                    "last_indexed": None,
                    "repo": repo,
                }))

            # Serve a recent cached payload instead of re-spawning the status
            # subprocess on every poll (the main source of panel load lag).
            import time as _time
            cached = _codegraph_status_cache.get(repo)
            if cached and cached[0] > _time.monotonic():
                return self._send(200, json.dumps(cached[1]))

            try:
                import subprocess
                # 20s (not 5s): codegraph status on a large indexed repo
                # (e.g. 60k+ nodes) takes ~5s in the packaged/frozen binary --
                # right at a 5s limit, which intermittently tripped a timeout
                # and showed "UNSUPPORTED" in the panel even though the repo is
                # fully indexed. The 30s status cache means this slower call is
                # only paid on a cache miss, so the panel stays responsive.
                proc = subprocess.run(
                    _puppetmaster_cmd("codegraph", "status", "--json"),
                    cwd=repo,
                    capture_output=True,
                    text=True,
                    timeout=20
                )
                if proc.returncode == 0:
                    data = json.loads(proc.stdout)
                    initialized = data.get("initialized", False)
                    status_val = "ready" if initialized else "unsupported"
                    
                    last_indexed = None
                    try:
                        import puppetmaster.codegraph as cg
                        mtime = cg.codegraph_index_mtime(repo)
                        if mtime:
                            import datetime
                            last_indexed = datetime.datetime.fromtimestamp(mtime).isoformat()
                    except Exception:
                        try:
                            db_path = os.path.join(repo, ".codegraph", "db")
                            if not os.path.exists(db_path):
                                db_path = os.path.join(repo, ".codegraph")
                            if os.path.exists(db_path):
                                mtime = os.path.getmtime(db_path)
                                import datetime
                                last_indexed = datetime.datetime.fromtimestamp(mtime).isoformat()
                        except Exception:
                            pass

                    _cg_payload = {
                        "indexed": initialized,
                        "status": status_val,
                        "nodes": data.get("nodeCount"),
                        "edges": data.get("edgeCount"),
                        "files": data.get("fileCount"),
                        "languages": data.get("languages"),
                        "last_indexed": last_indexed,
                        "repo": repo
                    }
                    _codegraph_status_cache[repo] = (
                        _time.monotonic() + _CODEGRAPH_STATUS_TTL, _cg_payload)
                    return self._send(200, json.dumps(_cg_payload))
                else:
                    return self._send(200, json.dumps({
                        "indexed": False,
                        "status": "unsupported",
                        "nodes": None,
                        "edges": None,
                        "files": None,
                        "languages": None,
                        "last_indexed": None,
                        "repo": repo
                    }))
            except Exception:
                return self._send(200, json.dumps({
                    "indexed": False,
                    "status": "unsupported",
                    "nodes": None,
                    "edges": None,
                    "files": None,
                    "languages": None,
                    "last_indexed": None,
                    "repo": repo
                }))
        if u.path == "/api/config":
            try:
                from .edit_engines import agentic_available, select_edit_engine
                _edit_engine = select_edit_engine(_cfg)
                _agentic_ready = agentic_available()
            except Exception:
                _edit_engine, _agentic_ready = "native", False
            return self._send(200, json.dumps({
                "driver": _cfg.driver, "reach": _cfg.reach,
                "budget": _cfg.budget, "state_dir": _session.state_dir,
                "models": _available_pilots(), "repo": _cfg.repo,
                "swarm_adapter": _cfg.swarm_adapter,
                "edit_engine": _edit_engine, "agentic_ready": _agentic_ready,
                "preflight": _session.preflight()}))
        if u.path == "/api/wiki/config":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            return self._send(200, json.dumps(get_wiki_config()))
        if u.path == "/api/wiki/graph":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            
            # WikiClient auto-detects the gated owner surface (WIKI_API_BASE +
            # WIKI_OWNER_TOKEN, same as the portable-llm-wiki MCP) or the public
            # HARNESS_WIKI_URL. config.wiki_url overrides base_url when set.
            from .wiki import WikiClient
            try:
                client = WikiClient(base_url=_cfg.wiki_url or "", timeout=8)
            except Exception as e:
                client = None
                _client_err = str(e)
            if client is None or not client.base_url:
                return self._send(200, json.dumps({
                    "configured": False,
                    "status": "not_configured",
                    "nodes": [],
                    "edges": [],
                    "base_url": ""
                }))
            import time as _time
            _wiki_cached = _wiki_graph_cache.get(client.base_url)
            if _wiki_cached and _wiki_cached[0] > _time.monotonic():
                return self._send(200, json.dumps(_wiki_cached[1]))
            try:
                res = client.graph()
            except Exception as e:
                res = {"error": f"Unexpected error: {str(e)}", "nodes": [], "edges": []}
            if res.get("error"):
                # Distinguish "wiki host unreachable / not actually set up" from a real
                # API error. An unreachable host (connection refused, DNS failure, timeout)
                # should look like NOT CONNECTED -- neutral -- not a scary red ERROR, so a
                # user who never set up a wiki is not confused by a broken-looking panel.
                _err_l = str(res.get("error", "")).lower()
                _unreachable = any(t in _err_l for t in (
                    "connection refused", "refused", "timed out", "timeout",
                    "name or service not known", "nodename nor servname",
                    "failed to establish", "max retries", "cannot connect",
                    "connection error", "urlopen error", "getaddrinfo",
                    "no route to host", "network is unreachable", "[errno",
                ))
                # If the wiki was NEVER configured (no base_url/token), an
                # unreachable result is just "not set up" -> neutral. But if a
                # base_url IS configured, a transient failure must NOT wipe the
                # connection -- keep configured + base_url and report a retryable
                # error so Refresh recovers instead of showing "not connected".
                _is_configured = bool(client.base_url)
                if _unreachable and not _is_configured:
                    return self._send(200, json.dumps({
                        "configured": False,
                        "status": "not_configured",
                        "nodes": [],
                        "edges": [],
                        "base_url": ""
                    }))
                return self._send(200, json.dumps({
                    "configured": True,
                    "status": "error",
                    "nodes": [],
                    "edges": [],
                    "error": ("Wiki temporarily unreachable -- click Refresh to retry."
                              if _unreachable else res["error"]),
                    "retryable": True,
                    "base_url": client.base_url
                }))
            _wiki_payload = {
                "configured": True,
                "status": "ok",
                "nodes": res.get("nodes") or [],
                "edges": res.get("edges") or [],
                "base_url": client.base_url
            }
            _wiki_graph_cache[client.base_url] = (
                _time.monotonic() + _WIKI_GRAPH_TTL, _wiki_payload)
            return self._send(200, json.dumps(_wiki_payload))
        if u.path == "/api/settings":
            return self._send(200, json.dumps(_get_settings_dict()))
        if u.path == "/api/reviews":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            with _pilot._pending_reviews_lock:
                reviews_list = list(_pilot._pending_reviews.values())
            return self._send(200, json.dumps(reviews_list))
        if u.path == "/api/platform":
            return self._send(200, json.dumps(_get_platform_adapters()))
        if u.path == "/api/jobs":
            return self._send(200, json.dumps(_jobs_snapshot()))
        if u.path == "/api/usage":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            # Resolve real per-Mtok pricing for the active driver: eval-catalog
            # native rates first, then the live OpenRouter price map (so picker
            # specs like 'anthropic:claude-opus-4-8' show the true $5/$25 instead
            # of a 0.5/2.0 placeholder).
            try:
                from pmharness.registry import resolve_price
                price_in, price_out = resolve_price(_cfg.driver)
            except Exception:
                price_in, price_out = 0.5, 2.0
            tokens_used = getattr(_pilot, "_tokens_used", 0)
            # Accurate split: input tokens at price_in, output at price_out, with
            # cached prompt tokens re-billed at the cache-read discount. Falls
            # back to a single-rate estimate if the in/out split isn't tracked.
            _t_in = getattr(_pilot, "_tokens_in", 0)
            _t_out = getattr(_pilot, "_tokens_out", 0)
            _t_cached = int(getattr(_pilot, "_tokens_cached", 0) or 0)
            est_session_cost = _session_cost(_t_in, _t_out, _t_cached, price_in, price_out)
            _cache_savings_usd = _cache_savings(_t_cached, price_in)
            jobs_list = []
            try:
                from puppetmaster.models import ArtifactType
                from puppetmaster.usage import aggregate_token_usage
                jobs = _session.state().list_jobs()
                store = _session.state().store
                jids = [j.get("id") for j in jobs if j.get("id")]
                # Batch: one bulk artifact read regrouped by job_id, instead of
                # one store.list_artifacts(jid) query PER job (the N+1).
                arts_by_job: dict = {}
                try:
                    for a in store.list_artifacts_for_jobs(jids):
                        arts_by_job.setdefault(getattr(a, "job_id", None), []).append(a)
                except Exception:
                    arts_by_job = None  # fall back to per-job reads
                for jid in jids:
                    try:
                        artifacts = (arts_by_job.get(jid, []) if arts_by_job is not None
                                     else store.list_artifacts(jid))
                        routing = []
                        seen_router_tasks = set()
                        total = 0.0
                        for artifact in artifacts:
                            if artifact.type == ArtifactType.ROUTING and artifact.created_by == "router":
                                task_id = artifact.task_id
                                if task_id:
                                    if task_id in seen_router_tasks:
                                        continue
                                    seen_router_tasks.add(task_id)
                                routing.append(artifact)
                        for artifact in routing:
                            payload = artifact.payload or {}
                            cost = float(payload.get("estimated_cost_usd") or 0.0)
                            total += cost
                        tokens = 0
                        try:
                            token_usage_dict = aggregate_token_usage(artifacts)
                            tokens = token_usage_dict.get("total_tokens", 0)
                        except Exception:
                            pass
                        jobs_list.append({
                            "job_id": jid,
                            "tokens": tokens,
                            "est_cost_usd": round(total, 6)
                        })
                    except Exception as e:
                        _diag("server.usage_job_cost", e, msg=f"job={jid}")
            except Exception as e:
                _diag("server.usage_jobs_aggregate", e)
            response_data = {
                "session": {
                    "tokens_used": tokens_used,
                    "est_cost_usd": round(est_session_cost, 6),
                    "driver": _cfg.driver,
                    "price_in": price_in,
                    "price_out": price_out,
                    # Prompt-cache hits (billed at the cache-read discount) and
                    # the USD that discount saved vs full input price.
                    "tokens_cached": _t_cached,
                    "cache_savings_usd": round(_cache_savings_usd, 6),
                },
                "jobs": jobs_list
            }
            return self._send(200, json.dumps(response_data))
        if u.path == "/api/artifacts":
            q = parse_qs(u.query)
            jid = q.get("job_id", [""])[0]
            return self._send(200, json.dumps(_session.state().job_artifacts(jid)))
        if u.path == "/api/swarm/live":
            if self._guard():
                return
            qtok = parse_qs(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))
            res_jobs = []
            try:
                from pmharness.registry import resolve_price
                price_in, price_out = resolve_price(_cfg.driver)
            except Exception:
                price_in, price_out = 0.5, 2.0
            try:
                state_obj = _session.state()
                jobs = state_obj.list_jobs()
                store = state_obj.store
                from puppetmaster.models import ArtifactType
                from puppetmaster.usage import aggregate_token_usage

                jids = [j.get("id") for j in jobs if j.get("id")]
                # Batch all three per-job reads (the old N+1 read artifacts TWICE
                # plus tasks, per job): one bulk artifacts read + one bulk tasks
                # read, regrouped by job_id.
                arts_by_job: dict = {}
                tasks_by_job: dict = {}
                try:
                    for a in store.list_artifacts_for_jobs(jids):
                        arts_by_job.setdefault(getattr(a, "job_id", None), []).append(a)
                except Exception:
                    arts_by_job = None
                try:
                    for t in store.list_tasks_for_jobs(jids):
                        tasks_by_job.setdefault(getattr(t, "job_id", None), []).append(t)
                except Exception:
                    tasks_by_job = None

                for j in jobs:
                    jid = j.get("id")
                    if not jid:
                        continue

                    raw_arts = (arts_by_job.get(jid, []) if arts_by_job is not None
                                else store.list_artifacts(jid))
                    # job_artifacts() formats the same artifacts for display; build
                    # it from the batched list when available to avoid a re-read.
                    try:
                        artifacts_list = state_obj.format_artifacts(raw_arts) if hasattr(state_obj, "format_artifacts") else state_obj.job_artifacts(jid)
                    except Exception:
                        artifacts_list = []

                    tokens = 0
                    est_cost_usd = 0.0
                    try:
                        seen_router_tasks = set()
                        for artifact in raw_arts:
                            if artifact.type == ArtifactType.ROUTING and artifact.created_by == "router":
                                task_id = artifact.task_id
                                if task_id:
                                    if task_id in seen_router_tasks:
                                        continue
                                    seen_router_tasks.add(task_id)
                                payload = artifact.payload or {}
                                cost = float(payload.get("estimated_cost_usd") or 0.0)
                                est_cost_usd += cost
                        try:
                            token_usage_dict = aggregate_token_usage(raw_arts)
                            tokens = token_usage_dict.get("total_tokens", 0)
                        except Exception:
                            pass
                    except Exception:
                        pass

                    tasks_list = []
                    try:
                        raw_tasks = (tasks_by_job.get(jid, []) if tasks_by_job is not None
                                     else store.list_tasks(jid))
                        for t in raw_tasks:
                            tasks_list.append({
                                "id": getattr(t, "id", ""),
                                "role": getattr(t, "role", ""),
                                "instruction": getattr(t, "instruction", ""),
                                "status": str(getattr(t, "status", "")),
                                "adapter": getattr(t, "adapter", ""),
                                "completed_at": getattr(t, "completed_at", None),
                            })
                    except Exception:
                        pass

                    res_jobs.append({
                        "id": jid,
                        "goal": j.get("goal", ""),
                        "status": j.get("status", ""),
                        "role": j.get("role", ""),
                        "adapter": j.get("adapter", ""),
                        "created_at": j.get("created_at"),
                        "task_count": j.get("task_count", 0),
                        "tokens": tokens,
                        "est_cost_usd": round(est_cost_usd, 6),
                        "artifacts": artifacts_list,
                        "tasks": tasks_list
                    })
            except Exception as e:
                _diag("server.jobs_list_aggregate", e)

            # Merge in-process provider-native worker jobs (job_id "local-*").
            # These run on the user's own key rather than a Puppetmaster adapter,
            # so they never enter the durable store above -- without this the panel
            # reads "No swarm jobs yet" while a worker is visibly running.
            try:
                existing_ids = {j.get("id") for j in res_jobs}
                for lj in _pilot.live_local_jobs():
                    if lj.get("id") not in existing_ids:
                        res_jobs.append(lj)
            except Exception as e:
                _diag("server.jobs_list_merge_local", e)
            
            tokens_used = getattr(_pilot, "_tokens_used", 0)
            # Accurate split: input tokens at price_in, output at price_out, with
            # cached prompt tokens re-billed at the cache-read discount. Falls
            # back to a single-rate estimate if the in/out split isn't tracked.
            _t_in = getattr(_pilot, "_tokens_in", 0)
            _t_out = getattr(_pilot, "_tokens_out", 0)
            _t_cached = int(getattr(_pilot, "_tokens_cached", 0) or 0)
            est_session_cost = _session_cost(_t_in, _t_out, _t_cached, price_in, price_out)
            _cache_savings_usd = _cache_savings(_t_cached, price_in)
            
            response_data = {
                "session": {
                    "tokens_used": tokens_used,
                    "est_cost_usd": round(est_session_cost, 6),
                    "driver": _cfg.driver,
                    # Prompt-cache hits (billed at the cache-read discount) so the
                    # UI can show how much input was served near-free -- proof the
                    # harness is not token-hungry -- plus the USD it saved.
                    "tokens_cached": _t_cached,
                    "cache_savings_usd": round(_cache_savings_usd, 6),
                },
                "jobs": res_jobs
            }
            return self._send(200, json.dumps(response_data))
        # action endpoints (SSE) mutate state / spend budget -> guard them.
        if u.path in ("/api/run", "/api/chat", "/api/auto", "/api/pilot", "/api/sessions/transcript", "/api/sessions/export",
                      "/api/providers", "/api/registry", "/api/roles", "/api/registry/recommend", "/api/context/usage"):
            if self._guard():
                return
            from urllib.parse import parse_qs as _pq
            qtok = _pq(u.query).get("token", [""])[0]
            if qtok != _TOKEN and self.headers.get("X-Harness-Token", "") != _TOKEN:
                return self._send(403, json.dumps({"error": "missing or bad token"}))

        if u.path == "/api/providers":
            from .registry_wizard import PROVIDERS, get_provider_key
            from .keys import provider_has_env, get_disconnected
            disconnected = get_disconnected()
            res = []
            for p in PROVIDERS:
                status = get_api_key_status(p.name)
                res.append({
                    "name": p.name,
                    "display_name": getattr(p, "display_name", "") or p.name,
                    "env_var": p.env_vars[0] if p.env_vars else "",
                    "base_url": p.base_url,
                    "has_key": (get_provider_key(p) is not None) or status["has_key"],
                    "masked": status["masked"],
                    "api_mode": p.api_mode,
                    "has_env": provider_has_env(p.name),
                    "disconnected": p.name in disconnected,
                })
            return self._send(200, json.dumps(res))

        if u.path == "/api/registry":
            from .registry_wizard import get_models_file_path
            path = get_models_file_path()
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        return self._send(200, f.read())
                except Exception as e:
                    return self._send(500, json.dumps({"error": f"Failed to read registry: {str(e)}"}))
            return self._send(200, json.dumps({"models": []}))

        if u.path == "/api/roles":
            from .registry_wizard import REAL_BASE_SCORES, get_routing_file_path
            path = get_routing_file_path()
            overrides = {}
            policy = "balanced"
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        data = json.load(f)
                        overrides = data.get("overrides", {})
                        policy = data.get("routing_policy", "balanced")
                except Exception:
                    pass
            
            roles_mapping = {}
            for k, v in REAL_BASE_SCORES.items():
                roles_mapping[k] = overrides.get(k, v)
                
            return self._send(200, json.dumps({
                "roles": roles_mapping,
                "policies": ["balanced", "cheap", "quality", "escalating"],
                "routing_policy": policy,
                "overrides": overrides
            }))

        if u.path == "/api/registry/recommend":
            from .registry_wizard import get_recommendations
            try:
                rec = get_recommendations()
                return self._send(200, json.dumps(rec))
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))
        if u.path == "/api/run":
            q = parse_qs(u.query)
            imgs = []
            upload_dir_real = os.path.realpath(_UPLOAD_DIR)
            for p in q.get("images", [""])[0].split("|"):
                if not p:
                    continue
                real_p = os.path.realpath(p)
                try:
                    if os.path.commonpath([upload_dir_real, real_p]) == upload_dir_real:
                        imgs.append(p)
                    else:
                        return self._send(400, json.dumps({"error": f"Invalid image path: {p}"}))
                except ValueError:
                    return self._send(400, json.dumps({"error": f"Invalid image path: {p}"}))
            return self._stream_run(q.get("prompt", [""])[0], imgs)
        if u.path == "/api/chat":
            q = parse_qs(u.query)
            imgs = []
            upload_dir_real = os.path.realpath(_UPLOAD_DIR)
            for p in q.get("images", [""])[0].split("|"):
                if not p:
                    continue
                real_p = os.path.realpath(p)
                try:
                    if os.path.commonpath([upload_dir_real, real_p]) == upload_dir_real:
                        imgs.append(p)
                    else:
                        return self._send(400, json.dumps({"error": f"Invalid image path: {p}"}))
                except ValueError:
                    return self._send(400, json.dumps({"error": f"Invalid image path: {p}"}))
            plan_val = q.get("plan", ["false"])[0].lower() in ("true", "1", "yes")
            resume_val = q.get("resume", ["false"])[0].lower() in ("true", "1", "yes")
            return self._stream_chat(q.get("message", [""])[0], imgs, plan=plan_val, resume=resume_val)
        if u.path == "/api/terminal/stream":
            q = parse_qs(u.query)
            return self._stream_terminal(q.get("id", [""])[0])
        if u.path == "/api/pilot":
            q = parse_qs(u.query)
            return self._swap_pilot(q.get("model", [""])[0])
        if u.path == "/api/context/usage":
            try:
                usage = _pilot.get_context_usage()
                return self._send(200, json.dumps(usage))
            except Exception as e:
                return self._send(500, json.dumps({"error": str(e)}))
        if u.path == "/api/workspaces":
            return self._send(200, json.dumps(_ws.list_workspaces(_cfg.repo)))
        if u.path == "/api/worktrees":
            from . import worktrees as _wt
            return self._send(200, json.dumps({
                "worktrees": _wt.list_worktrees(_cfg.repo),
                "max": _wt.get_max_worktrees()
            }))
        if u.path == "/api/hooks":
            from . import hooks as _hk
            return self._send(200, json.dumps({
                "hooks": _hk.get_hooks(),
                "events": _hk.ALLOWED_EVENTS
            }))
        if u.path == "/api/sessions/transcript":
            q = parse_qs(u.query)
            sid = q.get("session", [None])[0] or _sessions.active or ""
            data = load_transcript(_cfg.state_dir or _tf.gettempdir(), sid)
            if isinstance(data, dict):
                history_list = data.get("history", [])
                display_list = data.get("display", [])
                job_ids_list = data.get("job_ids", [])
            else:
                history_list = data
                display_list = []
                job_ids_list = []
            return self._send(200, json.dumps({
                "history": history_list,
                "display": display_list,
                "job_ids": job_ids_list
            }))
        if u.path == "/api/sessions/export":
            q = parse_qs(u.query)
            sid = q.get("session", [None])[0] or _sessions.active or ""
            fmt = q.get("format", ["json"])[0]
            
            meta = next((s for s in _sessions._sessions if s["id"] == sid), None)
            data = load_transcript(_cfg.state_dir or _tf.gettempdir(), sid)
            if isinstance(data, dict):
                history = data.get("history", [])
            else:
                history = data
            
            title = meta.get("title", "Unknown Session") if meta else "Unknown Session"
            filename_base = meta.get("title") if meta else ""
            if not filename_base:
                filename_base = sid or "session"
            
            import re
            safe_title = re.sub(r'[^a-zA-Z0-9\-_]', '_', filename_base)
            safe_title = re.sub(r'_+', '_', safe_title)
            safe_title = safe_title.strip('_-')
            if not safe_title:
                safe_title = sid or "session"
                
            if fmt == "md":
                import datetime
                import time
                created = meta.get("created") if meta else None
                created_str = datetime.datetime.fromtimestamp(created).strftime('%Y-%m-%d %H:%M:%S') if created else "Unknown"
                exported_str = datetime.datetime.fromtimestamp(time.time()).strftime('%Y-%m-%d %H:%M:%S')
                
                md_lines = []
                md_lines.append(f"# {title or 'Unknown Session'}")
                md_lines.append("")
                md_lines.append(f"**Session ID:** {sid}  ")
                md_lines.append(f"**Created:** {created_str}  ")
                md_lines.append(f"**Exported:** {exported_str}")
                md_lines.append("")
                
                for msg in history:
                    role = msg.get("role", "").capitalize()
                    content = msg.get("content", "")
                    md_lines.append(f"## {role}")
                    md_lines.append("")
                    md_lines.append(content)
                    md_lines.append("")
                
                body = "\n".join(md_lines)
                data = body.encode("utf-8")
                filename = f"{safe_title}.md"
                ctype = "text/markdown"
            else:
                import time
                created = meta.get("created") if meta else None
                export_data = {
                    "session_id": sid,
                    "title": title or "Unknown Session",
                    "created": created,
                    "exported_at": time.time(),
                    "messages": history
                }
                body = json.dumps(export_data, indent=2)
                data = body.encode("utf-8")
                filename = f"{safe_title}.json"
                ctype = "application/json"
                
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self._cors()
            self.end_headers()
            self.wfile.write(data)
            return
        if u.path == "/api/sessions":
            return self._send(200, json.dumps(_sessions.list()))
        if u.path == "/api/auto":
            q = parse_qs(u.query)
            return self._stream_auto(q.get("objective", [""])[0])
        return self._send(404, json.dumps({"error": "not found"}))

    def _stream_run(self, prompt: str, images=None):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        
        if _sessions.active and prompt:
            from .sessions import derive_title
            _sessions.set_title_if_default(_sessions.active, derive_title(prompt))

        if _cfg.repo and os.path.isdir(_cfg.repo):
            _maybe_refresh_codegraph(_cfg.repo)

        pre = _session.preflight()
        if pre:
            self.wfile.write(f"data: {json.dumps({'kind':'error','turn':0,'data':{'error':pre}})}\n\n".encode())
            self.wfile.write(b"data: {\"kind\": \"done\"}\n\n")
            self.wfile.flush()
            return

        from .hooks import run_hooks
        ctx = {"session_id": _sessions.active or "", "prompt": prompt}
        run_hooks("preRun", ctx)
        gen = _session.run(prompt, images=images or None)
        try:
            for ev in gen:
                payload = json.dumps({"kind": ev.kind, "turn": ev.turn, "data": ev.data})
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: {\"kind\": \"done\"}\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            # Close the generator so its finally (the _busy release) runs even on
            # client disconnect -- otherwise the session lock leaks and every later
            # message silently fails with "session busy".
            try:
                gen.close()
            except Exception:
                pass
            run_hooks("postRun", ctx)

    def _stream_auto(self, objective: str):
        """Stream the fully-auto loop (governor-bounded) over SSE."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.end_headers()
        
        if _sessions.active and objective:
            from .sessions import derive_title
            _sessions.set_title_if_default(_sessions.active, derive_title(objective))

        if _cfg.repo and os.path.isdir(_cfg.repo):
            _maybe_refresh_codegraph(_cfg.repo)

        from .hooks import run_hooks
        ctx = {"session_id": _sessions.active or "", "objective": objective}
        run_hooks("preRun", ctx)
        budget = AutoBudget.from_env()
        gen = _pilot.run_auto(objective, budget)
        try:
            for ev in gen:
                payload = json.dumps({"kind": ev.kind, "data": ev.data})
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: {\"kind\": \"done\"}\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # client (browser tab) went away -> stop the governor loop promptly
            # instead of burning budget for a gone client.
            _pilot.cancel()
        finally:
            # Close the generator so its finally (the _busy release) runs even on
            # disconnect -- otherwise the session lock leaks and later messages
            # silently fail with "session busy".
            try:
                gen.close()
            except Exception:
                pass
            _finalize_turn(ctx)

    def _swap_pilot(self, model: str):
        """Hot-swap the pilot model (the whole point: your key -> your pilot).

        Preserves the in-flight conversation: history, auto-distill, and MCP are
        carried onto the rebuilt pilot (mirrors _rebuild_pilot_and_session). A
        bare rebuild dropped history, so swapping mid-conversation silently reset
        the context to empty. We also refuse a swap while a turn is streaming, so
        the old pilot's busy stream is never orphaned underneath a fresh object."""
        global _pilot
        if not model:
            return self._send(400, json.dumps({"error": "model required"}))
        # Do not swap underneath a live stream -- let it finish or be cancelled.
        if getattr(_pilot, "_busy", None) is not None and _pilot._busy.locked():
            return self._send(409, json.dumps({
                "error": "a turn is in progress; stop it before switching models"}))
        try:
            with _pilot_swap_lock:
                old_history = getattr(_pilot, "_history", None)
                old_auto_distill = getattr(_pilot, "_auto_distill", False)
                _cfg.driver = model
                _apply_model_context_window()
                _pilot = ConversationalSession(_cfg)
                if old_history is not None:
                    _pilot._history = old_history
                _pilot._auto_distill = old_auto_distill
                _pilot._mcp = _mcp
            # Remember this model for the current workspace so switching dirs and
            # coming back restores it.
            _save_workspace_driver(_cfg.repo, model)
            return self._send(200, json.dumps({"ok": True, "driver": model}))
        except Exception as e:
            return self._send(500, json.dumps({"error": str(e)}))

    def _stream_terminal(self, sid: str):
        """Stream PTY output over SSE. Client sends keystrokes via POST /api/terminal/write."""
        sess = _pty.get(sid)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self._cors()
        self.end_headers()
        if not sess:
            try:
                self.wfile.write(b"data: {\"kind\": \"exit\"}\n\n")
                self.wfile.flush()
            except Exception:
                pass
            return
        offset = 0
        try:
            while sess.alive():
                data, offset = sess.read_since(offset)
                if data:
                    import base64 as _b64
                    payload = json.dumps({"kind": "data", "b64": _b64.b64encode(data).decode("ascii")})
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
                else:
                    time.sleep(0.05)
            # flush any final bytes after exit
            data, offset = sess.read_since(offset)
            if data:
                import base64 as _b64
                payload = json.dumps({"kind": "data", "b64": _b64.b64encode(data).decode("ascii")})
                self.wfile.write(f"data: {payload}\n\n".encode())
            self.wfile.write(b"data: {\"kind\": \"exit\"}\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _stream_chat(self, message: str, images=None, plan: bool = False, resume: bool = False):
        """Stream the conversational PILOT loop: prose messages + collapsible
        action cards (run_swarm) + assistant_done.

        ``resume=True`` runs a keep-alive continuation turn: no new user message
        is appended -- the pilot generates off the history that drain_swarm_results
        already extended with the finished job's result + continuation."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        
        if _sessions.active and message:
            from .sessions import derive_title
            _sessions.set_title_if_default(_sessions.active, derive_title(message))

        # Self-healing CodeGraph: debounced staleness check at the start of every
        # turn, so an index that drifted (files edited/added/DELETED since the last
        # build) reindexes in the background before it misleads the pilot. The
        # debounce in _maybe_refresh_codegraph prevents thrash during rapid turns.
        if _cfg.repo and os.path.isdir(_cfg.repo):
            _maybe_refresh_codegraph(_cfg.repo)

        # Resolve @-file and @symbol mentions in message
        resolved_files = []
        resolved_symbols = []
        total_size = 0
        repo = _cfg.repo
        if repo and os.path.isdir(repo) and message:
            import re
            tokens = re.findall(r'@([a-zA-Z0-9_\-\.\/:]+)', message)
            seen_tokens = set()
            for token in tokens:
                if token in seen_tokens:
                    continue
                seen_tokens.add(token)
                
                is_symbol_prefix = token.startswith("symbol:")
                symbol_name = token[7:] if is_symbol_prefix else token
                
                is_file = False
                file_to_read = None
                if not is_symbol_prefix:
                    full_path = os.path.abspath(os.path.join(repo, token))
                    repo_real = os.path.realpath(repo)
                    full_real = os.path.realpath(full_path)
                    try:
                        common = os.path.commonpath([repo_real, full_real])
                        if common == repo_real and os.path.isfile(full_real):
                            is_file = True
                            file_to_read = full_real
                    except Exception:
                        pass
                    # Also accept files dropped from OUTSIDE the workspace: the
                    # composer uploads those into the trusted upload dir and
                    # references them by absolute path. Allow reading that path
                    # too (drag-and-drop of external files).
                    if not is_file:
                        try:
                            upload_real = os.path.realpath(_UPLOAD_DIR)
                            abs_token = os.path.realpath(os.path.abspath(token))
                            if (os.path.commonpath([upload_real, abs_token]) == upload_real
                                    and os.path.isfile(abs_token)):
                                is_file = True
                                file_to_read = abs_token
                        except Exception:
                            pass
                
                if is_file and file_to_read:
                    try:
                        size = os.path.getsize(file_to_read)
                        read_size = min(size, 50 * 1024)
                        if total_size + read_size <= 150 * 1024:
                            with open(file_to_read, 'r', encoding='utf-8', errors='replace') as f:
                                content = f.read(read_size)
                            resolved_files.append(f"--- File: {token} ---\n{content}\n")
                            total_size += len(content.encode('utf-8'))
                    except Exception:
                        pass
                else:
                    try:
                        import puppetmaster.codegraph as cg
                        if cg.codegraph_available() and cg.codegraph_ready(repo):
                            res = cg.codegraph_query(search=symbol_name, cwd=repo, limit=1)
                            if res.get("ok") and res.get("stdout"):
                                data = json.loads(res["stdout"])
                                if isinstance(data, list) and len(data) > 0:
                                    node = data[0].get("node")
                                    if node:
                                        file_path = node.get("filePath")
                                        start_line = node.get("startLine")
                                        end_line = node.get("endLine")
                                        name = node.get("name")
                                        
                                        if file_path and start_line is not None:
                                            sym_full_path = os.path.abspath(os.path.join(repo, file_path))
                                            repo_real = os.path.realpath(repo)
                                            sym_full_real = os.path.realpath(sym_full_path)
                                            common = os.path.commonpath([repo_real, sym_full_real])
                                            if common == repo_real and os.path.isfile(sym_full_real):
                                                with open(sym_full_real, 'r', encoding='utf-8', errors='replace') as f:
                                                    lines = f.readlines()
                                                
                                                start_idx = max(0, int(start_line) - 1)
                                                if end_line is not None:
                                                    end_idx = min(len(lines), int(end_line))
                                                else:
                                                    end_idx = min(len(lines), start_idx + 60)
                                                
                                                snippet_lines = lines[start_idx:end_idx]
                                                snippet = "".join(snippet_lines)
                                                if len(snippet.encode('utf-8')) > 8 * 1024:
                                                    snippet = snippet.encode('utf-8')[:8 * 1024].decode('utf-8', errors='ignore')
                                                
                                                read_size = len(snippet.encode('utf-8'))
                                                if total_size + read_size <= 150 * 1024:
                                                    resolved_symbols.append(f"--- Symbol: {name} ({file_path}:{start_line}) ---\n{snippet}\n")
                                                    total_size += read_size
                    except Exception:
                        pass
            
            context_blocks = []
            if resolved_files:
                context_blocks.append("Referenced files:\n" + "\n".join(resolved_files))
            if resolved_symbols:
                context_blocks.append("Referenced symbols:\n" + "\n".join(resolved_symbols))
            
            if context_blocks:
                message = "\n\n".join(context_blocks) + "\n\n" + message
 
        pre = _pilot_preflight()
        if pre:
            self.wfile.write(f"data: {json.dumps({'kind':'error','data':{'error':pre}})}\n\n".encode())
            self.wfile.write(b"data: {\"kind\": \"done\"}\n\n")
            self.wfile.flush()
            return
 
        from .hooks import run_hooks
        ctx = {"session_id": _sessions.active or "", "message": message}
        run_hooks("preRun", ctx)
        # Hold the generator so we can ALWAYS close it -- closing runs send()'s
        # finally block, which releases the per-session _busy lock. If the client
        # disconnects mid-stream (BrokenPipeError below) and we merely abandon the
        # loop, that finally never runs and the lock LEAKS -- after which every
        # subsequent message silently fails with "session busy" and the pilot
        # appears to "stop doing anything." Closing the generator in finally is
        # the fix.
        gen = _pilot.send(message, images=images or None, plan=plan, resume=resume)
        try:
            for ev in gen:
                payload = json.dumps({"kind": ev.kind, "data": ev.data})
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
            
            # After a chat turn streams its events, also drain ready swarm results:
            for ev in _pilot.drain_swarm_results():
                payload = json.dumps({"kind": ev.kind, "data": ev.data})
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()

            self.wfile.write(b"data: {\"kind\": \"done\"}\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            # Close the generator so send()'s finally (the _busy release) always
            # runs, even on client disconnect. GeneratorExit from close() is
            # swallowed by the generator's own finally; guard just in case.
            try:
                gen.close()
            except Exception:
                pass
            _finalize_turn(ctx)


def _finalize_turn(ctx) -> None:
    """End-of-turn bookkeeping (post-run hooks + transcript persist) with each step
    isolated so a failure in one cannot break the streaming response or take the
    request handler thread down. The turn is already over for the client when the
    stream ends; a serialization error in export_transcript_data() or a misbehaving
    hook must be logged, never propagated. This is the finish-path hardening for the
    "backend dies right when the response finishes" class of failure."""
    try:
        from .hooks import run_hooks
        run_hooks("postRun", ctx)
    except Exception as e:
        import sys
        print(f"[postRun hook error] {e!r}", file=sys.stderr)
    try:
        if _sessions.active:
            save_transcript(_cfg.state_dir or _tf.gettempdir(),
                            _sessions.active, _pilot.export_transcript_data())
    except Exception as e:
        import sys
        print(f"[transcript persist error] {e!r}", file=sys.stderr)


_last_jobs_snapshot: list = []


def _jobs_snapshot() -> list:
    """List jobs with resilience to a transient SQLite 'database is locked'. A
    brief lock (e.g. a lingering second backend during a relaunch) must not 500
    the jobs poll and disconnect the UI -- retry briefly, then fall back to the
    last good snapshot so the panel holds steady instead of erroring out."""
    global _last_jobs_snapshot
    import sqlite3
    import time as _t
    for attempt in range(3):
        try:
            jobs = _session.state().list_jobs()
            _last_jobs_snapshot = jobs
            return jobs
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < 2:
                _t.sleep(0.15)
                continue
            import sys
            print(f"[jobs poll degraded] {e!r} -- serving last-known "
                  f"({len(_last_jobs_snapshot)})", file=sys.stderr)
            return _last_jobs_snapshot
        except Exception as e:
            import sys
            print(f"[jobs poll error] {e!r} -- serving last-known", file=sys.stderr)
            return _last_jobs_snapshot
    return _last_jobs_snapshot


def _pilot_preflight():
    return _session.preflight()


def _available_pilots():
    """The pilot picker's model list: the user's ENABLED set (curated in
    Settings -> Models), filtered to providers that currently have a key and are
    not disconnected. The Settings tab is the curation surface -- it shows the
    FULL live catalog (incl. newly released models like gpt-5.5) as toggles; the
    picker shows only what is toggled on there, so the two always agree.

    The current driver is forced first so the picker shows it selected. If the
    user has not curated anything yet, enabled_pilots() falls back to the full
    available set."""
    from . import model_visibility as _mv
    cur = _cfg.driver
    pilots = _mv.enabled_pilots()
    # ensure the current driver appears first (it may already be in the list)
    ordered = [cur] + [p for p in pilots if p != cur]
    # De-dup while preserving order.
    seen = set()
    out = []
    for s in ordered:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out or [cur]


def _get_settings_dict():
    reach = _cfg.reach
    status = get_api_key_status(reach)
    preflight_ok = (_session.preflight() is None)
    return {
        "driver": _cfg.driver,
        "reach": reach,
        "budget": _cfg.budget,
        "models": _available_pilots(),
        "auto_distill": getattr(_pilot, "_auto_distill", False),
        "reviewEditsBeforeApply": getattr(_pilot, "_review_edits_before_apply", False),
        "wiki_auto": getattr(_cfg, "wiki_auto", False),
        "autoCommandGuard": getattr(_pilot, "_auto_command_guard", True),
        "commandTimeout": (os.environ.get("HARNESS_COMMAND_TIMEOUT", "").strip() or "120"),
        "maxPilotSteps": (os.environ.get("HARNESS_MAX_PILOT_STEPS", "").strip() or "40"),
        "state_dir": _session.state_dir,
        "repo": _cfg.repo,
        "has_api_key": status["has_key"],
        "api_key_masked": status["masked"],
        "masked": status["masked"],
        "key_env_var": get_env_var_for_reach(reach),
        "preflight_ok": preflight_ok,
    }


_startup_index_fired = False


def _maybe_auto_index_codegraph():
    global _startup_index_fired, _codegraph_status, _codegraph_status_reason
    if _startup_index_fired:
        return
    _startup_index_fired = True

    repo = _cfg.repo
    if repo and os.path.isdir(repo):
        if not _puppetmaster_available():
            _codegraph_status = "unsupported"
            _codegraph_status_reason = "puppetmaster not found -- codegraph/swarm unavailable"
            return
        
        if _codegraph_indexed(repo):
            _codegraph_status = "ready"
        else:
            # No built DB yet (fresh checkout, or init'd-but-never-indexed):
            # build it in the background so the panel comes up ready without a
            # manual re-index. `_index_codegraph_bg` runs `codegraph init --index`.
            def target():
                _index_codegraph_bg(repo)
            t = threading.Thread(target=target, daemon=True)
            t.start()


def _cleanup_marker(marker_path: str, pid: int) -> None:
    try:
        if os.path.exists(marker_path):
            with open(marker_path, "r", encoding="utf-8") as f:
                m = json.load(f)
            if m and isinstance(m, dict) and m.get("pid") == pid:
                os.remove(marker_path)
    except Exception:
        pass


def serve(host: str = "127.0.0.1", port: int = 8799, force: bool = False) -> None:
    import errno
    import sys
    import urllib.request
    import urllib.error
    import time
    import atexit

    # Force line-buffered stdout/stderr. The packaged PyInstaller backend does not
    # honor PYTHONUNBUFFERED, so its output (including crash tracebacks) sat in a
    # pipe buffer and was LOST when the process exited -- which made backend deaths
    # invisible in the desktop app's log. Line buffering flushes every line to the
    # Electron [out]/[err] pipes in real time so failures are actually captured.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(line_buffering=True)
        except Exception:
            pass

    marker_dir = _state_home()
    marker_path = os.path.join(marker_dir, "backend.json")

    if not force:
        try:
            if os.path.exists(marker_path):
                with open(marker_path, "r", encoding="utf-8") as f:
                    m = json.load(f)
                if m and isinstance(m, dict) and m.get("port"):
                    m_port = m["port"]
                    try:
                        url = f"http://127.0.0.1:{m_port}/api/config"
                        with urllib.request.urlopen(url, timeout=2.0) as resp:
                            if resp.status == 200:
                                print(f"pm-harness already running at http://{host}:{m_port} — reusing")
                                return
                    except urllib.error.HTTPError as he:
                        # A live server that answers with an HTTP status (e.g. 403
                        # from the auth gate on /api/config) is DEFINITELY running
                        # -- the probe carries no token by design. Any HTTP
                        # response, including 403, proves reuse; treat it as alive.
                        if getattr(he, "code", 0):
                            print(f"pm-harness already running at http://{host}:{m_port} — reusing")
                            return
                    except Exception:
                        # Connection refused / unreachable -> stale marker, fall
                        # through to bind a fresh server below.
                        pass
        except Exception:
            pass

    # allow quick restarts without TIME_WAIT blocking the bind
    ThreadingHTTPServer.allow_reuse_address = True

    # Cap concurrent request threads. ThreadingHTTPServer is thread-per-request
    # with NO ceiling, so a burst of slow requests (e.g. many hung provider
    # calls) could fan out into unbounded threads and exhaust the process. A
    # bounded semaphore acquired before each handler thread turns that into
    # backpressure: excess connections wait in the accept queue instead.
    _max_workers = int(os.environ.get("HARNESS_MAX_WORKERS", "64"))

    class _HarnessServer(ThreadingHTTPServer):
        daemon_threads = True  # handler threads never block process shutdown
        _worker_slots = threading.BoundedSemaphore(_max_workers)

        def process_request(self, request, client_address):
            # Acquire in the accept loop so we block accepting new work when at
            # capacity; the slot is released when the handler thread finishes.
            self._worker_slots.acquire()
            super().process_request(request, client_address)

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                self._worker_slots.release()

        def handle_error(self, request, client_address):
            # The renderer closing a socket mid-request (navigating away, stopping
            # a stream, swapping models) raises ConnectionResetError/BrokenPipeError
            # deep in socketserver. That is benign -- suppress the per-request
            # traceback that otherwise floods ~/.pmharness/electron.log and buries
            # real errors. Anything else still gets a full traceback.
            exc = sys.exc_info()[1]
            if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
                return
            import traceback
            traceback.print_exc()

    try:
        srv = _HarnessServer((host, port), Handler)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            print(f"pm-harness: port {port} is already in use. Another harness GUI "
                  f"may be running.\n  - open the existing one at http://{host}:{port}\n"
                  f"  - or pick another port: harness gui --port {port + 1}",
                  file=sys.stderr)
            raise SystemExit(2)
        raise

    port = srv.server_address[1]

    try:
        os.makedirs(marker_dir, exist_ok=True)
        with open(marker_path, "w", encoding="utf-8") as f:
            json.dump({
                "port": port,
                "pid": os.getpid(),
                "at": int(time.time() * 1000)
            }, f)
    except Exception:
        pass

    print(f"pm-harness GUI on http://{host}:{port}  (driver={_cfg.driver})")
    # SECURITY/RESOURCE: ensure spawned MCP child processes are reaped on exit
    # (Ctrl-C, SIGTERM, SystemExit) instead of being orphaned.
    import signal
    atexit.register(_mcp.stop_all)
    atexit.register(_cleanup_marker, marker_path, os.getpid())

    def _graceful(signum, frame):
        try:
            _mcp.stop_all()
        finally:
            _cleanup_marker(marker_path, os.getpid())
            raise SystemExit(0)
    for _sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(_sig, _graceful)
        except (ValueError, OSError):
            pass  # not on the main thread (e.g. under tests) -- atexit still covers it
    try:
        # Sync the agentic registry at startup so models.json reflects current keys
        from .auto_registry import sync_agentic_registry_safe
        sync_agentic_registry_safe()
        
        _maybe_auto_index_codegraph()
        srv.serve_forever()
    except SystemExit:
        raise
    except BaseException:
        # Capture the real cause of an unexpected backend exit before it unwinds.
        # Without this the traceback could be swallowed and the desktop app would
        # only see the backend vanish. Flush explicitly in case buffering lingers.
        import traceback
        print("[backend FATAL] serve_forever exited abnormally:", file=sys.stderr)
        traceback.print_exc()
        try:
            sys.stderr.flush()
        except Exception:
            pass
        raise
    finally:
        _mcp.stop_all()
        _cleanup_marker(marker_path, os.getpid())


if __name__ == "__main__":
    import sys
    p = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    serve(port=p)
