"""Test isolation: block real network by default so a test that accidentally
makes a live API call fails FAST instead of hanging the whole suite.

Tests that legitimately need the network (the live-key analysis/eval tests) opt
back in with @pytest.mark.network, and are deselected by default in CI runs.

Wave 6 offline full-auto safety gate: modules listed in
``_FULL_AUTO_SAFETY_MODULES`` are auto-tagged ``full_auto_safety`` so CI can
run ``pytest -m full_auto_safety`` as a named invariant gate without live keys.
"""
import socket
import os
import shutil
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace
import pytest

# Offline full-auto safety invariants (Wave 6). Keep live-key / @network /
# @swarm evals out of this set — they stay opt-in and must not gate PRs.
_FULL_AUTO_SAFETY_MODULES = frozenset({
    "test_autobudget",
    "test_auto",
    "test_command_policy",
    "test_command_guard_integration",
    "test_api_command_approvals",
    "test_auto_receipts",
    "test_safe_boundary_cancel_steer",
    "test_tool_pair_sanitizer",
    "test_sse_ring_buffer",
    "test_stub_eval_deterministic",
    "test_warm_acp_lifecycle",
    "test_deferred_cold_attach",
})

os.environ["PMHARNESS_MCP_ALLOW_PRIVATE"] = "1"

# Windows: git marks object files read-only, which makes shutil.rmtree fail
# with PermissionError on every temp-git-repo teardown. Wrap rmtree once here
# (instead of touching dozens of per-test cleanup calls) with an error handler
# that clears the read-only bit and retries the failed delete.
if os.name == "nt":
    _real_rmtree = shutil.rmtree
    _rmtree_guard = False

    def _clear_readonly_and_retry(func, path, _exc_info):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception:
            pass

    def _rmtree_windows(path, ignore_errors=False, onerror=None, **kwargs):
        global _rmtree_guard
        # tempfile's teardown onerror re-enters shutil.rmtree; without this guard
        # the patch recurses until RecursionError on Windows 3.9 CI.
        if _rmtree_guard:
            return _real_rmtree(
                path, ignore_errors=ignore_errors, onerror=onerror, **kwargs
            )
        _rmtree_guard = True
        try:
            if onerror is None and not kwargs.get("onexc"):
                onerror = _clear_readonly_and_retry
            return _real_rmtree(
                path, ignore_errors=ignore_errors, onerror=onerror, **kwargs
            )
        finally:
            _rmtree_guard = False

    shutil.rmtree = _rmtree_windows

# Seal off the developer's real ~/.pmharness BEFORE pytest collects (imports)
# any test module. Several test modules import harness.server at top level, and
# that import writes a fresh auth token + touches workspace/marker files. Frozen
# to real home, those writes clobbered the running app's live token (renderer
# and backend then disagreed -> every request 403'd -> "the backend died") and
# corrupted workspace.json with a vanished temp repo. Setting the state dir here
# -- before collection -- guarantees every such write lands in a throwaway dir.
# The per-test fixture below layers a fresh subdir on top for finer isolation.
#
# Must FORCE (never setdefault): a pre-set HARNESS_STATE_DIR=~/.pmharness/state
# (common after a bare harness.server import anchors the shell) would otherwise
# keep pointing at the live tree and let SessionStore pollute harness_sessions.json.


def _path_under_dir(path: str, root: str) -> bool:
    """True when ``path`` resolves at or under ``root`` (slash/case-safe)."""
    try:
        path_r = os.path.normcase(os.path.realpath(path))
        root_r = os.path.normcase(os.path.realpath(root))
        if not path_r or not root_r:
            return False
        if path_r == root_r:
            return True
        return os.path.commonpath([root_r, path_r]) == root_r
    except (ValueError, OSError, TypeError):
        return False


def force_throwaway_harness_state_dir() -> str:
    """Ensure ``HARNESS_STATE_DIR`` is a process-unique throwaway state root.

    Forces a fresh temp dir when the env var is unset/blank OR resolves under
    the real ``~/.pmharness`` tree. Xdist workers receive separate children of
    the controller's throwaway root so collection-time SQLite setup cannot
    contend across processes. Never uses ``setdefault`` — a contaminated live
    value must be overwritten, not preserved.
    """
    live_root = os.path.expanduser("~/.pmharness")
    current = (os.environ.get("HARNESS_STATE_DIR") or "").strip()
    must_force = (not current) or _path_under_dir(current, live_root)
    if must_force:
        os.environ["HARNESS_STATE_DIR"] = tempfile.mkdtemp(
            prefix="pmharness-test-state-"
        )
    worker_id = (os.environ.get("PYTEST_XDIST_WORKER") or "").strip()
    if worker_id:
        current = os.environ["HARNESS_STATE_DIR"]
        if Path(current).name != worker_id:
            worker_root = Path(current) / worker_id
            worker_root.mkdir(parents=True, exist_ok=True)
            os.environ["HARNESS_STATE_DIR"] = str(worker_root)
    return os.environ["HARNESS_STATE_DIR"]


force_throwaway_harness_state_dir()
# Registry sync uses its own path, independently of HARNESS_STATE_DIR.
# Bind it before collection so pin tests cannot rewrite the running app's rows.
os.environ["PUPPETMASTER_MODELS_PATH"] = os.path.join(
    os.environ["HARNESS_STATE_DIR"], "marionette-models.json",
)
from harness import model_visibility as _model_visibility

_model_visibility._store_path = lambda: os.path.join(
    os.environ["HARNESS_STATE_DIR"], "models.json",
)

# Checkpoint metadata uses Path.home independently of HARNESS_STATE_DIR.
# Bind only that module's path facade before collection constructs any stores.
from harness import checkpoints as _checkpoint_module

_checkpoint_paths = _checkpoint_module.Path
_checkpoint_module.Path = SimpleNamespace(
    home=lambda: Path(os.environ["HARNESS_STATE_DIR"]) / "checkpoint-home",
)


def pytest_unconfigure(config):
    _checkpoint_module.Path = _checkpoint_paths


def _clear_live_puppetmaster_state_dir() -> None:
    """Drop a worker-injected ``PUPPETMASTER_STATE_DIR`` that points at a live store.

    Puppetmaster implement workers export ``PUPPETMASTER_STATE_DIR`` to the
    host project's SQLite. ``harness.cli_job_merge`` then opens that DB on
    every ``/api/usage`` poll; when the parent worker still holds the lock,
    scoped job merges stall for seconds and HTTP client timeouts flake
    (usage-meter / spill / swarm-live tests). Tests must resolve CLI stores
    from the fixture workspace, not the live parent project.
    """
    key = "PUPPETMASTER_STATE_DIR"
    current = (os.environ.get(key) or "").strip()
    if not current:
        return
    live_root = os.path.join(
        os.environ.get("APPDATA") or os.path.expanduser("~"),
        "puppetmaster",
    )
    # Also cover XDG / macOS-style app roots used by puppetmaster.state.
    alt_roots = (
        live_root,
        os.path.join(os.path.expanduser("~"), ".puppetmaster"),
        os.path.join(
            os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
            "puppetmaster",
        ),
    )
    if any(_path_under_dir(current, root) for root in alt_roots if root):
        os.environ.pop(key, None)


_clear_live_puppetmaster_state_dir()
# Dispatch tests use bare /mock/repo and tmp fixtures without .git. The
# production git/Home soft-refuse stays on by default outside pytest; unit
# tests that assert the refuse path re-enable via monkeypatch.
os.environ.setdefault("HARNESS_IMPLEMENT_GIT_GUARD", "0")
# Cross-project CLI merges scan ~/.puppetmaster/projects; under a live
# Puppetmaster worker those DBs are often locked. Keep unit tests on the
# fixture workspace only (tracker cross-project coverage has dedicated tests).
os.environ.setdefault("HARNESS_CLI_CROSS_PROJECT", "0")

_real_socket = socket.socket


class _BlockedNetwork(RuntimeError):
    pass


def _guard(*a, **k):
    raise _BlockedNetwork(
        "network access blocked in tests (no live API calls). If this test truly "
        "needs the network, mark it @pytest.mark.network and run with --network.")


def pytest_addoption(parser):
    parser.addoption("--network", action="store_true", default=False,
                     help="allow tests marked @pytest.mark.network to use the network")
    parser.addoption("--swarm", action="store_true", default=False,
                     help="run tests marked @pytest.mark.swarm (real Puppetmaster, slow)")
    parser.addoption(
        "--resource-soak",
        action="store_true",
        default=False,
        help="run tests marked @pytest.mark.resource_soak (million-token walks)",
    )


def parse_pytest_shard(spec):
    """Parse ``PYTEST_SHARD=2/4`` into a 1-based (index, total) pair."""
    raw = (spec or "").strip()
    if not raw:
        return None
    try:
        idx_s, total_s = raw.split("/", 1)
        idx = int(idx_s)
        total = int(total_s)
    except ValueError:
        return None
    if total < 2 or idx < 1 or idx > total:
        return None
    return idx, total


def select_pytest_shard(items, spec):
    """Stable interleave so one fat test cannot land with all of its neighbors."""
    parsed = parse_pytest_shard(spec)
    if parsed is None:
        return list(items)
    idx, total = parsed
    ordered = sorted(items, key=lambda item: getattr(item, "nodeid", str(item)))
    return [item for i, item in enumerate(ordered) if i % total == (idx - 1)]


def pytest_configure(config):
    config.addinivalue_line("markers", "network: test requires real network access")
    config.addinivalue_line("markers", "swarm: test drives real Puppetmaster (slow subprocess spawns)")
    config.addinivalue_line(
        "markers",
        "resource_soak: optional FD/RSS cleanup soak (manual/CI opt-in)",
    )
    config.addinivalue_line(
        "markers",
        "full_auto_safety: offline full-auto safety invariants "
        "(AutoBudget, command policy/approvals, tool-pair sanitizer, "
        "SSE ring-miss honesty, stub deterministic eval)",
    )


def pytest_collection_modifyitems(config, items):
    safety = pytest.mark.full_auto_safety
    for item in items:
        mod = getattr(item.module, "__name__", "") or ""
        leaf = mod.rsplit(".", 1)[-1]
        if leaf in _FULL_AUTO_SAFETY_MODULES:
            item.add_marker(safety)

    if not config.getoption("--swarm"):
        skip_swarm = pytest.mark.skip(reason="real-Puppetmaster swarm test; run with --swarm")
        for item in items:
            if item.get_closest_marker("swarm"):
                item.add_marker(skip_swarm)

    if not config.getoption("--resource-soak"):
        skip_soak = pytest.mark.skip(
            reason="resource soak; run with --resource-soak (tests-full.yml)"
        )
        for item in items:
            if item.get_closest_marker("resource_soak"):
                item.add_marker(skip_soak)

    shard_spec = os.environ.get("PYTEST_SHARD") or ""
    selected = select_pytest_shard(items, shard_spec)
    if selected is not items and len(selected) != len(items):
        items[:] = selected


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    # allow loopback (local harness server tests) but block outbound by patching
    # socket.socket.connect to refuse non-loopback addresses.
    if request.node.get_closest_marker("network"):
        if not request.config.getoption("--network"):
            pytest.skip("needs --network")
        return
    real_connect = _real_socket.connect

    def guarded_connect(self, address):
        host = address[0] if isinstance(address, tuple) else ""
        if host in ("127.0.0.1", "::1", "localhost"):
            return real_connect(self, address)
        raise _BlockedNetwork(f"blocked outbound connect to {address!r} in tests")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


@pytest.fixture(autouse=True)
def _clear_pm_resolver_cache():
    # The puppetmaster resolver caches availability/python globally; clear it
    # before every test so mock_which states never leak across tests.
    try:
        from harness._exec import _clear_puppetmaster_cache
        _clear_puppetmaster_cache()
    except Exception:
        pass
    yield
    try:
        from harness._exec import _clear_puppetmaster_cache
        _clear_puppetmaster_cache()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _clear_context_switch_latch():
    # Process-wide latch from context_switch_guard; reset so a relocate/switch
    # test cannot rewrite later danger verdicts to context-switch-unconfirmed.
    try:
        from harness.context_switch_guard import reset_for_tests
        reset_for_tests()
    except Exception:
        pass
    yield
    try:
        from harness.context_switch_guard import reset_for_tests
        reset_for_tests()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _clear_wiki_env(monkeypatch):
    monkeypatch.delenv("WIKI_API_BASE", raising=False)
    monkeypatch.delenv("WIKI_OWNER_TOKEN", raising=False)
    monkeypatch.delenv("HARNESS_WIKI_URL", raising=False)
    monkeypatch.delenv("HARNESS_WIKI_TOKEN", raising=False)


@pytest.fixture(autouse=True)
def _isolate_pilot_env(monkeypatch):
    """Reset env vars that the running Marionette app may have set in the
    developer's shell. Without this, HARNESS_MAX_PILOT_STEPS=0 (unlimited
    autopilot) makes every test with a non-terminating fake pilot hang forever,
    and HARNESS_AUTO_COMMAND_GUARD=off silently disables the command guard.
    The app also persists feature flags via _set_env_setting that can change
    test behavior (auto-verify, edit review, distill, hash-edit, etc.).
    HARNESS_SWARM_ADAPTER/HARNESS_REPO matter most: the running app exports
    them (agentic + the open workspace). Inherited ambient values can skew
    e2e/stage4 swarms. Tests that need a specific adapter set it explicitly;
    demo substrate requires HARNESS_ALLOW_DEMO_SWARM=1."""
    for _var in (
        "HARNESS_MAX_PILOT_STEPS",
        "HARNESS_MAX_TOKENS",
        "HARNESS_PILOT_TOOL_BUDGET",
        "HARNESS_WORKER_TOKEN_BUDGET",
        "HARNESS_AUTO_COMMAND_GUARD",
        "HARNESS_AUTO_DISTILL",
        "HARNESS_REVIEW_EDITS_BEFORE_APPLY",
        "HARNESS_AUTO_VERIFY",
        "HARNESS_HASH_EDIT",
        "HARNESS_COMPACTION_RESIDUAL",
        "HARNESS_BROWSER_REAL_PROFILE",
        "HARNESS_VERIFY_COMMAND",
        "HARNESS_COMMAND_TIMEOUT",
        "HARNESS_SWARM_ADAPTER",
        "HARNESS_REPO",
    ):
        monkeypatch.delenv(_var, raising=False)


@pytest.fixture(autouse=True)
def _isolate_provider_state(monkeypatch, tmp_path_factory):
    """Give every test its own clean state dir, isolated from the developer's
    real ~/.pmharness (keys.json / disconnected.json / workspace.json / token).
    A collection-safe default is already set at module import; this layers a
    fresh per-test subdir on top so state never bleeds between tests. Tests that
    set their own HARNESS_STATE_DIR in the test body still override it there.

    Replaces the loaded server's config, store, registry, tracker, and pilot
    together. A pilot's permanent queue owner must never outlive its store.
    """
    import sys

    d = tmp_path_factory.mktemp("pmstate")
    monkeypatch.setenv("HARNESS_STATE_DIR", str(d))
    models_path = d / "marionette-models.json"
    if not models_path.exists():
        models_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("PUPPETMASTER_MODELS_PATH", str(models_path))

    server_mod = sys.modules.get("harness.server")
    test_store = None
    if server_mod is not None:
        test_store = _isolate_server_session_state(server_mod, d, monkeypatch)

    yield

    if test_store is not None:
        test_store.flush()


def _isolate_server_session_state(server, state_dir, monkeypatch):
    from dataclasses import replace
    from harness.sessions import SessionStore
    from harness.session import Session
    from harness.session_runners import SessionRunnerRegistry

    # Queue ownership includes the state root and cannot follow a replaced store.
    server._sessions.flush()
    cfg = replace(server._cfg, state_dir=str(state_dir))
    # Isolate must not construct the default catalog driver (qwen3-coder-30b).
    # xdist can leave load_catalog unreadable; stub-oracle-v2 is offline.
    offline = replace(cfg, driver="stub-oracle-v2")
    monkeypatch.setattr(server, "_cfg", cfg)
    monkeypatch.setattr(server, "_sessions", SessionStore(str(state_dir / "harness_sessions.json")))
    monkeypatch.setattr(server, "_runners", SessionRunnerRegistry(
        on_drop=server._fold_runner_meters_into_boot_carry,
    ))
    monkeypatch.setattr(server, "_session", Session(offline))
    monkeypatch.setattr(server, "_pilot", server._build_conversational_pilot(config=offline))
    return server._sessions


@pytest.fixture
def owned_server(_isolate_provider_state, monkeypatch):
    import sys

    already_loaded = "harness.server" in sys.modules
    import harness.server as server

    if not already_loaded:
        _isolate_server_session_state(server, Path(os.environ["HARNESS_STATE_DIR"]), monkeypatch)
    session = server._sessions.create()
    server._attach_view(session["id"], load_transcript_on_create=False)
    assert server._ensure_active_pilot_ready() is server._runners.get(session["id"])
    store = server._sessions
    yield server
    store.flush()
