"""OpenCode Zen: registration, key reuse, live catalog, Ox Alpha, routing."""
import json

import pytest

import harness.model_fetch as mf
from harness import keys as hkeys
from harness import model_visibility as mv
from harness import opencode_go as go
from harness import opencode_zen as zen
from harness import providers as prov
from harness.auto_registry import is_dated_or_noisy_preview


@pytest.fixture(autouse=True)
def _keyed_opencode_zen(monkeypatch, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    (state / "keys.json").write_text("{}", encoding="utf-8")
    (state / "disconnected.json").write_text("[]", encoding="utf-8")
    monkeypatch.setenv(zen.API_KEY_ENV, "sk-zen-test")
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.delenv(go.API_KEY_ENV, raising=False)
    monkeypatch.setenv("PMHARNESS_LIVE_MODELS", "0")
    monkeypatch.setattr(mf, "_cache_path", lambda: str(tmp_path / "models_cache.json"))
    mf._MEM.clear()
    mf._RECORD_MEM.clear()
    mf._MEM_AT.clear()
    mf._LAST_ERROR.clear()
    yield
    mf._MEM.clear()
    mf._RECORD_MEM.clear()
    mf._MEM_AT.clear()
    mf._LAST_ERROR.clear()


def test_ox_alpha_is_zen_not_go():
    assert "x-preview-f-free" in zen.CURATED_FREE_MODELS
    assert zen.display_name_for("x-preview-f-free") == "Ox Alpha Free"
    assert "x-preview-f-free" not in go.CURATED_MODELS
    assert "ox-alpha-free" not in go.CURATED_MODELS
    assert "ox-alpha-free" not in zen.CURATED_FREE_MODELS
    assert go.display_name_for("ox-alpha-free") == "Ox Alpha Free"


def test_zen_display_name_treats_wire_echo_as_uninformative():
    assert zen.display_name_for(
        "x-preview-f-free", {"name": "x-preview-f-free"},
    ) == "Ox Alpha Free"
    assert zen.display_name_for(
        "x-preview-f-free", {"name": "X-PREVIEW-F-FREE"},
    ) == "Ox Alpha Free"
    assert zen.display_name_for(
        "x-preview-f-free", {"name": "Custom Ox"},
    ) == "Custom Ox"


def test_curated_fallback_is_verified_free_ids_only():
    for model in zen.CURATED_FREE_MODELS:
        assert "/" not in model
        assert model == model.lower()
        assert model.endswith("-free") or model in {"x-preview-f-free", "big-pickle"}


def test_curated_fallback_includes_all_verified_live_free_ids():
    """Complete verified free fallback from current Zen /models probe."""
    expected = {
        "x-preview-f-free",
        "big-pickle",
        "mimo-v2.5-free",
        "hy3-free",
        "deepseek-v4-flash-free",
        "laguna-s-2.1-free",
        "nemotron-3-ultra-free",
        "nemotron-3.5-lightning-free",
        "muse-spark-1.2-contributor-free",
    }
    assert set(zen.CURATED_FREE_MODELS) == expected
    assert zen.display_name_for("deepseek-v4-flash-free") == "DeepSeek V4 Flash Free"
    assert zen.display_name_for("laguna-s-2.1-free") == "Laguna S 2.1 Free"


def test_provider_is_registered_with_key_reuse():
    p = prov.get_provider("opencode-zen")
    assert p is not None
    assert p.env_vars == zen.API_KEY_ENVS
    assert p.api_mode == "opencode_zen"
    assert p.base_url == zen.BASE_URL
    assert p.display_name == "OpenCode Zen"
    assert prov.get_provider("zen") is p


def test_zen_reuses_go_account_key(monkeypatch):
    monkeypatch.delenv(zen.API_KEY_ENV, raising=False)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.setenv(go.API_KEY_ENV, "sk-go-shared")
    p = prov.get_provider("opencode-zen")
    assert p.key() == "sk-go-shared"
    assert p.key_env() == go.API_KEY_ENV
    names = [row.name for row in prov.available_providers()]
    assert "opencode-zen" in names
    assert "opencode-go" in names


def test_zen_prefers_its_own_key_over_go(monkeypatch):
    monkeypatch.setenv(zen.API_KEY_ENV, "sk-zen-own")
    monkeypatch.setenv(go.API_KEY_ENV, "sk-go-shared")
    p = prov.get_provider("opencode-zen")
    assert p.key() == "sk-zen-own"
    assert p.key_env() == zen.API_KEY_ENV


def test_live_models_are_normalized_and_keep_names(monkeypatch):
    p = prov.get_provider("opencode-zen")
    monkeypatch.setattr(mf, "_get", lambda url, headers: {
        "data": [
            {"id": "opencode/x-preview-f-free", "name": "Ox Alpha Free"},
            {"id": "big-pickle"},
        ],
    })
    assert mf._fetch_provider_models(p, "sk-zen-test") == [
        "x-preview-f-free", "big-pickle",
    ]
    records = mf._fetch_opencode_records(p, "sk-zen-test")
    by_id = {row["id"]: row for row in records}
    assert by_id["x-preview-f-free"]["name"] == "Ox Alpha Free"
    assert mf.last_fetch_error("opencode-zen") is None


def test_unreachable_listing_falls_back_to_curated_free(monkeypatch):
    p = prov.get_provider("opencode-zen")

    def _offline(url, headers):
        raise RuntimeError("simulated connection refused")

    monkeypatch.setattr(mf, "_get", _offline)
    assert mf._fetch_provider_models(p, "sk-zen-test") == []
    monkeypatch.setattr(mf, "fetch_models", lambda provider, key, **kw: [])
    models = mv.provider_models(p)
    assert models == list(zen.CURATED_FREE_MODELS)
    assert "x-preview-f-free" in models
    assert "gpt-5.5" not in models


def test_live_listing_does_not_advertise_non_live_paid(monkeypatch):
    p = prov.get_provider("opencode-zen")
    monkeypatch.setattr(
        mf, "fetch_models",
        lambda provider, key, **kw: ["x-preview-f-free", "big-pickle"],
    )
    models = mv.provider_models(p)
    assert models == ["x-preview-f-free", "big-pickle"]
    assert "gpt-5.5" not in models
    assert "claude-sonnet-5" not in models


def test_ox_alpha_routes_through_chat_completions():
    assert zen.api_mode_for_model("x-preview-f-free") == zen.CHAT_COMPLETIONS
    assert zen.api_mode_for_model("opencode-zen/x-preview-f-free") == zen.CHAT_COMPLETIONS


def test_zen_gpt_and_claude_follow_the_endpoint_table():
    assert zen.api_mode_for_model("gpt-5.5") == zen.OPENAI_RESPONSES
    assert zen.api_mode_for_model("claude-sonnet-4-6") == zen.ANTHROPIC_MESSAGES
    assert zen.api_mode_for_model("qwen3.7-plus") == zen.ANTHROPIC_MESSAGES
    assert zen.api_mode_for_model("unknown-new-model") == zen.CHAT_COMPLETIONS


def test_build_pilot_routes_ox_alpha_to_chat(monkeypatch):
    from pmharness.drivers.openai_compat import OpenAICompatDriver

    driver = prov.build_pilot("opencode-zen:x-preview-f-free")
    assert isinstance(driver, OpenAICompatDriver)
    assert driver.base_url == zen.BASE_URL
    assert driver.model == "x-preview-f-free"
    assert driver.api_key_env == zen.API_KEY_ENV


def test_build_pilot_uses_go_key_env_when_zen_key_absent(monkeypatch):
    """Driver must read the env var that actually holds the token."""
    from pmharness.drivers.openai_compat import OpenAICompatDriver

    monkeypatch.delenv(zen.API_KEY_ENV, raising=False)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.setenv(go.API_KEY_ENV, "sk-go-shared")
    driver = prov.build_pilot("opencode-zen:big-pickle")
    assert isinstance(driver, OpenAICompatDriver)
    assert driver.api_key_env == go.API_KEY_ENV


def test_catalog_exposes_ox_alpha_friendly_name(monkeypatch):
    monkeypatch.setattr(
        prov, "available_providers",
        lambda: [prov.get_provider("opencode-zen")],
    )
    monkeypatch.setattr(
        mf, "fetch_models",
        lambda provider, key, **kw: ["x-preview-f-free"],
    )
    monkeypatch.setattr(
        mf, "model_metadata",
        lambda provider, slug: (
            {"id": slug, "name": "Ox Alpha Free", "source": "live"}
            if slug == "x-preview-f-free" else None
        ),
    )
    entries = [
        row for row in mv.catalog(available_only=True)
        if row["provider"] == "opencode-zen"
    ]
    ox = next(row for row in entries if row["model"] == "x-preview-f-free")
    assert ox["spec"] == "opencode-zen:x-preview-f-free"
    assert ox["name"] == "Ox Alpha Free"


def test_catalog_replaces_zen_wire_echo_name_with_ox_label(monkeypatch):
    monkeypatch.setattr(
        prov, "available_providers",
        lambda: [prov.get_provider("opencode-zen")],
    )
    monkeypatch.setattr(
        mf, "fetch_models",
        lambda provider, key, **kw: ["x-preview-f-free"],
    )
    monkeypatch.setattr(
        mf, "model_metadata",
        lambda provider, slug: {"id": slug, "name": slug},
    )
    seen = {}

    def fake_overlay(model, *, allow_network=False):
        seen["called"] = True
        return {"name": "Ox Alpha Free", "source": "models.dev"}

    monkeypatch.setattr(zen, "overlay_metadata", fake_overlay)
    entries = [
        row for row in mv.catalog(available_only=True)
        if row["provider"] == "opencode-zen"
    ]
    ox = next(row for row in entries if row["model"] == "x-preview-f-free")
    assert seen.get("called") is True
    assert ox["name"] == "Ox Alpha Free"


def test_catalog_keeps_informative_zen_native_name(monkeypatch):
    monkeypatch.setattr(
        prov, "available_providers",
        lambda: [prov.get_provider("opencode-zen")],
    )
    monkeypatch.setattr(
        mf, "fetch_models",
        lambda provider, key, **kw: ["x-preview-f-free"],
    )
    monkeypatch.setattr(
        mf, "model_metadata",
        lambda provider, slug: {"id": slug, "name": "Native Ox Label"},
    )
    seen = []

    def fake_overlay(model, *, allow_network=False):
        seen.append(model)
        return {"name": "Overlay Must Not Win"}

    monkeypatch.setattr(zen, "overlay_metadata", fake_overlay)
    entries = [
        row for row in mv.catalog(available_only=True)
        if row["provider"] == "opencode-zen"
    ]
    ox = next(row for row in entries if row["model"] == "x-preview-f-free")
    assert ox["name"] == "Native Ox Label"
    assert seen == []


def test_preview_filter_keeps_ox_alpha_drops_dated_snapshots():
    assert is_dated_or_noisy_preview("x-preview-f-free") is False
    assert is_dated_or_noisy_preview("gemini-2.5-flash-preview-04-17") is True
    assert is_dated_or_noisy_preview("foo-preview-0813") is True
    assert is_dated_or_noisy_preview("foo-preview-20240614") is True
    assert is_dated_or_noisy_preview("gemini-3-pro-preview") is False


def test_explicit_enabled_set_does_not_auto_enable_ox(monkeypatch, tmp_path):
    store = tmp_path / "models.json"
    monkeypatch.setattr(mv, "_store_path", lambda: str(store))
    monkeypatch.setattr(
        prov, "available_providers",
        lambda: [prov.get_provider("opencode-zen")],
    )
    mv.set_enabled(["opencode-zen:big-pickle"])
    monkeypatch.setattr(
        mf, "fetch_models",
        lambda provider, key, **kw: ["x-preview-f-free", "big-pickle"],
    )
    cat = [
        row for row in mv.catalog(available_only=True)
        if row["provider"] == "opencode-zen"
    ]
    by_model = {row["model"]: row for row in cat}
    assert by_model["x-preview-f-free"]["enabled"] is False
    assert by_model["big-pickle"]["enabled"] is True
    pilots = mv.enabled_pilots()
    assert "opencode-zen:x-preview-f-free" not in pilots
    assert "opencode-zen:big-pickle" in pilots


def test_persist_imports_generic_opencode_key_not_go_key(monkeypatch):
    monkeypatch.delenv(zen.API_KEY_ENV, raising=False)
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-oc-generic")
    monkeypatch.setenv(go.API_KEY_ENV, "sk-go-shared")
    open(hkeys.get_keys_file_path(), "w", encoding="utf-8").write("{}")
    imported = hkeys.persist_env_api_keys()
    assert "opencode-zen" in imported
    with open(hkeys.get_keys_file_path(), encoding="utf-8") as f:
        stored = json.load(f)
    assert stored["opencode-zen"] == "sk-oc-generic"
    assert stored.get("opencode-go") == "sk-go-shared"


def test_persist_does_not_copy_go_key_into_zen_identity(monkeypatch):
    monkeypatch.delenv(zen.API_KEY_ENV, raising=False)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.setenv(go.API_KEY_ENV, "sk-go-shared")
    open(hkeys.get_keys_file_path(), "w", encoding="utf-8").write("{}")
    imported = hkeys.persist_env_api_keys()
    assert "opencode-zen" not in imported
    with open(hkeys.get_keys_file_path(), encoding="utf-8") as f:
        stored = json.load(f)
    assert "opencode-zen" not in stored
    assert stored.get("opencode-go") == "sk-go-shared"
    assert prov.get_provider("opencode-zen").key() == "sk-go-shared"


def test_set_api_key_persists_zen_identity(monkeypatch):
    monkeypatch.delenv(zen.API_KEY_ENV, raising=False)
    hkeys.set_api_key("opencode-zen", "sk-zen-stored")
    import os

    assert os.environ[zen.API_KEY_ENV] == "sk-zen-stored"
    with open(hkeys.get_keys_file_path(), encoding="utf-8") as f:
        assert json.load(f)["opencode-zen"] == "sk-zen-stored"


def test_credential_pool_maps_zen_key_env():
    from harness import credential_pool as cp

    assert cp.provider_for_env_var("OPENCODE_ZEN_API_KEY") == "opencode-zen"
    assert cp.env_var_for_provider("opencode-zen") == "OPENCODE_ZEN_API_KEY"
    assert "opencode-zen" in cp.known_pool_providers()
