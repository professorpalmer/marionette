"""Guard the live-streaming wiring in pmharness.bridge._install_delta_sink.

The swarm streaming feature hangs on a tiny contract: bridge registers an
``on_delta(worker_id, kind, text)`` callback as Puppetmaster's *broadcast* delta
sink, the agentic adapter fans every worker's tokens to it, and the returned
cleanup clears the global sink so it never leaks across runs. This test locks
that contract so a refactor of either side fails loudly instead of silently
falling back to the blocking (no live tokens) path.
"""

import pytest

from pmharness.bridge import _install_delta_sink

# The streaming bus is the whole point; skip only if puppetmaster isn't present.
_delta_bus = pytest.importorskip("puppetmaster.adapters._delta_bus")


def _current_broadcast():
    return _delta_bus._broadcast


def test_none_sink_is_noop_and_leaves_bus_untouched():
    _delta_bus.set_broadcast_sink(None)
    cleanup = _install_delta_sink(None)
    assert _current_broadcast() is None, "None on_delta must not register a sink"
    cleanup()  # must be safe to call
    assert _current_broadcast() is None


def test_sink_registers_and_cleanup_clears():
    _delta_bus.set_broadcast_sink(None)
    seen: list[tuple[str, str, str]] = []

    cleanup = _install_delta_sink(lambda wid, kind, text: seen.append((wid, kind, text)))
    try:
        registered = _current_broadcast()
        assert registered is not None, "on_delta must be installed as the broadcast sink"

        # The adapter delivers per-worker deltas via delta_sink_for(worker_id),
        # which routes through the broadcast sink as (worker_id, kind, text).
        adapter_sink = _delta_bus.delta_sink_for("worker-7")
        assert adapter_sink is not None
        adapter_sink("text", "hello")
        assert seen == [("worker-7", "text", "hello")]
    finally:
        cleanup()

    assert _current_broadcast() is None, "cleanup must clear the global sink"
    assert _delta_bus.delta_sink_for("worker-7") is None
