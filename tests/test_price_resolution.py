"""Price resolution: live OpenRouter rates win over the offline catalog."""
import pmharness.registry as reg


def test_price_catalog_fallback_when_live_disabled(monkeypatch):
    monkeypatch.setenv("PMHARNESS_OR_LIVE_WINDOWS", "0")
    monkeypatch.setattr(reg, "_PRICE_MEM", {})
    pin, pout = reg.price("glm-5.2")
    assert pin == 1.40 and pout == 4.40


def test_price_live_wins_over_catalog(monkeypatch):
    monkeypatch.setenv("PMHARNESS_OR_LIVE_WINDOWS", "0")
    monkeypatch.setattr(reg, "_resolve_live_price", lambda name: (0.55, 2.19))
    pin, pout = reg.price("glm-5.2")
    assert (pin, pout) == (0.55, 2.19)
