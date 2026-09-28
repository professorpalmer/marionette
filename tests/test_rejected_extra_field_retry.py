"""A relay that rejects a provider-dialect extra must not fail every turn.

Real report (2026-09-26): OpenCode Go began rejecting GLM-5.3's ``thinking``
field with HTTP 400 ``unknown field "thinking"``. Every glm-5.3-flash turn
failed. The driver now drops a rejected ``extra_body`` field for the session
and retries once. Core request fields are never dropped.
"""
from __future__ import annotations

import io
import json
import urllib.error

from pmharness.drivers.openai_compat import OpenAICompatDriver

_GO_THINKING_400 = {
    "error": {
        "param": "thinking",
        "type": "invalid_request_error",
        "message": 'Upstream request failed: [unknown_parameter] invalid request body: json: unknown field "thinking"',
    }
}

_BUDGET_400 = {
    "error": {
        "param": "reasoning_budget_tokens",
        "type": "invalid_request_error",
        "message": "unsupported provider control",
    }
}


class _Resp:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode()

    def read(self):
        return self._data

    def __iter__(self):
        return iter([])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ok_payload():
    return {
        "choices": [{"message": {"content": "ok", "tool_calls": []}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 1},
    }


def _driver(monkeypatch, extra_body):
    monkeypatch.setenv("TEST_OAI_KEY", "k")
    return OpenAICompatDriver(
        "opencode-go:glm-5.3-flash", "glm-5.3-flash", "http://x/v1", "TEST_OAI_KEY",
        extra_body=extra_body,
    )


def _reject(field: str, payload: dict):
    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode())
        fake_urlopen.bodies.append(body)
        if field in body:
            raise urllib.error.HTTPError(
                "http://x", 400, "bad", {}, io.BytesIO(json.dumps(payload).encode()))
        return _Resp(_ok_payload())
    fake_urlopen.bodies = []
    return fake_urlopen


def test_chat_drops_rejected_extra_field_and_retries(monkeypatch):
    d = _driver(monkeypatch, {"thinking": {"type": "enabled"}, "reasoning_effort": "low"})
    fake = _reject("thinking", _GO_THINKING_400)
    monkeypatch.setattr("urllib.request.urlopen", fake)

    resp = d.chat([{"role": "user", "content": "hi"}])

    assert not resp.error
    assert resp.text == "ok"
    assert "thinking" in fake.bodies[0]
    assert "thinking" not in fake.bodies[-1]
    assert fake.bodies[-1]["reasoning_effort"] == "low"
    assert d.extra_body == {"reasoning_effort": "low"}


def test_chat_reports_only_rejected_control_name_on_retry_and_later_turns(monkeypatch):
    d = _driver(monkeypatch, {"reasoning_budget_tokens": 2048})
    fake = _reject("reasoning_budget_tokens", _BUDGET_400)
    monkeypatch.setattr("urllib.request.urlopen", fake)

    first = d.chat([{"role": "user", "content": "hi"}])
    second = d.chat([{"role": "user", "content": "again"}])

    assert first.meta["omitted_provider_controls"] == ["reasoning_budget_tokens"]
    assert second.meta["omitted_provider_controls"] == ["reasoning_budget_tokens"]
    assert all("reasoning_budget_tokens" not in body for body in fake.bodies[1:])
    assert "2048" not in json.dumps(first.meta)
    assert "unsupported provider control" not in json.dumps(first.meta)


def test_cancelled_fallback_reports_only_controls_omitted_from_dispatched_request(monkeypatch):
    d = _driver(monkeypatch, {"reasoning_budget_tokens": 2048})
    cancelled = False
    bodies = []

    def rejecting(req, timeout=None):
        nonlocal cancelled
        bodies.append(json.loads(req.data.decode()))
        cancelled = True
        raise urllib.error.HTTPError(
            "http://x", 400, "bad", {},
            io.BytesIO(json.dumps(_BUDGET_400).encode()),
        )

    monkeypatch.setattr("urllib.request.urlopen", rejecting)

    cancelled_response = d.chat(
        [{"role": "user", "content": "first"}],
        is_cancelled=lambda: cancelled,
    )

    assert cancelled_response.error == "cancelled before provider fallback"
    assert bodies[0]["reasoning_budget_tokens"] == 2048
    assert "omitted_provider_controls" not in cancelled_response.meta

    cancelled = False
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _Resp(_ok_payload()))
    later = d.chat([{"role": "user", "content": "later"}])
    assert later.meta["omitted_provider_controls"] == ["reasoning_budget_tokens"]


def test_cancelled_stream_fallback_uses_last_dispatched_stream_body(monkeypatch):
    d = _driver(monkeypatch, {"reasoning_budget_tokens": 2048})
    cancelled = False
    bodies = []

    def rejecting(req, timeout=None):
        nonlocal cancelled
        bodies.append(json.loads(req.data.decode()))
        cancelled = True
        raise urllib.error.HTTPError(
            "http://x", 400, "bad", {},
            io.BytesIO(json.dumps(_BUDGET_400).encode()),
        )

    monkeypatch.setattr("urllib.request.urlopen", rejecting)

    response = d.chat_stream(
        [{"role": "user", "content": "first"}],
        on_delta=lambda _s: None,
        is_cancelled=lambda: cancelled,
    )

    assert response.error == "cancelled before provider retry"
    assert len(bodies) == 1
    assert bodies[0]["reasoning_budget_tokens"] == 2048
    assert "omitted_provider_controls" not in response.meta


def test_cancelled_stream_fallback_preserves_last_stream_omissions(monkeypatch):
    d = _driver(monkeypatch, {
        "reasoning_budget_tokens": 2048,
        "thinking": {"type": "enabled"},
    })
    cancelled = False
    bodies = []

    def fake_urlopen(req, timeout=None):
        nonlocal cancelled
        body = json.loads(req.data.decode())
        bodies.append(body)
        if len(bodies) == 1:
            raise urllib.error.HTTPError(
                "http://x", 400, "bad", {},
                io.BytesIO(json.dumps(_GO_THINKING_400).encode()),
            )
        if body.get("stream") is True:
            cancelled = True
            raise urllib.error.HTTPError(
                "http://x", 400, "bad", {},
                io.BytesIO(json.dumps(_BUDGET_400).encode()),
            )
        return _Resp(_ok_payload())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    first = d.chat([{"role": "user", "content": "first"}])
    response = d.chat_stream(
        [{"role": "user", "content": "second"}],
        on_delta=lambda _s: None,
        is_cancelled=lambda: cancelled,
    )

    assert first.meta["omitted_provider_controls"] == ["thinking"]
    assert len(bodies) == 3
    assert bodies[-1]["reasoning_budget_tokens"] == 2048
    assert "thinking" not in bodies[-1]
    assert response.error == "cancelled before provider retry"
    assert response.meta["retry_attempts"] == 1
    assert response.meta["omitted_provider_controls"] == ["thinking"]

    cancelled = False
    later = d.chat([{"role": "user", "content": "later"}])
    assert later.meta["omitted_provider_controls"] == [
        "reasoning_budget_tokens", "thinking",
    ]


def test_failed_fallback_reports_control_omitted_from_final_request(monkeypatch):
    d = _driver(monkeypatch, {"reasoning_budget_tokens": 2048})
    bodies = []

    def failing(req, timeout=None):
        body = json.loads(req.data.decode())
        bodies.append(body)
        if "reasoning_budget_tokens" in body:
            raise urllib.error.HTTPError(
                "http://x", 400, "bad", {},
                io.BytesIO(json.dumps(_BUDGET_400).encode()),
            )
        raise TimeoutError("fallback transport failed")

    monkeypatch.setattr("urllib.request.urlopen", failing)

    response = d.chat([{"role": "user", "content": "first"}])

    assert "reasoning_budget_tokens" in bodies[0]
    assert "reasoning_budget_tokens" not in bodies[-1]
    assert response.error == "TimeoutError('fallback transport failed')"
    assert response.meta["omitted_provider_controls"] == [
        "reasoning_budget_tokens",
    ]


def test_healthy_control_is_unchanged_and_not_reported(monkeypatch):
    d = _driver(monkeypatch, {"reasoning_budget_tokens": 2048})
    bodies = []

    def fake_urlopen(req, timeout=None):
        bodies.append(json.loads(req.data.decode()))
        return _Resp(_ok_payload())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    resp = d.chat([{"role": "user", "content": "hi"}])

    assert bodies == [{
        "model": "glm-5.3-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1500,
        "reasoning_budget_tokens": 2048,
    }]
    assert "omitted_provider_controls" not in resp.meta


def test_multiple_rejected_controls_are_stable_and_deduplicated(monkeypatch):
    d = _driver(monkeypatch, {
        "reasoning_budget_tokens": 2048,
        "thinking": {"type": "enabled"},
    })
    payloads = {
        "reasoning_budget_tokens": _BUDGET_400,
        "thinking": _GO_THINKING_400,
    }

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode())
        for field in ("reasoning_budget_tokens", "thinking"):
            if field in body:
                raise urllib.error.HTTPError(
                    "http://x", 400, "bad", {},
                    io.BytesIO(json.dumps(payloads[field]).encode()),
                )
        return _Resp(_ok_payload())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    first = d.chat([{"role": "user", "content": "one"}])
    second = d.chat([{"role": "user", "content": "two"}])
    third = d.chat([{"role": "user", "content": "three"}])

    assert first.meta["omitted_provider_controls"] == ["reasoning_budget_tokens"]
    assert second.meta["omitted_provider_controls"] == [
        "reasoning_budget_tokens", "thinking",
    ]
    assert third.meta["omitted_provider_controls"] == [
        "reasoning_budget_tokens", "thinking",
    ]


def test_chat_stream_drops_rejected_extra_field_and_retries(monkeypatch):
    d = _driver(monkeypatch, {"thinking": {"type": "enabled"}, "reasoning_effort": "low"})
    fake = _reject("thinking", _GO_THINKING_400)
    monkeypatch.setattr("urllib.request.urlopen", fake)

    resp = d.chat_stream([{"role": "user", "content": "hi"}], on_delta=lambda _s: None)

    assert not resp.error
    assert resp.text == "ok"
    assert "thinking" not in fake.bodies[-1]
    assert "thinking" not in d.extra_body
    assert resp.meta["omitted_provider_controls"] == ["thinking"]


def test_restored_control_and_rebuilt_driver_have_no_stale_omission(monkeypatch):
    d = _driver(monkeypatch, {"reasoning_budget_tokens": 2048})
    rejecting = _reject("reasoning_budget_tokens", _BUDGET_400)
    monkeypatch.setattr("urllib.request.urlopen", rejecting)
    assert d.chat([{"role": "user", "content": "first"}]).meta[
        "omitted_provider_controls"
    ] == ["reasoning_budget_tokens"]

    accepted_bodies = []

    def accepting(req, timeout=None):
        accepted_bodies.append(json.loads(req.data.decode()))
        return _Resp(_ok_payload())

    d.extra_body["reasoning_budget_tokens"] = 4096
    monkeypatch.setattr("urllib.request.urlopen", accepting)
    restored = d.chat([{"role": "user", "content": "restored"}])
    rebuilt = _driver(monkeypatch, {"reasoning_budget_tokens": 8192})
    fresh = rebuilt.chat([{"role": "user", "content": "fresh"}])

    assert accepted_bodies[0]["reasoning_budget_tokens"] == 4096
    assert accepted_bodies[1]["reasoning_budget_tokens"] == 8192
    assert "omitted_provider_controls" not in restored.meta
    assert "omitted_provider_controls" not in fresh.meta


def test_rejected_core_field_is_not_dropped(monkeypatch):
    d = _driver(monkeypatch, {})
    payload = {"error": {"param": "messages", "message": 'unknown field "messages"'}}
    fake = _reject("messages", payload)
    monkeypatch.setattr("urllib.request.urlopen", fake)

    resp = d.chat([{"role": "user", "content": "hi"}])

    assert resp.error and "400" in resp.error
    assert len(fake.bodies) == 1
