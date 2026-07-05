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
    out = s._humanize_pilot_error("HTTP 401: invalid_api_key")
    assert "authentication failed" in out.lower()
    assert "Settings" in out


def test_generic_error_passes_through():
    s = _s()
    out = s._humanize_pilot_error("HTTP 500: internal server error")
    assert out == "pilot: HTTP 500: internal server error"
