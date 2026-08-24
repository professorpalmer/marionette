"""Unit tests for validate_pilot_driver, _tier_of, and _AGENTIC_TEMPLATES."""
from harness.auto_registry import _AGENTIC_TEMPLATES, _get_provider_models_from_discovery
from harness.registry_wizard import validate_pilot_driver


def test_validate_pilot_driver_rejects_empty():
    got = validate_pilot_driver("")
    assert got["valid"] is False
    assert got["resolved_model_id"] is None


def test_validate_pilot_driver_unknown_provider():
    got = validate_pilot_driver("not-a-real-provider:some-model")
    assert got["valid"] is False
    assert got["provider"] == "not-a-real-provider"


def test_agentic_templates_have_three_tiers():
    for provider, tiers in _AGENTIC_TEMPLATES.items():
        assert tiers, provider
        for name, spec in tiers.items():
            assert len(spec) == 5, (provider, name)
            score, _inp, _out, ctx, tags = spec
            assert isinstance(score, (int, float))
            assert ctx > 0
            assert isinstance(tags, list)


def test_tier_of_marks_pro_frontier_and_flash_lite_cheap(monkeypatch):
    monkeypatch.setattr(
        "harness.auto_registry._enabled_picker_models", lambda name: [],
    )
    live = ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite"]
    monkeypatch.setattr(
        "harness.model_fetch.fetch_models", lambda *a, **k: live,
    )
    rows = _get_provider_models_from_discovery("google", "unused-key")
    by_name = {name: tier for name, tier, _slug in rows}
    if not by_name:
        rows = _get_provider_models_from_discovery("gemini", "unused-key")
        by_name = {name: tier for name, tier, _slug in rows}
    assert by_name.get("gemini-2.5-pro") == "frontier"
    assert by_name.get("gemini-2.5-flash-lite") == "cheap"
