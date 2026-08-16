"""Focused classification for provider HTTP failures (incl. OpenCode Go)."""

from pmharness.drivers.error_classifier import (
    ErrorClass,
    classify,
    is_upstream_provider_block,
)


def test_invalid_key_401_is_auth():
    assert classify(401, "invalid_api_key") == ErrorClass.AUTH
    assert classify(403, "unauthorized") == ErrorClass.AUTH


def test_opencode_go_upstream_block_is_retryable_not_auth():
    body = '{"error":{"message":"Request blocked by upstream provider","type":"AuthError"}}'
    assert is_upstream_provider_block(body)
    assert classify(401, body) == ErrorClass.RETRYABLE
    assert classify(401, "HTTP 401: Request blocked by upstream provider") == ErrorClass.RETRYABLE


def test_genuine_auth_still_fatal_for_other_providers():
    assert not is_upstream_provider_block("invalid_api_key")
    assert classify(401, "HTTP 401: incorrect api key provided") == ErrorClass.AUTH


def test_real_context_overflow_still_classifies():
    assert classify(
        400, "HTTP 400: maximum context length exceeded"
    ) == ErrorClass.CONTEXT_OVERFLOW
    assert classify(
        400,
        '{"error":{"message":"prompt is too long: 250000 tokens > 200000 maximum"}}',
    ) == ErrorClass.CONTEXT_OVERFLOW
    assert classify(413, "request too large") == ErrorClass.CONTEXT_OVERFLOW


def test_completion_max_tokens_reject_is_not_overflow():
    """Kimi/GLM accept a large max_tokens; other models 400 on the same field.

    That is a request-parameter error, not a prompt-window blow. Compacting
    history cannot fix it.
    """
    assert classify(
        400, 'HTTP 400: {"error":{"message":"max_tokens is too large"}}'
    ) == ErrorClass.FATAL
    assert classify(
        400, "invalid max_tokens: exceeds the maximum allowed value"
    ) == ErrorClass.FATAL
    assert classify(None, "HTTP 400: max_tokens exceeds the maximum") == ErrorClass.FATAL
