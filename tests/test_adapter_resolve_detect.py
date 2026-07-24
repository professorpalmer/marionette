"""_detect_default_implement_adapter: platform_lock over CLI scrape + avail cache."""

from __future__ import annotations

import harness.adapter_resolve as ar
from harness.adapter_resolve import AdapterResolveMixin, clear_adapter_avail_cache


class _Host(AdapterResolveMixin):
    pass


def test_detect_uses_platform_lock_not_subprocess(monkeypatch):
    host = _Host()
    calls = {"n": 0}

    def boom(*_a, **_k):
        calls["n"] += 1
        raise AssertionError("subprocess should not run when platform_lock works")

    monkeypatch.setattr(ar.subprocess, "run", boom)
    monkeypatch.setattr(
        "puppetmaster.platform_lock.enabled_adapters",
        lambda: ["cursor", "agentic"],
    )
    monkeypatch.setattr(
        "puppetmaster.platform_lock.is_adapter_enabled",
        lambda name: name in ("cursor", "agentic"),
    )
    assert host._detect_default_implement_adapter() == "agentic"
    assert calls["n"] == 0


def test_detect_prefers_hermes_when_agentic_off(monkeypatch):
    host = _Host()
    monkeypatch.setattr(
        "puppetmaster.platform_lock.enabled_adapters",
        lambda: ["hermes", "cursor"],
    )
    monkeypatch.setattr(
        "puppetmaster.platform_lock.is_adapter_enabled",
        lambda name: name in ("hermes", "cursor"),
    )
    monkeypatch.setattr(host, "_external_adapter_available", lambda a: a == "hermes")
    assert host._detect_default_implement_adapter() == "hermes"


def test_external_adapter_avail_cache_hits(monkeypatch):
    clear_adapter_avail_cache()
    host = _Host()
    which_calls = {"n": 0}

    def counting_which(name):
        which_calls["n"] += 1
        return "/bin/cursor" if name == "cursor" else None

    monkeypatch.setattr(
        "puppetmaster.platform_lock.KNOWN_ADAPTERS",
        frozenset({"cursor", "agentic"}),
    )
    monkeypatch.setattr(
        "puppetmaster.platform_lock.is_adapter_enabled",
        lambda name: True,
    )
    monkeypatch.setattr("shutil.which", counting_which)
    clock = {"t": 100.0}
    monkeypatch.setattr(ar.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(ar, "_AVAIL_CACHE_TTL_SECONDS", 20.0)

    assert host._external_adapter_available("cursor") is True
    assert which_calls["n"] == 1
    clock["t"] = 110.0
    assert host._external_adapter_available("cursor") is True
    assert which_calls["n"] == 1  # cache hit
    clock["t"] = 121.0
    assert host._external_adapter_available("cursor") is True
    assert which_calls["n"] == 2  # TTL expired
    clear_adapter_avail_cache()
