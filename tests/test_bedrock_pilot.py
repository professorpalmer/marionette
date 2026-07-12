"""BedrockDriver: interactive pilot over puppetmaster.bedrock.bedrock_chat."""
from __future__ import annotations

import json
import os
import tempfile
from types import SimpleNamespace

import pytest

from harness import providers as prov
from pmharness.drivers.bedrock import BedrockDriver


_BEDROCK_ENV = (
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_REGION",
    "BEDROCK_REGION",
    "BEDROCK_MODEL_ID",
    "AWS_PROFILE",
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    state = tempfile.mkdtemp()
    monkeypatch.setenv("HARNESS_STATE_DIR", state)
    # Empty keys.json so get_keys_file_path does not migrate to the real
    # ~/.pmharness/keys.json (which may hold live Bedrock BYOK).
    with open(os.path.join(state, "keys.json"), "w", encoding="utf-8") as f:
        f.write("{}")
    with open(os.path.join(state, "disconnected.json"), "w", encoding="utf-8") as f:
        f.write("[]")
    for ev in _BEDROCK_ENV:
        monkeypatch.delenv(ev, raising=False)
    yield


def test_build_pilot_returns_bedrock_driver(monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-bearer-test")
    model = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    d = prov.build_pilot(f"bedrock:{model}")
    assert isinstance(d, BedrockDriver)
    assert d.model == model
    assert d.name == f"bedrock:{model}"


def test_available_pilots_include_bedrock_and_model_id(monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-bearer-test")
    monkeypatch.setenv(
        "BEDROCK_MODEL_ID",
        "us.anthropic.claude-opus-4-6-20250514-v1:0",
    )
    # Avoid live AWS listing in unit tests (would replace curated fallback).
    monkeypatch.setenv("PMHARNESS_LIVE_MODELS", "0")
    pilots = prov.available_pilots()
    assert "bedrock:us.anthropic.claude-sonnet-4-5-20250929-v1:0" in pilots
    assert "bedrock:us.anthropic.claude-haiku-4-5-20251001-v1:0" in pilots
    assert "bedrock:us.anthropic.claude-opus-4-6-20250514-v1:0" in pilots
    assert "bedrock:amazon.nova-micro-v1:0" in pilots
    assert "bedrock:deepseek.v3.2" in pilots
    assert "bedrock:zai.glm-4.7-flash" in pilots


def test_chat_maps_tool_use_to_openai_tool_calls(monkeypatch):
    turn = SimpleNamespace(
        text="calling tool",
        tool_calls=[{
            "id": "toolu_1",
            "name": "read_file",
            "arguments": {"path": "README.md"},
        }],
        finish_reason="tool_use",
        usage={
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
        },
        raw={},
    )

    def _fake_chat(**kwargs):
        assert kwargs["model"].startswith("us.anthropic.")
        assert any(m.get("role") == "system" for m in kwargs["messages"])
        assert kwargs["tools"]
        return turn

    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-bearer-test")
    monkeypatch.setattr(
        "puppetmaster.bedrock.bedrock_chat", _fake_chat, raising=False
    )
    # Import path used inside BedrockDriver._invoke
    import puppetmaster.bedrock as bedrock_mod
    monkeypatch.setattr(bedrock_mod, "bedrock_chat", _fake_chat)

    driver = BedrockDriver(
        name="bedrock:us.anthropic.claude-haiku-4-5-20251001-v1:0",
        model="us.anthropic.claude-haiku-4-5-20251001-v1:0",
    )
    resp = driver.chat(
        [{"role": "user", "content": "read the readme"}],
        tools=[{
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            },
        }],
        system="You are a coding agent.",
    )
    assert resp.error is None
    assert resp.text == "calling tool"
    assert resp.tokens_in == 11
    assert resp.tokens_out == 7
    tcs = resp.meta["tool_calls"]
    assert len(tcs) == 1
    assert tcs[0]["id"] == "toolu_1"
    assert tcs[0]["type"] == "function"
    assert tcs[0]["function"]["name"] == "read_file"
    assert json.loads(tcs[0]["function"]["arguments"]) == {"path": "README.md"}


def test_missing_creds_surfaces_actionable_error(monkeypatch):
    # Ensure no AWS auth is visible (including profile / shared creds).
    monkeypatch.setenv("HOME", tempfile.mkdtemp())
    monkeypatch.setenv("USERPROFILE", os.environ["HOME"])
    for ev in _BEDROCK_ENV:
        monkeypatch.delenv(ev, raising=False)

    driver = BedrockDriver(
        name="bedrock:us.anthropic.claude-haiku-4-5-20251001-v1:0",
        model="us.anthropic.claude-haiku-4-5-20251001-v1:0",
    )
    with pytest.raises(RuntimeError) as ei:
        driver.chat([{"role": "user", "content": "hi"}])
    msg = str(ei.value).lower()
    assert "bedrock" in msg or "aws" in msg
    assert any(
        token in msg
        for token in ("aws_bearer_token_bedrock", "aws_access_key_id", "credential")
    )


def test_build_pilot_bedrock_without_creds_raises(monkeypatch):
    for ev in _BEDROCK_ENV:
        monkeypatch.delenv(ev, raising=False)
    with pytest.raises(prov.ProviderError) as ei:
        prov.build_pilot(
            "bedrock:us.anthropic.claude-sonnet-4-5-20250929-v1:0"
        )
    assert "no provider key" in str(ei.value).lower()
