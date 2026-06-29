"""Provider registry: detection from env keys, driver selection by api_mode,
spec resolution, and MIT attribution presence. Data adapted from Hermes (MIT)."""
import os
import tempfile
import pytest
from harness import providers as prov


@pytest.fixture(autouse=True)
def _isolate_disconnected(monkeypatch):
    """Point provider-disconnect state at an empty temp dir so these tests do not
    inherit the developer's real ~/.pmharness/disconnected.json (which would make
    a keyed provider read as unavailable)."""
    monkeypatch.setenv("HARNESS_STATE_DIR", tempfile.mkdtemp())
    yield


def test_attribution_present():
    src = open(os.path.join(os.path.dirname(prov.__file__), "providers.py")).read()
    assert "MIT" in src and "Nous Research" in src and "Hermes" in src


def test_detection_from_env(monkeypatch):
    for ev in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
               "GEMINI_API_KEY", "DEEPSEEK_API_KEY", "GLM_API_KEY"):
        monkeypatch.delenv(ev, raising=False)
    assert prov.available_providers() == []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
    names = [p.name for p in prov.available_providers()]
    assert names == ["anthropic"]
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-xxx")
    names = [p.name for p in prov.available_providers()]
    assert "openrouter" in names and "anthropic" in names


def test_provider_aliases():
    assert prov.get_provider("claude").name == "anthropic"
    assert prov.get_provider("glm").name == "zai"
    assert prov.get_provider("grok").name == "xai"


def test_build_pilot_selects_anthropic_driver(monkeypatch):
    from pmharness.drivers.anthropic import AnthropicDriver
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
    d = prov.build_pilot("anthropic:claude-opus-4-8")
    assert isinstance(d, AnthropicDriver)
    assert d.base_url.endswith("/v1")
    assert d.model == "claude-opus-4-8"


def test_build_pilot_selects_openai_compat(monkeypatch):
    from pmharness.drivers.openai_compat import OpenAICompatDriver
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-xxx")
    d = prov.build_pilot("openrouter:qwen/qwen3-coder-30b-a3b-instruct")
    assert isinstance(d, OpenAICompatDriver)
    assert "openrouter.ai" in d.base_url


def test_build_pilot_no_key_raises(monkeypatch):
    for ev in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
               "GEMINI_API_KEY", "DEEPSEEK_API_KEY", "GLM_API_KEY",
               "ZAI_API_KEY", "Z_AI_API_KEY", "MINIMAX_API_KEY", "XAI_API_KEY",
               "NVIDIA_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_TOKEN"):
        monkeypatch.delenv(ev, raising=False)
    try:
        prov.build_pilot("anthropic:claude-opus-4-8")
        assert False, "should raise ProviderError"
    except prov.ProviderError as e:
        assert "no provider key" in str(e)


def test_available_pilots_are_provider_scoped(monkeypatch):
    for ev in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
               "DEEPSEEK_API_KEY", "GLM_API_KEY", "MINIMAX_API_KEY",
               "XAI_API_KEY", "NVIDIA_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY",
               "GOOGLE_API_KEY", "ANTHROPIC_TOKEN"):
        monkeypatch.delenv(ev, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-xxx")
    pilots = prov.available_pilots()
    assert all(p.startswith("openrouter:") or p == "moa-planner" or p.startswith("moa:") for p in pilots)
    assert len(pilots) >= 3
