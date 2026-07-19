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
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import secrets as _secrets
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse, parse_qs
import tempfile
import uuid

from dataclasses import replace as _dc_replace

from .config import HarnessConfig
from .session import Session
from .conversation import ConversationalSession
from .mcp_manager import McpManager
from .skill_store import SkillStore
from .rule_store import RuleStore
from .command_store import CommandStore
from .memory_store import MemoryStore, MEMORY_CHAR_LIMIT
from . import workspaces as _ws
from .sessions import (
    SessionStore,
    save_transcript,
    load_transcript,
    session_stored_root,
    session_visible_for_workspace,
)
from .session_runners import (
    SessionRunnerRegistry,
    LeaseExhaustedError,
    build_lease_exhausted_payload,
)
from .deferred_attach import is_deferred_placeholder
# Re-export for tests that patch harness.server.AutoBudget (stream_auto uses
# AutoBudget.from_env via harness.api.streams).
from .autobudget import AutoBudget  # noqa: F401
from ._exec import _puppetmaster_python, _puppetmaster_available, _puppetmaster_cmd, _ensure_node_on_path
from .diag import note as _diag
from .secure_files import restrict_dir_to_owner, restrict_to_owner
# SSE ring + pump/write live in harness.api.sse; stream bodies in
# harness.api.streams. Re-export historical names so Handler methods and
# tests keep importing harness.server.
from .api.sse import (
    _SSE_RING_CAP,
    _SSE_RING_TTL,
    _SSE_RING_MAX_SESSIONS,
    SseEventRing,
    _sse_ring_generation,
    _sse_rings,
    _sse_rings_lock,
    _sse_ring_begin,
    _sse_ring_lookup,
    _sse_ring_current_generation,
    _sse_ring_clear_for_tests,
    sse_pump,
    sse_write,
)
from .api.streams import CHECKPOINT_KINDS as _CHECKPOINT_KINDS


# Cost / usage / swarm-accounting helpers live under harness.api.cost*
# (cost facade + cost_accounting / usage_meters / swarm_cost). Re-export
# historical names so tests and send_loop keep importing harness.server.
from .api.cost import (  # noqa: E402
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_5M_MULTIPLIER,
    CACHE_WRITE_1H_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    CostDeps as _CostDeps,
    _BOOT_METER_ATTRS,
    _BOOT_METER_CARRY,
    _BOOT_REPOS,
    _BOOT_USAGE_PERSIST_LOCK,
    _COST_OPTIMIZING_POLICIES,
    _USAGE_RESPONSE_TTL,
    _usage_response_cache,
    _usage_response_lock,
    bind_deps as _bind_cost_deps,
    _active_session_total,
    _app_run_id,
    _arts_for_swarm_usage,
    _boot_cost_source,
    _boot_session_cost,
    _boot_usage_meters,
    _boot_usage_path,
    _cache_saved_usd_swarm,
    _cache_savings,
    _cost_source_label,
    _fold_all_live_runners_into_boot_carry,
    _fold_runner_meters_into_boot_carry,
    _freeze_pilot_meters_into_boot_carry,
    _job_cost,
    _job_in_cost_window,
    _job_savings_fields,
    _job_swarm_accounting,
    _live_price_task,
    _live_price_unpriced_tasks,
    _note_boot_repo,
    _persist_boot_usage,
    _pilot_write_buckets,
    _registry_input_per_mtok,
    _repo_session_stamped_meters,
    _resolve_active_prices,
    _resolve_prices_for_runner,
    _restore_boot_usage,
    _routing_estimate_by_task,
    _routing_estimate_cost,
    _routing_saved_usd,
    _scoped_jobs_snapshot,
    _scoped_jobs_with_stores,
    _session_cost,
    _session_cost_split,
    _sum_job_set_savings,
    _swarm_registry,
    _task_swarm_accounting,
    _tokens_cached_swarm,
    _tool_output_savings_fields,
    _boot_usage_reset_for_tests,
    _usage_cache_clear_for_tests,
    _usage_cache_get,
    _usage_cache_put,
)

def _sync_pilot_session_id() -> None:
    """Keep the pilot's savings-ledger session scope aligned with SessionStore."""
    try:
        _pilot.harness_session_id = _sessions.active or ""
    except Exception:
        pass


def _job_status_is_terminal(status: str) -> bool:
    """Finished swarm rows: complete / fail / cancel / stall (not in-flight)."""
    s = (status or "").lower()
    if not s:
        return False
    if any(tok in s for tok in ("run", "progress", "active", "pending", "queued", "dispatch")):
        return False
    return any(
        tok in s
        for tok in ("complete", "done", "fail", "cancel", "error", "stall")
    )


def _slim_swarm_list_artifacts(raw_arts, state_obj) -> list:
    """Keep only what live/finished cards need: ROUTING + verdict rows.

    Full FINDING/RISK/DECISION streams are fetched on expand via /api/artifacts.
    Applied to both in-progress and terminal jobs on /api/swarm/live so polls
    stay cheap while a swarm is still running.
    """
    try:
        from puppetmaster.models import ArtifactType
    except Exception:
        return state_obj.format_artifacts(raw_arts) if hasattr(state_obj, "format_artifacts") else []

    keep = []
    for art in raw_arts or []:
        atype = getattr(art, "type", None)
        if atype == ArtifactType.ROUTING:
            keep.append(art)
            continue
        if atype == ArtifactType.VERIFICATION:
            payload = getattr(art, "payload", None) or {}
            if payload.get("result") or payload.get("failure"):
                keep.append(art)
    try:
        return state_obj.format_artifacts(keep) if hasattr(state_obj, "format_artifacts") else []
    except Exception:
        return []


def _job_dead_run_failure(raw_arts, status: str):
    """Mirror SwarmPane dead-run detection against raw store artifacts.

    Computed server-side before the live payload is slimmed -- otherwise a
    finished job that still has FINDING rows would look like an all-failed
    dead run once those findings are stripped from the poll response.

    Returns the failure class string, or None when the job is not a dead run.
    """
    s = (status or "").lower()
    if "complete" not in s and "done" not in s:
        return None
    if not raw_arts:
        return None
    failed = []
    for art in raw_arts:
        payload = getattr(art, "payload", None)
        if not isinstance(payload, dict):
            # Already-formatted dict rows (local jobs) carry result on the art.
            if isinstance(art, dict):
                payload = art
            else:
                return None
        result = str(payload.get("result") or "").lower()
        if result in ("failed", "blocked"):
            failed.append(payload)
        else:
            return None
    if not failed:
        return None
    for payload in failed:
        failure = payload.get("failure")
        if failure:
            return str(failure)
    return "workers failed"


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
        with os.fdopen(tmp_fd, 'w', encoding="utf-8", newline="\n") as f:
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
    
    # A file with a well-formed "disabled" list is a configured install even
    # without our marker: Puppetmaster's CLI historically rewrote platform.json
    # with only its own keys, stripping "harness_initialized" -- re-applying
    # standalone defaults here would silently disable adapters the operator
    # just enabled (`puppetmaster platform enable cursor` undone on every
    # Marionette boot). Only seed defaults when the file is truly absent or
    # carries no adapter configuration at all.
    already_configured = isinstance(pdata.get("disabled"), list)
    if not already_configured and (
        not os.path.exists(path) or "harness_initialized" not in pdata
    ):
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


def _marionette_allowed_agentic_providers(pm_providers) -> set:
    """Intersect Puppetmaster's sniff with Marionette's own key/disconnect state.

    ``puppetmaster.providers.available_providers`` can report bedrock from
    ``~/.aws/credentials`` or shell env even when Marionette has bedrock
    disconnected or only a doctor/placeholder token in keys.json. Seeding must
    never re-introduce those providers into models.json.
    """
    from .providers import PROVIDERS
    from .registry_wizard import get_provider_key
    from .keys import get_disconnected

    disconnected = get_disconnected()
    # harness name -> agentic/puppetmaster slug
    harness_to_pm = {
        "anthropic": "anthropic",
        "openai": "openai-api",
        "gemini": "gemini",
        "openrouter": "openrouter",
        "deepseek": "deepseek",
        "zai": "zai",
        "xai": "xai",
        "bedrock": "bedrock",
    }
    live_pm = set()
    for p in PROVIDERS:
        if p.name in disconnected:
            continue
        if get_provider_key(p) is None:
            continue
        live_pm.add(harness_to_pm.get(p.name, p.name))
    try:
        candidates = set(pm_providers or ())
    except TypeError:
        candidates = set()
    if not candidates:
        # Puppetmaster reported nothing; still allow Marionette-live providers
        # so a key pasted only into Settings seeds the catalog.
        return live_pm
    return candidates & live_pm


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
        allowed = _marionette_allowed_agentic_providers(available_providers())
        merged, _report = merge_curated_into_registry(
            "agentic", "api", existing, allowed_providers=allowed
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
from .api.static import PUBLIC_GET_PATHS as _STATIC_PUBLIC_GET_PATHS  # noqa: E402
from . import http_routes as _http_routes  # noqa: E402
# One shared session per server process (single-user local app).
_state_dir = os.environ.get("HARNESS_STATE_DIR", "")
_cfg = HarnessConfig.from_env()
def _pmharness_root() -> str:
    """Install root (~/.pmharness). models.json and caches stay here; durable
    session files live under ``state/`` once HARNESS_STATE_DIR is anchored."""
    return os.path.expanduser("~/.pmharness")


def _state_home() -> str:
    """Base dir for app state files (workspace.json, token, drivers, markers).

    Honors HARNESS_STATE_DIR so the test suite -- which sets it to an isolated
    temp dir per test (tests/conftest.py::_isolate_provider_state) -- can NEVER
    read or write the developer's real ~/.pmharness. These paths used to be
    frozen to real home at import time, so importing harness.server during tests
    leaked live state: a dead pytest temp repo in workspace.json and, worse, a
    rewritten auth token. A respawned backend then held a token the renderer no
    longer knew, every request 403'd, and it read as "the backend died."

    When HARNESS_STATE_DIR is unset, prefer ``~/.pmharness/state`` if that dir
    already exists (where live Saves write after the stable-state anchor). Fall
    back to the legacy flat ``~/.pmharness`` root so older installs still restore
    workspace_drivers.json / workspace.json written before the state/ split.
    Matches Electron ``readPmHarnessStateFile`` (state first, then legacy).
    """
    explicit = os.environ.get("HARNESS_STATE_DIR")
    if explicit:
        return explicit
    root = _pmharness_root()
    durable = os.path.join(root, "state")
    if os.path.isdir(durable):
        return durable
    return root


def _home_workspace_path() -> str:
    """Durable default workspace for chats with no Open Folder.

    Production: ``~/.pmharness/home``. Under ``HARNESS_STATE_DIR`` (tests /
    isolated runs): ``{state_dir}/home`` so we never touch the real home tree.
    This path is a real user project root -- not ephemeral -- and must remain
    boot-restorable via ``_record_recent_workspace``.
    """
    explicit = os.environ.get("HARNESS_STATE_DIR")
    if explicit:
        return os.path.join(explicit, "home")
    return os.path.join(_pmharness_root(), "home")


def _is_home_workspace(path: str) -> bool:
    """True when ``path`` is the durable Home workspace (slash/case-insensitive)."""
    if not path:
        return False
    try:
        return _paths_same_workspace(path, _home_workspace_path())
    except Exception:
        return False


def _ensure_home_workspace() -> str:
    """Create the Home workspace on demand, seed a minimal AGENTS.md, record it.

    Returns the absolute home path. Never raises for normal filesystem errors
    beyond returning the intended path; callers may still use it as a bind root.
    """
    home = os.path.abspath(_home_workspace_path())
    try:
        os.makedirs(home, exist_ok=True)
    except Exception as e:
        _diag("server.home_workspace_mkdir", e)
    agents = os.path.join(home, "AGENTS.md")
    try:
        if not os.path.isfile(agents):
            with open(agents, "w", encoding="utf-8", newline="\n") as f:
                f.write(
                    "# Home workspace\n\n"
                    "Default Marionette workspace for chats started without "
                    "Open Folder. Prefer moving durable project work into a "
                    "real repository via relocate_session / Open Folder.\n"
                )
    except Exception as e:
        _diag("server.home_workspace_seed", e)
    try:
        _record_recent_workspace(home, as_active=False)
    except Exception as e:
        _diag("server.home_workspace_record", e)
    return home


def _env_settings_path() -> str:
    return os.path.join(_state_home(), "env_settings.json")


# Env-backed settings that must survive a backend restart. The Settings page
# stores these in os.environ (cheap live-reload: readers check the env each
# turn), but env vars die with the process -- so every relaunch silently reset
# command guard / timeouts / step caps to defaults while the UI claimed they
# were saved. Every write goes through _persist_env_setting and startup
# replays the file with setdefault so an explicit shell/env override still wins.
def _persist_env_setting(env_var: str, value: str) -> None:
    path = _env_settings_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        if not isinstance(data, dict):
            data = {}
        data[env_var] = value
        from .registry_wizard import write_json_atomic
        write_json_atomic(path, data)
    except Exception as e:
        _diag("server.persist_env_setting", e)


def _load_env_settings() -> None:
    path = _env_settings_path()
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(k, str) and k.startswith("HARNESS_") and isinstance(v, str):
                    os.environ.setdefault(k, v)
    except Exception as e:
        _diag("server.load_env_settings", e)


def _resolve_existing_state_file(name: str) -> str:
    """Prefer the current write path for ``name``; fall back to ``~/.pmharness/<name>``.

    After the stable HARNESS_STATE_DIR anchor (~/.pmharness/state), saves land
    under state/ while older installs still have workspace.json /
    workspace_drivers.json at the legacy root. Only fall back when state home
    IS that stable anchor — never when tests point HARNESS_STATE_DIR at an
    isolated temp dir (that would leak the developer's real drivers into tests).

    Known names route through ``_workspace_json_path`` /
    ``_workspace_drivers_path`` so test monkeypatches of those helpers still
    cover both reads and writes.
    """
    if name == "workspace.json":
        primary = _workspace_json_path()
    elif name == "workspace_drivers.json":
        primary = _workspace_drivers_path()
    else:
        primary = os.path.join(_state_home(), name)
    if os.path.exists(primary):
        return primary
    legacy_root = _pmharness_root()
    if os.path.realpath(_state_home()) == os.path.realpath(os.path.join(legacy_root, "state")):
        legacy = os.path.join(legacy_root, name)
        if os.path.exists(legacy):
            return legacy
    return primary


def _workspace_json_path() -> str:
    """Write path for workspace.json (always under current state home)."""
    return os.path.join(_state_home(), "workspace.json")


def _workspace_drivers_path() -> str:
    """Write path for workspace_drivers.json (always under current state home)."""
    return os.path.join(_state_home(), "workspace_drivers.json")


# Global fallback key in workspace_drivers.json: the last driver the user chose
# anywhere. Restored on boot when the active workspace has no saved entry (or no
# workspace is open at all), so a settings-page model choice always sticks.
_LAST_DRIVER_KEY = "__last__"


def _save_workspace_driver(repo: str, driver: str) -> None:
    """Remember which model the user last used in a given workspace, so opening
    that dir later restores it (use opus-4-8 in repo A, gpt-5.5 in repo B, and
    each comes back correctly on switch)."""
    if not driver:
        return
    import tempfile as _tf
    # Never persist ephemeral temp dirs (test state leaks otherwise).
    if repo and os.path.realpath(repo).startswith(os.path.realpath(_tf.gettempdir())):
        return
    path = _workspace_drivers_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {}
        # Seed from the readable copy (state/ or legacy) so a first save after
        # the state-dir move does not drop other workspaces' remembered drivers.
        read_path = _resolve_existing_state_file("workspace_drivers.json")
        if os.path.exists(read_path):
            try:
                with open(read_path, encoding="utf-8", errors="replace") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        if not isinstance(data, dict):
            data = {}
        if repo:
            data[os.path.realpath(repo)] = driver
        data[_LAST_DRIVER_KEY] = driver
        from .registry_wizard import write_json_atomic
        write_json_atomic(path, data)
    except Exception as e:
        _diag("server.workspace_driver_write", e)


def _get_workspace_driver(repo: str):
    """The model last used in this workspace, falling back to the last driver
    chosen anywhere (so a fresh/unknown workspace still boots on the user's
    pick, not the compiled-in default). None if nothing was ever saved."""
    path = _resolve_existing_state_file("workspace_drivers.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
        if repo:
            saved = data.get(os.path.realpath(repo))
            if saved:
                return saved
        return data.get(_LAST_DRIVER_KEY)
    except Exception:
        return None

def _norm_realpath(path: str) -> str:
    """Canonical form for path comparisons: resolve + normcase.

    On Windows the same directory surfaces with mixed drive-letter / component
    casing (env-var spelling, 8.3 short names), so raw path strings are not
    comparable with ``==``. Uses ``paths._resolve`` instead of bare
    ``os.path.realpath`` -- the latter can hang indefinitely on Windows when
    the path no longer exists (moved/deleted recents like a relocated Ashita
    tree). Mirrors ``_norm_path`` in job_scoping/sessions.
    """
    from .paths import _resolve
    return os.path.normcase(_resolve(path))


def _paths_same_workspace(a: str, b: str) -> bool:
    """True when two workspace roots refer to the same directory."""
    if not a or not b:
        return False
    if a == b:
        return True
    try:
        return _norm_realpath(a) == _norm_realpath(b)
    except Exception:
        # Fall back to slash/case fold when resolve fails.
        na = os.path.normcase(a.replace("/", os.sep).replace("\\", os.sep)).rstrip(os.sep)
        nb = os.path.normcase(b.replace("/", os.sep).replace("\\", os.sep)).rstrip(os.sep)
        return na == nb


def _app_install_roots() -> list:
    """Paths that are the Marionette app itself, not user projects.

    The packaged checkout (~/.marionette/marionette), the live source root
    Electron passes as MARIONETTE_APP_ROOT / MARIONETTE_CHECKOUT, and the
    checkout that is actually running this process (derived from
    ``harness.__file__``) must never auto-appear as the open workspace or in
    PROJECTS recents -- users only see them if they open that folder manually
    for the current session. Entries are ``_norm_realpath`` canonical so
    comparisons stay case-insensitive on Windows.
    """
    roots = []
    for key in (
        "MARIONETTE_APP_ROOT",
        "HARNESS_APP_ROOT",
        "MARIONETTE_CHECKOUT",
        "HARNESS_CHECKOUT",
    ):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            try:
                roots.append(_norm_realpath(raw))
            except OSError:
                pass
    packaged = os.path.join(os.path.expanduser("~"), ".marionette", "marionette")
    try:
        if os.path.isdir(packaged):
            roots.append(_norm_realpath(packaged))
    except OSError:
        pass
    # Whatever checkout is executing this backend -- catches both the
    # packaged ~/.marionette/marionette tree and a developer
    # Projects/marionette checkout when that is what Electron spawned.
    try:
        import harness as _harness_pkg
        _pkg_dir = os.path.dirname(os.path.abspath(_harness_pkg.__file__))
        _running = os.path.dirname(_pkg_dir)
        if _running:
            roots.append(_norm_realpath(_running))
    except Exception:
        pass
    # Dedupe while preserving order.
    out = []
    seen = set()
    for r in roots:
        if r and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _is_app_install_root(path: str) -> bool:
    """True when path is the Marionette app checkout (not a user project)."""
    if not path:
        return False
    try:
        rp = _norm_realpath(path)
    except OSError:
        return False
    return rp in set(_app_install_roots())


def _pick_boot_workspace(ws_data: dict) -> str:
    """Choose the workspace to restore on launch.

    Prefer the persisted ``repo`` key, then recents, skipping the app install
    root and vanished dirs. Empty string = open nothing (first-launch / scrubbed).
    """
    if not isinstance(ws_data, dict):
        return ""
    candidates = []
    repo = (ws_data.get("repo") or "").strip()
    if repo:
        candidates.append(repo)
    for r in (ws_data.get("recents") or []):
        r = (r or "").strip()
        if r and r not in candidates:
            candidates.append(r)
    for c in candidates:
        try:
            if c and os.path.isdir(c) and not _is_app_install_root(c):
                return c
        except OSError:
            continue
    return ""


# Recent-list persistence lives in harness.api.workspace; re-export historical
# names for tests and boot/open callers. Path/app-install deps are injected.
from .api.workspace import (  # noqa: E402
    WorkspaceRecentDeps as _WorkspaceRecentDeps,
    bind_recent_deps as _bind_workspace_recent_deps,
    forget_recent_workspace as _forget_recent_workspace,
    record_recent_workspace as _record_recent_workspace,
)

_bind_workspace_recent_deps(_WorkspaceRecentDeps(
    # Late-bind through module globals so test monkeypatches on harness.server
    # (e.g. _workspace_json_path) still reach recent-list persistence.
    workspace_json_path=lambda: _workspace_json_path(),
    resolve_existing_state_file=lambda name: _resolve_existing_state_file(name),
    paths_same_workspace=lambda a, b: _paths_same_workspace(a, b),
    is_app_install_root=lambda p: _is_app_install_root(p),
    restrict_to_owner=lambda p: restrict_to_owner(p),
    diag=lambda *a, **k: _diag(*a, **k),
))


# DURABLE STATE must be anchored BEFORE workspace.json / workspace_drivers
# restore. Saves write under ~/.pmharness/state/; if we restore first,
# _state_home() still points at ~/.pmharness and boot misses the saved
# driver — falling through to enabled_pilots()[0] (often glm-5.2).
# Always join under _pmharness_root() — never _state_home()/state, which
# would nest state/state once _state_home already prefers the durable dir.
if _state_dir:
    _cfg.state_dir = _state_dir
elif not _cfg.state_dir:
    # With no explicit HARNESS_STATE_DIR and no config value, state_dir was
    # left blank -- so the session and pilot each fell back to their OWN
    # throwaway mkdtemp(), landing swarm history / transcripts / job stores
    # in a fresh temp dir every launch that nothing ever reads again. Anchor
    # to a stable per-install dir so history survives close/reopen.
    _stable = os.path.join(_pmharness_root(), "state")
    try:
        os.makedirs(_stable, exist_ok=True)
        _cfg.state_dir = _stable
        os.environ.setdefault("HARNESS_STATE_DIR", _stable)
    except Exception as e:
        _diag("server.stable_state_dir", e)

_bind_cost_deps(_CostDeps(
    # Late-bind through module globals so test monkeypatches on harness.server
    # still reach cost helpers.
    diag=lambda *a, **k: _diag(*a, **k),
    get_cfg=lambda: _cfg,
    get_pilot=lambda: _pilot,
    get_runners=lambda: _runners,
    get_sessions=lambda: _sessions,
    get_session=lambda: _session,
    jobs_snapshot=lambda: _jobs_snapshot(),
))

# Same Electron app-run: restore boot spend/savings after a backend respawn.
try:
    _restore_boot_usage()
except Exception as e:
    _diag("server.boot_usage_restore_boot", e)

def _scrub_leaked_app_root_harness_repo() -> None:
    """Drop HARNESS_REPO when it points at the Marionette app checkout itself.

    Packaged Electron may inherit a process-level HARNESS_REPO equal to the
    app install root; that must not skip workspace.json boot restore. Direct
    CLI use without MARIONETTE_APP_ROOT is intentional even when the path is
    this running checkout (dev working in the Marionette repo).
    """
    repo = (_cfg.repo or os.environ.get("HARNESS_REPO") or "").strip()
    if not repo or not _is_app_install_root(repo):
        return
    app_root_env = (os.environ.get("MARIONETTE_APP_ROOT") or "").strip()
    if app_root_env:
        try:
            if _norm_realpath(repo) == _norm_realpath(app_root_env):
                _cfg.repo = ""
                os.environ.pop("HARNESS_REPO", None)
                return
        except OSError:
            pass
    packaged = os.path.join(os.path.expanduser("~"), ".marionette", "marionette")
    try:
        if os.path.isdir(packaged) and _norm_realpath(repo) == _norm_realpath(packaged):
            _cfg.repo = ""
            os.environ.pop("HARNESS_REPO", None)
    except OSError:
        pass


try:
    _scrub_leaked_app_root_harness_repo()
except Exception as e:
    _diag("server.scrub_leaked_app_root", e)

_ws_boot_path = _resolve_existing_state_file("workspace.json")
if not os.environ.get("HARNESS_REPO") and os.path.exists(_ws_boot_path):
    try:
        with open(_ws_boot_path, "r", encoding="utf-8", errors="replace") as _ws_f:
            _ws_data = json.load(_ws_f)
        if not isinstance(_ws_data, dict):
            _ws_data = {}
        # Prefer last user project; never boot into the Marionette app
        # checkout even if an older build wrote it into workspace.json.
        _boot_repo = _pick_boot_workspace(_ws_data)
        if _boot_repo:
            _cfg.repo = _boot_repo
            os.environ["HARNESS_REPO"] = _boot_repo
        # Scrub app-install paths left in recents by older builds so the
        # PROJECTS rail does not keep surfacing Marionette itself. Close the
        # read handle first -- Windows cannot replace an open file.
        try:
            _scrubbed = [
                r for r in (_ws_data.get("recents") or [])
                if r and os.path.isdir(r) and not _is_app_install_root(r)
            ]
            _prior = (_ws_data.get("repo") or "").strip()
            _persist_repo = _boot_repo or (
                _prior if _prior and not _is_app_install_root(_prior) and os.path.isdir(_prior) else ""
            )
            if _scrubbed != list(_ws_data.get("recents") or []) or _persist_repo != _prior:
                from .registry_wizard import write_json_atomic as _ws_atomic
                _ws_atomic(_workspace_json_path(), {"repo": _persist_repo, "recents": _scrubbed[:8]})
        except Exception as _scrub_e:
            _diag("server.workspace_boot_scrub", _scrub_e)
    except Exception as e:
        _diag("server.workspace_boot_load", e)

# Restore the model last used in the adopted workspace (parity with
# /api/workspace/open). Without this, the saved driver was only read on an
# explicit workspace switch, so every app relaunch silently reset the pilot
# to the compiled-in default even though the picker said the choice was saved.
if "HARNESS_DRIVER" not in os.environ:
    try:
        _boot_saved_driver = _get_workspace_driver(_cfg.repo)
        if _boot_saved_driver and _boot_saved_driver != _cfg.driver:
            _cfg.driver = _boot_saved_driver
            if "HARNESS_MAX_CONTEXT_TOKENS" not in os.environ:
                try:
                    from pmharness.registry import context_window as _boot_ctx_window
                    _cfg.max_context_tokens = _boot_ctx_window(_cfg.driver, default=200000)
                except Exception as e:
                    _diag("server.boot_driver_context_window", e)
    except Exception as e:
        _diag("server.boot_restore_workspace_driver", e)

# Replay persisted Settings-page values into the environment BEFORE the pilot
# is constructed (it snapshots several of these at build time). setdefault
# semantics: an explicit env var set by the host/shell always wins.
_load_env_settings()

# Masker-safe live key: if HARNESS_KEY_FILE points at a file, load it into the
# expected env var for the chosen reach before the Session builds its driver.
from .keys import load_api_keys_on_startup, get_api_key_status, get_env_var_for_reach, set_api_key, clear_api_key
from .keys import (
    get_bedrock_status,
    set_bedrock_credentials,
    clear_bedrock_credentials,
)
from .wiki_config import load_wiki_config_on_startup
from .wiki_backend import ensure_wiki_backend_async
load_api_keys_on_startup(_cfg.reach)
# The Electron host spawns the backend with a stripped PATH; make Node visible so
# CodeGraph (a Node CLI) works out of the box instead of reporting "unsupported".
_ensure_node_on_path()
load_wiki_config_on_startup()
# Boot a local wiki backend only when wiki.json / env already points at loopback.
# Fresh installs stay unconfigured so the UI can guide users to portablellm.wiki.
# Opt out: MARIONETTE_NO_WIKI=1.
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


def _driver_in_enabled_set(driver: str, enabled: list) -> bool:
    """True if a driver spec matches any enabled picker spec. Handles the
    spelling variants: an enabled spec is 'provider:model' while the driver may
    be the same spec, the bare model id, or a bare catalog name whose provider
    slug differs (e.g. 'qwen3-coder-30b' vs
    'openrouter:qwen/qwen3-coder-30b-a3b-instruct')."""
    if not driver:
        return False
    aliases = {driver}
    try:
        from pmharness.registry import load_catalog
        for m in load_catalog().get("models", []):
            if m.get("name") == driver and m.get("openrouter"):
                aliases.add(m["openrouter"])
    except Exception:
        pass
    for spec in enabled:
        model = spec.split(":", 1)[1] if ":" in spec else spec
        if spec in aliases or model in aliases:
            return True
    return False


def _resolve_available_driver():
    """Make sure the active driver is one the user can actually use: its
    provider must have a key AND, when the user has curated an enabled picker
    set, the driver must be in that set. Otherwise fall back to the first
    available enabled model -- so a fresh boot never lands on the compiled-in
    default (qwen3-coder-30b) when the user disabled it, and never lands on a
    dead provider."""
    global _cfg
    try:
        if not _driver_provider_available(_cfg.driver):
            driver_ok = False
        elif _cfg.driver.startswith("stub") or "HARNESS_DRIVER" in os.environ:
            # Stub/offline drivers and an explicit env override are deliberate
            # choices -- never second-guess them against the picker curation.
            driver_ok = True
        else:
            from . import model_visibility as _mv
            enabled = _mv.get_enabled()
            driver_ok = not enabled or _driver_in_enabled_set(_cfg.driver, enabled)
        if driver_ok:
            return
        from . import model_visibility as _mv
        # Pick the first available pilot (enabled set, key-filtered).
        # enabled_pilots() is ordered by provider then catalog — first toggled
        # model on the first keyed provider wins when the compiled-in default
        # is not in the curated set.
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


def _resync_driver_after_model_curation() -> dict:
    """After Models toggles, keep the active pilot inside the enabled set.

    Returns {driver, changed} so the Settings UI / picker can refresh. Persists
    the new driver like /api/pilot/swap so a relaunch does not snap back to
    the compiled-in qwen default.
    """
    prev = _cfg.driver
    _resolve_available_driver()
    changed = _cfg.driver != prev
    if changed:
        try:
            _rebuild_pilot_and_session()
        except Exception as e:
            # Busy mid-turn: leave the resolved _cfg.driver for the next
            # rebuild; still report the intended driver so the picker label
            # matches what will run after the turn.
            _diag("server.model_curation_driver_rebuild", e)
        try:
            _save_workspace_driver(_cfg.repo, _cfg.driver)
        except Exception as e:
            _diag("server.model_curation_driver_persist", e)
    return {"driver": _cfg.driver, "changed": changed}


_resolve_available_driver()
# Tracker Session may share the global view config; each ConversationalSession
# runner gets its OWN HarnessConfig copy so mutating _cfg.repo (workspace open /
# cross-repo switch) cannot retarget a busy turn's tools/cwd.
_session = Session(_cfg)
_pilot = ConversationalSession(_dc_replace(_cfg))
# Session and pilot each fall back to their OWN mkdtemp() when config.state_dir
# is blank (the default), landing run_swarm's job store (pilot's state_dir) and
# the tracker's read store (session's state_dir) in two DIFFERENT temp dirs. The
# Swarm Tracker (/api/swarm/live) and Session Jobs (/api/jobs) read the session
# store, so they stayed empty even after a real swarm ran in the pilot store.
# Pin the session to the pilot's store so both read exactly where jobs are written.
_session.state_dir = _pilot.state_dir
import tempfile as _tf
_sessions = SessionStore(os.path.join(_cfg.state_dir or _tf.gettempdir(), "harness_sessions.json"))
# Per-session runners: active VIEW is which session the UI attaches to; other
# sessions may keep executing under the concurrent-session lease. on_drop is
# wired below once _fold_runner_meters_into_boot_carry is defined.
_runners = SessionRunnerRegistry()
# Fold dropped-runner meters into boot carry (cost helpers own the fold).
_runners._on_drop = _fold_runner_meters_into_boot_carry
_mcp = McpManager()
# Serialize pilot rebinds so a /api/pilot swap and a workspace-switch rebuild
# cannot interleave their history-copy/rebind steps and leave a torn _pilot.
_pilot_swap_lock = threading.Lock()
# One-shot resume latch for self-edit backend restarts. Set ONLY by
# /api/session/persist or /api/restart (the explicit restart path); never by a
# trailing user turn alone. Survives process respawn via a state-dir flag file
# so the fresh process can report resume_pending exactly once.
_resume_latch = False
from .pty_manager import PtyManager
_pty = PtyManager()
_pilot._mcp = _mcp
_pilot._session_store = _sessions
_init_platform_lock()
_seed_agentic_catalog()
# Seed boot-pill swarm aggregation with the workspace adopted at process start.
if _cfg.repo and os.path.isdir(_cfg.repo):
    _BOOT_REPOS.add(os.path.abspath(_cfg.repo))


def _resume_latch_path() -> str:
    return os.path.join(_cfg.state_dir or _tf.gettempdir(), ".resume_latch")


def _set_resume_latch() -> None:
    """Arm the one-shot auto-resume signal for the next process / state poll."""
    global _resume_latch
    _resume_latch = True
    try:
        p = _resume_latch_path()
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write("1\n")
        restrict_to_owner(p)
    except Exception as e:
        _diag("server.resume_latch_set", e)


def _clear_resume_latch() -> None:
    """Consume the latch (in-memory + on disk) so a later view cannot re-fire."""
    global _resume_latch
    _resume_latch = False
    try:
        p = _resume_latch_path()
        if os.path.exists(p):
            os.unlink(p)
    except Exception as e:
        _diag("server.resume_latch_clear", e)


def _load_resume_latch() -> None:
    """Adopt a latch left by a prior process (self-edit restart continuity)."""
    global _resume_latch
    try:
        p = _resume_latch_path()
        if os.path.exists(p):
            with open(p, encoding="utf-8", errors="replace") as f:
                _resume_latch = f.read().strip() == "1"
            if not _resume_latch:
                _clear_resume_latch()
    except Exception as e:
        _diag("server.resume_latch_load", e)
        _resume_latch = False


def _consume_resume_pending(idle: bool) -> bool:
    """True once when the latch is armed and the pilot is idle; then clear it."""
    global _resume_latch
    if not (_resume_latch and idle):
        return False
    _clear_resume_latch()
    return True


def _copy_pilot_meters(old_pilot: Any, new_pilot: Any) -> None:
    """Copy cost meters onto a replacement runner (legacy / explicit opt-in).

    Idle model swap and same-view rebuild no longer use this -- they freeze
    meters into ``_BOOT_METER_CARRY`` / ``_BOOT_CARRY_COST_USD`` at the old
    rates instead, so historical ``est_cost_usd`` cannot jump when the new
    model is cheaper or dearer. Prefer ``_freeze_pilot_meters_into_boot_carry``.
    """
    for attr in _BOOT_METER_ATTRS:
        try:
            setattr(new_pilot, attr, getattr(old_pilot, attr, getattr(new_pilot, attr, 0)))
        except Exception:
            pass


def _runner_config_snapshot() -> HarnessConfig:
    """Per-runner HarnessConfig copy so mutating ``_cfg.repo`` cannot retarget a busy turn."""
    return _dc_replace(_cfg)


def _bind_pilot_services(pilot: Any) -> None:
    """Attach shared MCP / session-store handles to a runner."""
    pilot._mcp = _mcp
    pilot._session_store = _sessions
    pilot._on_wiki_ingest = _clear_wiki_graph_cache


def _build_conversational_pilot(*, copy_meters_from: Any = None) -> ConversationalSession:
    """Construct a ConversationalSession with a frozen per-runner config copy.

    New runners start at zero meters -- idle rebuild/swap freezes spend into
    boot carry instead of copying token counters. ``copy_meters_from`` may
    still opt into legacy meter copy + auto-distill continuity; attach/create
    must omit it.
    """
    new_pilot = ConversationalSession(_runner_config_snapshot())
    _bind_pilot_services(new_pilot)
    if copy_meters_from is not None:
        _copy_pilot_meters(copy_meters_from, new_pilot)
        try:
            new_pilot._auto_distill = getattr(
                copy_meters_from, "_auto_distill", getattr(new_pilot, "_auto_distill", False)
            )
        except Exception:
            pass
    return new_pilot


def _active_pilot() -> Any:
    """Return the runner for the current active view (compat: same as ``_pilot``)."""
    return _pilot


def _lease_exhausted_body(exc: Optional[BaseException] = None) -> dict:
    """Build the shared lease_exhausted 409 JSON from the live registry.

    Titles come from SessionStore when cheap (unscoped list); missing titles
    are omitted rather than blocking the response.
    """
    titles_by_id: dict[str, str] = {}
    try:
        for row in _sessions.list():
            sid = str(row.get("id") or "")
            title = str(row.get("title") or "").strip()
            if sid and title:
                titles_by_id[sid] = title
    except Exception as e:
        _diag("server.lease_exhausted_titles", e)
    return build_lease_exhausted_payload(
        _runners,
        error=str(exc) if exc else None,
        titles_by_id=titles_by_id or None,
    )


def _attach_view(
    session_id: str,
    *,
    factory=None,
    load_transcript_on_create: bool = True,
    defer_cold_build: Optional[bool] = None,
) -> Any:
    """Point the UI at ``session_id`` via the runner registry.

    Body lives in ``harness.api.attach.attach_view``; this wrapper injects
    live module globals so tests can keep patching ``harness.server``.
    """
    from .api.attach import attach_view
    return attach_view(
        session_id,
        _attach_services(),
        factory=factory,
        load_transcript_on_create=load_transcript_on_create,
        defer_cold_build=defer_cold_build,
    )


def _ensure_active_pilot_ready(*, timeout: float = 120.0) -> Any:
    """Block until the active view's deferred cold build finishes (if any)."""
    from .api.attach import ensure_active_pilot_ready
    return ensure_active_pilot_ready(_attach_services(), timeout=timeout)


def _gate_active_pilot_ready(*, timeout: float = 120.0) -> Optional[dict]:
    """Ensure the active pilot is a real ConversationalSession."""
    from .api.attach import gate_active_pilot_ready
    return gate_active_pilot_ready(_attach_services(), timeout=timeout)


def _attach_view_transcript_payload(runner: Any, session_id: str) -> dict[str, list]:
    """Transcript for attach/switch responses (live runner, else disk)."""
    from .api.attach import attach_view_transcript_payload
    return attach_view_transcript_payload(runner, session_id, _attach_services())


def _save_active_transcript() -> None:
    """Persist the current active view's transcript (if any)."""
    if _sessions.active:
        save_transcript(
            _sessions_state_dir(),
            _sessions.active,
            _pilot.export_transcript_data(),
        )


_load_resume_latch()


def _reap_stale_swarms_on_boot() -> None:
    """Sweep dead-but-'running' jobs to 'stalled' in every store the tracker
    reads (harness store + the per-project CLI store). Pre-update zombies --
    jobs whose orchestrator died with the old process -- otherwise show as
    running forever and can't be cancelled, since there is nothing left to
    cancel."""
    try:
        from puppetmaster.liveness import reap_stalled_jobs
    except Exception as e:
        _diag("server.boot_reaper_import", e)
        return
    stores = []
    try:
        stores.append(_session.state().store)
    except Exception as e:
        _diag("server.boot_reaper_harness_store", e)
    try:
        from .cli_job_merge import open_cli_durable_state
        cli_state = open_cli_durable_state(_cfg.repo or "")
        if cli_state is not None:
            stores.append(cli_state.store)
    except Exception as e:
        _diag("server.boot_reaper_cli_store", e)
    for store in stores:
        try:
            reaped = reap_stalled_jobs(store)
            if reaped:
                _diag(
                    "server.boot_reaper",
                    msg=f"stalled {len(reaped)} zombie job(s): "
                        f"{[r['job_id'] for r in reaped]}",
                )
        except Exception as e:
            _diag("server.boot_reaper_sweep", e)


threading.Thread(target=_reap_stale_swarms_on_boot, daemon=True).start()


def _sessions_state_dir() -> str:
    return _cfg.state_dir or _tf.gettempdir()


_CODEGRAPH_REASON_UNSET = object()


def _set_codegraph_status(
    status: str,
    reason: Any = _CODEGRAPH_REASON_UNSET,
) -> None:
    """Mutate codegraph status globals (injected into SessionServices).

    Passing only ``status`` leaves ``_codegraph_status_reason`` untouched
    (matches prior inline assignments for ready/unsupported). Passing an
    explicit ``reason`` (including ``None``) updates both.
    """
    from .api import codegraph_index as _cgi
    if reason is _CODEGRAPH_REASON_UNSET:
        _cgi.set_codegraph_status(status)
    else:
        _cgi.set_codegraph_status(status, reason)


def _session_services():
    """Build SessionServices from live server module globals (call-time lookup)."""
    from .api.sessions import SessionServices
    return SessionServices(
        sessions=_sessions,
        runners=_runners,
        cfg=_cfg,
        get_pilot=lambda: _pilot,
        sessions_state_dir=_sessions_state_dir,
        save_active_transcript=_save_active_transcript,
        attach_view=_attach_view,
        sync_pilot_session_id=_sync_pilot_session_id,
        diag=_diag,
        is_app_install_root=_is_app_install_root,
        ensure_home_workspace=_ensure_home_workspace,
        note_boot_repo=_note_boot_repo,
        record_recent_workspace=_record_recent_workspace,
        puppetmaster_available=_puppetmaster_available,
        index_codegraph_bg=_index_codegraph_bg,
        maybe_refresh_codegraph=_maybe_refresh_codegraph,
        get_codegraph_status=_get_codegraph_status,
        lease_exhausted_body=_lease_exhausted_body,
        attach_view_transcript_payload=_attach_view_transcript_payload,
        parse_bool=_parse_bool,
        set_codegraph_status=_set_codegraph_status,
    )


def _stream_services():
    """Build StreamServices from live server module globals (call-time lookup)."""
    from .api.streams import StreamServices
    return StreamServices(
        cfg=_cfg,
        sessions=_sessions,
        get_pilot=lambda: _pilot,
        get_session=lambda: _session,
        ensure_pilot_matches_driver=_ensure_pilot_matches_driver,
        maybe_refresh_codegraph=_maybe_refresh_codegraph,
        pilot_preflight=_pilot_preflight,
        checkpoint_transcript=_checkpoint_transcript,
        finalize_turn=_finalize_turn,
        upload_dir=_UPLOAD_DIR,
        auto_budget_from_env=lambda: AutoBudget.from_env(),
    )


def _job_services():
    """Build JobServices from live server module globals (call-time lookup)."""
    from .api.jobs import JobServices
    return JobServices(
        cfg=_cfg,
        sessions=_sessions,
        get_pilot=lambda: _pilot,
        get_session=lambda: _session,
        diag=_diag,
        scoped_jobs_snapshot=_scoped_jobs_snapshot,
        scoped_jobs_with_stores=_scoped_jobs_with_stores,
        retry_on_locked=_retry_on_locked,
        swarm_registry=_swarm_registry,
        job_status_is_terminal=_job_status_is_terminal,
        slim_swarm_list_artifacts=_slim_swarm_list_artifacts,
        job_swarm_accounting=_job_swarm_accounting,
        task_swarm_accounting=_task_swarm_accounting,
        routing_saved_usd=_routing_saved_usd,
        cache_saved_usd_swarm=_cache_saved_usd_swarm,
        tokens_cached_swarm=_tokens_cached_swarm,
        job_dead_run_failure=_job_dead_run_failure,
        job_savings_fields=_job_savings_fields,
        repo_session_stamped_meters=_repo_session_stamped_meters,
        session_cost_split=_session_cost_split,
        cache_savings=_cache_savings,
        tool_output_savings_fields=_tool_output_savings_fields,
        cost_source_label=_cost_source_label,
    )


def _wiki_services():
    """Build WikiServices from live server module globals (call-time lookup)."""
    from .api.wiki import WikiServices
    return WikiServices(
        cfg=_cfg,
        get_pilot=lambda: _pilot,
    )


def _mcp_services():
    """Build McpServices from live server module globals (call-time lookup)."""
    from .api.mcp import McpServices
    return McpServices(mcp=_mcp)


def _skills_services():
    """Build SkillsServices from live server module globals (call-time lookup)."""
    from .api.skills import SkillsServices
    return SkillsServices(
        skills=_skills,
        rules=_rules,
        memory=_memory,
        get_pilot=lambda: _pilot,
        memory_char_limit=MEMORY_CHAR_LIMIT,
    )


def _worktree_services():
    """Build WorktreeServices from live server module globals (call-time lookup)."""
    from .api.worktrees import WorktreeServices
    return WorktreeServices(cfg=_cfg, parse_bool=_parse_bool)


def _terminal_services():
    """Build TerminalServices from live server module globals (call-time lookup)."""
    from .api.terminals import TerminalServices
    return TerminalServices(cfg=_cfg, pty=_pty)


def _sse_services():
    """Build SseServices from live server module globals (call-time lookup)."""
    from .api.sse import SseServices
    return SseServices(
        ring_lookup=_sse_ring_lookup,
        current_generation=_sse_ring_current_generation,
        default_session_id=lambda: (
            _sessions.active or getattr(_pilot, "harness_session_id", "") or ""
        ),
    )


def _pilot_services():
    """Build PilotServices from live server module globals (call-time lookup)."""
    from .api.pilot import PilotServices
    return PilotServices(
        cfg=_cfg,
        get_pilot=lambda: _pilot,
        apply_model_context_window=_apply_model_context_window,
        save_workspace_driver=_save_workspace_driver,
        perform_pilot_swap=_perform_pilot_swap,
    )


def _commands_services():
    """Build CommandsServices from live server module globals (call-time lookup)."""
    from .api.commands import CommandsServices
    return CommandsServices(commands=_commands, cfg=_cfg)


def _command_approval_services():
    """Build command approval services from the session runner registry."""
    from .api.command_approvals import CommandApprovalServices
    return CommandApprovalServices(get_runners=lambda: _runners)


def _hooks_services():
    """Build HooksServices from live server module globals (call-time lookup)."""
    from .api.hooks import HooksServices
    return HooksServices(parse_bool=_parse_bool)


def _checkpoint_services():
    """Build CheckpointServices from live server module globals (call-time lookup)."""
    from .api.checkpoints import CheckpointServices
    return CheckpointServices(
        cfg=_cfg,
        get_active_session_id=lambda: _sessions.active or "",
    )


def _git_services():
    """Build GitServices from live server module globals (call-time lookup)."""
    from .api.git import GitServices
    return GitServices(cfg=_cfg)


def _review_services():
    """Build ReviewServices from live server module globals (call-time lookup)."""
    from .api.reviews import ReviewServices
    return ReviewServices(
        cfg=_cfg,
        get_pilot=lambda: _pilot,
        resolve_editor_path=_resolve_editor_path,
        strip_markdown_fences=_strip_markdown_fences,
    )


def _registry_services():
    """Build RegistryServices from live server module globals (call-time lookup)."""
    from .api.registry import RegistryServices
    return RegistryServices(diag=_diag)


def _platform_services():
    """Build PlatformServices from live server module globals (call-time lookup)."""
    from .api.platform import PlatformServices
    return PlatformServices(
        get_platform_json_path=_get_platform_json_path,
        write_platform_json_atomic=_write_platform_json_atomic,
        get_platform_adapters=_get_platform_adapters,
        diag=_diag,
    )


def _codegraph_services():
    """Build CodegraphServices from live server module globals (call-time lookup)."""
    from .api.codegraph import CodegraphServices
    from .api import codegraph_index as _cgi
    import time as _time

    def _set_reason(reason: str) -> None:
        _cgi.codegraph_status_reason = reason

    def _set_live_status(status: str) -> None:
        _cgi.codegraph_status = status

    def _status_cache_put(repo: str, payload) -> None:
        _cgi.codegraph_status_cache[repo] = (
            _time.monotonic() + _cgi.CODEGRAPH_STATUS_TTL, payload)

    return CodegraphServices(
        cfg=_cfg,
        index_alive=_codegraph_index_alive,
        reindex_bg=_reindex_codegraph_bg,
        index_bg=_index_codegraph_bg,
        get_status=_get_codegraph_status,
        get_reason=lambda: _cgi.codegraph_status_reason,
        set_reason=_set_reason,
        get_live_status=lambda: _cgi.codegraph_status,
        set_live_status=_set_live_status,
        get_preflight=lambda: _cgi.codegraph_preflight,
        get_suggested_action=lambda: _cgi.codegraph_suggested_action,
        puppetmaster_available=_puppetmaster_available,
        codegraph_indexed=_codegraph_indexed,
        status_cache_get=lambda repo: _cgi.codegraph_status_cache.get(repo),
        status_cache_put=_status_cache_put,
        status_cache_pop=lambda repo: _cgi.codegraph_status_cache.pop(repo, None),
        fail_until_for=lambda repo: float(_cgi.codegraph_fail_until.get(repo) or 0),
        puppetmaster_cmd=_puppetmaster_cmd,
        status_ttl=_cgi.CODEGRAPH_STATUS_TTL,
    )


def _workspace_services():
    """Build WorkspaceServices from live server module globals (call-time lookup)."""
    from .api.workspace import WorkspaceServices

    _UNSET = object()

    def _clear_active_codegraph() -> None:
        from .api.codegraph_index import clear_active_codegraph
        clear_active_codegraph()

    def _set_codegraph_status(status: str, reason=_UNSET) -> None:
        from .api import codegraph_index as _cgi
        if reason is _UNSET:
            _cgi.set_codegraph_status(status)
        else:
            _cgi.set_codegraph_status(status, reason)

    return WorkspaceServices(
        cfg=_cfg,
        parse_bool=_parse_bool,
        ws=_ws,
        paths_same_workspace=_paths_same_workspace,
        forget_recent_workspace=_forget_recent_workspace,
        clear_active_codegraph=_clear_active_codegraph,
        get_codegraph_status=_get_codegraph_status,
        workspace_json_path=_workspace_json_path,
        ensure_home_workspace=_ensure_home_workspace,
        home_workspace_path=_home_workspace_path,
        is_app_install_root=_is_app_install_root,
        diag=_diag,
        sessions=_sessions,
        save_active_transcript=_save_active_transcript,
        note_boot_repo=_note_boot_repo,
        get_workspace_driver=_get_workspace_driver,
        apply_model_context_window=_apply_model_context_window,
        record_recent_workspace=_record_recent_workspace,
        sessions_state_dir=_sessions_state_dir,
        session_visible_for_workspace=session_visible_for_workspace,
        attach_view=_attach_view,
        lease_exhausted_body=_lease_exhausted_body,
        lease_exhausted_error=LeaseExhaustedError,
        puppetmaster_available=_puppetmaster_available,
        set_codegraph_status=_set_codegraph_status,
        index_codegraph_bg=_index_codegraph_bg,
        maybe_refresh_codegraph=_maybe_refresh_codegraph,
    )


def _settings_services():
    """Build SettingsServices from live server module globals (call-time lookup)."""
    from .api.settings import SettingsServices
    return SettingsServices(
        cfg=_cfg,
        get_pilot=lambda: _pilot,
        get_session=lambda: _session,
        parse_bool=_parse_bool,
        set_api_key=set_api_key,
        clear_api_key=clear_api_key,
        rebuild_pilot_and_session=_rebuild_pilot_and_session,
        available_pilots=_available_pilots,
        save_workspace_driver=_save_workspace_driver,
        persist_env_setting=_persist_env_setting,
        get_settings_dict=_get_settings_dict,
        driver_provider_available=_driver_provider_available,
        resolve_available_driver=_resolve_available_driver,
    )


def _session_control_services():
    """Build SessionControlServices from live server module globals."""
    from .api.session_control import SessionControlServices
    from .turn_context import context_at as _context_at
    return SessionControlServices(
        cfg=_cfg,
        get_pilot=lambda: _pilot,
        get_runners=lambda: _runners,
        gate_active_pilot_ready=_gate_active_pilot_ready,
        stash_put=_stash_put,
        save_active_transcript=_save_active_transcript,
        upload_dir=_UPLOAD_DIR,
        diag=_diag,
        get_sessions=lambda: _sessions,
        save_transcript=save_transcript,
        set_resume_latch=_set_resume_latch,
        persist_boot_usage=_persist_boot_usage,
        consume_resume_pending=_consume_resume_pending,
        checkpoint_transcript=_checkpoint_transcript,
        context_at=_context_at,
    )


def _usage_services():
    """Build UsageServices from live server module globals (call-time lookup)."""
    from .api.usage import UsageServices
    return UsageServices(
        cfg=_cfg,
        boot_repos=lambda: set(_BOOT_REPOS),
        boot_usage_meters=_boot_usage_meters,
        usage_cache_get=_usage_cache_get,
        usage_cache_put=_usage_cache_put,
        boot_session_cost=_boot_session_cost,
        scoped_jobs_with_stores=_scoped_jobs_with_stores,
        job_in_cost_window=_job_in_cost_window,
        swarm_registry=_swarm_registry,
        job_swarm_accounting=_job_swarm_accounting,
        tokens_cached_swarm=_tokens_cached_swarm,
        job_savings_fields=_job_savings_fields,
        active_session_total=_active_session_total,
        sum_job_set_savings=_sum_job_set_savings,
        cache_savings=_cache_savings,
        boot_cost_source=_boot_cost_source,
        tool_output_savings_fields=_tool_output_savings_fields,
        persist_boot_usage=_persist_boot_usage,
        retry_on_locked=_retry_on_locked,
        diag=_diag,
        get_pilot=lambda: _pilot,
    )


def _provider_services():
    """Build ProviderServices from live server module globals (call-time lookup)."""
    from .api.providers import ProviderServices
    return ProviderServices(
        cfg=_cfg,
        diag=_diag,
        parse_bool=_parse_bool,
        resync_driver_after_model_curation=_resync_driver_after_model_curation,
        driver_provider_available=_driver_provider_available,
        resolve_available_driver=_resolve_available_driver,
        rebuild_pilot_and_session=_rebuild_pilot_and_session,
    )


def _file_services():
    """Build FileServices from live server module globals (call-time lookup)."""
    from .api.files import FileServices
    return FileServices(
        cfg=_cfg,
        sessions=_sessions,
        upload_dir=_UPLOAD_DIR,
    )


def _remove_session_transcript(sid: str) -> None:
    from .api.sessions import remove_session_transcript
    remove_session_transcript(sid, state_dir=_sessions_state_dir(), diag=_diag)


def _handle_session_delete(sid: str) -> tuple[int, dict]:
    from .api.sessions import handle_session_delete
    return handle_session_delete(sid, _session_services())


def _handle_session_relocate(body: dict) -> tuple[int, dict]:
    """Move an existing session into a project workspace (no new blank session).

    Updates ``workspace_root``/``repo``, records the target in recents, opens
    the workspace as active, and keeps the same session id / transcript file.
    """
    from .api.sessions import handle_session_relocate
    return handle_session_relocate(body, _session_services())


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


def _attach_services():
    """Build AttachServices from live server module globals (call-time lookup)."""
    from .api.attach import AttachServices

    def _set_pilot(pilot: Any) -> None:
        global _pilot
        _pilot = pilot

    def _set_session(session: Any) -> None:
        global _session
        _session = session

    return AttachServices(
        get_pilot=lambda: _pilot,
        set_pilot=_set_pilot,
        get_session=lambda: _session,
        set_session=_set_session,
        cfg=_cfg,
        runners=_runners,
        sessions=_sessions,
        pilot_swap_lock=_pilot_swap_lock,
        bind_pilot_services=_bind_pilot_services,
        build_conversational_pilot=_build_conversational_pilot,
        sync_pilot_session_id=_sync_pilot_session_id,
        sessions_state_dir=_sessions_state_dir,
        diag=_diag,
        apply_model_context_window=_apply_model_context_window,
        freeze_pilot_meters_into_boot_carry=_freeze_pilot_meters_into_boot_carry,
        runner_config_snapshot=_runner_config_snapshot,
    )


def _live_pilot_driver() -> str:
    """Driver bound to the live ConversationalSession (may lag ``_cfg.driver``
    after a deferred mid-turn picker change)."""
    try:
        d = getattr(getattr(_pilot, "config", None), "driver", None)
        # MagicMock / half-init pilots used in detach tests have a non-str
        # driver; never treat those as a real mismatch worth rebuilding.
        return d.strip() if isinstance(d, str) else ""
    except Exception:
        return ""


def _history_for_pilot_swap(pilot: Any) -> Any:
    """History to copy onto a replacement pilot (prefer live transcript).

    Deferred placeholders keep turns in ``_transcript`` with ``_history=[]``.
    Prefer non-empty ``export_history`` / ``export_transcript_data`` so an idle
    swap cannot wipe the session (mirror hydrate-prefer-live from v0.9.67).
    """
    old_history = getattr(pilot, "_history", None)
    if old_history:
        return old_history
    try:
        exported = None
        export_history = getattr(pilot, "export_history", None)
        if callable(export_history):
            exported = export_history()
        if not exported:
            export_transcript = getattr(pilot, "export_transcript_data", None)
            if callable(export_transcript):
                data = export_transcript()
                if isinstance(data, dict):
                    exported = data.get("history") or []
                elif isinstance(data, list):
                    exported = data
        if exported:
            return list(exported)
    except Exception as e:
        _diag("server.pilot_swap_history_export", e)
    return old_history


def _perform_pilot_swap(model: str) -> None:
    """Rebuild the active pilot onto ``model``, preserving history/MCP.

    Freezes cost meters into boot carry at the OLD pilot's rates before the
    rebuild so historical ``est_cost_usd`` cannot jump when the new model is
    cheaper or dearer. Token meters are not copied onto the replacement.
    Caller must ensure the pilot is not mid-turn. Raises on build failure.
    """
    global _pilot
    # Finish deferred cold build before reading history — placeholders keep
    # turns in _transcript with empty _history; copying that would wipe disk.
    if is_deferred_placeholder(_pilot) or callable(
        getattr(_pilot, "ensure_ready", None)
    ):
        _ensure_active_pilot_ready()
    with _pilot_swap_lock:
        old_history = _history_for_pilot_swap(_pilot)
        old_auto_distill = getattr(_pilot, "_auto_distill", False)
        old_pilot = _pilot
        # Freeze spend at old rates before retargeting _cfg.driver.
        try:
            _freeze_pilot_meters_into_boot_carry(old_pilot)
        except Exception:
            pass
        # S3: pilot swap owns the outgoing warm ACP process.
        try:
            release = getattr(old_pilot, "release_warm_acp", None)
            if callable(release):
                release(reason="session_switch")
        except Exception:
            pass
        _cfg.driver = model
        _apply_model_context_window()
        # Frozen per-runner config; meters already in carry -- start clean.
        _pilot = ConversationalSession(_runner_config_snapshot())
        if old_history is not None:
            _pilot._history = old_history
        _pilot._auto_distill = old_auto_distill
        _pilot._mcp = _mcp
        try:
            _bind_pilot_services(_pilot)
        except Exception:
            # Older call sites relied on bare _mcp assign; binding is best-effort.
            pass
        try:
            _sync_pilot_session_id()
        except Exception:
            pass
        active_id = _sessions.active or _runners.active_view_id
        if active_id:
            # notify=False: meters already frozen above; drop must not re-fold.
            _runners.drop(active_id, notify=False)
            _runners.get_or_create(active_id, lambda: _pilot)
            _runners.set_active_view(active_id)
    _save_workspace_driver(_cfg.repo, model)


def _ensure_pilot_matches_driver(target: str | None = None) -> bool:
    """Apply a deferred picker swap before starting an idle turn.

    Returns True if the live pilot already matches (or was rebuilt). Returns
    False when the pilot is busy (caller should not start a conflicting turn
    under a mismatched driver -- the deferred choice waits).
    """
    # Cold-attach may still be building; never start a turn on a placeholder.
    _ensure_active_pilot_ready()
    want = (target or _cfg.driver or "").strip()
    if not want:
        return True
    have = _live_pilot_driver()
    if not have:
        # No bound string driver (unit-test MagicMock, half-init) -- leave alone.
        return True
    if want == have:
        return True
    busy = getattr(_pilot, "_busy", None)
    if busy is not None and busy.locked():
        return False
    _perform_pilot_swap(want)
    return True


def _rebuild_pilot_and_session():
    """Rebuild the ACTIVE view's runner for the current driver, preserving history.

    Body lives in ``harness.api.attach.rebuild_pilot_and_session``; this
    wrapper injects live module globals.
    """
    from .api.attach import rebuild_pilot_and_session
    rebuild_pilot_and_session(_attach_services())


def _session_row_is_empty(row: dict) -> bool:
    """True for a never-used session: zero token meters and no transcript body."""
    tokens = 0
    for key in ("input_tokens", "output_tokens", "cache_read_tokens"):
        try:
            tokens += int(row.get(key, 0) or 0)
        except Exception:
            pass
    if tokens:
        return False
    transcript = load_transcript(_sessions_state_dir(), row.get("id") or "")
    if isinstance(transcript, dict):
        return not (transcript.get("history") or transcript.get("display"))
    return not transcript


def _scrub_app_root_sessions_on_boot() -> None:
    """Boot hygiene for session rows rooted at the Marionette app checkout.

    Rows persisted by pre-v0.9.36 builds still carry repo/workspace_root =
    the app install root; the boot active-session attach and
    /api/sessions/switch then re-point the workspace back at the checkout
    (the "snaps back to marionette" bug). Best-effort, never raises:

    - Purge EMPTY app-root rows (zero tokens, no transcript) including their
      transcript files (state-scoping invariant #5). Non-empty rows survive
      but must not drive workspace selection.
    - If the active session is still rooted at the app checkout while the
      restored workspace repo differs, activate the newest session under the
      restored repo instead (same-workspace promotion, invariant #2).
    """
    try:
        app_rows = [
            s for s in _sessions.rows()
            if _is_app_install_root(session_stored_root(s))
        ]
        if not app_rows:
            return
        prior_active = _sessions.active
        empty_ids = [s["id"] for s in app_rows if _session_row_is_empty(s)]
        removed = _sessions.remove_rows(empty_ids) if empty_ids else []
        for sid in removed:
            _remove_session_transcript(sid)

        restored_repo = (_cfg.repo or "").strip()
        if not restored_repo or _is_app_install_root(restored_repo):
            # The user really is working in the checkout (or nothing was
            # restored): leave the active session alone.
            return
        active_row = next(
            (s for s in _sessions.rows() if s.get("id") == _sessions.active),
            None,
        )
        active_on_app_root = active_row is not None and _is_app_install_root(
            session_stored_root(active_row)
        )
        if active_on_app_root or (prior_active in removed):
            _sessions.activate_newest_for_root(restored_repo)
    except Exception as e:
        _diag("server.boot_app_root_sessions", e)


_scrub_app_root_sessions_on_boot()


def _migrate_orphan_sessions_to_home() -> None:
    """Bind empty-root session rows to the durable Home workspace on boot.

    Pre-home builds left rootless sessions visible everywhere (or nowhere in
    the Projects rail). Migrating them into Home keeps transcripts reachable
    under Projects -> Home without creating new session ids.
    """
    try:
        home = _ensure_home_workspace()
        _sessions.migrate_empty_roots(home)
    except Exception as e:
        _diag("server.boot_home_session_migrate", e)


_migrate_orphan_sessions_to_home()

# Startup: Restore the active/most-recent session's transcript into _pilot
# and register it as the active view in the runner registry.
if _sessions.active:
    _startup_history = load_transcript(_cfg.state_dir or _tf.gettempdir(), _sessions.active)
    _runners.get_or_create(_sessions.active, lambda: _pilot)
    _runners.set_active_view(_sessions.active)
    # Session ownership before hydrate so pending DANGER approval restore
    # validates display rows against the owning session.
    _sync_pilot_session_id()
    if _startup_history:
        _pilot.load_history(_startup_history)
else:
    _sync_pilot_session_id()

_skills = SkillStore()
_rules = RuleStore()
_commands = CommandStore()
_memory = MemoryStore()
_UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "harness-uploads")
os.makedirs(_UPLOAD_DIR, mode=0o700, exist_ok=True)

# Default cap for JSON POST bodies (DoS gate in Handler._read_json). 8 MiB
# matches WEB_FETCH_MAX_BYTES and sits under the upload multipart cap (10 MiB);
# transcript-sized JSON POSTs fit comfortably. Override via env.
_DEFAULT_JSON_BODY_MAX_BYTES = 8 * 1024 * 1024


def _json_body_max_bytes() -> int:
    return int(
        os.environ.get("HARNESS_JSON_BODY_MAX_BYTES", str(_DEFAULT_JSON_BODY_MAX_BYTES))
    )


class _JsonBodyTooLarge(Exception):
    """Raised by Handler._read_json when Content-Length exceeds the cap."""

    def __init__(self, size: int, limit: int):
        self.size = size
        self.limit = limit
        super().__init__(size, limit)
# The mode above only applies when the dir is created (and is umask-clipped);
# harden explicitly so uploaded images are never world-readable under the
# shared system temp dir -- on Windows too (icacls), where makedirs mode is a
# no-op.
restrict_dir_to_owner(_UPLOAD_DIR)

# Message stash: large chat/autopilot payloads cannot ride in the SSE GET's
# query string (they'd blow past the HTTP request-line limit and get
# silently dropped -- real data loss on a big paste). The client instead
# POSTs the payload here first and hands the stream only a short id via
# ?mid=. Small in-process dict, capped so a client that stashes-and-never-
# consumes (e.g. an abandoned tab) can't leak memory forever.
# Chat stash lives in harness.api.sessions; re-export historical names for
# tests and SSE GET mid= resolution.
from .api.sessions import (  # noqa: E402
    _CHAT_STASH,
    _CHAT_STASH_MAX,
    stash_put as _stash_put,
    stash_pop as _stash_pop,
)

# Wiki graph cache / handoff nonces / status helpers live in harness.api.wiki;
# re-export historical names for tests and pilot._on_wiki_ingest.
from .api.wiki import (  # noqa: E402
    WIKI_NEEDS_AUTH_HINT as _WIKI_NEEDS_AUTH_HINT,
    wiki_graph_cache as _wiki_graph_cache,
    WIKI_GRAPH_TTL as _WIKI_GRAPH_TTL,
    wiki_connect_nonces as _wiki_connect_nonces,
    WIKI_CONNECT_NONCE_TTL as _WIKI_CONNECT_NONCE_TTL,
    wiki_cache_key as _wiki_cache_key,
    clear_wiki_graph_cache as _clear_wiki_graph_cache,
    mint_wiki_connect_nonce as _mint_wiki_connect_nonce,
    consume_wiki_connect_nonce as _consume_wiki_connect_nonce,
    wiki_status_extras as _wiki_status_extras,
)

# Per-process auth token (defense-in-depth). Written owner-only (chmod 600 on
# POSIX, NTFS ACL on Windows) so the local client (Electron main / served page)
# can read it; required on mutating endpoints. Origin/Host validation below is
# the primary anti-RCE guard.
_TOKEN = os.environ.get("HARNESS_TOKEN") or _secrets.token_hex(16)
_TOKEN_FILE = os.path.join(_state_home(), "token")
try:
    os.makedirs(os.path.dirname(_TOKEN_FILE), exist_ok=True)
    with open(_TOKEN_FILE, "w", encoding="utf-8") as _tf2:
        _tf2.write(_TOKEN)
    if not restrict_to_owner(_TOKEN_FILE):
        _diag("secure_files.restrict_failed", msg=_TOKEN_FILE)
except OSError:
    pass

_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _host_ok(host_header: str) -> bool:
    """Defeat DNS-rebinding: the Host must be a literal loopback name. A rebound
    attacker domain (evil.com -> 127.0.0.1) shows its own name in Host."""
    if not host_header:
        return False
    if host_header.startswith("["):
        # Bracketed IPv6 ("[::1]" or "[::1]:8000"): the bracket pair is the
        # host. A blind rsplit(":") would mangle the portless form to "[:".
        host = host_header.split("]", 1)[0] + "]"
    else:
        host = host_header.rsplit(":", 1)[0]
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


# CodeGraph indexer runtime lives in harness.api.codegraph_index; re-export
# historical names for tests and callers. Scalar status/proc state is read
# through __getattr__ so assignments on the api module stay visible here.
from .api.codegraph_index import (  # noqa: E402
    CODEGRAPH_STALE_DEBOUNCE as _CODEGRAPH_STALE_DEBOUNCE,
    CODEGRAPH_STATUS_TTL as _CODEGRAPH_STATUS_TTL,
    CodegraphIndexDeps as _CodegraphIndexDeps,
    bind_deps as _bind_codegraph_index_deps,
    clear_active_codegraph as _clear_active_codegraph,
    codegraph_api_payload as _codegraph_api_payload,
    codegraph_fail_until as _codegraph_fail_until,
    codegraph_index_alive as _codegraph_index_alive,
    codegraph_index_lock as _codegraph_index_lock,
    codegraph_index_log_path as _codegraph_index_log_path,
    codegraph_indexed as _codegraph_indexed,
    codegraph_is_stale as _codegraph_is_stale,
    codegraph_status_cache as _codegraph_status_cache,
    codegraph_stale_check_at as _codegraph_stale_check_at,
    codegraph_tail_log as _codegraph_tail_log,
    get_codegraph_status as _get_codegraph_status,
    index_codegraph_bg as _index_codegraph_bg,
    maybe_auto_index_codegraph as _maybe_auto_index_codegraph,
    maybe_refresh_codegraph as _maybe_refresh_codegraph,
    prepare_codegraph_scope as _prepare_codegraph_scope,
    reindex_codegraph_bg as _reindex_codegraph_bg,
)
from .api import codegraph_index as _codegraph_index_mod  # noqa: E402

_bind_codegraph_index_deps(_CodegraphIndexDeps(
    # Late-bind through module globals so test monkeypatches on harness.server
    # still reach the indexer runtime.
    puppetmaster_available=lambda: _puppetmaster_available(),
    puppetmaster_cmd=lambda *a, **k: _puppetmaster_cmd(*a, **k),
    diag=lambda *a, **k: _diag(*a, **k),
    get_state_dir=lambda: (_cfg.state_dir if _cfg else "") or os.path.expanduser("~/.pmharness/state"),
    get_repo=lambda: _cfg.repo if _cfg else None,
))

_pilot._on_wiki_ingest = _clear_wiki_graph_cache


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


# Editor path / mime / multipart helpers live in harness.api.files; re-export
# historical names for inline-edit and any tests that patch harness.server.
from .api.files import (  # noqa: E402
    resolve_editor_path as _resolve_editor_path,
    guess_file_mime as _guess_file_mime,
    sqlite_table_names as _sqlite_table_names,
    binary_file_payload as _binary_file_payload,
    parse_multipart_files as _parse_multipart_files,
)



# Lazy path → handler tables (built once; bodies live in harness.api.*).
_POST_JSON_ROUTES = None
_GET_ROUTES = None


def _route_services():
    """Service factories + helpers closed over by http_routes tables."""
    from types import SimpleNamespace
    return SimpleNamespace(
        review_services=_review_services,
        job_services=_job_services,
        session_control_services=_session_control_services,
        checkpoint_services=_checkpoint_services,
        codegraph_services=_codegraph_services,
        commands_services=_commands_services,
        command_approval_services=_command_approval_services,
        file_services=_file_services,
        workspace_services=_workspace_services,
        mcp_services=_mcp_services,
        skills_services=_skills_services,
        wiki_services=_wiki_services,
        provider_services=_provider_services,
        session_services=_session_services,
        terminal_services=_terminal_services,
        platform_services=_platform_services,
        settings_services=_settings_services,
        registry_services=_registry_services,
        worktree_services=_worktree_services,
        hooks_services=_hooks_services,
        git_services=_git_services,
        usage_services=_usage_services,
        sse_services=_sse_services,
        handle_session_relocate=_handle_session_relocate,
        host_ok=_host_ok,
        diag=_diag,
        get_upload_dir=lambda: _UPLOAD_DIR,
        stash_pop=_stash_pop,
    )


def _post_json_routes():
    global _POST_JSON_ROUTES
    if _POST_JSON_ROUTES is None:
        _POST_JSON_ROUTES = _http_routes.build_post_json_routes(_route_services())
    return _POST_JSON_ROUTES


def _get_routes():
    global _GET_ROUTES
    if _GET_ROUTES is None:
        _GET_ROUTES = _http_routes.build_get_routes(_route_services())
    return _GET_ROUTES


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
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")

    def _guard(self) -> bool:
        """Reject cross-origin / rebound / unauthenticated requests. Returns True
        if the request should be BLOCKED (and sends the 403)."""
        if not _host_ok(self.headers.get("Host", "")):
            self._send(403, json.dumps({"error": "host not allowed"})); return True
        if not _origin_ok(self.headers.get("Origin", "")):
            self._send(403, json.dumps({"error": "origin not allowed"})); return True
        return False

    def _token_ok(self) -> bool:
        # Only accept the auth token in the header. Any query-string token is
        # treated as untrusted data (prevents token leakage into logs/errors).
        return self.headers.get("X-Harness-Token", "") == _TOKEN

    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _handle_wiki_connect(self, u):
        """Apply wiki config from a loopback handoff (nonce + personal LLM URL)."""
        # Host gate stays in the Handler; nonce/token/HTML live in api.wiki.
        if not _host_ok(self.headers.get("Host", "")):
            return self._send(403, json.dumps({"error": "host not allowed"}))
        from .api import wiki as _wiki_api
        status, body, ctype = _wiki_api.handle_wiki_connect(parse_qs(u.query))
        return self._send(status, body, ctype)

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def do_DELETE(self):
        if self._guard():
            return
        if not self._token_ok():
            return self._send(403, json.dumps({"error": "missing or bad token"}))
        u = urlparse(self.path)
        prefix = "/api/sessions/"
        if u.path.startswith(prefix) and u.path not in ("/api/sessions/clear",):
            sid = u.path[len(prefix):].strip("/")
            if not sid or "/" in sid:
                return self._send(400, json.dumps({"error": "missing session id"}))
            status, payload = _handle_session_delete(sid)
            return self._send(status, json.dumps(payload))
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        global _codegraph_status, _codegraph_status_reason
        global _codegraph_preflight, _codegraph_suggested_action
        if self._guard():
            return
        if not self._token_ok():
            return self._send(403, json.dumps({"error": "missing or bad token"}))
        u = urlparse(self.path)
        if u.path == "/api/upload":
            return self._handle_upload()
        if u.path in _post_json_routes():
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
        """Parse the request JSON body, rejecting oversized Content-Length first.

        Cap mirrors the upload DoS gate style: refuse before ``rfile.read`` so a
        huge POST cannot exhaust memory on the thread-per-request server.
        """
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            n = 0
        if not n:
            return {}
        limit = _json_body_max_bytes()
        if n > limit:
            raise _JsonBodyTooLarge(n, limit)
        data = self.rfile.read(n)
        try:
            decoded = data.decode()
        except Exception as e:
            raise json.JSONDecodeError("Unicode decode error", doc="", pos=0) from e
        return json.loads(decoded or "{}")

    def _handle_post_json(self, path):
        try:
            body = self._read_json()
        except _JsonBodyTooLarge as exc:
            return self._send(
                413,
                json.dumps({
                    "error": (
                        f"request body too large: {exc.size} bytes exceeds "
                        f"cap of {exc.limit}"
                    ),
                }),
            )
        except json.JSONDecodeError:
            return self._send(400, json.dumps({"error": "invalid JSON"}))
        route = _post_json_routes().get(path)
        if route is None:
            return self._send(404, json.dumps({"error": "not found"}))
        return route(self, body)

    def _handle_upload(self):
        from .api import files as _files_api
        ctype = self.headers.get("Content-Type", "")
        try:
            content_length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            content_length = 0
        # Size/ctype gate stays before rfile.read (DoS); parse+save in api.files.
        early = _files_api.check_upload_request(ctype, content_length)
        if early is not None:
            return self._send(early[0], json.dumps(early[1]))
        body = self.rfile.read(content_length)
        status, payload = _files_api.save_upload(body, ctype, _UPLOAD_DIR)
        return self._send(status, json.dumps(payload))

    # GET endpoints that are intentionally public (the same-origin renderer
    # bootstrap assets, which must load BEFORE the page has the token to make
    # authenticated calls). Everything else under /api requires the token.
    # Owned by harness.api.static; Handler keeps the auth gate.
    _PUBLIC_GET_PATHS = _STATIC_PUBLIC_GET_PATHS

    def do_GET(self):
        global _codegraph_status, _codegraph_status_reason
        global _codegraph_preflight, _codegraph_suggested_action
        u = urlparse(self.path)
        # Loopback wiki handoff: browser navigates here with a one-shot nonce
        # (no harness token). Must run before the centralized auth gate.
        if u.path == "/api/wiki/connect":
            return self._handle_wiki_connect(u)
        # CENTRALIZED AUTH GATE: every non-public path requires the token.
        # Route bodies assume auth already happened (no per-GET token copies).
        if u.path not in self._PUBLIC_GET_PATHS:
            if self._guard():
                return
            if not self._token_ok():
                return self._send(403, json.dumps({"error": "missing or bad token"}))
        from .api import static as _static_api
        shell = _static_api.try_static_shell(u.path, web_root=_WEB, token=_TOKEN)
        if shell is not None:
            status, body, ctype = shell
            return self._send(status, body, ctype)
        q = parse_qs(u.query)
        route = _get_routes().get(u.path)
        if route is None:
            return self._send(404, json.dumps({"error": "not found"}))
        return route(self, u, q)

    def _sse_write(self, payload: bytes) -> bool:
        """Write one SSE frame. Returns False if the client has detached."""
        return sse_write(self.wfile, payload)

    def _sse_pump(self, gen, frame_for_event, *, on_event=None, write_done: bool = True,
                  ring: Optional[SseEventRing] = None) -> bool:
        """Pump a turn generator over SSE with Hermes-style detach semantics."""
        return sse_pump(
            self.wfile,
            gen,
            frame_for_event,
            on_event=on_event,
            write_done=write_done,
            ring=ring,
        )

    def _stream_run(self, prompt: str, images=None):
        from .api.streams import stream_run
        return stream_run(self, prompt, images, _stream_services())

    def _stream_auto(self, objective: str):
        """Stream the fully-auto loop (governor-bounded) over SSE."""
        from .api.streams import stream_auto
        return stream_auto(self, objective, _stream_services())

    def _swap_pilot(self, model: str):
        """Hot-swap the pilot model (the whole point: your key -> your pilot).

        Body lives in ``harness.api.pilot``; this wrapper injects live globals.
        Hermes-style mid-turn deferral and idle rebuild semantics are unchanged.
        """
        from .api.pilot import get_pilot_swap
        status, payload = get_pilot_swap(model, _pilot_services())
        return self._send(status, json.dumps(payload))

    def _stream_terminal(self, sid: str):
        """Stream PTY output over SSE. Client sends keystrokes via POST /api/terminal/write."""
        from .api.terminals import stream_terminal
        return stream_terminal(self, sid, _terminal_services())

    def _stream_chat(self, message: str, images=None, plan: bool = False, resume: bool = False):
        """Stream the conversational PILOT loop over SSE."""
        from .api.streams import stream_chat
        return stream_chat(
            self, message, images, _stream_services(), plan=plan, resume=resume,
        )


def _checkpoint_transcript(ctx=None) -> None:
    """Persist the turn's transcript mid-stream so a hard crash before
    _finalize_turn() doesn't lose the in-flight turn. Mirrors the transcript step
    of _finalize_turn (no postRun hooks) and is fully exception-isolated: it must
    never break the SSE stream or take the handler thread down.

    Prefer the turn-bound session_id/pilot from ``ctx`` (captured at stream start)
    so a mid-turn view switch cannot overwrite the newly active session's file.
    """
    try:
        sid = ""
        pilot = None
        if ctx:
            sid = (ctx.get("session_id") or "") if isinstance(ctx, dict) else ""
            pilot = ctx.get("pilot") if isinstance(ctx, dict) else None
        if not pilot:
            pilot = _pilot
        if not sid:
            sid = getattr(pilot, "harness_session_id", "") or (_sessions.active or "")
        if sid and pilot is not None:
            save_transcript(_cfg.state_dir or _tf.gettempdir(),
                            sid, pilot.export_transcript_data())
    except Exception as e:
        import sys
        print(f"[transcript checkpoint error] {e!r}", file=sys.stderr)


def _finalize_turn(ctx) -> None:
    """End-of-turn bookkeeping (post-run hooks + transcript persist) with each step
    isolated so a failure in one cannot break the streaming response or take the
    request handler thread down. The turn is already over for the client when the
    stream ends; a serialization error in export_transcript_data() or a misbehaving
    hook must be logged, never propagated. This is the finish-path hardening for the
    "backend dies right when the response finishes" class of failure.

    Prefer turn-bound session_id/pilot from ``ctx`` so a mid-turn view switch
    cannot overwrite the newly active session's transcript.
    """
    try:
        from .hooks import run_hooks
        run_hooks("postRun", ctx)
    except Exception as e:
        import sys
        print(f"[postRun hook error] {e!r}", file=sys.stderr)
    try:
        sid = ""
        pilot = None
        if ctx and isinstance(ctx, dict):
            sid = ctx.get("session_id") or ""
            pilot = ctx.get("pilot")
        if not pilot:
            pilot = _pilot
        if not sid:
            sid = getattr(pilot, "harness_session_id", "") or (_sessions.active or "")
        if sid and pilot is not None:
            save_transcript(_cfg.state_dir or _tf.gettempdir(),
                            sid, pilot.export_transcript_data())
    except Exception as e:
        import sys
        print(f"[transcript persist error] {e!r}", file=sys.stderr)


def _retry_on_locked(read, attempts: int = 3, delay: float = 0.15):
    """Run a store read, retrying briefly on SQLite 'database is locked'.

    Windows raises these transient lock errors far more readily than macOS
    (concurrent swarm workers + CodeGraph indexer + usage polling on one
    SQLite file), so cost/token endpoints retry instead of erroring out.
    """
    import sqlite3
    import time as _t
    for attempt in range(attempts):
        try:
            return read()
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < attempts - 1:
                _t.sleep(delay)
                continue
            raise
    return read()


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

    The current driver is forced first when it is still in the enabled set so
    the picker shows it selected. A stale compiled-in default (e.g. qwen) that
    the user never toggled on is NOT injected — that made the composer look
    like it was on a model that could not run.
    """
    from . import model_visibility as _mv
    cur = _cfg.driver
    pilots = _mv.enabled_pilots()
    curated = _mv.get_enabled()
    cur_allowed = False
    if cur:
        if cur in pilots:
            cur_allowed = True
        elif curated and _driver_in_enabled_set(cur, curated):
            cur_allowed = True
        elif not curated:
            # No curation yet: full available set — keep current first if present.
            cur_allowed = cur in pilots or _driver_in_enabled_set(cur, pilots)
    if cur_allowed:
        ordered = [cur] + [p for p in pilots if p != cur]
    else:
        ordered = list(pilots)
    # De-dup while preserving order.
    seen = set()
    out = []
    for s in ordered:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out or ([cur] if cur else [])


def _get_settings_dict():
    from harness.hash_edit import hash_edit_enabled
    from harness.reasoning_effort import current_reasoning_effort

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
        "autoVerify": getattr(_cfg, "auto_verify", True),
        "verifyCommand": getattr(_cfg, "verify_command", ""),
        "autoCommandGuard": getattr(_pilot, "_auto_command_guard", True),
        "hash_edit_enabled": hash_edit_enabled(),
        "commandTimeout": (os.environ.get("HARNESS_COMMAND_TIMEOUT", "").strip() or "120"),
        "maxPilotSteps": (os.environ.get("HARNESS_MAX_PILOT_STEPS", "").strip() or "40"),
        "workerTokenBudget": (
            os.environ.get("HARNESS_WORKER_TOKEN_BUDGET", "").strip() or "40000"
        ),
        "reasoning_effort": current_reasoning_effort(),
        "state_dir": _session.state_dir,
        "repo": _cfg.repo,
        "has_api_key": status["has_key"],
        "api_key_masked": status["masked"],
        "masked": status["masked"],
        "key_env_var": get_env_var_for_reach(reach),
        "preflight_ok": preflight_ok,
        "bedrock": get_bedrock_status(),
    }



def _cleanup_marker(marker_path: str, pid: int) -> None:
    try:
        if os.path.exists(marker_path):
            with open(marker_path, "r", encoding="utf-8") as f:
                m = json.load(f)
            if m and isinstance(m, dict) and m.get("pid") == pid:
                os.remove(marker_path)
    except Exception:
        pass


# Read-through aliases so `harness.server._codegraph_status` (etc.) reflects
# the owning module after the index-runtime peel. Writes should target
# harness.api.codegraph_index (tests that assign on server are updated).
_CODEGRAPH_STATE_ALIASES = {
    "_codegraph_status": "codegraph_status",
    "_codegraph_status_reason": "codegraph_status_reason",
    "_codegraph_preflight": "codegraph_preflight",
    "_codegraph_suggested_action": "codegraph_suggested_action",
    "_codegraph_index_proc": "codegraph_index_proc",
    "_startup_index_fired": "startup_index_fired",
}

# Scalar boot/cost state lives in harness.api.cost; read/write through aliases so
# tests that assign `harness.server._COST_EPOCH = ...` still update the owner.
_COST_STATE_ALIASES = {
    "_COST_EPOCH": "_COST_EPOCH",
    "_BOOT_CARRY_COST_USD": "_BOOT_CARRY_COST_USD",
    "_BOOT_PLAN_BILLING": "_BOOT_PLAN_BILLING",
    "_BOOT_USAGE_RESTORED": "_BOOT_USAGE_RESTORED",
    "_BOOT_USAGE_LAST_PERSIST": "_BOOT_USAGE_LAST_PERSIST",
}


def __getattr__(name: str):
    alias = _CODEGRAPH_STATE_ALIASES.get(name)
    if alias is not None:
        from .api import codegraph_index as _cgi
        return getattr(_cgi, alias)
    cost_alias = _COST_STATE_ALIASES.get(name)
    if cost_alias is not None:
        from .api import cost as _cost
        return getattr(_cost, cost_alias)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(
        list(globals().keys())
        + list(_CODEGRAPH_STATE_ALIASES.keys())
        + list(_COST_STATE_ALIASES.keys())
    )


import types as _types

class _HarnessServerModule(_types.ModuleType):
    def __setattr__(self, name, value):
        cost_alias = _COST_STATE_ALIASES.get(name)
        if cost_alias is not None:
            from .api import cost as _cost
            setattr(_cost, cost_alias, value)
            return
        _types.ModuleType.__setattr__(self, name, value)


# Enable write-through aliases for boot/cost scalars on this module.
import sys as _sys
_sys.modules[__name__].__class__ = _HarnessServerModule




def boot_mcp_servers(mcp: Any = None, diag: Any = None) -> None:
    """Start configured MCP servers and record failures via ``diag``.

    Extracted from the serve() boot thread so tests exercise the real
    production path (msg=/exc= kwargs) instead of reimplementing the body.
    """
    mcp_client = _mcp if mcp is None else mcp
    note = _diag if diag is None else diag
    try:
        report = mcp_client.start_all()
        for name, result in report.items():
            if isinstance(result, str):
                note("mcp.boot_error", msg=f"{name}: {result}")
    except Exception as exc:
        note("mcp.boot_fail", exc=exc)


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

    # allow quick restarts without TIME_WAIT blocking the bind. POSIX-only:
    # on Windows SO_REUSEADDR means "two live sockets may bind the same port",
    # which silently defeats the already-in-use guard (EADDRINUSE never fires)
    # and lets a second backend hijack the first one's port.
    ThreadingHTTPServer.allow_reuse_address = os.name == "posix"

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
        with open(marker_path, "w", encoding="utf-8", newline="\n") as f:
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

    def _shutdown_warm_acp() -> None:
        """S3: reap owned Cursor warm ACP children on process shutdown."""
        try:
            for runner in list(_runners.runners()):
                release = getattr(runner, "release_warm_acp", None)
                if callable(release):
                    release(reason="shutdown")
        except Exception:
            pass
        try:
            release = getattr(_pilot, "release_warm_acp", None)
            if callable(release):
                release(reason="shutdown")
        except Exception:
            pass

    atexit.register(_mcp.stop_all)
    atexit.register(_shutdown_warm_acp)
    atexit.register(_cleanup_marker, marker_path, os.getpid())

    def _graceful(signum, frame):
        try:
            _shutdown_warm_acp()
        except Exception:
            pass
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
        # Connect configured MCP servers (incl. local Docker HTTP) without
        # blocking the GUI bind. Failures land on status().error for State→MCP.
        threading.Thread(target=boot_mcp_servers, name="mcp-boot", daemon=True).start()
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
