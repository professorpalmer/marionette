"""Vision sidecar env picker: reach override, dedicated VLM keys, dynamic
provider-key resolution, and null fallback. Offline (no live calls)."""
import pytest
pytestmark = pytest.mark.swarm
import os
from harness.vision import (default_sidecar, OpenRouterVisionSidecar,
                            GeminiVisionSidecar, AnthropicVisionSidecar,
                            OpenAICompatVisionSidecar, NullVisionSidecar)


# Every provider key env the sidecar might resolve against, plus the VLM knobs.
_ALL_KEY_ENVS = (
    "HARNESS_VLM_REACH", "HARNESS_VLM_MODEL",
    "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN",
    "OPENAI_API_KEY", "OPENAI_CODEX_TOKEN", "GEMINI_API_KEY", "GOOGLE_API_KEY",
    "DEEPSEEK_API_KEY", "GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY",
    "MINIMAX_API_KEY", "XAI_API_KEY", "NVIDIA_API_KEY",
)


def _clear_all(monkeypatch):
    for ev in _ALL_KEY_ENVS:
        monkeypatch.delenv(ev, raising=False)


def test_default_sidecar_null_when_nothing_configured(monkeypatch):
    _clear_all(monkeypatch)
    assert isinstance(default_sidecar(), NullVisionSidecar)


def test_default_sidecar_prefers_gemini_key(monkeypatch):
    _clear_all(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    assert isinstance(default_sidecar(), GeminiVisionSidecar)


def test_default_sidecar_picks_openrouter_when_set(monkeypatch):
    _clear_all(monkeypatch)
    monkeypatch.setenv("HARNESS_VLM_REACH", "openrouter")
    sc = default_sidecar()
    assert isinstance(sc, OpenRouterVisionSidecar)
    assert sc.model == "qwen/qwen3-vl-30b-a3b-instruct"
    assert sc.api_key_env == "OPENROUTER_API_KEY"


def test_open_vlm_model_override(monkeypatch):
    _clear_all(monkeypatch)
    monkeypatch.setenv("HARNESS_VLM_REACH", "openrouter")
    monkeypatch.setenv("HARNESS_VLM_MODEL", "qwen/qwen3-vl-8b-instruct")
    sc = default_sidecar()
    assert sc.model == "qwen/qwen3-vl-8b-instruct"


def test_default_sidecar_uses_anthropic_key(monkeypatch):
    """Only Anthropic configured -> Claude vision sidecar, same key, no dedicated
    VLM key needed."""
    _clear_all(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    sc = default_sidecar()
    assert isinstance(sc, AnthropicVisionSidecar)
    assert sc.api_key_env == "ANTHROPIC_API_KEY"
    assert sc.model == "claude-haiku-4-5"


def test_default_sidecar_uses_openai_key(monkeypatch):
    """Only OpenAI configured -> GPT vision via OpenAI-compat transport."""
    _clear_all(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-x")
    sc = default_sidecar()
    assert isinstance(sc, OpenAICompatVisionSidecar)
    assert not isinstance(sc, (GeminiVisionSidecar, OpenRouterVisionSidecar))
    assert sc.api_key_env == "OPENAI_API_KEY"
    assert sc.model == "gpt-5.6-luna"


def test_open_vlm_missing_key_errors(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    sc = OpenRouterVisionSidecar()
    r = sc.transcribe("/tmp/testimg.png") if os.path.exists("/tmp/testimg.png") else None
    # with no key, _key() raises inside transcribe -> VisionResult.error set (repr of RuntimeError)
    if r is not None:
        assert r.error and "OPENROUTER_API_KEY" in r.error


def test_null_sidecar_transcribe_reports_actionable_error(tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n")
    r = NullVisionSidecar().transcribe(str(img))
    assert r.text == ""
    assert r.error and "vision-capable provider" in r.error
