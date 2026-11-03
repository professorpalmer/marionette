from __future__ import annotations

"""HTTP error excerpts never carry the credential the request sent."""

import io
import urllib.error
import urllib.request

from pmharness.drivers.base import http_error_excerpt
from pmharness.drivers.gemini import GeminiDriver
from pmharness.drivers.openai_compat import OpenAICompatDriver

KEY = "sk-test-0123456789abcdefghijklmnopqrstuv"


def _err(body: str, code: int = 400) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://example.invalid", code, "bad", {}, io.BytesIO(body.encode("utf-8")),
    )


def _echo_across_cut(limit: int = 500) -> str:
    # The key starts 10 characters before the cut, so the old excerpt kept
    # its first 10 characters.
    head = '{"error": "invalid key '
    return head + "x" * (limit - 10 - len(head)) + KEY + '"}'


def test_bearer_key_that_crosses_the_cut_leaves_no_prefix():
    out = http_error_excerpt(
        _err(_echo_across_cut()), {"Authorization": f"Bearer {KEY}"}, 500,
    )
    assert KEY[:10] not in out
    assert len(out) <= 500


def test_api_key_headers_are_scrubbed_and_other_headers_are_not():
    body = f"keys {KEY} and {KEY[::-1]} with model gpt-test-model"
    out = http_error_excerpt(
        _err(body),
        {"x-api-key": KEY, "x-goog-api-key": KEY[::-1], "User-Agent": "gpt-test-model"},
    )
    assert out == "keys REDACTED and REDACTED with model gpt-test-model"


def test_short_values_and_no_headers_keep_the_body():
    assert http_error_excerpt(_err("busy abc"), {"Authorization": "Bearer abc"}) == "busy abc"
    assert http_error_excerpt(_err("y" * 900), None, 800) == "y" * 800


def test_openai_compat_stream_error_does_not_echo_the_key(monkeypatch):
    d = OpenAICompatDriver(
        name="t", model="gpt-4o", base_url="https://api.openai.com/v1",
        api_key_env="TEST_OAI_KEY",
    )
    d._key = lambda: KEY

    def _raise(*_a, **_k):
        raise _err(_echo_across_cut())

    monkeypatch.setattr(urllib.request, "urlopen", _raise)
    resp = d.chat_stream([{"role": "user", "content": "hi"}], on_delta=lambda _t: None)
    assert resp.error and "HTTP 400" in resp.error
    assert KEY[:10] not in resp.error


def test_gemini_stream_error_does_not_echo_the_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    d = GeminiDriver("gemini-3.5-flash", "gemini-3.5-flash")

    def _raise(*_a, **_k):
        raise _err(_echo_across_cut())

    monkeypatch.setattr(urllib.request, "urlopen", _raise)
    resp = d.chat_stream([{"role": "user", "content": "hi"}], on_delta=lambda _t: None)
    assert resp.error and "HTTP 400" in resp.error
    assert KEY[:10] not in resp.error
