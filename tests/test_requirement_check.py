"""A turn that edits files for a long request checks the request once before it ends.

Hidden tests on spec-style tasks check single sentences of the request (an
exact error text, a field in a String() output). The pilot ended its turn
with those sentences unmet. One check step before the end lists each
requirement and its proof.
"""

from __future__ import annotations

import json
import tempfile

import pytest

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.send_loop_phases import REQUIREMENT_CHECK_MIN_CHARS, REQUIREMENT_CHECK_NOTE
from pmharness.drivers.openai_compat import DriverResponse

SPEC = "Implement the parser. " + "Reject a bad default with the error `invalid default argument declaration`. " * 20


class _Pilot:
    supports_streaming = True
    name = "spec-pilot"

    def __init__(self, edit: bool):
        self.edit = edit
        self.calls = 0
        self.seen = []

    def chat(self, messages, **_k):
        raise AssertionError("chat() must not run when streaming is available")

    def chat_stream(self, messages, *, tools=None, system=None, on_delta, **_k):
        self.calls += 1
        self.seen.append(json.dumps(messages, default=str))
        if self.calls == 1 and self.edit:
            call = {"id": "c1", "type": "function", "function": {
                "name": "write_file", "arguments": json.dumps({"path": "a.txt", "content": "x\n"})}}
            return DriverResponse(text="", meta={"tool_calls": [call], "finish_reason": "tool_calls",
                                                 "stream_terminal": "tool_calls"})
        on_delta("Done.")
        return DriverResponse(text="Done.", tokens_out=2, meta={
            "tool_calls": [], "finish_reason": "stop", "stream_terminal": "stop", "stream_started": True})


def _run(message: str, edit: bool = True):
    repo = tempfile.mkdtemp()
    s = ConversationalSession(HarnessConfig(driver="stub-oracle-v2", repo=repo, state_dir=tempfile.mkdtemp()))
    s.pilot = _Pilot(edit)
    events = list(s.send(message))
    return s, events


def _checks(s):
    return sum(1 for m in s._history if m.get("source") == "requirement_check")


def test_a_long_request_with_edits_gets_one_check():
    assert len(SPEC) >= REQUIREMENT_CHECK_MIN_CHARS
    s, events = _run(SPEC)
    assert not [e for e in events if e.kind == "error"]
    assert _checks(s) == 1
    first = next(i for i, seen in enumerate(s.pilot.seen) if "acceptance check" in seen)
    assert first >= 2  # after the edit step and the first final answer
    rows = [m.get("source") or m.get("role") for m in s._history]
    assert rows[rows.index("requirement_check") + 1] == "assistant"


def test_a_short_request_gets_no_check():
    s, _ = _run("Fix the typo in a.txt.")
    assert _checks(s) == 0
    assert "acceptance check" not in s.pilot.seen[-1]


def test_a_long_request_without_edits_gets_no_check():
    s, _ = _run(SPEC, edit=False)
    assert _checks(s) == 0
    assert s.pilot.calls == 1


def test_the_check_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("HARNESS_REQUIREMENT_CHECK", "0")
    s, _ = _run(SPEC)
    assert _checks(s) == 0


def test_the_note_names_the_request_parts_hidden_tests_check():
    note = REQUIREMENT_CHECK_NOTE.lower()
    for word in ("error", "edge case", "format"):
        assert word in note


def test_the_minimum_length_can_be_lowered(monkeypatch):
    monkeypatch.setenv("HARNESS_REQUIREMENT_CHECK_MIN_CHARS", "10")
    s, _ = _run("Fix the typo in a.txt and keep the rest.")
    assert _checks(s) == 1


def test_a_second_round_asks_for_test_author_checks(monkeypatch):
    monkeypatch.setenv("HARNESS_REQUIREMENT_CHECK_ROUNDS", "2")
    s, events = _run(SPEC)
    assert _checks(s) == 2
    notes = [m["content"] for m in s._history if m.get("source") == "requirement_check"]
    assert notes[0] == REQUIREMENT_CHECK_NOTE
    assert "hidden acceptance tests" in notes[1]
    rounds = [e.data["message"] for e in events if e.kind == "notice" and e.data.get("kind") == "requirement_check"]
    assert rounds == ["Requirement check, round 1", "Requirement check, round 2"]


def test_the_default_is_one_round():
    s, events = _run(SPEC)
    assert _checks(s) == 1
    assert [e for e in events if e.kind == "notice" and e.data.get("kind") == "requirement_check"]


class _ReviewPilot(_Pilot):
    def __init__(self, edit: bool, reply: str):
        super().__init__(edit)
        self.reply = reply
        self.reviews = []

    def chat(self, messages, *, system=None, **_k):
        self.reviews.append((system, messages[0]["content"]))
        return DriverResponse(text=self.reply)


def _git_repo():
    import subprocess

    repo = tempfile.mkdtemp()
    for args in (["init", "-q"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "base"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    return repo


def _run_review(reply: str, monkeypatch):
    monkeypatch.setenv("HARNESS_REQUIREMENT_CHECK_ROUNDS", "2")
    monkeypatch.setenv("HARNESS_REQUIREMENT_REVIEW", "1")
    s = ConversationalSession(HarnessConfig(driver="stub-oracle-v2", repo=_git_repo(), state_dir=tempfile.mkdtemp()))
    s.pilot = _ReviewPilot(True, reply)
    list(s.send(SPEC))
    return s


def test_the_review_round_sends_request_and_diff_to_a_fresh_context(monkeypatch):
    s = _run_review("1. `invalid default argument declaration` is not raised.", monkeypatch)
    assert len(s.pilot.reviews) == 1
    system, prompt = s.pilot.reviews[0]
    assert "hidden acceptance tests" in system.lower()
    assert "invalid default argument declaration" in prompt and "+x" in prompt and "a.txt" in prompt
    notes = [m["content"] for m in s._history if m.get("source") == "requirement_check"]
    assert len(notes) == 2
    assert notes[1].startswith("An independent reviewer") and "is not raised" in notes[1]


def test_no_gaps_ends_the_turn_after_round_one(monkeypatch):
    s = _run_review("NO GAPS", monkeypatch)
    assert len(s.pilot.reviews) == 1
    assert _checks(s) == 1


def test_workspace_diff_leaves_the_real_index_alone():
    import subprocess

    from harness.spec_review import workspace_diff

    repo = _git_repo()
    open(f"{repo}/new.py", "w").write("print(1)\n")
    assert "+print(1)" in workspace_diff(repo)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
    assert status.strip() == "?? new.py"


def test_the_review_diff_keeps_work_the_pilot_committed(monkeypatch):
    import subprocess
    from types import SimpleNamespace

    from harness.spec_review import turn_base, workspace_diff

    monkeypatch.setenv("HARNESS_REQUIREMENT_REVIEW", "1")
    repo = _git_repo()
    session = SimpleNamespace(config=SimpleNamespace(repo=repo))
    base = turn_base(session)
    assert base
    open(f"{repo}/done.py", "w").write("ok = True\n")
    for args in (["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "work"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    assert workspace_diff(repo) == ""
    assert "+ok = True" in workspace_diff(repo, base)


def test_turn_base_is_empty_when_the_review_is_off(monkeypatch):
    from types import SimpleNamespace

    from harness.spec_review import turn_base

    monkeypatch.delenv("HARNESS_REQUIREMENT_REVIEW", raising=False)
    assert turn_base(SimpleNamespace(config=SimpleNamespace(repo=_git_repo()))) == ""
