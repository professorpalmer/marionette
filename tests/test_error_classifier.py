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
