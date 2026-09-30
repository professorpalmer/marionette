"""Drivers report each finished model call exactly once, as a fact."""
from __future__ import annotations

import threading

import pytest

from pmharness.drivers.base import DriverResponse
from pmharness.drivers.metering import attribution, metered, register_sink


@metered
class _Driver:
    name = "fake"
    model = "vendor/model-x"
    base_url = "https://api.example.test/v1"

    def chat_stream(self, messages, **kw):
        return DriverResponse(text="hi", tokens_in=1000, tokens_out=50, meta={
            "cache_read_tokens": 900, "cache_write_tokens": 20, "reasoning_tokens": 7,
            "provider_cost_usd": 0.0123, "served_model": "vendor/model-x-2026",
        })

    def chat(self, messages, **kw):
        # Delegates to its own streaming path: still ONE call.
        return self.chat_stream(messages, **kw)

    def complete(self, prompt, **kw):
        return DriverResponse(text="ok", tokens_in=10, tokens_out=2)


@pytest.fixture
def calls():
    seen = []
    unregister = register_sink(seen.append)
    yield seen
    unregister()


def test_one_report_per_outer_call_with_normalized_usage(calls):
    _Driver().chat([{"role": "user", "content": "x"}])
    assert len(calls) == 1
    call = calls[0]
    assert (call.tokens_in, call.tokens_out, call.cache_read_tokens, call.cache_write_tokens) == (1000, 50, 900, 20)
    assert call.reasoning_tokens == 7
    assert call.provider_cost_usd == pytest.approx(0.0123)
    assert call.served_model == "vendor/model-x-2026"
    assert call.base_url == "https://api.example.test/v1"


def test_attribution_is_ambient_and_nests(calls):
    driver = _Driver()
    with attribution(session_id="s1", purpose="pilot", turn=3):
        with attribution(purpose="compaction"):
            driver.complete("p")
        driver.complete("p")
    driver.complete("p")
    assert [c.attribution.get("purpose") for c in calls] == ["compaction", "pilot", None]
    assert calls[0].attribution["session_id"] == "s1" and calls[0].attribution["turn"] == 3
    assert calls[2].attribution == {}


def test_type_identity_is_preserved():
    driver = _Driver()
    assert type(driver) is _Driver and isinstance(driver, _Driver)


def test_a_failing_sink_never_breaks_the_call(calls):
    def boom(_call):
        raise RuntimeError("sink down")

    unregister = register_sink(boom)
    try:
        assert _Driver().complete("p").text == "ok"
    finally:
        unregister()
    assert len(calls) == 1


def test_threads_carry_attribution_only_when_the_context_is_copied(calls):
    import contextvars

    driver = _Driver()
    with attribution(session_id="s2"):
        ctx = contextvars.copy_context()
        worker = threading.Thread(target=ctx.run, args=(driver.complete, "p"))
        worker.start()
        worker.join()
    assert calls[0].attribution == {"session_id": "s2"}
