from __future__ import annotations

"""A provider failure with no HTTP status keeps its terminal class.

A Responses stream ends with response.failed and an error code. Retry text in
the message ("try again") must not make a quota or policy failure transient.
"""

import json

import pytest

from pmharness.drivers.codex_responses import _consume_codex_sse
from pmharness.drivers.error_classifier import ErrorClass, classify


@pytest.mark.parametrize("message, expected", [
    ("You've hit your usage limit. Try again at 3:00 PM.", ErrorClass.QUOTA),
    ("insufficient_quota: You exceeded your current quota, please retry later", ErrorClass.QUOTA),
    ("content_policy_violation: Your request was flagged. Please modify your prompt and try again.",
     ErrorClass.FATAL),
    ("Invalid prompt: your prompt was flagged as potentially violating our usage policy. Please try again with a different prompt.",
     ErrorClass.FATAL),
])
def test_statusless_terminal_failures_are_not_retryable(message, expected):
    assert classify(None, message) == expected


def test_statusless_transient_failures_stay_retryable():
    assert classify(None, "The server is overloaded. Please try again.") == ErrorClass.RETRYABLE
    assert classify(None, "Read timed out") == ErrorClass.RETRYABLE


def test_responses_failure_keeps_the_error_code():
    failed = {"type": "response.failed", "response": {"status": "failed", "error": {
        "code": "content_policy_violation",
        "message": "Your request was flagged. Please modify your prompt and try again.",
    }}}
    raw = _consume_codex_sse([("data: " + json.dumps(failed) + "\n").encode("utf-8")])
    assert raw["error"].startswith("content_policy_violation: ")
    assert classify(None, raw["error"]) == ErrorClass.FATAL
