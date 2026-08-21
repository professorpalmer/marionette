"""Hermetic provider cache-usage normalization + provenance coverage.

Covers the shared token-usage seam aliases, OpenAICompatDriver metadata,
Bedrock Converse camelCase cache fields, and Anthropic TTL provenance.
No live API calls.
"""

from __future__ import annotations

import json
import urllib.request
from types import SimpleNamespace

import pytest

from pmharness.drivers.anthropic import (
    AnthropicDriver,
    _anthropic_usage_fields,
)
from pmharness.drivers.bedrock import (
    BedrockDriver,
    _usage_from_converse_usage,
)
from pmharness.drivers.openai_compat import OpenAICompatDriver
from pmharness.drivers.token_usage import coerce_token_usage_detail, coerce_token_usage_record
import pmharness.drivers.retry


@pytest.fixture(autouse=True)
def mock_retry_sleep(monkeypatch):
    orig = pmharness.drivers.retry.with_retry

    def _wrapped(fn, **kwargs):
        kwargs["sleep"] = lambda _x: None
        return orig(fn, **kwargs)

    monkeypatch.setattr(pmharness.drivers.retry, "with_retry", _wrapped)


# ---------------------------------------------------------------------------
# Shared seam aliases
# ---------------------------------------------------------------------------


def test_shared_seam_openai_prompt_and_input_details():
    tin, tout, cost, cached, write = coerce_token_usage_detail(
        {
            "usage": {
                "prompt_tokens": 1_000,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 700},
            }
        }
    )
    assert (tin, tout, cost, cached, write) == (1_000, 20, None, 700, 0)

    tin, _tout, _cost, cached, write = coerce_token_usage_detail(
        {
            "usage": {
                "input_tokens": 2_000,
                "output_tokens": 10,
                "input_tokens_details": {
                    "cached_tokens": 1_200,
                    "cache_write_tokens": 100,
                },
            }
        }
    )
    # OpenAI-style: cached is a subset of full prompt total — do not expand.
    assert tin == 2_000
    assert cached == 1_200
    assert write == 100


def test_shared_seam_cached_input_tokens_aliases():
    for key, value in (
        ("cached_input_tokens", 400),
        ("cachedInputTokens", 450),
        ("cacheReadInputTokens", 500),
        ("cacheReadInputTokenCount", 550),
    ):
        detail = coerce_token_usage_record(
            {"usage": {"input_tokens": 900, "output_tokens": 3, key: value}}
        )
        assert detail.cache_read == value, key
        # Subset of reported input → no double-count expansion.
        assert detail.tokens_in == 900, key


def test_shared_seam_cache_write_aliases_and_nested_details():
    top = coerce_token_usage_record(
        {
            "usage": {
                "prompt_tokens": 800,
                "completion_tokens": 5,
                "cacheWriteInputTokens": 120,
            }
        }
    )
    assert top.cache_write == 120
    assert top.tokens_in == 800

    nested = coerce_token_usage_record(
        {
            "usage": {
                "prompt_tokens": 1_500,
                "completion_tokens": 7,
                "prompt_tokens_details": {
                    "cacheReadInputTokens": 900,
                    "cacheWriteInputTokenCount": 80,
                },
            }
        }
    )
    assert nested.cache_read == 900
    assert nested.cache_write == 80
    assert nested.tokens_in == 1_500


def test_shared_seam_bedrock_uncached_expands_without_double_count():
    """Bedrock reports inputTokens as uncached-only; expand once."""
    detail = coerce_token_usage_record(
        {
            "usage": {
                "inputTokens": 10,
                "outputTokens": 5,
                "cacheReadInputTokens": 100,
                "cacheWriteInputTokens": 20,
            }
        }
    )
    assert detail.tokens_in == 130
    assert detail.cache_read == 100
    assert detail.cache_write == 20


# ---------------------------------------------------------------------------
# OpenAICompatDriver
# ---------------------------------------------------------------------------


def _fake_json_response(payload: dict):
    raw = json.dumps(payload).encode("utf-8")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return raw

    return FakeResponse()


def _capturing_openai_response(monkeypatch, captured):
    def mock_urlopen(req, timeout=None):
        captured.update(json.loads(req.data.decode("utf-8")))
        return _fake_json_response({
            "choices": [{
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)


def _capturing_openai_stream(monkeypatch, captured):
    sse = (
        'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
        'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n'
        "data: [DONE]\n\n"
    )

    class FakeStream:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def __iter__(self):
            for line in sse.splitlines(keepends=True):
                yield line.encode("utf-8")

    def mock_urlopen(req, timeout=None):
        captured.update(json.loads(req.data.decode("utf-8")))
        return FakeStream()

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)


def test_openai_compat_gpt_5_uses_max_completion_tokens(monkeypatch):
    driver = OpenAICompatDriver(
        name="openai:gpt-5.6-luna",
        model="gpt-5.6-luna",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        max_tokens=8000,
    )
    driver._key = lambda: "fake-key"
    captured = {}
    _capturing_openai_response(monkeypatch, captured)

    response = driver.complete("hi", system="sys")

    assert response.error is None
    assert captured["max_completion_tokens"] == 8000
    assert "max_tokens" not in captured
    assert "temperature" not in captured


def test_openai_compat_gpt_5_tools_disable_reasoning_effort(monkeypatch):
    driver = OpenAICompatDriver(
        name="openai:gpt-5.6-luna",
        model="gpt-5.6-luna",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        enable_reasoning=True,
    )
    driver._key = lambda: "fake-key"
    captured = {}
    _capturing_openai_response(monkeypatch, captured)

    response = driver.chat(
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "noop", "parameters": {}}}],
    )

    assert response.error is None
    assert captured["reasoning_effort"] == "none"
    assert "reasoning" not in captured


def test_openai_compat_gpt_5_streaming_tools_disable_reasoning_effort(monkeypatch):
    driver = OpenAICompatDriver(
        name="openai:gpt-5.6-luna",
        model="gpt-5.6-luna",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        enable_reasoning=True,
    )
    driver._key = lambda: "fake-key"
    captured = {}
    _capturing_openai_stream(monkeypatch, captured)

    response = driver.chat_stream(
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "noop", "parameters": {}}}],
        on_delta=lambda _text: None,
    )

    assert response.error is None
    assert captured["reasoning_effort"] == "none"
    assert "reasoning" not in captured


def test_openai_compat_gpt_5_extra_body_cannot_reintroduce_rejected_fields(monkeypatch):
    driver = OpenAICompatDriver(
        name="openai:gpt-5.6-luna",
        model="gpt-5.6-luna",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        max_tokens=8000,
        extra_body={
            "temperature": 0.2,
            "max_tokens": 99,
            "reasoning": {"max_tokens": 1024},
        },
    )
    driver._key = lambda: "fake-key"
    captured = {}
    _capturing_openai_response(monkeypatch, captured)

    response = driver.complete("hi", system="sys")

    assert response.error is None
    assert captured["max_completion_tokens"] == 8000
    assert "max_tokens" not in captured
    assert "temperature" not in captured
    assert "reasoning" not in captured


def test_openai_compat_gpt_5_omits_openrouter_reasoning_without_tools(monkeypatch):
    driver = OpenAICompatDriver(
        name="openai:gpt-5.6-luna",
        model="gpt-5.6-luna",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        enable_reasoning=True,
    )
    driver._key = lambda: "fake-key"
    captured = {}
    _capturing_openai_response(monkeypatch, captured)

    response = driver.chat([{"role": "user", "content": "hi"}])

    assert response.error is None
    assert "reasoning" not in captured
    assert "reasoning_effort" not in captured


def test_openai_compat_gpt_5_tools_keep_reasoning_effort_none_after_extra_body(monkeypatch):
    driver = OpenAICompatDriver(
        name="openai:gpt-5.6-luna",
        model="gpt-5.6-luna",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        extra_body={"reasoning_effort": "high", "temperature": 0.2},
    )
    driver._key = lambda: "fake-key"
    captured = {}
    _capturing_openai_response(monkeypatch, captured)

    response = driver.chat(
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "noop", "parameters": {}}}],
    )

    assert response.error is None
    assert captured["reasoning_effort"] == "none"
    assert "temperature" not in captured
    assert "reasoning" not in captured


def test_openai_compat_legacy_provider_keeps_max_tokens(monkeypatch):
    driver = OpenAICompatDriver(
        name="legacy:model",
        model="legacy-model",
        base_url="https://api.example.com/v1",
        api_key_env="LEGACY_API_KEY",
        max_tokens=1500,
    )
    driver._key = lambda: "fake-key"
    captured = {}
    _capturing_openai_response(monkeypatch, captured)

    response = driver.complete("hi", system="sys")

    assert response.error is None
    assert captured["max_tokens"] == 1500
    assert "max_completion_tokens" not in captured
    assert captured["temperature"] == 0.0


def test_openai_compat_usage_meta_reads_alias_shapes(monkeypatch):
    driver = OpenAICompatDriver(
        name="openai-test",
        model="gpt-4o",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
    )
    driver._key = lambda: "fake-key"

    shapes = [
        {
            "prompt_tokens": 200,
            "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 150},
            "expected_read": 150,
            "expected_write": None,  # write field absent → omitted
        },
        {
            "prompt_tokens": 300,
            "completion_tokens": 12,
            "cached_input_tokens": 180,
            "cacheWriteInputTokens": 40,
            "expected_read": 180,
            "expected_write": 40,
        },
        {
            "input_tokens": 400,
            "output_tokens": 8,
            "input_tokens_details": {
                "cachedInputTokens": 220,
                "cacheWriteInputTokens": 30,
            },
            "expected_read": 220,
            "expected_write": 30,
        },
        {
            "prompt_tokens": 500,
            "completion_tokens": 9,
            "cacheReadInputTokenCount": 310,
            "expected_read": 310,
            "expected_write": None,
        },
    ]

    for shape in shapes:
        expected_read = shape["expected_read"]
        expected_write = shape["expected_write"]
        usage = {
            k: v
            for k, v in shape.items()
            if k not in ("expected_read", "expected_write")
        }
        expected_tin = int(
            usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        )

        def mock_urlopen(req, timeout=None, _usage=usage):
            return _fake_json_response(
                {
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": _usage,
                }
            )

        monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
        resp = driver.complete("hi", system="sys")
        assert resp.meta["cache_read_tokens"] == expected_read
        if expected_write is None:
            assert "cache_write_tokens" not in resp.meta
        else:
            assert resp.meta["cache_write_tokens"] == expected_write
        assert resp.meta["raw_usage"] == usage
        # Full prompt total is not inflated when cache is a subset.
        assert resp.tokens_in == expected_tin


def test_openai_compat_cache_fields_from_usage_uses_shared_seam():
    read, write = OpenAICompatDriver._cache_fields_from_usage(
        {
            "prompt_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 60},
            "cacheWriteInputTokens": 15,
        }
    )
    assert (read, write) == (60, 15)

    read, write = OpenAICompatDriver._cache_fields_from_usage(
        {"inputTokens": 50, "cacheReadInputTokens": 40, "cacheWriteInputTokenCount": 5}
    )
    # Uncached-only expansion applies in the shared seam; cache fields remain.
    assert (read, write) == (40, 5)


# ---------------------------------------------------------------------------
# Bedrock
# ---------------------------------------------------------------------------


def test_bedrock_converse_usage_aliases_reach_normalized_shape():
    for read_key, write_key in (
        ("cacheReadInputTokens", "cacheWriteInputTokens"),
        ("cacheReadInputTokenCount", "cacheWriteInputTokenCount"),
    ):
        raw = {
            "inputTokens": 12,
            "outputTokens": 4,
            read_key: 80,
            write_key: 16,
        }
        folded = _usage_from_converse_usage(raw)
        assert folded["prompt_tokens"] == 12 + 80 + 16
        assert folded["completion_tokens"] == 4
        assert folded["cached_tokens"] == 80
        assert folded["cache_write_tokens"] == 16
        assert folded["raw_usage"] is raw


def test_bedrock_turn_to_response_preserves_raw_and_camelcase_cache():
    driver = BedrockDriver(
        name="bedrock:claude",
        model="anthropic.claude-sonnet-4-20250514-v1:0",
    )
    raw_usage = {
        "inputTokens": 10,
        "outputTokens": 3,
        "cacheReadInputTokenCount": 90,
        "cacheWriteInputTokens": 10,
    }
    turn = SimpleNamespace(
        text="hi",
        tool_calls=[],
        finish_reason="end_turn",
        usage=raw_usage,
    )
    resp = driver._turn_to_response(turn, latency_ms=1.0)
    assert resp.tokens_in == 110
    assert resp.tokens_out == 3
    assert resp.meta["cache_read_tokens"] == 90
    assert resp.meta["cache_write_tokens"] == 10
    assert resp.meta["raw_usage"] == raw_usage


# ---------------------------------------------------------------------------
# Anthropic TTL provenance
# ---------------------------------------------------------------------------


def test_anthropic_inferred_ttl_is_explicit_not_provider_measured(monkeypatch):
    monkeypatch.delenv("HARNESS_ANTHROPIC_CACHE_TTL", raising=False)
    fields = _anthropic_usage_fields(
        {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_creation_input_tokens": 80,
            "cache_read_input_tokens": 10,
            # cache_creation omitted → TTL split is local inference
        }
    )
    assert fields["cache_write_tokens"] == 80
    assert fields["cache_read_tokens"] == 10
    assert fields["cache_write_1h_tokens"] == 80
    assert fields["cache_write_5m_tokens"] == 0
    assert fields["cache_write_ttl_basis"] == "inferred"
    assert fields["cache_write_basis"] == "provider"
    assert fields["cache_read_basis"] == "provider"


def test_anthropic_provider_ttl_split_marked_provider():
    fields = _anthropic_usage_fields(
        {
            "input_tokens": 50,
            "output_tokens": 10,
            "cache_creation_input_tokens": 40,
            "cache_read_input_tokens": 5,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 15,
                "ephemeral_1h_input_tokens": 25,
            },
        }
    )
    assert fields["cache_write_5m_tokens"] == 15
    assert fields["cache_write_1h_tokens"] == 25
    assert fields["cache_write_ttl_basis"] == "provider"
    assert fields["cache_read_basis"] == "provider"


def test_anthropic_omitted_cache_fields_are_absent_not_invented():
    fields = _anthropic_usage_fields(
        {"input_tokens": 40, "output_tokens": 8}
    )
    assert fields["cache_read_tokens"] == 0
    assert fields["cache_write_tokens"] == 0
    assert fields["cache_write_5m_tokens"] == 0
    assert fields["cache_write_1h_tokens"] == 0
    assert fields["cache_write_ttl_basis"] == "absent"
    assert fields["cache_read_basis"] == "absent"
    assert fields["cache_write_basis"] == "absent"


def test_anthropic_driver_meta_surfaces_ttl_provenance(monkeypatch):
    driver = AnthropicDriver(
        name="claude-test",
        model="claude-3-haiku-20240307",
        api_key_env="ANTHROPIC_API_KEY",
        enable_prompt_cache=True,
    )
    driver._key = lambda: "fake-key"
    monkeypatch.delenv("HARNESS_ANTHROPIC_CACHE_TTL", raising=False)

    def mock_urlopen(req, timeout=None):
        payload = {
            "content": [{"type": "text", "text": "ok"}],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_creation_input_tokens": 80,
                "cache_read_input_tokens": 10,
            },
            "stop_reason": "end_turn",
        }
        return _fake_json_response(payload)

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
    resp = driver.complete("hi", system="sys")
    assert resp.meta["cache_write_tokens"] == 80
    assert resp.meta["cache_read_tokens"] == 10
    assert resp.meta["cache_write_1h_tokens"] == 80
    assert resp.meta["cache_write_ttl_basis"] == "inferred"
    assert resp.meta["cache_write_basis"] == "provider"
    assert resp.meta["cache_read_basis"] == "provider"
    assert resp.meta["raw_usage"]["cache_creation_input_tokens"] == 80
    assert "cache_creation" not in resp.meta["raw_usage"]


# ---------------------------------------------------------------------------
# OpenAI-compat: omit absent cache fields; keep explicit zeros; served_model
# ---------------------------------------------------------------------------


def test_openai_compat_omits_absent_cache_fields_keeps_explicit_zero(monkeypatch):
    driver = OpenAICompatDriver(
        name="openai-test",
        model="gpt-4o",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
    )
    driver._key = lambda: "fake-key"

    # No cache fields at all → omit both keys; keep raw_usage.
    def mock_absent(req, timeout=None):
        return _fake_json_response(
            {
                "model": "gpt-4o-2024-08-06",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 40, "completion_tokens": 2},
            }
        )

    monkeypatch.setattr(urllib.request, "urlopen", mock_absent)
    resp = driver.complete("hi", system="sys")
    assert "cache_read_tokens" not in resp.meta
    assert "cache_write_tokens" not in resp.meta
    assert resp.meta["raw_usage"] == {"prompt_tokens": 40, "completion_tokens": 2}
    assert resp.meta["served_model"] == "gpt-4o-2024-08-06"

    # Explicit zero cache_read must be retained (not omitted / not invented).
    def mock_zero(req, timeout=None):
        return _fake_json_response(
            {
                "model": "gpt-4o-2024-08-06",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 40,
                    "completion_tokens": 2,
                    "prompt_tokens_details": {"cached_tokens": 0},
                    "cacheWriteInputTokens": 0,
                },
            }
        )

    monkeypatch.setattr(urllib.request, "urlopen", mock_zero)
    resp2 = driver.chat([{"role": "user", "content": "hi"}])
    assert resp2.meta["cache_read_tokens"] == 0
    assert resp2.meta["cache_write_tokens"] == 0
    assert resp2.meta["served_model"] == "gpt-4o-2024-08-06"


def test_openai_compat_usage_meta_direct_absent_vs_zero():
    absent = OpenAICompatDriver._usage_meta({"prompt_tokens": 10, "completion_tokens": 1})
    assert "cache_read_tokens" not in absent
    assert "cache_write_tokens" not in absent
    assert absent["raw_usage"]["prompt_tokens"] == 10

    zeroed = OpenAICompatDriver._usage_meta(
        {
            "prompt_tokens": 10,
            "completion_tokens": 1,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
        }
    )
    assert zeroed["cache_read_tokens"] == 0
    assert zeroed["cache_write_tokens"] == 0


def test_openai_compat_chat_stream_omits_absent_cache_and_captures_served(
    monkeypatch,
):
    driver = OpenAICompatDriver(
        name="openai-test",
        model="gpt-4o",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
    )
    driver._key = lambda: "fake-key"

    sse = (
        'data: {"id":"1","model":"gpt-4o-mini","choices":[{"delta":{"content":"hi"},'
        '"finish_reason":"stop"}]}\n\n'
        'data: {"id":"1","model":"gpt-4o-mini","choices":[],'
        '"usage":{"prompt_tokens":12,"completion_tokens":1}}\n\n'
        "data: [DONE]\n\n"
    )

    class FakeStream:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def __iter__(self):
            for line in sse.splitlines(keepends=True):
                yield line.encode("utf-8")

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: FakeStream())
    deltas = []
    resp = driver.chat_stream(
        [{"role": "user", "content": "hi"}],
        on_delta=deltas.append,
    )
    assert "".join(deltas) == "hi"
    assert "cache_read_tokens" not in resp.meta
    assert "cache_write_tokens" not in resp.meta
    assert resp.meta["raw_usage"] == {"prompt_tokens": 12, "completion_tokens": 1}
    assert resp.meta["served_model"] == "gpt-4o-mini"


def test_openai_compat_never_infers_served_model_when_absent(monkeypatch):
    driver = OpenAICompatDriver(
        name="openai-test",
        model="gpt-4o",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
    )
    driver._key = lambda: "fake-key"

    def mock_urlopen(req, timeout=None):
        return _fake_json_response(
            {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            }
        )

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
    resp = driver.complete("hi", system="sys")
    assert "served_model" not in resp.meta
