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


def test_max_tokens_parameter_reject_is_not_overflow():
    s = _s()
    out = s._humanize_pilot_error(
        'HTTP 400: {"error":{"message":"max_tokens is too large"}}'
    )
    assert "context window" not in out.lower()
    assert "pilot:" in out


def test_server_error_is_retryable_guidance():
    s = _s()
    out = s._humanize_pilot_error("HTTP 503: service unavailable")
    assert "transient" in out.lower() and "retry" in out.lower()


def test_quota_exhaustion_is_explained():
    s = _s()
    out = s._humanize_pilot_error('HTTP 400: {"error":{"message":"insufficient quota / billing"}}')
    assert "credit" in out.lower() or "quota" in out.lower()


def test_opencode_responses_stream_death_does_not_say_codex():
    s = _s()
    s.config.driver = "opencode-go:muse-spark-1.2-contributor"
    raw = (
        "OpenCode Responses stream did not emit a terminal response "
        "(last event: response.output_item.added)"
    )
    out = s._humanize_pilot_error(raw)
    assert out.startswith("pilot:")
    assert "opencode go" in out.lower()
    assert "continue" in out.lower()
    assert "codex" not in out.lower()
    assert "response.output_item.added" in out


def test_legacy_codex_stream_death_on_muse_driver_is_rewritten():
    s = _s()
    s.config.driver = "opencode-go:muse-spark-1.2-contributor"
    out = s._humanize_pilot_error(
        "Codex Responses stream did not emit a terminal response"
    )
    assert "opencode go" in out.lower()
    assert "continue" in out.lower()
    assert "codex" not in out.lower()


def test_codex_stream_death_on_codex_driver_keeps_codex_host():
    s = _s()
    s.config.driver = "openai-codex:gpt-5.5"
    out = s._humanize_pilot_error(
        "Codex Responses stream did not emit a terminal response"
    )
    assert "codex closed the stream" in out.lower()
    assert "continue" in out.lower()


def test_empty_error_has_message():
    s = _s()
    out = s._humanize_pilot_error("")
    assert out and "pilot:" in out


def test_truly_generic_error_passes_through():
    s = _s()
    out = s._humanize_pilot_error("something weird happened xyz")
    assert out == "pilot: something weird happened xyz"


def test_empty_chat_completion_400_stub_is_humanized():
    s = _s()
    raw = (
        'HTTP 400: {"id":"chatcmpl_8msks0tuby4","object":"chatcompletion",'
        '"created":1787293566,"model":"muse-spark-1.2-contributor",'
        '"choices":[{"index":0,"message":{"role":"assistant"},'
        '"finish_reason":null}]}'
    )
    out = s._humanize_pilot_error(raw)
    assert "rejected" in out.lower()
    assert "protocol" in out.lower()
    assert "fresh session" in out.lower()
    assert "chatcmpl" not in out
    assert "{" not in out
    assert "provider said" not in out.lower()


def test_empty_chat_completion_dotted_object_400_is_humanized():
    s = _s()
    raw = (
        'HTTP 400: {"id":"chatcmpl_x","object":"chat.completion",'
        '"choices":[{"index":0,"message":{"role":"assistant"}}]}'
    )
    out = s._humanize_pilot_error(raw)
    assert "history protocol" in out.lower()
    assert "chatcmpl" not in out


def test_real_http_400_stays_actionable():
    s = _s()
    raw = 'HTTP 400: {"error":{"message":"max_tokens is too large"}}'
    out = s._humanize_pilot_error(raw)
    assert "max_tokens is too large" in out
    assert "rejected this request" not in out.lower()
    assert "history protocol" not in out.lower()


def test_nonempty_chat_completion_400_is_not_misclassified():
    s = _s()
    raw = (
        'HTTP 400: {"object":"chat.completion","choices":[{"index":0,'
        '"message":{"role":"assistant","content":"provider detail"},'
        '"finish_reason":"stop"}]}'
    )
    out = s._humanize_pilot_error(raw)
    assert "provider detail" in out
    assert "history protocol" not in out.lower()
