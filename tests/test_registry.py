"""Registry + catalog integrity (no network, no keys)."""
import json
from pathlib import Path

from pmharness import registry as reg
from pmharness.drivers.stub import StubDriver


def test_catalog_loads_and_is_well_formed():
    cat = reg.load_catalog()
    assert cat["models"]
    required = {"name", "tier", "license", "price_in", "price_out", "openrouter"}
    for m in cat["models"]:
        assert required <= set(m), f"{m.get('name')} missing keys"
        assert m["tier"] in ("flagship", "value", "frontier_control")


def test_minimax_present_and_open():
    names = reg.model_names()
    assert any("minimax" in n for n in names), "MiniMax must be in the registry"


def test_tiers_populated():
    assert reg.model_names("flagship")
    assert reg.model_names("value")
    assert reg.model_names("frontier_control")


def test_price_lookup():
    pin, pout = reg.price("glm-5.2")
    assert pin == 1.40 and pout == 4.40


def test_build_stub_needs_no_key():
    d = reg.build("stub-oracle")
    assert isinstance(d, StubDriver)


def test_build_openrouter_driver_constructs():
    # constructs without a key (key only read at call time)
    d = reg.build("kimi-k2.6", reach="openrouter")
    assert d.name == "kimi-k2.6"
    assert "openrouter.ai" in d.base_url


def test_build_native_driver_constructs():
    d = reg.build("glm-5.2", reach="native")
    assert "z.ai" in d.base_url


def test_native_unavailable_raises():
    import pytest
    with pytest.raises(ValueError):
        reg.build("glm-4.7-flash", reach="native")  # native=None in catalog


def test_all_driver_names_includes_stub():
    assert "stub-oracle" in reg.all_driver_names()
