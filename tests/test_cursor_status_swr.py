"""An expired Cursor CLI status never blocks the caller on a Node spawn."""
import threading
import time

import harness.cursor_cli_auth as cca


def _fresh(monkeypatch):
    monkeypatch.setattr(cca, "_status_cache", None)
    monkeypatch.setattr(cca, "_status_cache_at", 0.0)
    monkeypatch.setattr(cca, "_status_generation", 0)
    monkeypatch.setattr(cca, "_status_refreshing", False)


def test_expired_status_is_served_while_refreshing_in_background(monkeypatch):
    _fresh(monkeypatch)
    release = threading.Event()
    probes = []

    def probe():
        probes.append(threading.current_thread().name)
        if len(probes) > 1:
            release.wait(5)
        return {"authenticated": len(probes) > 1}

    monkeypatch.setattr(cca, "_get_status_uncached", probe)
    assert cca.get_status()["authenticated"] is False  # cold cache blocks once
    monkeypatch.setattr(cca, "_status_cache_at", time.monotonic() - cca._STATUS_CACHE_TTL - 1)
    started = time.monotonic()
    assert cca.get_status()["authenticated"] is False  # stale, returned at once
    assert time.monotonic() - started < 0.5
    release.set()
    deadline = time.monotonic() + 5
    while cca.get_status()["authenticated"] is False and time.monotonic() < deadline:
        time.sleep(0.01)
    assert cca.get_status()["authenticated"] is True
    assert probes[1] == "cursor-cli-status"


def test_invalidation_discards_an_in_flight_refresh(monkeypatch):
    _fresh(monkeypatch)
    release = threading.Event()
    monkeypatch.setattr(cca, "_get_status_uncached", lambda: {"authenticated": True})
    cca.get_status()
    monkeypatch.setattr(cca, "_status_cache_at", time.monotonic() - cca._STATUS_CACHE_TTL - 1)

    def slow():
        release.wait(5)
        return {"authenticated": True}

    monkeypatch.setattr(cca, "_get_status_uncached", slow)
    cca.get_status()  # starts the background refresh
    cca.invalidate_status_cache()  # logout lands while it runs
    release.set()
    time.sleep(0.2)
    assert cca._status_cache is None
