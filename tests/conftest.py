"""Test isolation: block real network by default so a test that accidentally
makes a live API call fails FAST instead of hanging the whole suite.

Tests that legitimately need the network (the live-key analysis/eval tests) opt
back in with @pytest.mark.network, and are deselected by default in CI runs.
"""
import socket
import os
import shutil
import stat
import tempfile
import pytest

os.environ["PMHARNESS_MCP_ALLOW_PRIVATE"] = "1"

# Windows: git marks object files read-only, which makes shutil.rmtree fail
# with PermissionError on every temp-git-repo teardown. Wrap rmtree once here
# (instead of touching dozens of per-test cleanup calls) with an error handler
# that clears the read-only bit and retries the failed delete.
if os.name == "nt":
    _real_rmtree = shutil.rmtree

    def _clear_readonly_and_retry(func, path, _exc_info):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception:
            pass

    def _rmtree_windows(path, ignore_errors=False, onerror=None, **kwargs):
        if onerror is None and not kwargs.get("onexc"):
            onerror = _clear_readonly_and_retry
        return _real_rmtree(path, ignore_errors=ignore_errors, onerror=onerror, **kwargs)

    shutil.rmtree = _rmtree_windows

# Seal off the developer's real ~/.pmharness BEFORE pytest collects (imports)
# any test module. Several test modules import harness.server at top level, and
# that import writes a fresh auth token + touches workspace/marker files. Frozen
# to real home, those writes clobbered the running app's live token (renderer
# and backend then disagreed -> every request 403'd -> "the backend died") and
# corrupted workspace.json with a vanished temp repo. Setting the state dir here
# -- before collection -- guarantees every such write lands in a throwaway dir.
# The per-test fixture below layers a fresh subdir on top for finer isolation.
os.environ.setdefault(
    "HARNESS_STATE_DIR", tempfile.mkdtemp(prefix="pmharness-test-state-")
)

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


def pytest_configure(config):
    config.addinivalue_line("markers", "network: test requires real network access")
    config.addinivalue_line("markers", "swarm: test drives real Puppetmaster (slow subprocess spawns)")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--swarm"):
        return
    skip = pytest.mark.skip(reason="real-Puppetmaster swarm test; run with --swarm")
    for item in items:
        if item.get_closest_marker("swarm"):
            item.add_marker(skip)


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
    test behavior (auto-verify, edit review, distill, hash-edit, etc.)."""
    for _var in (
        "HARNESS_MAX_PILOT_STEPS",
        "HARNESS_AUTO_COMMAND_GUARD",
        "HARNESS_AUTO_DISTILL",
        "HARNESS_REVIEW_EDITS_BEFORE_APPLY",
        "HARNESS_AUTO_VERIFY",
        "HARNESS_HASH_EDIT",
        "HARNESS_VERIFY_COMMAND",
        "HARNESS_COMMAND_TIMEOUT",
    ):
        monkeypatch.delenv(_var, raising=False)


@pytest.fixture(autouse=True)
def _isolate_provider_state(monkeypatch, tmp_path_factory):
    """Give every test its own clean state dir, isolated from the developer's
    real ~/.pmharness (keys.json / disconnected.json / workspace.json / token).
    A collection-safe default is already set at module import; this layers a
    fresh per-test subdir on top so state never bleeds between tests. Tests that
    set their own HARNESS_STATE_DIR in the test body still override it there."""
    d = tmp_path_factory.mktemp("pmstate")
    monkeypatch.setenv("HARNESS_STATE_DIR", str(d))
