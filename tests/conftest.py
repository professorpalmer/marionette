"""Test isolation: block real network by default so a test that accidentally
makes a live API call fails FAST instead of hanging the whole suite.

Tests that legitimately need the network (the live-key analysis/eval tests) opt
back in with @pytest.mark.network, and are deselected by default in CI runs.
"""
import socket
import os
import pytest

os.environ["PMHARNESS_MCP_ALLOW_PRIVATE"] = "1"

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
