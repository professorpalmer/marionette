"""Tests for append-only context mode resolution."""
from __future__ import annotations

import pytest

from harness.append_only_context import (
    append_only_setting,
    should_enable_append_only,
)


@pytest.mark.parametrize(
    "driver_name",
    [
        "ollama",
        "Ollama-local",
        "my-lm-studio",
        "lmstudio-runner",
        "llama.cpp-server",
        "llamacpp",
        "vllm-worker",
        "sglang-node",
        "deepseek-chat",
    ],
)
def test_provider_name_match(driver_name):
    assert should_enable_append_only("auto", "https://api.openai.com/v1", driver_name)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:11434/v1",
        "http://127.0.0.1:8080",
        "http://0.0.0.0:8000",
        "http://[::1]:1234",
        "http://10.0.0.5/v1",
        "http://192.168.1.42:8080",
        "http://172.16.0.1/v1",
        "http://172.31.255.1/v1",
        "http://llama-box.local:8080",
    ],
)
def test_local_base_urls(base_url):
    assert should_enable_append_only("auto", base_url, "custom-provider")


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.openai.com/v1",
        "https://openrouter.ai/api/v1",
        "not-a-url",
        "",
    ],
)
def test_public_or_garbage_urls(base_url):
    assert not should_enable_append_only("auto", base_url, "gpt-4")


def test_settings_on_off():
    assert should_enable_append_only("on", "https://api.openai.com/v1", "gpt-4")
    assert not should_enable_append_only("off", "http://localhost:11434", "ollama")


def test_env_normalization(monkeypatch):
    monkeypatch.setenv("HARNESS_APPEND_ONLY_CONTEXT", "ON")
    assert append_only_setting() == "on"
    monkeypatch.setenv("HARNESS_APPEND_ONLY_CONTEXT", "0")
    assert append_only_setting() == "off"
    monkeypatch.setenv("HARNESS_APPEND_ONLY_CONTEXT", "true")
    assert append_only_setting() == "on"
    monkeypatch.setenv("HARNESS_APPEND_ONLY_CONTEXT", "false")
    assert append_only_setting() == "off"
    monkeypatch.delenv("HARNESS_APPEND_ONLY_CONTEXT", raising=False)
    assert append_only_setting() == "auto"
    monkeypatch.setenv("HARNESS_APPEND_ONLY_CONTEXT", "garbage")
    assert append_only_setting() == "auto"


def test_never_raises():
    should_enable_append_only(None, None, None)  # type: ignore[arg-type]
