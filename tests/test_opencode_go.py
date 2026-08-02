"""OpenCode Go: catalog, flat-id normalization, per-model endpoint routing,
model-specific reasoning/ceiling policy, key persistence, and picker visibility.

Go is a reseller whose wire protocol changes per model, so the interesting
invariants are all about picking the right driver for a bare id -- not about
one provider-wide api_mode.
"""
import json

import pytest

import harness.model_fetch as mf
from harness import keys as hkeys
from harness import model_visibility as mv
from harness import opencode_go as go
from harness import providers as prov


@pytest.fixture(autouse=True)
def _keyed_opencode_go(monkeypatch, tmp_path):
    """A configured Go subscription with no live catalog and no disk cache."""
    monkeypatch.setenv(go.API_KEY_ENV, "sk-go-test")
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


# ── Catalog ────────────────────────────────────────────────────────────────

def test_curated_catalog_is_flat_and_current():
    """Go ids are bare and dotted; a vendor namespace would 404 the relay."""
    assert go.CURATED_MODELS
    for model in go.CURATED_MODELS:
        assert "/" not in model, f"{model} carries a vendor namespace"
        assert model == model.lower()


def test_deepseek_entry_is_v4_flash_not_a_stale_alias():
    """The Go DeepSeek build is DeepSeek-V4-Flash-0731; v3/v2 were never Go."""
    assert "deepseek-v4-flash" in go.CURATED_MODELS
    stale = [m for m in go.CURATED_MODELS if m.startswith(("deepseek-v3", "deepseek-v2"))]
    assert stale == []


def test_curated_catalog_covers_the_published_endpoint_table():
    for model in (
        "grok-4.5", "gpt-5.6-luna", "glm-5.2", "glm-5.1", "kimi-k3",
        "kimi-k2.7-code", "kimi-k2.6", "mimo-v2.5", "mimo-v2.5-pro",
        "minimax-m3", "minimax-m2.7", "qwen3.7-max", "qwen3.7-plus",
        "qwen3.6-plus", "deepseek-v4-pro", "deepseek-v4-flash", "hy3",
    ):
        assert model in go.CURATED_MODELS


# ── Flat-namespace normalization ───────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("kimi-k3", "kimi-k3"),
    ("opencode-go/kimi-k3", "kimi-k3"),
    ("moonshotai/kimi-k2.7-code", "kimi-k2.7-code"),
    ("  deepseek-v4-flash  ", "deepseek-v4-flash"),
    ("z-ai/glm-5.2", "glm-5.2"),
    ("", ""),
    (None, ""),
])
def test_normalize_model_id(raw, expected):
    assert go.normalize_model_id(raw) == expected


def test_normalize_model_id_preserves_dots():
    assert go.normalize_model_id("opencode-go/mimo-v2.5-pro") == "mimo-v2.5-pro"


# ── Endpoint routing ───────────────────────────────────────────────────────

@pytest.mark.parametrize("model,mode", [
    ("glm-5.2", go.CHAT_COMPLETIONS),
    ("kimi-k3", go.CHAT_COMPLETIONS),
    ("deepseek-v4-flash", go.CHAT_COMPLETIONS),
    ("mimo-v2.5-pro", go.CHAT_COMPLETIONS),
    ("grok-4.5", go.CHAT_COMPLETIONS),
    ("hy3", go.CHAT_COMPLETIONS),
    ("minimax-m3", go.ANTHROPIC_MESSAGES),
    ("minimax-m2.7", go.ANTHROPIC_MESSAGES),
    ("qwen3.7-max", go.ANTHROPIC_MESSAGES),
    ("qwen3.6-plus", go.ANTHROPIC_MESSAGES),
    ("gpt-5.6-luna", go.OPENAI_RESPONSES),
    ("opencode-go/gpt-5.6-luna", go.OPENAI_RESPONSES),
    ("", go.CHAT_COMPLETIONS),
])
def test_api_mode_for_model(model, mode):
    assert go.api_mode_for_model(model) == mode


def test_every_curated_model_routes_somewhere_known():
    modes = {go.api_mode_for_model(m) for m in go.CURATED_MODELS}
    assert modes <= {go.CHAT_COMPLETIONS, go.ANTHROPIC_MESSAGES, go.OPENAI_RESPONSES}


def test_driver_base_url_heals_a_stripped_v1_suffix():
    """An anthropic-routed config can persist a /v1-stripped base; all three
    Marionette drivers append only the endpoint segment and need /v1 back."""
    assert go.driver_base_url("https://opencode.ai/zen/go") == go.BASE_URL
    assert go.driver_base_url(go.BASE_URL) == go.BASE_URL
    assert go.driver_base_url(go.BASE_URL + "/") == go.BASE_URL
    assert go.driver_base_url(None) == go.BASE_URL


def test_driver_base_url_leaves_custom_relays_alone():
    assert go.driver_base_url("https://relay.internal/go") == "https://relay.internal/go"


# ── Model-specific ceilings and reasoning dialects ─────────────────────────

def test_mimo_pro_output_ceiling_is_clamped_to_what_xiaomi_serves():
    assert go.max_tokens_for_model("mimo-v2.5-pro", 262144) == 131072
    assert go.max_tokens_for_model("opencode-go/mimo-v2.5-pro", 262144) == 131072
    # A smaller request is honored as-is.
    assert go.max_tokens_for_model("mimo-v2.5-pro", 8000) == 8000
    # Other models keep whatever the caller asked for.
    assert go.max_tokens_for_model("kimi-k3", 262144) == 262144


@pytest.mark.parametrize("effort,expected", [
    ("none", {}),
    ("low", {"reasoning_effort": "high"}),
    ("high", {"reasoning_effort": "high"}),
    ("xhigh", {"reasoning_effort": "max"}),
    ("max", {"reasoning_effort": "max"}),
])
def test_glm_5_2_reasoning_collapses_onto_its_two_enabled_levels(effort, expected):
    assert go.reasoning_body_extras("glm-5.2", effort) == expected


def test_glm_5_2_alias_spellings_are_recognized():
    for alias in ("glm-5-2", "glm-5p2", "opencode-go/glm-5.2"):
        assert go.reasoning_body_extras(alias, "high") == {"reasoning_effort": "high"}


@pytest.mark.parametrize("effort,expected", [
    ("none", {"thinking": {"type": "disabled"}}),
    ("low", {"reasoning_effort": "low"}),
    ("medium", {"reasoning_effort": "medium"}),
    ("xhigh", {"reasoning_effort": "high"}),
])
def test_kimi_k2_uses_moonshots_native_thinking_dialect(effort, expected):
    assert go.reasoning_body_extras("kimi-k2.6", effort) == expected


@pytest.mark.parametrize("effort,expected", [
    ("none", {"thinking": {"type": "disabled"}}),
    ("medium", {"reasoning_effort": "medium"}),
    ("max", {"reasoning_effort": "max"}),
])
def test_deepseek_v4_reasoning(effort, expected):
    assert go.reasoning_body_extras("deepseek-v4-flash", effort) == expected


def test_reasoning_extras_never_send_thinking_and_effort_together():
    """The relay 400s on 'cannot specify both thinking and reasoning_effort'."""
    for model in ("kimi-k2.6", "deepseek-v4-pro", "glm-5.2"):
        for effort in ("none", "low", "medium", "high", "xhigh", "max"):
            extras = go.reasoning_body_extras(model, effort)
            assert not ("thinking" in extras and "reasoning_effort" in extras)


def test_models_without_a_reasoning_knob_send_nothing_extra():
    for model in ("mimo-v2.5-pro", "grok-4.5", "hy3", "minimax-m3"):
        assert go.reasoning_body_extras(model, "high") == {}


# ── Provider registration + driver selection ───────────────────────────────

def test_provider_is_registered_with_its_own_key_env():
    p = prov.get_provider("opencode-go")
    assert p is not None
    assert p.env_vars == ("OPENCODE_GO_API_KEY",)
    assert p.api_mode == "opencode_go"
    assert p.base_url == go.BASE_URL
    assert prov.get_provider("opencode_go") is p


def test_provider_available_from_the_subscription_key(monkeypatch):
    assert "opencode-go" in [p.name for p in prov.available_providers()]
    monkeypatch.delenv(go.API_KEY_ENV, raising=False)
    assert "opencode-go" not in [p.name for p in prov.available_providers()]


def test_disconnect_hides_the_provider_even_with_the_key_exported():
    hkeys.mark_disconnected("opencode-go")
    try:
        assert prov.get_provider("opencode-go").key() is None
        assert "opencode-go" not in [p.name for p in prov.available_providers()]
    finally:
        hkeys.unmark_disconnected("opencode-go")


def test_build_pilot_routes_chat_completions_models():
    from pmharness.drivers.openai_compat import OpenAICompatDriver

    driver = prov.build_pilot("opencode-go:deepseek-v4-flash")
    assert isinstance(driver, OpenAICompatDriver)
    assert driver.base_url == go.BASE_URL
    assert driver.model == "deepseek-v4-flash"
    assert driver.api_key_env == "OPENCODE_GO_API_KEY"


def test_build_pilot_routes_anthropic_messages_models():
    from pmharness.drivers.anthropic import AnthropicDriver

    driver = prov.build_pilot("opencode-go:minimax-m3")
    assert isinstance(driver, AnthropicDriver)
    # AnthropicDriver appends /messages, so the base keeps its /v1 segment.
    assert driver.base_url == go.BASE_URL
    assert driver.model == "minimax-m3"


def test_build_pilot_routes_openai_responses_models():
    from pmharness.drivers.codex_responses import CodexResponsesDriver

    driver = prov.build_pilot("opencode-go:gpt-5.6-luna")
    assert isinstance(driver, CodexResponsesDriver)
    assert driver.base_url == go.BASE_URL
    assert driver.chatgpt_backend is False
    assert driver.api_key_env == "OPENCODE_GO_API_KEY"


def test_build_pilot_strips_a_namespaced_model_before_the_wire():
    driver = prov.build_pilot("opencode-go:moonshotai/kimi-k2.7-code")
    assert driver.model == "kimi-k2.7-code"


def test_build_pilot_clamps_the_mimo_pro_ceiling():
    driver = prov.build_pilot("opencode-go:mimo-v2.5-pro", max_tokens=262144)
    assert driver.max_tokens == 131072


def test_build_pilot_attaches_the_models_reasoning_dialect(monkeypatch):
    monkeypatch.setenv("HARNESS_CODEX_REASONING_EFFORT", "max")
    driver = prov.build_pilot("opencode-go:glm-5.2")
    assert driver.extra_body == {"reasoning_effort": "max"}


def test_bare_model_names_still_prefer_the_direct_vendor(monkeypatch):
    """Go resells GLM; a user with a Z.AI key should keep hitting Z.AI direct."""
    monkeypatch.setenv("GLM_API_KEY", "glm-test")
    driver = prov.build_pilot("glm-5.2")
    assert driver.base_url.startswith("https://api.z.ai")


# ── Key configuration and persistence ──────────────────────────────────────

def test_set_api_key_persists_and_injects_the_env_var(monkeypatch):
    monkeypatch.delenv(go.API_KEY_ENV, raising=False)
    hkeys.set_api_key("opencode-go", "sk-go-stored")
    import os

    assert os.environ[go.API_KEY_ENV] == "sk-go-stored"
    with open(hkeys.get_keys_file_path(), encoding="utf-8") as f:
        assert json.load(f)["opencode-go"] == "sk-go-stored"
    assert hkeys.get_api_key_status("opencode-go")["has_key"] is True


def test_env_var_name_resolves_for_the_reach():
    assert hkeys.get_env_var_for_reach("opencode-go") == "OPENCODE_GO_API_KEY"


def test_shell_exported_key_is_persisted_for_the_next_cold_start():
    assert "opencode-go" in hkeys.persist_env_api_keys()
    with open(hkeys.get_keys_file_path(), encoding="utf-8") as f:
        assert json.load(f)["opencode-go"] == "sk-go-test"


def test_credential_pool_maps_the_key_env_to_its_own_pool():
    from harness import credential_pool as cp

    assert cp.provider_for_env_var("OPENCODE_GO_API_KEY") == "opencode-go"
    assert cp.env_var_for_provider("opencode-go") == "OPENCODE_GO_API_KEY"
    assert "opencode-go" in cp.known_pool_providers()


# ── Catalog fetch + curated fallback ───────────────────────────────────────

def test_live_models_are_normalized_to_bare_ids(monkeypatch):
    p = prov.get_provider("opencode-go")
    monkeypatch.setattr(mf, "_get", lambda url, headers: {
        "data": [{"id": "opencode-go/kimi-k3"}, {"id": "deepseek-v4-flash"}],
    })
    assert mf._fetch_provider_models(p, "sk-go-test") == ["kimi-k3", "deepseek-v4-flash"]
    assert mf.last_fetch_error("opencode-go") is None


def test_live_catalog_filters_retired_deepseek_v2_v3(monkeypatch):
    p = prov.get_provider("opencode-go")
    monkeypatch.setattr(mf, "_get", lambda url, headers: {
        "data": [
            {"id": "deepseek-v3.1"},
            {"id": "deepseek-v2"},
            {"id": "deepseek-v4-flash"},
        ],
    })
    assert mf._fetch_provider_models(p, "sk-go-test") == ["deepseek-v4-flash"]


def test_live_models_tolerate_an_id_keyed_catalog(monkeypatch):
    p = prov.get_provider("opencode-go")
    monkeypatch.setattr(mf, "_get", lambda url, headers: {
        "models": {"glm-5.2": {"name": "GLM-5.2"}, "hy3": {}},
    })
    assert sorted(mf._fetch_provider_models(p, "sk-go-test")) == ["glm-5.2", "hy3"]


def test_unreachable_models_endpoint_falls_back_to_the_curated_catalog(monkeypatch):
    p = prov.get_provider("opencode-go")

    def _offline(url, headers):
        raise RuntimeError("simulated connection refused")

    monkeypatch.setattr(mf, "_get", _offline)
    assert mf._fetch_provider_models(p, "sk-go-test") == []
    assert "simulated connection refused" in mf.last_fetch_error("opencode-go")

    monkeypatch.setattr(mf, "fetch_models", lambda provider, key, **kw: [])
    assert mv.provider_models(p) == list(go.CURATED_MODELS)


def test_empty_catalog_is_reported_rather_than_looking_like_no_models(monkeypatch):
    p = prov.get_provider("opencode-go")
    monkeypatch.setattr(mf, "_get", lambda url, headers: {"data": []})
    assert mf._fetch_provider_models(p, "sk-go-test") == []
    assert "empty" in mf.last_fetch_error("opencode-go").lower()


def test_a_working_live_catalog_leads_and_curated_backfills(monkeypatch):
    p = prov.get_provider("opencode-go")
    monkeypatch.setattr(
        mf, "fetch_models", lambda provider, key, **kw: ["hy3", "kimi-k3"],
    )
    models = mv.provider_models(p)
    assert models[:2] == ["hy3", "kimi-k3"]
    assert "deepseek-v4-flash" in models
    assert models.count("kimi-k3") == 1


# ── Driver plumbing the routing depends on ─────────────────────────────────

def test_openai_compat_merges_extra_body_without_touching_the_conversation():
    from pmharness.drivers.openai_compat import OpenAICompatDriver

    driver = OpenAICompatDriver(
        name="go", model="deepseek-v4-flash", base_url=go.BASE_URL,
        api_key_env=go.API_KEY_ENV,
        extra_body={"reasoning_effort": "max", "model": "hijacked"},
    )
    body = driver._prepare_body({"model": "deepseek-v4-flash", "messages": []})
    assert body["reasoning_effort"] == "max"
    assert body["model"] == "deepseek-v4-flash"


def test_codex_responses_drops_chatgpt_only_wire_bits_for_other_hosts():
    from pmharness.drivers.codex_responses import CodexResponsesDriver

    driver = CodexResponsesDriver(
        name="go", model="gpt-5.6-luna", base_url=go.BASE_URL,
        api_key_env=go.API_KEY_ENV, chatgpt_backend=False,
    )
    headers = driver._request_headers("sk-go-test")
    assert headers["Authorization"] == "Bearer sk-go-test"
    assert "originator" not in headers
    body = driver._build_body(
        [{"role": "user", "content": "hi"}], session_id="chat-1",
    )
    assert "client_metadata" not in body


def test_codex_responses_keeps_chatgpt_headers_by_default():
    from pmharness.drivers.codex_responses import CodexResponsesDriver

    driver = CodexResponsesDriver(name="codex", model="gpt-5.5")
    assert driver.chatgpt_backend is True
    assert driver._request_headers("tok")["originator"] == "codex_cli_rs"


# ── Picker visibility ──────────────────────────────────────────────────────

def test_models_appear_in_the_pilot_picker_when_keyed():
    pilots = prov.available_pilots()
    assert "opencode-go:deepseek-v4-flash" in pilots
    assert "opencode-go:minimax-m3" in pilots


def test_catalog_exposes_the_provider_for_model_curation():
    entries = [c for c in mv.catalog(available_only=True) if c["provider"] == "opencode-go"]
    assert entries
    assert entries[0]["provider_display"] == "OpenCode Go"
    assert {c["model"] for c in entries} >= {"deepseek-v4-flash", "gpt-5.6-luna"}
