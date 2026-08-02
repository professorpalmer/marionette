"""Direct Codex cache token stamping and pilot meter integration."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

from harness.api.cost_accounting import _cache_savings
from harness.send_loop_phases import meter_pilot_step
from pmharness.drivers.codex_responses import CodexResponsesDriver


def test_response_from_raw_stamps_cache_read_tokens():
    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5")
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
            "output_tokens": 200,
            "input_tokens_details": {"cached_tokens": 8_000},
        },
    }
    resp = driver._response_from_raw(raw, t0=time.time())
    assert resp.meta["cache_read_tokens"] == 8_000
    assert resp.meta["raw_usage"]["input_tokens_details"]["cached_tokens"] == 8_000
    assert resp.tokens_in == 10_000
    assert resp.tokens_out == 200


def test_response_from_raw_keeps_explicit_zero_cache_fields():
    """Provider-reported zeros must not be omitted or inferred away."""
    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5")
    raw = {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "ok"}],
            }
        ],
        "usage": {
            "input_tokens": 40,
            "output_tokens": 2,
            "input_tokens_details": {"cached_tokens": 0},
            "cache_write_tokens": 0,
        },
    }
    resp = driver._response_from_raw(raw, t0=time.time())
    assert resp.meta["cache_read_tokens"] == 0
    assert resp.meta["cache_write_tokens"] == 0
    assert resp.meta["raw_usage"]["input_tokens_details"]["cached_tokens"] == 0
    assert resp.meta["raw_usage"]["cache_write_tokens"] == 0


def test_response_from_raw_omits_absent_cache_fields():
    """Absent provider cache fields stay absent — never invent zeros/hits."""
    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5")
    raw = {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "ok"}],
            }
        ],
        "usage": {
            "input_tokens": 40,
            "output_tokens": 2,
        },
    }
    resp = driver._response_from_raw(raw, t0=time.time())
    assert "cache_read_tokens" not in resp.meta
    assert "cache_write_tokens" not in resp.meta
    assert resp.meta["raw_usage"] == {"input_tokens": 40, "output_tokens": 2}


def test_continuation_aggregates_explicit_zero_cache_provenance(monkeypatch):
    """Continuation sums keep explicit zeros and omit never-reported fields."""
    monkeypatch.setenv("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT", "off")
    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5")
    monkeypatch.setattr(driver, "_key", lambda: "tok-test")
    monkeypatch.setenv("OPENAI_CODEX_TOKEN", "tok-test")

    calls = {"n": 0}

    def _sse_lines(usage: dict, text: str, *, incomplete: bool):
        item = {
            "type": "message",
            "id": "msg",
            "content": [{"type": "output_text", "text": text}],
        }
        terminal_type = (
            "response.incomplete" if incomplete else "response.completed"
        )
        terminal = {
            "type": terminal_type,
            "response": {
                "status": "incomplete" if incomplete else "completed",
                "incomplete_details": (
                    {"reason": "max_output_tokens"} if incomplete else None
                ),
                "usage": usage,
            },
        }
        if not incomplete:
            terminal["response"].pop("incomplete_details", None)
        done = {"type": "response.output_item.done", "item": item}
        return [
            f"data: {json.dumps(done)}\n".encode("utf-8"),
            f"data: {json.dumps(terminal)}\n".encode("utf-8"),
        ]

    class _Resp:
        def __init__(self, lines):
            self._lines = lines

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            yield from self._lines

    def fake_urlopen(req, timeout=0):  # noqa: ARG001
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp(
                _sse_lines(
                    {
                        "input_tokens": 10,
                        "output_tokens": 1,
                        "input_tokens_details": {"cached_tokens": 0},
                        "cache_write_tokens": 0,
                    },
                    "part-",
                    incomplete=True,
                )
            )
        return _Resp(
            _sse_lines(
                {
                    "input_tokens": 12,
                    "output_tokens": 2,
                    "input_tokens_details": {"cached_tokens": 0},
                    "cache_write_tokens": 0,
                },
                "done",
                incomplete=False,
            )
        )

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    body = driver._build_body(
        [{"role": "user", "content": "hi"}],
        session_id="cache-zero-cont",
    )
    resp = driver._post_stream(body)
    assert resp.error is None
    assert calls["n"] == 2
    assert resp.meta["cache_read_tokens"] == 0
    assert resp.meta["cache_write_tokens"] == 0
    assert resp.text == "part-done"


def test_continuation_omits_cache_when_provider_never_reports(monkeypatch):
    monkeypatch.setenv("HARNESS_CODEX_PROMPT_CACHE_BREAKPOINT", "off")
    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5")
    monkeypatch.setattr(driver, "_key", lambda: "tok-test")
    monkeypatch.setenv("OPENAI_CODEX_TOKEN", "tok-test")

    calls = {"n": 0}

    def _sse_lines(usage: dict, text: str, *, incomplete: bool):
        item = {
            "type": "message",
            "id": "msg",
            "content": [{"type": "output_text", "text": text}],
        }
        terminal = {
            "type": (
                "response.incomplete" if incomplete else "response.completed"
            ),
            "response": {
                "status": "incomplete" if incomplete else "completed",
                "usage": usage,
            },
        }
        if incomplete:
            terminal["response"]["incomplete_details"] = {
                "reason": "max_output_tokens"
            }
        done = {"type": "response.output_item.done", "item": item}
        return [
            f"data: {json.dumps(done)}\n".encode("utf-8"),
            f"data: {json.dumps(terminal)}\n".encode("utf-8"),
        ]

    class _Resp:
        def __init__(self, lines):
            self._lines = lines

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            yield from self._lines

    def fake_urlopen(req, timeout=0):  # noqa: ARG001
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp(
                _sse_lines(
                    {"input_tokens": 10, "output_tokens": 1},
                    "a",
                    incomplete=True,
                )
            )
        return _Resp(
            _sse_lines(
                {"input_tokens": 11, "output_tokens": 1},
                "b",
                incomplete=False,
            )
        )

    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    body = driver._build_body(
        [{"role": "user", "content": "hi"}],
        session_id="cache-absent-cont",
    )
    resp = driver._post_stream(body)
    assert resp.error is None
    assert "cache_read_tokens" not in resp.meta
    assert "cache_write_tokens" not in resp.meta
    assert resp.meta["raw_usage"] == {"input_tokens": 11, "output_tokens": 1}


def test_meter_pilot_step_codex_cached_tokens_and_savings(monkeypatch):
    """Real ChatGPT Codex usage shape must increment _tokens_cached without double count."""
    meters = {}
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
        config=SimpleNamespace(driver="openai-codex/test"),
        _accumulate_session_meters=lambda **kw: meters.update(kw),
    )
    driver = CodexResponsesDriver(name="openai-codex/test", model="gpt-5")
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
            "output_tokens": 200,
            "input_tokens_details": {"cached_tokens": 8_000},
        },
    }
    resp = driver._response_from_raw(raw, t0=time.time())
    monkeypatch.setattr(
        "pmharness.registry.resolve_price_with_source",
        lambda _name: (3.0, 15.0, "catalog"),
        raising=False,
    )
    from harness.api.cost_accounting import _session_cost

    monkeypatch.setattr("harness.server._session_cost", _session_cost, raising=False)
    meter_pilot_step(session, resp, prompt="hello")

    assert session._tokens_cached == 8_000
    assert session._last_turn_cache_read_tokens == 8_000
    assert session._tokens_in == 10_000
    assert session._tokens_used == 10_000 + 200
    assert session._plan_billing is True
    assert resp.meta.get("provider_cost_usd") is None
    savings = _cache_savings(8_000, 3.0)
    assert savings > 0
    assert session._last_prompt_cache_activity_at > 0
