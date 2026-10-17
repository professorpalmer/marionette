"""OpenCode Go and Zen serve Gemini only on Google generateContent.

Marionette sent ``gemini-4-argon-contributor`` (Go) and every Zen ``gemini-*``
to chat/completions, and the relay answered 400 ``ModelProtocolUnsupported``.
Go ``claude-haiku-5-5`` also went to chat/completions. Go serves it on
Anthropic messages.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from harness import opencode_go, opencode_zen
from pmharness.drivers.anthropic import AnthropicDriver
from pmharness.drivers.gemini import GeminiDriver


@pytest.mark.parametrize("module,model,mode", [
    (opencode_go, "gemini-4-argon-contributor", "gemini_native"),
    (opencode_go, "claude-haiku-5-5", "anthropic_messages"),
    (opencode_go, "kimi-k3", "chat_completions"),
    (opencode_zen, "gemini-4-argon", "gemini_native"),
    (opencode_zen, "gemini-3.8-flash", "gemini_native"),
    (opencode_zen, "nemotron-3.5-super-vl-free", "chat_completions"),
])
def test_endpoint_table(module, model, mode):
    assert module.api_mode_for_model(model) == mode


@pytest.mark.parametrize("module,model,base", [
    (opencode_go, "gemini-4-argon-contributor", "https://opencode.ai/inference/go/google/v1beta"),
    (opencode_zen, "gemini-4-argon", "https://opencode.ai/inference/google/v1beta"),
])
def test_gemini_models_get_the_google_driver(module, model, base):
    d = module.build_driver(spec=f"{module.PROVIDER_NAME}:{model}", model=model,
                            api_key_env=module.API_KEY_ENV, max_tokens=None)
    assert isinstance(d, GeminiDriver)
    assert d.base_url == base
    assert d.extra_headers["User-Agent"] == "Marionette"


def test_go_claude_gets_the_anthropic_driver():
    d = opencode_go.build_driver(spec="opencode-go:claude-haiku-5-5", model="claude-haiku-5-5",
                                 api_key_env=opencode_go.API_KEY_ENV, max_tokens=None)
    assert isinstance(d, AnthropicDriver)


def test_custom_relay_host_keeps_its_base():
    d = opencode_go.build_driver(spec="opencode-go:gemini-x", model="gemini-x",
                                 api_key_env=opencode_go.API_KEY_ENV, max_tokens=None,
                                 base_url="https://relay.example/v1")
    assert d.base_url == "https://relay.example/v1"


def test_relay_request_carries_session_and_key_header(monkeypatch):
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "go-key")
    monkeypatch.setattr("harness.credential_pool.resolve_entry_for_env", lambda env: None)
    d = opencode_go.build_driver(spec="opencode-go:gemini-4-argon-contributor",
                                 model="gemini-4-argon-contributor",
                                 api_key_env=opencode_go.API_KEY_ENV, max_tokens=None)
    captured = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"candidates": [{"content": {"parts": [{"text": "ok"}]},
                                               "finishReason": "STOP"}]}).encode()

    def fake_urlopen(req, timeout=None):
        captured.append(req)
        return _Resp()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        resp = d.chat([{"role": "user", "content": "hi"}], session_id="sess-1")
    assert resp.text == "ok"
    req = captured[0]
    assert req.full_url == ("https://opencode.ai/inference/go/google/v1beta/models/"
                            "gemini-4-argon-contributor:generateContent")
    assert req.get_header("X-goog-api-key") == "go-key"
    assert req.get_header("X-opencode-session")
    assert req.get_header("User-agent") == "Marionette"
