"""Provider errors must read as clear, actionable guidance -- not raw JSON.

Real report: selecting a model not on the user's key (e.g. Fable 5 on an
enterprise key that doesn't include it) produced a cryptic 400/404 that read as
"something broke." The pilot now names the real cause and the fix.

Hermetic: exercises the humanizer directly, no model/network.
"""
import tempfile

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


def _s():
    return ConversationalSession(HarnessConfig(state_dir=tempfile.mkdtemp()))


def test_model_not_available_is_explained():
    s = _s()
    s.config.driver = "claude-fable-5"
    out = s._humanize_pilot_error('HTTP 404: {"error":{"message":"model claude-fable-5 not found"}}')
    assert "isn't available on your current" in out
    assert "claude-fable-5" in out
    assert "Switch to a model" in out


def test_auth_error_is_explained():
    s = _s()
    out = s._humanize_pilot_error(
        "HTTP 401: invalid_api_key sk-or-v1-shouldneverappear0123456789"
    )
    assert "authentication failed" in out.lower()
    assert "Settings" in out
    assert "sk-or-v1-shouldneverappear0123456789" not in out
    assert "provider said" not in out.lower()


def test_opencode_go_upstream_block_preserves_detail():
    """OpenCode Go 401 AuthError 'blocked by upstream' is not a dead local key."""
    s = _s()
    raw = (
        'HTTP 401: {"error":{"message":"Request blocked by upstream provider",'
        '"type":"AuthError"}}'
    )
    out = s._humanize_pilot_error(raw)
    assert "upstream provider blocked" in out.lower()
    assert "authentication failed" not in out.lower()
    assert "blocked by upstream provider" in out.lower()
    assert "sk-" not in out.lower()


def test_opencode_go_region_error_explains_opt_in_instead_of_bad_key():
    s = _s()
    raw = (
        'HTTP 403: {"type":"error","error":{"type":"RegionError","message":'
        '"The latest version of this model is only available hosted in China '
        'and requires explicit opt in: '
        'https://opencode.ai/workspace/wrk_example/go"}}'
    )
    out = s._humanize_pilot_error(raw)
    assert "workspace region" in out.lower()
    assert "api key is valid" in out.lower()
    assert "opt in" in out.lower()
    assert "https://opencode.ai/workspace/wrk_example/go" in out
    assert "authentication failed" not in out.lower()


def test_rate_limit_is_explained():
    s = _s()
    out = s._humanize_pilot_error("HTTP 429: rate limit exceeded")
    assert "rate-limiting" in out.lower()


def test_context_overflow_is_explained():
    s = _s()
    out = s._humanize_pilot_error(
        'HTTP 400: {"error":{"message":"prompt is too long: 250000 tokens > 200000 maximum"}}')
    assert "context window" in out.lower()


def test_server_error_is_retryable_guidance():
    s = _s()
    out = s._humanize_pilot_error("HTTP 503: service unavailable")
    assert "transient" in out.lower() and "retry" in out.lower()


def test_quota_exhaustion_is_explained():
    s = _s()
    out = s._humanize_pilot_error('HTTP 400: {"error":{"message":"insufficient quota / billing"}}')
    assert "credit" in out.lower() or "quota" in out.lower()


def test_empty_error_has_message():
    s = _s()
    out = s._humanize_pilot_error("")
    assert out and "pilot:" in out


def test_truly_generic_error_passes_through():
    s = _s()
    out = s._humanize_pilot_error("something weird happened xyz")
    assert out == "pilot: something weird happened xyz"
