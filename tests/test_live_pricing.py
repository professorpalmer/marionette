"""Tests for live model pricing resolution (cost estimator shows real $5/$25 for
Opus 4.8 etc., not a 0.5/2.0 placeholder) -- offline via a mocked price map."""
import pmharness.registry as reg


def test_resolve_live_price_exact_slug(monkeypatch):
    monkeypatch.setattr(reg, "_PRICE_MEM", {"anthropic/claude-opus-4.8": (5.0, 25.0)})
    monkeypatch.setattr(reg, "_live_windows", lambda: {})  # don't hit network
    pair = reg._resolve_live_price("anthropic:claude-opus-4-8")
    assert pair == (5.0, 25.0)


def test_resolve_live_price_fuzzy_prefers_base(monkeypatch):
    monkeypatch.setattr(reg, "_PRICE_MEM", {
        "anthropic/claude-opus-4.8": (5.0, 25.0),
        "anthropic/claude-opus-4.8-fast": (10.0, 50.0),
    })
    monkeypatch.setattr(reg, "_live_windows", lambda: {})
    pair = reg._resolve_live_price("anthropic:claude-opus-4-8")
    # Base model (shorter slug) wins over -fast variant.
    assert pair == (5.0, 25.0)


def test_resolve_price_uses_catalog_when_live_unavailable(monkeypatch):
    # claude-frontier is in the eval catalog at native 5.0/25.0.
    monkeypatch.setenv("PMHARNESS_OR_LIVE_WINDOWS", "0")
    monkeypatch.setattr(reg, "_PRICE_MEM", {})
    pin, pout = reg.resolve_price("claude-frontier")
    assert (pin, pout) == (5.0, 25.0)


def test_resolve_price_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(reg, "_PRICE_MEM", {})
    monkeypatch.setattr(reg, "_live_windows", lambda: {})
    pin, pout = reg.resolve_price("totally-unknown-model-xyz", default_in=0.5, default_out=2.0)
    assert (pin, pout) == (0.5, 2.0)


def test_resolve_price_live_for_picker_spec(monkeypatch):
    monkeypatch.setattr(reg, "_PRICE_MEM", {"openai/gpt-5.5": (5.0, 30.0)})
    monkeypatch.setattr(reg, "_live_windows", lambda: {})
    pin, pout = reg.resolve_price("openrouter:openai/gpt-5.5")
    assert (pin, pout) == (5.0, 30.0)


def test_price_cache_roundtrip_restores_prices(monkeypatch):
    # Prices persisted in the disk cache must restore into _PRICE_MEM.
    monkeypatch.setattr(reg, "_PRICE_MEM", {})
    disk = {"prices": {"anthropic/claude-opus-4.8": [5.0, 25.0]}}
    reg._restore_prices_from_disk(disk)
    assert reg._PRICE_MEM.get("anthropic/claude-opus-4.8") == (5.0, 25.0)
