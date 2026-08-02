"""Codex incomplete / content_filter / reasoning-only continuation helpers."""
from __future__ import annotations

import json

import pytest

from pmharness.drivers.codex_responses import (
    _CODEX_INCOMPLETE_NUDGE,
    _CODEX_MAX_INCOMPLETE_RETRIES,
    _codex_continuation_kind,
    _consume_codex_sse,
    _extract_text_and_tools,
    _incomplete_reason,
    CodexResponsesDriver,
)


def test_content_filter_maps_to_finish_reason():
    raw = {
        "status": "incomplete",
        "incomplete_details": {"reason": "content_filter"},
        "output": [],
        "output_text": "",
    }
    text, tools, finish = _extract_text_and_tools(raw)
    assert finish == "content_filter"
    assert text == ""
    assert tools == []
    assert _codex_continuation_kind(finish, text, tools) is None


def test_reasoning_only_incomplete_needs_nudge():
    assert _codex_continuation_kind("incomplete", "", []) == "nudge"
    assert _CODEX_INCOMPLETE_NUDGE.startswith("[System:")


def test_partial_text_incomplete_needs_length_continue():
    assert _codex_continuation_kind("incomplete", "partial answer", []) == "length"


def test_incomplete_with_tools_does_not_continue():
    tools = [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]
    assert _codex_continuation_kind("incomplete", "", tools) is None


def test_consume_sse_captures_incomplete_details():
    lines = [
        b'data: {"type":"response.incomplete","response":{"status":"incomplete","incomplete_details":{"reason":"content_filter"},"usage":{}}}\n',
    ]
    raw = _consume_codex_sse(lines)
    assert raw["status"] == "incomplete"
    assert _incomplete_reason(raw) == "content_filter"
    _, _, finish = _extract_text_and_tools(raw)
    assert finish == "content_filter"


def _sse_incomplete_chunk(
    text: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cost: float,
    cached_tokens: int = 40,
    cache_write_tokens: int = 5,
):
    # OpenAI-style: input_tokens is the full prompt total (cached <= input).
    assert input_tokens >= cached_tokens + cache_write_tokens
    item = {
        "type": "message",
        "id": "msg",
        "content": [{"type": "output_text", "text": text}],
    }
    terminal = {
        "type": "response.incomplete",
        "response": {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost": cost,
                "input_tokens_details": {
                    "cached_tokens": cached_tokens,
                    "cache_write_tokens": cache_write_tokens,
                },
            },
        },
    }
    done = {"type": "response.output_item.done", "item": item}
    return [
        f"data: {json.dumps(done)}\n".encode("utf-8"),
        f"data: {json.dumps(terminal)}\n".encode("utf-8"),
    ]


def test_max_continuation_exhaustion_reports_completed_retries(monkeypatch):
    """Exhaustion meta reports completed attempts (= MAX), not post-increment."""
    monkeypatch.setenv("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT", "off")
    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5.6-sol")
    monkeypatch.setattr(driver, "_key", lambda: "tok-test")
    monkeypatch.setenv("OPENAI_CODEX_TOKEN", "tok-test")

    attempts = {"n": 0}
    # Original + MAX continuations that remain incomplete → exhaust.
    total_attempts = _CODEX_MAX_INCOMPLETE_RETRIES + 1

    def fake_urlopen(req, timeout=0):  # noqa: ARG001
        attempts["n"] += 1
        n = attempts["n"]
        lines = _sse_incomplete_chunk(
            f"part-{n} ",
            input_tokens=100 * n,
            output_tokens=5 * n,
            cost=0.01 * n,
            cached_tokens=40,
            cache_write_tokens=5,
        )

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def __iter__(self):
                yield from lines

        return _Resp()

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    body = driver._build_body(
        [{"role": "user", "content": "keep going"}],
        session_id="exhaust-sess",
    )
    resp = driver._post_stream(body)
    assert resp.error
    assert "continuation attempts" in (resp.error or "")
    assert attempts["n"] == total_attempts
    assert resp.meta["incomplete_retries"] == _CODEX_MAX_INCOMPLETE_RETRIES
    assert resp.text == "".join(f"part-{i} " for i in range(1, total_attempts + 1))
    # All incomplete attempts were provider-reported and must be summed.
    assert resp.tokens_in == sum(100 * i for i in range(1, total_attempts + 1))
    assert resp.tokens_out == sum(5 * i for i in range(1, total_attempts + 1))
    assert resp.meta["cache_read_tokens"] == 40 * total_attempts
    assert resp.meta["cache_write_tokens"] == 5 * total_attempts
    assert resp.meta["provider_cost_usd"] == pytest.approx(
        sum(0.01 * i for i in range(1, total_attempts + 1))
    )


def test_later_continuation_failure_keeps_prior_usage_and_text(monkeypatch):
    """HTTP/transport failure after a prior incomplete keeps accumulated accounting."""
    monkeypatch.setenv("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT", "off")
    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5.6-sol")
    monkeypatch.setattr(driver, "_key", lambda: "tok-test")
    monkeypatch.setenv("OPENAI_CODEX_TOKEN", "tok-test")

    attempts = {"n": 0}

    class _FakeHTTPError(Exception):
        def __init__(self, code: int, payload: bytes):
            self.code = code
            self._payload = payload

        def read(self):
            return self._payload

    def fake_urlopen(req, timeout=0):  # noqa: ARG001
        attempts["n"] += 1
        if attempts["n"] == 1:
            lines = _sse_incomplete_chunk(
                "partial-a ",
                input_tokens=100,
                output_tokens=50,
                cost=0.01,
            )

            class _Resp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def __iter__(self):
                    yield from lines

            return _Resp()
        raise _FakeHTTPError(502, b'{"detail":"upstream timeout"}')

    import urllib.error
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(urllib.error, "HTTPError", _FakeHTTPError)

    body = driver._build_body(
        [{"role": "user", "content": "continue me"}],
        session_id="fail-later-sess",
    )
    resp = driver._post_stream(body)
    assert resp.error
    assert "upstream timeout" in (resp.error or "")
    assert "HTTP 502" in (resp.error or "")
    assert attempts["n"] == 2
    # Still an error — never promote a partial provider failure to success.
    assert resp.text == "partial-a "
    assert resp.tokens_in == 100
    assert resp.tokens_out == 50
    assert resp.meta["cache_read_tokens"] == 40
    assert resp.meta["cache_write_tokens"] == 5
    assert resp.meta["provider_cost_usd"] == pytest.approx(0.01)
    assert resp.meta["incomplete_retries"] == 1
    # Blocks with_retry from replaying the mutated continuation body.
    assert resp.meta.get("stream_started") is True
