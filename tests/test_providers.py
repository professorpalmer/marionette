"""Provider registry: detection from env keys, driver selection by api_mode,
spec resolution, and MIT attribution presence. Data adapted from Hermes (MIT)."""
import json
import os
import tempfile
import urllib.request
import pytest
from harness import providers as prov


# Extra env vars not listed on Provider.env_vars but still gate detection.
_EXTRA_PROVIDER_ENV = (
    "XAI_OAUTH_TOKEN",
    "AWS_SECRET_ACCESS_KEY",
    "CURSOR_CLI_LOGIN",
    "CURSOR_API_KEY",
)


def _clear_provider_env(monkeypatch) -> None:
    """Strip every provider-detection env key (including OpenCode Go).

    Hand-maintained delenv lists go stale when a new Provider.env_vars entry
    lands (e.g. OPENCODE_GO_API_KEY) and make these tests non-hermetic under
    a developer shell that has that key set.
    """
    for provider in prov.PROVIDERS:
        for ev in provider.env_vars:
            monkeypatch.delenv(ev, raising=False)
    for ev in _EXTRA_PROVIDER_ENV:
        monkeypatch.delenv(ev, raising=False)
    monkeypatch.setattr("harness.cursor_cli_auth.is_authenticated", lambda: False)


@pytest.fixture(autouse=True)
def _isolate_disconnected(monkeypatch):
    """Point provider-disconnect state at an empty temp dir so these tests do not
    inherit the developer's real ~/.pmharness/disconnected.json (which would make
    a keyed provider read as unavailable)."""
    state = tempfile.mkdtemp()
    monkeypatch.setenv("HARNESS_STATE_DIR", state)
    with open(os.path.join(state, "keys.json"), "w", encoding="utf-8") as f:
        f.write("{}")
    # Empty list beats the keys.py fallback to ~/.pmharness/disconnected.json
    # when the state-dir file is missing.
    with open(os.path.join(state, "disconnected.json"), "w", encoding="utf-8") as f:
        f.write("[]")
    try:
        from harness import credential_pool as cp
        cp.clear_pools_for_tests()
    except Exception:
        pass
    yield
    try:
        from harness import credential_pool as cp
        cp.clear_pools_for_tests()
    except Exception:
        pass


def test_attribution_present():
    src = open(os.path.join(os.path.dirname(prov.__file__), "providers.py")).read()
    assert "MIT" in src and "Nous Research" in src and "Hermes" in src


def test_detection_from_env(monkeypatch):
    _clear_provider_env(monkeypatch)
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
    assert prov.get_provider("cursor-agent").name == "cursor-cli"


def test_zai_defaults_to_coding_plan_endpoint(monkeypatch):
    """GLM Coding Plan keys must hit /api/coding/paas/v4, not pay-as-you-go."""
    monkeypatch.delenv("ZAI_BASE_URL", raising=False)
    monkeypatch.delenv("GLM_BASE_URL", raising=False)
    p = prov.get_provider("zai")
    assert p is not None
    assert p.base_url == prov.ZAI_CODING_BASE_URL
    assert p.resolved_base_url() == "https://api.z.ai/api/coding/paas/v4"
    assert prov.zai_uses_coding_plan() is True
    monkeypatch.setenv("ZAI_BASE_URL", "https://api.z.ai/api/paas/v4")
    assert p.resolved_base_url() == "https://api.z.ai/api/paas/v4"
    assert prov.zai_uses_coding_plan() is False


def test_build_pilot_zai_uses_coding_plan_url(monkeypatch):
    from pmharness.drivers.openai_compat import OpenAICompatDriver

    _clear_provider_env(monkeypatch)
    monkeypatch.delenv("ZAI_BASE_URL", raising=False)
    monkeypatch.delenv("GLM_BASE_URL", raising=False)
    monkeypatch.setenv("GLM_API_KEY", "sk-zai-test")
    d = prov.build_pilot("zai:glm-5.2")
    assert isinstance(d, OpenAICompatDriver)
    assert d.base_url == "https://api.z.ai/api/coding/paas/v4"
    assert d.model == "glm-5.2"


def test_build_pilot_zai_glm_53_sends_required_thinking(monkeypatch):
    from pmharness.drivers.openai_compat import OpenAICompatDriver

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("GLM_API_KEY", "sk-zai-test")
    d = prov.build_pilot("zai:glm-5.3")
    assert isinstance(d, OpenAICompatDriver)
    assert d.model == "glm-5.3"
    assert d.extra_body.get("thinking") == {"type": "enabled"}
    assert d.extra_body.get("reasoning_effort") in {"low", "high", "max"}


def test_ensure_zai_worker_base_url_does_not_clobber_override(monkeypatch):
    monkeypatch.setenv("ZAI_BASE_URL", "https://api.z.ai/api/paas/v4")
    prov.ensure_zai_worker_base_url()
    assert os.environ["ZAI_BASE_URL"] == "https://api.z.ai/api/paas/v4"
    monkeypatch.delenv("ZAI_BASE_URL", raising=False)
    monkeypatch.delenv("GLM_BASE_URL", raising=False)
    prov.ensure_zai_worker_base_url()
    assert os.environ["ZAI_BASE_URL"] == prov.ZAI_CODING_BASE_URL


def test_build_pilot_selects_cursor_cli_driver(monkeypatch):
    from pmharness.drivers.cursor_acp import CursorAcpDriver
    from pmharness.drivers.cursor_cli import CursorCliDriver
    monkeypatch.setenv("CURSOR_CLI_LOGIN", "1")
    monkeypatch.setattr(
        "harness.cursor_cli_auth.is_authenticated",
        lambda: True,
    )
    # Default: --print. ACP is opt-in and only for auto/empty.
    monkeypatch.delenv("HARNESS_CURSOR_ACP", raising=False)
    d = prov.build_pilot("cursor-cli:auto")
    assert isinstance(d, CursorCliDriver)
    assert d.model == "auto"
    assert d.supports_streaming is True
    monkeypatch.setenv("HARNESS_CURSOR_ACP", "1")
    d_acp = prov.build_pilot("cursor-cli:auto")
    assert isinstance(d_acp, CursorAcpDriver)
    d_explicit = prov.build_pilot("cursor-cli:claude-fable-5-high")
    assert isinstance(d_explicit, CursorCliDriver)
    assert d_explicit.model == "claude-fable-5-high"
    monkeypatch.setenv("HARNESS_CURSOR_ACP", "0")
    d2 = prov.build_pilot("cursor-cli:auto")
    assert isinstance(d2, CursorCliDriver)


def test_available_pilots_include_cursor_cli_when_authed(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setattr("harness.cursor_cli_auth.is_authenticated", lambda: True)
    monkeypatch.setenv("CURSOR_CLI_LOGIN", "1")
    # Avoid live `agent models` spawn during unit test.
    monkeypatch.setenv("PMHARNESS_LIVE_MODELS", "0")
    pilots = prov.available_pilots()
    assert any(p.startswith("cursor-cli:") for p in pilots)
    assert not any(p.startswith("cursor:") for p in pilots)


def test_platform_cursor_adapter_distinct_from_cursor_cli():
    """Platform implement adapter stays which('cursor'); pilot is cursor-cli."""
    p = prov.get_provider("cursor-cli")
    assert p is not None
    assert p.api_mode == "cursor_cli"
    assert prov.get_provider("cursor") is None  # no provider named bare cursor


def test_build_pilot_selects_anthropic_driver(monkeypatch):
    from pmharness.drivers.anthropic import AnthropicDriver
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
    d = prov.build_pilot("anthropic:claude-opus-4-8")
    assert isinstance(d, AnthropicDriver)
    assert d.base_url.endswith("/v1")
    assert d.model == "claude-opus-4-8"


def test_build_pilot_selects_openai_responses_driver(monkeypatch):
    """First-party openai: routes through CodexResponsesDriver (api.openai.com)."""
    from pmharness.drivers.codex_responses import CodexResponsesDriver

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")
    d = prov.build_pilot("openai:gpt-5.6-luna")
    assert isinstance(d, CodexResponsesDriver)
    assert d.base_url == "https://api.openai.com/v1"
    assert d.api_key_env == "OPENAI_API_KEY"
    assert d.chatgpt_backend is False
    assert d.model == "gpt-5.6-luna"


def test_build_pilot_openai_responses_wire(monkeypatch):
    """openai:gpt-5.6-luna posts /v1/responses with reasoning + tools, not chat."""
    from pmharness.drivers.codex_responses import CodexResponsesDriver

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")
    monkeypatch.setenv("HARNESS_CODEX_REASONING_EFFORT", "max")
    d = prov.build_pilot("openai:gpt-5.6-luna", max_tokens=8000)
    assert isinstance(d, CodexResponsesDriver)

    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def __iter__(self):
            return iter([
                (
                    b'data: {"type":"response.output_item.added","output_index":0,'
                    b'"item":{"type":"function_call","id":"fc_1","name":"read_file",'
                    b'"arguments":"{\\"path\\":\\"x\\"}"}}\n'
                ),
                (
                    b'data: {"type":"response.output_item.done","output_index":0,'
                    b'"item":{"type":"function_call","id":"fc_1","call_id":"fc_1",'
                    b'"name":"read_file","arguments":"{\\"path\\":\\"x\\"}"}}\n'
                ),
                (
                    b'data: {"type":"response.completed","response":{"status":"completed",'
                    b'"usage":{"input_tokens":10,"output_tokens":5}}}\n'
                ),
            ])

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    resp = d.chat(
        [{"role": "user", "content": "hi"}],
        tools=[{
            "type": "function",
            "function": {
                "name": "read_file",
                "parameters": {"type": "object", "properties": {}},
            },
        }],
    )

    assert captured["url"] == "https://api.openai.com/v1/responses"
    body = captured["body"]
    assert body["reasoning"]["effort"] == "max"
    assert body["reasoning"]["summary"] == "auto"
    assert body.get("tools")
    assert body.get("tool_choice") == "auto"
    assert body.get("parallel_tool_calls") is True
    assert body.get("max_output_tokens", 0) > 0
    assert "reasoning_effort" not in body
    assert "messages" not in body
    assert "originator" not in captured["headers"]
    assert resp.meta.get("billing") == "api"
    assert resp.meta.get("api_mode") == "responses"
    assert resp.meta.get("tool_calls")


def test_build_pilot_selects_openai_compat(monkeypatch):
    from pmharness.drivers.openai_compat import OpenAICompatDriver
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-xxx")
    d = prov.build_pilot("openrouter:qwen/qwen3-coder-30b-a3b-instruct")
    assert isinstance(d, OpenAICompatDriver)
    assert "openrouter.ai" in d.base_url


def test_build_pilot_no_key_raises(monkeypatch):
    _clear_provider_env(monkeypatch)
    try:
        prov.build_pilot("anthropic:claude-opus-4-8")
        assert False, "should raise ProviderError"
    except prov.ProviderError as e:
        assert "no provider key" in str(e)


def test_available_pilots_are_provider_scoped(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-xxx")
    pilots = prov.available_pilots()
    assert all(p.startswith("openrouter:") or p == "moa-planner" or p.startswith("moa:") for p in pilots)
    assert len(pilots) >= 3
    assert not any(p.startswith("opencode-go:") for p in pilots)


def test_build_pilot_bare_gpt55_prefers_openai_codex(monkeypatch):
    """Bare gpt-5.5 must not shadow ChatGPT Codex OAuth with OPENAI_API_KEY."""
    from harness import credential_pool as cp
    from pmharness.drivers.codex_responses import CodexResponsesDriver

    for ev in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
               "OPENAI_CODEX_TOKEN", "GEMINI_API_KEY"):
        monkeypatch.delenv(ev, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-xxx")
    cp.clear_pools_for_tests()
    cp.add_oauth_entry(
        "openai-codex",
        access_token="eyJhbGciOiJub25lIn0.e30.",
        label="codex",
    )
    try:
        d = prov.build_pilot("gpt-5.5")
        assert isinstance(d, CodexResponsesDriver)
        assert d.model == "gpt-5.5"
        assert d.chatgpt_backend is True
        assert d.api_key_env == "OPENAI_CODEX_TOKEN"
    finally:
        cp.clear_pools_for_tests()


def test_build_pilot_selects_bedrock_driver(monkeypatch):
    from pmharness.drivers.bedrock import BedrockDriver
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-bearer-xxx")
    model = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    d = prov.build_pilot(f"bedrock:{model}")
    assert isinstance(d, BedrockDriver)
    assert d.model == model


def test_resolve_bare_model_is_canonical_not_key_dependent():
    """The canonical winner for a bare id is stable regardless of keys."""
    p, model = prov.resolve_bare_model("gpt-5.5")
    assert p.name == "openai-codex" and model == "gpt-5.5"
    p2, _ = prov.resolve_bare_model("glm-5.3")
    assert p2.name == "zai"
    p3, _ = prov.resolve_bare_model("claude-opus-4-8")
    assert p3.name == "anthropic"


def test_build_pilot_bare_model_fails_loudly_when_canonical_unkeyed(monkeypatch):
    """A bare spec whose canonical provider has no key must raise, not
    silently reroute to whichever keyed provider (e.g. OpenRouter) is up."""
    from harness import credential_pool as cp

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-xxx")
    cp.clear_pools_for_tests()
    try:
        with pytest.raises(prov.ProviderError) as exc:
            prov.build_pilot("glm-5.2")
        assert "zai" in str(exc.value).lower() or "GLM" in str(exc.value)
    finally:
        cp.clear_pools_for_tests()


def test_build_pilot_bare_model_uses_canonical_provider_when_keyed(monkeypatch):
    """With the canonical key present, the bare spec lands on that vendor."""
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("GLM_API_KEY", "glm-test")
    driver = prov.build_pilot("glm-5.2")
    assert driver.base_url.startswith("https://api.z.ai")
