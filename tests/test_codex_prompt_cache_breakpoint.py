"""GPT-5.6 Codex prompt-cache: durable key, breakpoint placement, usage, meters.

Hermetic wire-shape / parsing tests. Live network checks are opt-in via
``HARNESS_LIVE_CODEX_CACHE=1`` and are skipped in the default suite.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from types import SimpleNamespace
from typing import Any, List

import pytest

from harness.send_loop_phases import meter_pilot_step
from pmharness.drivers.codex_responses import CodexResponsesDriver
from pmharness.drivers.prompt_cache import (
    CODEX_CACHE_BOUNDARY_TEXT,
    apply_codex_responses_prompt_cache,
    body_has_codex_prompt_cache_extensions,
    clear_codex_prompt_cache_breakpoint_memory,
    codex_prompt_cache_breakpoint_enabled,
    codex_prompt_cache_breakpoint_known_unsupported,
    codex_prompt_cache_unsupported_error,
    codex_request_cache_snapshot,
    durable_codex_prompt_cache_key,
    mark_codex_prompt_cache_breakpoint_unsupported,
    strip_codex_prompt_cache_extensions,
    supports_gpt56_explicit_prompt_cache,
)
from pmharness.drivers.token_usage import coerce_token_usage_record


@pytest.fixture(autouse=True)
def _clear_codex_breakpoint_memory():
    clear_codex_prompt_cache_breakpoint_memory()
    yield
    clear_codex_prompt_cache_breakpoint_memory()


def _header_value(headers: dict[str, str], name: str) -> str | None:
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def _tools() -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        }
    ]


def _boundary_breakpoint(body: dict) -> dict | None:
    for item in body.get("input") or []:
        if not isinstance(item, dict):
            continue
        if item.get("role") != "developer":
            continue
        for part in item.get("content") or []:
            if (
                isinstance(part, dict)
                and part.get("text") == CODEX_CACHE_BOUNDARY_TEXT
            ):
                return part.get("prompt_cache_breakpoint")
    return None


def test_supports_gpt56_family_detection():
    assert supports_gpt56_explicit_prompt_cache("gpt-5.6-sol")
    assert supports_gpt56_explicit_prompt_cache("openai/gpt-5.6-terra")
    assert supports_gpt56_explicit_prompt_cache("openai-codex:gpt-5.6-luna")
    assert supports_gpt56_explicit_prompt_cache("gpt-5.7")
    assert not supports_gpt56_explicit_prompt_cache("gpt-5.5")
    assert not supports_gpt56_explicit_prompt_cache("gpt-5")
    assert not supports_gpt56_explicit_prompt_cache("claude-opus-4")


def test_breakpoint_gate_auto_and_env(monkeypatch):
    monkeypatch.delenv("HARNESS_PROMPT_CACHE", raising=False)
    monkeypatch.setenv("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT", "auto")
    assert codex_prompt_cache_breakpoint_enabled("gpt-5.6-sol") is True
    assert codex_prompt_cache_breakpoint_enabled("gpt-5.5") is False

    monkeypatch.setenv("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT", "off")
    assert codex_prompt_cache_breakpoint_enabled("gpt-5.6-sol") is False

    monkeypatch.setenv("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT", "on")
    assert codex_prompt_cache_breakpoint_enabled("gpt-5.5") is True

    monkeypatch.setenv("HARNESS_PROMPT_CACHE", "0")
    monkeypatch.setenv("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT", "on")
    assert codex_prompt_cache_breakpoint_enabled("gpt-5.6-sol") is False


def test_durable_cache_key_stable_uuid_per_session():
    a1 = durable_codex_prompt_cache_key("abc123def456")
    a2 = durable_codex_prompt_cache_key("abc123def456")
    b = durable_codex_prompt_cache_key("abc123def457")
    assert a1 == a2
    assert a1 != b
    uuid.UUID(a1)  # raises if not UUID-shaped
    assert durable_codex_prompt_cache_key(None) is None
    assert durable_codex_prompt_cache_key("") is None

    raw = "550e8400-e29b-41d4-a716-446655440000"
    assert durable_codex_prompt_cache_key(raw) == raw
    assert durable_codex_prompt_cache_key(raw.replace("-", "")) == raw


def test_build_body_gpt56_places_breakpoint_before_user_history(monkeypatch):
    monkeypatch.delenv("HARNESS_PROMPT_CACHE", raising=False)
    monkeypatch.setenv("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT", "auto")
    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5.6-sol")
    body = driver._build_body(
        [
            {"role": "user", "content": "turn one"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "turn two"},
        ],
        tools=_tools(),
        system="You are Marionette.",
        session_id="sess-cache-1",
    )
    assert body["prompt_cache_key"] == durable_codex_prompt_cache_key("sess-cache-1")
    assert body["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
    assert body["store"] is False
    assert body["stream"] is True
    assert _boundary_breakpoint(body) == {"mode": "explicit"}

    inp = body["input"]
    assert inp[0]["role"] == "developer"
    assert inp[0]["content"][0]["text"] == CODEX_CACHE_BOUNDARY_TEXT
    # Changing user/tool history stays AFTER the stable boundary.
    roles_after = [i.get("role") or i.get("type") for i in inp[1:]]
    assert "user" in roles_after
    assert all(
        not (
            isinstance(part, dict) and part.get("prompt_cache_breakpoint")
        )
        for item in inp[1:]
        if isinstance(item, dict)
        for part in (item.get("content") or [])
    )


def test_build_body_omits_breakpoint_for_pre_gpt56(monkeypatch):
    monkeypatch.delenv("HARNESS_PROMPT_CACHE", raising=False)
    monkeypatch.setenv("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT", "auto")
    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5.5")
    body = driver._build_body(
        [{"role": "user", "content": "hello"}],
        tools=_tools(),
        session_id="sess-old",
    )
    assert body["prompt_cache_key"] == durable_codex_prompt_cache_key("sess-old")
    assert "prompt_cache_options" not in body
    assert _boundary_breakpoint(body) is None


def test_build_body_breakpoint_env_off(monkeypatch):
    monkeypatch.setenv("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT", "off")
    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5.6-sol")
    body = driver._build_body(
        [{"role": "user", "content": "hello"}],
        session_id="sess-off",
    )
    assert body["prompt_cache_key"] == durable_codex_prompt_cache_key("sess-off")
    assert "prompt_cache_options" not in body
    assert _boundary_breakpoint(body) is None


def test_stable_key_identical_across_turns_and_pilot_steps(monkeypatch):
    monkeypatch.setenv("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT", "auto")
    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5.6-terra")
    sid = "chat-affinity-9"
    keys: List[str] = []
    for msgs in (
        [{"role": "user", "content": "a"}],
        [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ],
        [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "file"},
            {"role": "user", "content": "continue"},
        ],
    ):
        body = driver._build_body(msgs, tools=_tools(), session_id=sid)
        keys.append(body["prompt_cache_key"])
        assert _boundary_breakpoint(body) == {"mode": "explicit"}
        # Boundary remains index 0 even as history grows.
        assert body["input"][0]["content"][0]["text"] == CODEX_CACHE_BOUNDARY_TEXT
    assert keys == [keys[0], keys[0], keys[0]]


def test_different_sessions_get_different_keys(monkeypatch):
    monkeypatch.setenv("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT", "off")
    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5.6-sol")
    a = driver._build_body(
        [{"role": "user", "content": "x"}], session_id="sess-a",
    )["prompt_cache_key"]
    b = driver._build_body(
        [{"role": "user", "content": "x"}], session_id="sess-b",
    )["prompt_cache_key"]
    assert a != b


def test_wire_shape_ab_breakpoint_arm(monkeypatch):
    """Deterministic A/B seam: same messages, breakpoint on vs off."""
    monkeypatch.delenv("HARNESS_PROMPT_CACHE", raising=False)
    msgs = [{"role": "user", "content": "hello"}]
    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5.6-sol")

    monkeypatch.setenv("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT", "on")
    with_bp = driver._build_body(msgs, tools=_tools(), session_id="ab-sess")
    monkeypatch.setenv("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT", "off")
    without = driver._build_body(msgs, tools=_tools(), session_id="ab-sess")

    assert with_bp["prompt_cache_key"] == without["prompt_cache_key"]
    assert "prompt_cache_options" in with_bp
    assert "prompt_cache_options" not in without
    assert len(with_bp["input"]) == len(without["input"]) + 1
    assert _boundary_breakpoint(with_bp) == {"mode": "explicit"}
    assert _boundary_breakpoint(without) is None


def test_apply_codex_kill_switch_removes_all_markers(monkeypatch):
    """HARNESS_PROMPT_CACHE=0 clears key + extensions; no Codex cache markers."""
    monkeypatch.setenv("HARNESS_PROMPT_CACHE", "0")
    monkeypatch.setenv("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT", "on")
    body = {
        "model": "gpt-5.6-sol",
        "prompt_cache_key": "stale-key",
        "prompt_cache_options": {"mode": "explicit", "ttl": "30m"},
        "input": [
            {
                "type": "message",
                "role": "developer",
                "content": [
                    {
                        "type": "input_text",
                        "text": CODEX_CACHE_BOUNDARY_TEXT,
                        "prompt_cache_breakpoint": {"mode": "explicit"},
                    }
                ],
            },
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
            },
        ],
    }
    detail = apply_codex_responses_prompt_cache(
        body, model="gpt-5.6-sol", session_id="kill-sess",
    )
    assert detail["reason"] == "cache_disabled"
    assert detail["prompt_cache_key"] is None
    assert detail["breakpoint"] is False
    assert "prompt_cache_key" not in body
    assert "prompt_cache_options" not in body
    assert not body_has_codex_prompt_cache_extensions(body)
    assert _boundary_breakpoint(body) is None
    # Driver path must also leave no markers under the kill switch.
    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5.6-sol")
    built = driver._build_body(
        [{"role": "user", "content": "hello"}],
        session_id="kill-sess",
    )
    assert "prompt_cache_key" not in built
    assert "prompt_cache_options" not in built
    assert _boundary_breakpoint(built) is None


def test_codex_request_cache_snapshot_is_sanitized():
    body = {
        "prompt_cache_key": "11111111-1111-1111-1111-111111111111",
        "prompt_cache_options": {"mode": "explicit", "ttl": "30m", "secret": "nope"},
        "instructions": "SYSTEM PROMPT MUST NOT LEAK",
        "Authorization": "Bearer tok-secret",
        "input": [
            {
                "type": "message",
                "role": "developer",
                "content": [
                    {
                        "type": "input_text",
                        "text": CODEX_CACHE_BOUNDARY_TEXT,
                        "prompt_cache_breakpoint": {"mode": "explicit"},
                    }
                ],
            },
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "user secret prompt"}],
            },
        ],
    }
    snap = codex_request_cache_snapshot(body)
    assert snap["prompt_cache_key_present"] is True
    assert snap["prompt_cache_key"] == "11111111-1111-1111-1111-111111111111"
    assert snap["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
    assert snap["explicit_breakpoint"] is True
    blob = json.dumps(snap)
    assert "SYSTEM PROMPT" not in blob
    assert "user secret" not in blob
    assert "Bearer" not in blob
    assert "tok-secret" not in blob
    assert CODEX_CACHE_BOUNDARY_TEXT not in blob


def test_strip_extensions_keeps_cache_key():
    body = {
        "prompt_cache_key": "keep-me",
        "prompt_cache_options": {"mode": "explicit", "ttl": "30m"},
        "input": [
            {
                "type": "message",
                "role": "developer",
                "content": [
                    {
                        "type": "input_text",
                        "text": CODEX_CACHE_BOUNDARY_TEXT,
                        "prompt_cache_breakpoint": {"mode": "explicit"},
                    }
                ],
            },
            {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "hi",
                        "prompt_cache_breakpoint": {"mode": "explicit"},
                    }
                ],
            },
        ],
    }
    assert body_has_codex_prompt_cache_extensions(body) is True
    strip_codex_prompt_cache_extensions(body)
    assert body["prompt_cache_key"] == "keep-me"
    assert "prompt_cache_options" not in body
    assert body_has_codex_prompt_cache_extensions(body) is False
    assert len(body["input"]) == 1
    assert body["input"][0]["role"] == "user"
    assert "prompt_cache_breakpoint" not in body["input"][0]["content"][0]


def test_unsupported_error_detector():
    assert codex_prompt_cache_unsupported_error(
        '{"detail":"Unsupported parameter: \'prompt_cache_options\'"}'
    )
    assert codex_prompt_cache_unsupported_error(
        "Unknown parameter: 'prompt_cache_breakpoint'"
    )
    assert not codex_prompt_cache_unsupported_error('{"detail":"Stream must be set to true"}')


def test_apply_helper_idempotent(monkeypatch):
    monkeypatch.setenv("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT", "on")
    body = {
        "model": "gpt-5.6-sol",
        "instructions": "sys",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
            }
        ],
        "store": False,
        "stream": True,
    }
    apply_codex_responses_prompt_cache(body, model="gpt-5.6-sol", session_id="id1")
    apply_codex_responses_prompt_cache(body, model="gpt-5.6-sol", session_id="id1")
    boundaries = [
        i for i in body["input"]
        if isinstance(i, dict)
        and i.get("role") == "developer"
        and (i.get("content") or [{}])[0].get("text") == CODEX_CACHE_BOUNDARY_TEXT
    ]
    assert len(boundaries) == 1


def test_usage_parsing_cached_and_cache_write_variants():
    detail = coerce_token_usage_record(
        {
            "usage": {
                "input_tokens": 12_000,
                "output_tokens": 40,
                "input_tokens_details": {
                    "cached_tokens": 9_000,
                    "cache_write_tokens": 1_500,
                },
            }
        }
    )
    assert detail.cache_read == 9_000
    assert detail.cache_write == 1_500
    assert detail.tokens_in == 12_000

    # Omitted cache fields must stay zero — never invent hits.
    empty = coerce_token_usage_record(
        {"usage": {"input_tokens": 100, "output_tokens": 5}}
    )
    assert empty.cache_read == 0
    assert empty.cache_write == 0

    camel = coerce_token_usage_record(
        {
            "usage": {
                "inputTokens": 500,
                "outputTokens": 10,
                "inputTokensDetails": {
                    "cachedTokens": 200,
                    "cacheWriteTokens": 50,
                },
            }
        }
    )
    assert camel.cache_read == 200
    assert camel.cache_write == 50

    # Provider/CLI aliases: top-level cached_input_tokens / cachedInputTokens.
    top_cached = coerce_token_usage_record(
        {
            "usage": {
                "input_tokens": 1_000,
                "output_tokens": 5,
                "cached_input_tokens": 800,
            }
        }
    )
    assert top_cached.cache_read == 800
    assert top_cached.cache_write == 0
    assert top_cached.tokens_in == 1_000

    top_cached_camel = coerce_token_usage_record(
        {
            "usage": {
                "inputTokens": 2_000,
                "outputTokens": 8,
                "cachedInputTokens": 1_500,
            }
        }
    )
    assert top_cached_camel.cache_read == 1_500
    assert top_cached_camel.cache_write == 0

    # Top-level cacheWriteInputTokens.
    top_write = coerce_token_usage_record(
        {
            "usage": {
                "input_tokens": 3_000,
                "output_tokens": 12,
                "cacheWriteInputTokens": 600,
            }
        }
    )
    assert top_write.cache_read == 0
    assert top_write.cache_write == 600
    assert top_write.tokens_in == 3_000

    # Detail-dict aliases (snake and camel).
    detail_aliases = coerce_token_usage_record(
        {
            "usage": {
                "input_tokens": 5_000,
                "output_tokens": 15,
                "input_tokens_details": {
                    "cached_input_tokens": 3_000,
                    "cacheWriteInputTokens": 400,
                },
            }
        }
    )
    assert detail_aliases.cache_read == 3_000
    assert detail_aliases.cache_write == 400

    detail_aliases_camel = coerce_token_usage_record(
        {
            "usage": {
                "inputTokens": 4_000,
                "outputTokens": 10,
                "inputTokensDetails": {
                    "cachedInputTokens": 2_500,
                    "cacheWriteInputTokens": 300,
                },
            }
        }
    )
    assert detail_aliases_camel.cache_read == 2_500
    assert detail_aliases_camel.cache_write == 300

    # Precedence: existing keys win over aliases when both present.
    precedence = coerce_token_usage_record(
        {
            "usage": {
                "input_tokens": 1_000,
                "output_tokens": 5,
                "cache_read_tokens": 700,
                "cached_input_tokens": 900,
                "cacheWriteInputTokens": 100,
                "cache_write_tokens": 200,
            }
        }
    )
    assert precedence.cache_read == 700
    assert precedence.cache_write == 200

    # Bedrock-style cacheReadInputTokens / cacheReadInputTokenCount aliases.
    bedrock_alias = coerce_token_usage_record(
        {
            "usage": {
                "input_tokens": 900,
                "output_tokens": 4,
                "cacheReadInputTokens": 600,
            }
        }
    )
    assert bedrock_alias.cache_read == 600
    count_alias = coerce_token_usage_record(
        {
            "usage": {
                "input_tokens": 800,
                "output_tokens": 3,
                "cacheReadInputTokenCount": 500,
            }
        }
    )
    assert count_alias.cache_read == 500


def test_response_from_raw_preserves_raw_usage_and_cache_write():
    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5.6-sol")
    raw = {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "ok"}],
            }
        ],
        "usage": {
            "input_tokens": 10_000,
            "output_tokens": 20,
            "input_tokens_details": {
                "cached_tokens": 0,
                "cache_write_tokens": 4_000,
            },
        },
    }
    resp = driver._response_from_raw(raw, t0=time.time())
    assert resp.meta["raw_usage"]["input_tokens_details"]["cache_write_tokens"] == 4_000
    # Explicit provider zero is retained (not invented, not omitted).
    assert resp.meta["cache_read_tokens"] == 0
    assert resp.meta["cache_write_tokens"] == 4_000


def test_meter_integration_cache_write_without_inventing_read(monkeypatch):
    meters: dict[str, Any] = {}
    session = SimpleNamespace(
        _tokens_used=0,
        _tokens_out=0,
        _turn_output_tokens=0,
        _tokens_in=0,
        _last_prompt_tokens=0,
        _tokens_cached=0,
        _tokens_cache_write=0,
        _tokens_cache_write_5m=0,
        _tokens_cache_write_1h=0,
        _last_turn_cache_read_tokens=0,
        _last_prompt_cache_activity_at=0.0,
        _plan_billing=False,
        _price_source="",
        _provider_cost_usd=0.0,
        _provider_billed_tokens_in=0,
        _provider_billed_tokens_out=0,
        _provider_billed_tokens_cached=0,
        _provider_billed_tokens_cache_write=0,
        _provider_billed_tokens_cache_write_5m=0,
        _provider_billed_tokens_cache_write_1h=0,
        config=SimpleNamespace(driver="openai-codex/gpt-5.6-sol"),
        _accumulate_session_meters=lambda **kw: meters.update(kw),
    )
    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5.6-sol")
    resp = driver._response_from_raw(
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "ok"}],
                }
            ],
            "usage": {
                "input_tokens": 8_000,
                "output_tokens": 10,
                "input_tokens_details": {
                    "cached_tokens": 6_000,
                    "cache_write_tokens": 500,
                },
            },
        },
        t0=time.time(),
    )
    monkeypatch.setattr(
        "pmharness.registry.resolve_price_with_source",
        lambda _name: (3.0, 15.0, "catalog"),
        raising=False,
    )
    from harness.api.cost_accounting import _session_cost

    monkeypatch.setattr("harness.server._session_cost", _session_cost, raising=False)
    meter_pilot_step(session, resp, prompt="hello")
    assert session._tokens_cached == 6_000
    assert session._tokens_cache_write == 500
    assert session._last_turn_cache_read_tokens == 6_000
    assert resp.meta["raw_usage"]["input_tokens_details"]["cached_tokens"] == 6_000


def test_http_400_strips_breakpoint_and_retries(monkeypatch):
    """Codex backend rejecting prompt_cache_options → strip + retry with key only."""
    monkeypatch.setenv("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT", "on")
    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5.6-sol")
    bodies: list[dict] = []
    captured_headers: list[dict[str, str]] = []
    from pmharness.drivers.codex_responses import _codex_logical_thread_id

    expected_key = durable_codex_prompt_cache_key("retry-sess")
    expected_thread = _codex_logical_thread_id(expected_key)

    class _FakeHTTPError(Exception):
        def __init__(self, code: int, payload: bytes):
            self.code = code
            self._payload = payload

        def read(self):
            return self._payload

    posted = {"n": 0}

    def fake_urlopen(req, timeout=0):  # noqa: ARG001
        body = json.loads(req.data.decode("utf-8"))
        bodies.append(body)
        captured_headers.append(dict(req.headers))
        posted["n"] += 1
        if posted["n"] == 1:
            assert "prompt_cache_options" in body
            raise _FakeHTTPError(
                400,
                b'{"detail":"Unsupported parameter: \'prompt_cache_options\'"}',
            )

        # Second attempt: extensions gone, key retained.
        assert "prompt_cache_options" not in body
        assert body.get("prompt_cache_key")
        assert _boundary_breakpoint(body) is None

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def __iter__(self):
                done = {
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                        "output": [],
                    },
                }
                yield f"data: {json.dumps(done)}\n".encode("utf-8")

        return _Resp()

    import urllib.error
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    # Make isinstance checks see our fake as HTTPError for the driver path.
    monkeypatch.setattr(urllib.error, "HTTPError", _FakeHTTPError)
    monkeypatch.setenv("OPENAI_CODEX_TOKEN", "tok-test")

    # Bypass credential pool; use env token.
    monkeypatch.setattr(driver, "_key", lambda: "tok-test")

    body = driver._build_body(
        [{"role": "user", "content": "hi"}],
        session_id="retry-sess",
    )
    assert "prompt_cache_options" in body
    resp = driver._post_stream(body)
    assert resp.error is None
    assert posted["n"] == 2
    assert len(bodies) == 2
    assert len(captured_headers) == 2
    assert expected_thread and expected_thread != expected_key
    for hdrs in captured_headers:
        assert _header_value(hdrs, "session-id") == expected_key
        assert _header_value(hdrs, "thread-id") == expected_thread
        # Native Codex: x-client-request-id = logical thread id (stable).
        assert _header_value(hdrs, "x-client-request-id") == expected_thread
    assert _header_value(captured_headers[0], "session-id") == _header_value(
        captured_headers[1], "session-id"
    )
    assert _header_value(captured_headers[0], "x-client-request-id") == _header_value(
        captured_headers[1], "x-client-request-id"
    )
    # Capability memory: later builds omit the rejected extensions.
    assert codex_prompt_cache_breakpoint_known_unsupported(
        driver.base_url, driver.model,
    )
    next_body = driver._build_body(
        [{"role": "user", "content": "again"}],
        session_id="retry-sess",
    )
    assert next_body["prompt_cache_key"] == expected_key
    assert "prompt_cache_options" not in next_body
    assert _boundary_breakpoint(next_body) is None
    # Receipt diagnostics: initial had explicit breakpoint/options; final stripped.
    pc = (resp.meta or {}).get("prompt_cache") or {}
    assert pc["initial"]["explicit_breakpoint"] is True
    assert pc["initial"]["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
    assert pc["initial"]["prompt_cache_key"] == expected_key
    assert pc["final"]["explicit_breakpoint"] is False
    assert pc["final"]["prompt_cache_options"] is None
    assert pc["final"]["prompt_cache_key"] == expected_key
    assert pc["final"]["prompt_cache_key_present"] is True
    diag_blob = json.dumps(pc)
    assert "Authorization" not in diag_blob
    assert "tok-test" not in diag_blob
    assert "hi" not in diag_blob


def test_post_stream_preserves_cache_diagnostics_on_error(monkeypatch):
    monkeypatch.setenv("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT", "on")
    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5.6-sol")
    monkeypatch.setattr(driver, "_key", lambda: "tok-test")
    monkeypatch.setenv("OPENAI_CODEX_TOKEN", "tok-test")

    class _FakeHTTPError(Exception):
        def __init__(self, code: int, payload: bytes):
            self.code = code
            self._payload = payload

        def read(self):
            return self._payload

    def fake_urlopen(req, timeout=0):  # noqa: ARG001
        raise _FakeHTTPError(400, b'{"detail":"Stream must be set to true"}')

    import urllib.error
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(urllib.error, "HTTPError", _FakeHTTPError)

    body = driver._build_body(
        [{"role": "user", "content": "secret-user-text"}],
        session_id="diag-err-sess",
    )
    expected_key = durable_codex_prompt_cache_key("diag-err-sess")
    resp = driver._post_stream(body)
    assert resp.error
    pc = (resp.meta or {}).get("prompt_cache") or {}
    assert pc["initial"]["explicit_breakpoint"] is True
    assert pc["final"]["explicit_breakpoint"] is True
    assert pc["final"]["prompt_cache_key"] == expected_key
    assert "secret-user-text" not in json.dumps(pc)


def test_build_body_exception_fallback_uses_durable_key(monkeypatch):
    """_build_body except-path must not stamp the raw session id."""
    monkeypatch.setenv("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT", "off")
    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5")
    sid = "fallback-raw-sess"

    def boom(*_a, **_k):
        raise RuntimeError("stamp failed")

    monkeypatch.setattr(
        "pmharness.drivers.prompt_cache.apply_codex_responses_prompt_cache",
        boom,
    )
    body = driver._build_body(
        [{"role": "user", "content": "hi"}],
        session_id=sid,
    )
    assert body["prompt_cache_key"] == durable_codex_prompt_cache_key(sid)
    assert body["prompt_cache_key"] != sid


def test_complete_propagates_session_id_to_cache_key(monkeypatch):
    monkeypatch.setenv("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT", "off")
    posted: list[dict] = []

    class _Capture(CodexResponsesDriver):
        def _post_stream(self, body, **kwargs):
            posted.append(body)
            return SimpleNamespace(
                text="ok",
                tokens_in=0,
                tokens_out=0,
                latency_ms=0,
                model=self.name,
                meta={},
                error=None,
            )

    driver = _Capture(name="openai-codex/test", model="gpt-5")
    sid = "complete-sess-1"
    driver.complete("ping", session_id=sid)
    assert posted
    assert posted[0]["prompt_cache_key"] == durable_codex_prompt_cache_key(sid)


def test_complete_without_session_id_omits_cache_key(monkeypatch):
    """Compaction / synthetic callers omit session_id → no cache key stamp."""
    monkeypatch.setenv("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT", "off")
    posted: list[dict] = []

    class _Capture(CodexResponsesDriver):
        def _post_stream(self, body, **kwargs):
            posted.append(body)
            return SimpleNamespace(
                text="summary",
                tokens_in=0,
                tokens_out=0,
                latency_ms=0,
                model=self.name,
                meta={},
                error=None,
            )

    driver = _Capture(name="openai-codex/test", model="gpt-5")
    driver.complete("summarize this", system="You are a compaction summarizer.")
    assert posted
    assert "prompt_cache_key" not in posted[0]


def test_breakpoint_capability_memory_skips_repeat_extensions(monkeypatch):
    monkeypatch.setenv("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT", "on")
    base = "https://chatgpt.com/backend-api/codex"
    model = "gpt-5.6-sol"
    assert not codex_prompt_cache_breakpoint_known_unsupported(base, model)
    mark_codex_prompt_cache_breakpoint_unsupported(base, model)
    body = {
        "model": model,
        "instructions": "sys",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
            }
        ],
    }
    detail = apply_codex_responses_prompt_cache(
        body, model=model, session_id="mem-sess", base_url=base,
    )
    assert detail["reason"] == "breakpoint_unsupported_cached"
    assert detail["breakpoint"] is False
    assert body["prompt_cache_key"] == durable_codex_prompt_cache_key("mem-sess")
    assert "prompt_cache_options" not in body
    # Different model is unaffected.
    other = {
        "model": "gpt-5.6-terra",
        "input": list(body["input"]),
    }
    other_detail = apply_codex_responses_prompt_cache(
        other, model="gpt-5.6-terra", session_id="mem-sess", base_url=base,
    )
    assert other_detail["breakpoint"] is True


def test_continuation_aggregates_usage_cache_and_cost(monkeypatch):
    """Incomplete/length retries sum provider usage; text is not double-counted."""
    monkeypatch.setenv("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT", "off")
    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5.6-sol")
    monkeypatch.setattr(driver, "_key", lambda: "tok-test")
    monkeypatch.setenv("OPENAI_CODEX_TOKEN", "tok-test")

    attempts = {"n": 0}

    def fake_urlopen(req, timeout=0):  # noqa: ARG001
        attempts["n"] += 1
        n = attempts["n"]

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def __iter__(self):
                if n == 1:
                    item = {
                        "type": "message",
                        "id": "msg-a",
                        "content": [
                            {"type": "output_text", "text": "part-a "}
                        ],
                    }
                    terminal = {
                        "type": "response.incomplete",
                        "response": {
                            "status": "incomplete",
                            "incomplete_details": {"reason": "max_output_tokens"},
                            "usage": {
                                "input_tokens": 100,
                                "output_tokens": 50,
                                "cost": 0.01,
                                "input_tokens_details": {
                                    "cached_tokens": 80,
                                    "cache_write_tokens": 10,
                                },
                            },
                        },
                    }
                else:
                    item = {
                        "type": "message",
                        "id": "msg-b",
                        "content": [
                            {"type": "output_text", "text": "part-b"}
                        ],
                    }
                    terminal = {
                        "type": "response.completed",
                        "response": {
                            "status": "completed",
                            "usage": {
                                "input_tokens": 120,
                                "output_tokens": 30,
                                "cost": 0.02,
                                "input_tokens_details": {
                                    "cached_tokens": 90,
                                    "cache_write_tokens": 5,
                                },
                            },
                        },
                    }
                done = {
                    "type": "response.output_item.done",
                    "item": item,
                }
                yield f"data: {json.dumps(done)}\n".encode("utf-8")
                yield f"data: {json.dumps(terminal)}\n".encode("utf-8")

        return _Resp()

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    body = driver._build_body(
        [{"role": "user", "content": "continue me"}],
        session_id="cont-sess",
    )
    resp = driver._post_stream(body)
    assert resp.error is None
    assert attempts["n"] == 2
    assert resp.text == "part-a part-b"
    assert resp.tokens_in == 220
    assert resp.tokens_out == 80
    assert resp.meta["cache_read_tokens"] == 170
    assert resp.meta["cache_write_tokens"] == 15
    assert resp.meta["provider_cost_usd"] == pytest.approx(0.03)
    assert resp.meta["incomplete_retries"] == 1


def test_unrelated_http_400_is_not_stripped(monkeypatch):
    """Capability strip only fires when the 400 names cache extension fields."""
    monkeypatch.setenv("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT", "on")
    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5.6-sol")
    monkeypatch.setattr(driver, "_key", lambda: "tok-test")
    monkeypatch.setenv("OPENAI_CODEX_TOKEN", "tok-test")

    class _FakeHTTPError(Exception):
        def __init__(self, code: int, payload: bytes):
            self.code = code
            self._payload = payload

        def read(self):
            return self._payload

    def fake_urlopen(req, timeout=0):  # noqa: ARG001
        raise _FakeHTTPError(400, b'{"detail":"Stream must be set to true"}')

    import urllib.error
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(urllib.error, "HTTPError", _FakeHTTPError)

    body = driver._build_body(
        [{"role": "user", "content": "hi"}],
        session_id="no-strip-sess",
    )
    assert "prompt_cache_options" in body
    resp = driver._post_stream(body)
    assert resp.error
    assert "Stream must be set to true" in (resp.error or "")
    assert not codex_prompt_cache_breakpoint_known_unsupported(
        driver.base_url, driver.model,
    )


@pytest.mark.skipif(
    os.environ.get("HARNESS_LIVE_CODEX_CACHE", "").strip() not in ("1", "true", "yes"),
    reason="opt-in live Codex cache probe (HARNESS_LIVE_CODEX_CACHE=1)",
)
def test_live_codex_cache_probe_opt_in():
    """Live network smoke — never runs in the default suite."""
    pytest.skip("placeholder opt-in seam; enable with credentials + env flag")
