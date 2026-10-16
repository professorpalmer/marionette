"""One image-input rule for every provider, and a sidecar chain that falls back.

Every OpenCode Go and Zen pilot was text-only: the gate read the relay's
provider ``api_mode`` (``opencode_go``), not the protocol of the model. A
text-only pilot then had one sidecar and no fallback, so one failed VLM call
ended the turn as ``image_conversion_failed``.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

import harness.vision as v
from harness import model_fetch, models_dev
from harness.providers import get_provider
from harness.vision import VisionResult

REGISTRY = {
    "opencode-go": {"models": {
        "kimi-k3": {"modalities": {"input": ["text", "image"]}},
        "glm-5.3": {"modalities": {"input": ["text"]}},
        "qwen3.8-max": {"modalities": {"input": ["text", "image"]}},
        "gpt-5.6-luna": {"modalities": {"input": ["text", "image"]}},
    }},
    "openai": {"models": {"gpt-5.6-luna": {"modalities": {"input": ["text", "image"]}}}},
    "deepseek": {"models": {"deepseek-chat": {"modalities": {"input": ["text"]}}}},
}


@pytest.fixture
def sources(monkeypatch):
    """A fixed models.dev registry and an empty live catalog."""
    live = {}
    monkeypatch.setattr(models_dev, "_registry", lambda *, allow_network: REGISTRY)
    monkeypatch.setattr(model_fetch, "model_metadata",
                        lambda provider, slug: live.get((provider, slug)))
    return live


def test_go_models_travel_on_their_own_protocol():
    go = get_provider("opencode-go")
    assert v.wire_api_mode(go, "kimi-k3") == "chat_completions"
    assert v.wire_api_mode(go, "qwen3.8-max") == "anthropic_messages"
    assert v.wire_api_mode(go, "gpt-5.6-luna") == "responses"
    zen = get_provider("opencode-zen")
    assert v.wire_api_mode(zen, "kimi-k3") in v._NATIVE_IMAGE_API_MODES
    assert v.wire_api_mode(get_provider("openai-codex"), "gpt-5.6-luna") == "codex_responses"


@pytest.mark.parametrize("model,native", [
    ("kimi-k3", True),
    ("qwen3.8-max", True),
    ("gpt-5.6-luna", True),
    ("glm-5.3", False),
])
def test_go_pilot_native_images_follow_the_model(sources, model, native):
    go = get_provider("opencode-go")
    assert v.pilot_supports_native_images(go, model=model) is native


def test_models_dev_capability_ids_cover_subscription_providers(sources):
    assert models_dev.image_input("openai-codex", "gpt-5.6-luna") is True
    assert models_dev.image_input("deepseek", "deepseek-chat") is False
    assert models_dev.image_input("opencode-go", "no-such-model") is None


def test_live_catalog_wins_over_models_dev(sources):
    go = get_provider("opencode-go")
    sources[("opencode-go", "glm-5.3")] = {"id": "glm-5.3",
                                           "architecture": {"input_modalities": ["text", "image"]}}
    assert v.model_image_input(go, "glm-5.3") is True
    sources[("opencode-go", "kimi-k3")] = {"id": "kimi-k3", "modalities": ["text"]}
    assert v.model_image_input(go, "kimi-k3") is False


def test_unknown_everywhere_is_none(sources):
    go = get_provider("opencode-go")
    assert v.model_image_input(go, "no-such-model") is None


def test_text_only_relay_mode_still_refuses(sources):
    provider = SimpleNamespace(name="opencode-go", api_mode="cli_only")
    assert v.pilot_supports_native_images(provider, model="kimi-k3") is False


@dataclass
class _Provider:
    name: str
    api_mode: str
    vision_model: str = ""
    base_url: str = "https://example.invalid/v1"

    def key_env(self):
        return f"{self.name.upper().replace('-', '_')}_KEY"


def test_go_only_setup_has_a_go_sidecar(monkeypatch):
    for env in ("HARNESS_VLM_REACH", "GEMINI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    from harness import opencode_go
    monkeypatch.setattr("harness.providers.available_providers",
                        lambda: [_Provider("opencode-go", "opencode_go",
                                           base_url=opencode_go.BASE_URL)])
    sc = v.default_sidecar()
    assert isinstance(sc, v.OpenAICompatVisionSidecar)
    assert sc.model == opencode_go.VISION_SIDECAR_MODEL
    assert sc.base_url == opencode_go.driver_base_url()
    assert sc.extra_headers["User-Agent"] == opencode_go.USER_AGENT
    headers = sc._headers("k", {"messages": [{"role": "user", "content": "x"}]})
    assert headers["x-opencode-session"]


def test_non_opencode_sidecar_sends_no_session_header():
    sc = v.OpenAICompatVisionSidecar(model="m", base_url="https://api.openai.com/v1",
                                     api_key_env="OPENAI_API_KEY")
    headers = sc._headers("k", {"messages": [{"role": "user", "content": "x"}]})
    assert "x-opencode-session" not in headers


class _Sidecar:
    def __init__(self, name, error=None):
        self.name = name
        self.base_url = f"https://{name}.invalid"
        self.model = name
        self.error = error
        self.calls = 0

    def transcribe(self, path):
        self.calls += 1
        if self.error:
            return VisionResult("", error=self.error, model=self.name)
        return VisionResult(f"seen by {self.name}", model=self.name)


def test_transcribe_falls_back_to_the_next_sidecar(monkeypatch):
    broken, good = _Sidecar("a", error="HTTP 404: retired"), _Sidecar("b")
    monkeypatch.setattr(v, "sidecar_candidates", lambda: [broken, good])
    results = v.transcribe_images(["one.png", "two.png"])
    assert [r.text for r in results] == ["seen by b", "seen by b"]
    assert broken.calls == 2


def test_transcribe_names_every_failure(monkeypatch):
    monkeypatch.setattr(v, "sidecar_candidates",
                        lambda: [_Sidecar("a", error="HTTP 404"), _Sidecar("b", error="HTTP 429")])
    (result,) = v.transcribe_images(["one.png"])
    assert result.error == "a: HTTP 404; b: HTTP 429"


def test_transcribe_with_nothing_configured_is_actionable(monkeypatch):
    monkeypatch.setattr(v, "sidecar_candidates", lambda: [])
    (result,) = v.transcribe_images(["one.png"])
    assert "no vision-capable provider configured" in result.error


def test_sidecar_key_prefers_the_pool(monkeypatch):
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "from-env")
    monkeypatch.setattr("harness.credential_pool.resolve_entry_for_env",
                        lambda env: SimpleNamespace(runtime_token="from-pool"))
    assert v._sidecar_key("OPENCODE_GO_API_KEY") == "from-pool"
    monkeypatch.setattr("harness.credential_pool.resolve_entry_for_env", lambda env: None)
    assert v._sidecar_key("OPENCODE_GO_API_KEY") == "from-env"


def test_anthropic_sidecar_key_skips_an_oauth_token(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-env")
    monkeypatch.setattr("harness.credential_pool.resolve_entry_for_env",
                        lambda env: SimpleNamespace(runtime_token="sk-ant-oat01-pool"))
    assert v._sidecar_key("ANTHROPIC_API_KEY", api_key_only=True) == "sk-ant-api03-env"
