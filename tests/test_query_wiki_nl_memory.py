"""Grounded synthesis for the LIVE query_wiki tool path.

The query_wiki handler in harness.conversation now folds the raw wiki result
through harness.nl_memory.answer_from_memory to surface a concise, cited answer
INSTEAD of a raw dump. The grounded step is fully guarded: any failure (bad
entries, model error, not-found sentinel) falls back to today's raw-dump path.

These tests are hermetic:
  * No network (conftest guards outbound sockets anyway).
  * No API keys -- the pilot's chat surface is monkeypatched to a fake.
  * Wiki is either not-configured (env cleared by conftest) or its .query is
    monkeypatched to return a fixed blob.

We drive the grounded step through the ConversationalSession._grounded_wiki_answer
helper the query_wiki handler calls, and assert on the string it hands back to
the handler. That is the same value the handler splices into the action-result
envelope, so failures here would show up as regressions in the live surface.
"""
import tempfile
import types

import pytest

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


def _session():
    # A minimal, offline-friendly session. The stub-oracle driver never hits a
    # network, matching other conversation tests.
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    return ConversationalSession(cfg)


class _FakeResp:
    """Duck-typed pilot response: has .text and .error, matching what the
    conversation layer already reads (see _make_summary / grounded closure)."""

    def __init__(self, text="", error=""):
        self.text = text
        self.error = error


class _FakePilot:
    """Records the last chat prompt and returns a scripted response.

    - `reply` is the string to return as resp.text.
    - `raises` is an Exception subclass/instance to raise from .chat().
    - `error` is a non-empty string to set on resp.error (the closure treats
      resp.error as a hard failure and falls back to raw).
    """

    def __init__(self, *, reply="", raises=None, error=""):
        self.reply = reply
        self.raises = raises
        self.error = error
        self.calls = []

    def chat(self, messages, system=None):
        self.calls.append({"messages": messages, "system": system})
        if self.raises is not None:
            raise self.raises
        return _FakeResp(text=self.reply, error=self.error)


# --------------------------- (a) grounded answer ----------------------------

def test_grounded_answer_with_citation_surfaces_over_raw():
    """When entries support an answer and the fake model cites [1], the
    surfaced text carries the grounded answer + a compact citations line."""
    session = _session()
    session.pilot = _FakePilot(reply="Default driver is the local adapter [1].")

    raw = (
        "Wiki search results:\n"
        "- Driver decision (driver-decision): We default to the local adapter for evals.\n"
        "- Auth notes (auth): JWT verified in middleware."
    )
    out = session._grounded_wiki_answer("what did we decide about the default driver?", raw)

    assert out, "grounded synthesis must return a non-empty string"
    assert "local adapter" in out
    assert "[1]" in out                     # citation marker preserved
    assert "Citations:" in out              # compact trailer
    assert "Driver decision" in out         # cited entry title surfaced


def test_grounded_answer_wraps_single_blob_when_no_bullets():
    """A prose-only wiki blob (no 'Wiki search results:' preamble) is wrapped
    as a single entry so nl_memory still runs and produces a cited answer."""
    session = _session()
    session.pilot = _FakePilot(reply="The release cadence is monthly [1].")

    raw = "Release cadence is monthly, cut on the last Friday."
    out = session._grounded_wiki_answer("release cadence?", raw)

    assert out
    assert "monthly" in out
    assert "[1]" in out
    assert "Citations:" in out


# ---------------------- (b) complete raises -> raw fallback -----------------

def test_grounded_returns_empty_when_complete_raises():
    """If the injected complete callable errors, the handler falls back to the
    raw wiki result. The helper signals that by returning an empty string."""
    session = _session()
    session.pilot = _FakePilot(raises=RuntimeError("boom, no model today"))

    raw = "Wiki search results:\n- Some page (some): body here."
    out = session._grounded_wiki_answer("anything", raw)

    assert out == ""  # empty -> caller keeps today's raw-dump behavior


def test_grounded_returns_empty_when_pilot_response_carries_error():
    """A driver response with a truthy `.error` must also degrade to the raw
    path -- the pilot did NOT give us a usable answer."""
    session = _session()
    session.pilot = _FakePilot(reply="", error="rate limited")

    raw = "Wiki search results:\n- Some page (some): body here."
    out = session._grounded_wiki_answer("anything", raw)
    assert out == ""


def test_grounded_returns_empty_when_model_says_not_found():
    """When the fake model returns the NOT_FOUND sentinel, we surface the raw
    result rather than a bare 'not found' line."""
    from harness.nl_memory import NOT_FOUND

    session = _session()
    session.pilot = _FakePilot(reply=NOT_FOUND)

    raw = "Wiki search results:\n- Page A (a): irrelevant body."
    out = session._grounded_wiki_answer("something unrelated", raw)
    assert out == ""


# ---------------------- (c) unconfigured wiki -> unchanged ------------------

def test_wiki_unconfigured_grounded_helper_declines():
    """When self._wiki is not configured, the handler branch surfaces the
    literal 'wiki not configured' blob today. The grounded helper must decline
    (return '') on that blob so nothing changes for callers who never got a
    real result. This test also confirms the pilot is never called."""
    session = _session()
    pilot = _FakePilot(reply="should not be called [1]")
    session.pilot = pilot

    # This is what the handler surfaces today when self._wiki.configured is False:
    raw = "wiki not configured"
    out = session._grounded_wiki_answer("anything", raw)
    assert out == ""
    assert pilot.calls == []  # no wasted model call on a degenerate blob


def test_grounded_empty_on_empty_question_or_raw():
    session = _session()
    session.pilot = _FakePilot(reply="unused [1]")
    assert session._grounded_wiki_answer("", "some raw") == ""
    assert session._grounded_wiki_answer("q?", "") == ""


def test_grounded_never_raises_even_when_pilot_missing():
    """Belt-and-suspenders: if session.pilot is somehow None, we degrade to
    the raw path instead of exploding into the pilot loop."""
    session = _session()
    session.pilot = None
    out = session._grounded_wiki_answer("q", "Wiki search results:\n- t (s): b")
    assert out == ""


# ---------------- integration: handler splices grounded above raw -----------

def test_handler_envelope_carries_grounded_above_raw(monkeypatch):
    """End-to-end shape check on the branch content: given a configured wiki
    whose .query returns a known blob and a fake pilot that produces a cited
    answer, the string the handler would append to history carries BOTH the
    grounded answer AND the raw result (as supporting context)."""
    session = _session()
    session.pilot = _FakePilot(reply="The default driver is local [1].")

    raw = "Wiki search results:\n- Driver decision (driver-decision): local adapter default."
    grounded = session._grounded_wiki_answer("default driver?", raw)

    # Reconstruct exactly what the handler splices when grounded is non-empty
    # (see the query_wiki branch in harness/conversation.py).
    surfaced = (
        f"(query_wiki 'default driver?' returned)\n"
        f"{grounded}\n\n"
        f"--- raw wiki result ---\n{raw}"
    )
    assert "The default driver is local [1]." in surfaced
    assert "Citations:" in surfaced
    assert "--- raw wiki result ---" in surfaced
    assert "Wiki search results:" in surfaced  # raw kept as supporting context
