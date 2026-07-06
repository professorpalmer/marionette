"""Tests for the opt-in advisor pass (harness/advisor.py)."""
import json
import os
import tempfile
import shutil
from dataclasses import dataclass, field

from harness.advisor import (
    advise,
    advisor_enabled,
    build_advisor_prompt,
    _parse_warnings,
)
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


@dataclass
class FakeResponse:
    text: str
    error: str = ""
    tokens_out: int = 0
    tokens_in: int = 0


@dataclass
class _Act:
    kind: str
    command: str = ""
    path: str = ""


class _RecordingDriver:
    def __init__(self, reply_text):
        self.reply_text = reply_text
        self.calls = []

    def complete(self, prompt, system=None):
        self.calls.append((prompt, system))
        return FakeResponse(text=self.reply_text)


class _ExplodingDriver:
    def complete(self, prompt, system=None):
        raise RuntimeError("provider down")


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("HARNESS_ADVISOR", raising=False)
    assert advisor_enabled() is False


def test_enabled_via_env(monkeypatch):
    monkeypatch.setenv("HARNESS_ADVISOR", "1")
    assert advisor_enabled() is True


def test_advise_returns_warnings_from_json_array():
    driver = _RecordingDriver(json.dumps(["rm -rf targets the repo root"]))
    warnings = advise([_Act(kind="run_command", command="rm -rf .")], "/repo", driver)
    assert warnings == ["rm -rf targets the repo root"]
    assert len(driver.calls) == 1
    prompt, system = driver.calls[0]
    assert "run_command" in prompt and "rm -rf ." in prompt
    assert "JSON array" in system


def test_advise_tolerates_fenced_reply():
    driver = _RecordingDriver('Here you go:\n```json\n["careful"]\n```')
    assert advise([_Act(kind="run_command", command="x")], "", driver) == ["careful"]


def test_garbage_reply_yields_no_warnings():
    for reply in ("not json", "{}", '{"warnings": []}', "", "[1, 2, 3]"):
        driver = _RecordingDriver(reply)
        assert advise([_Act(kind="run_command", command="x")], "", driver) == []


def test_driver_exception_yields_no_warnings():
    assert advise([_Act(kind="run_command", command="x")], "", _ExplodingDriver()) == []


def test_no_actions_makes_no_call():
    driver = _RecordingDriver("[]")
    assert advise([], "/repo", driver) == []
    assert driver.calls == []


def test_warning_caps():
    many = json.dumps([f"warning {i} " + "x" * 500 for i in range(10)])
    warnings = _parse_warnings(many)
    assert len(warnings) == 5
    assert all(len(w) <= 200 for w in warnings)


def test_prompt_caps_action_count():
    actions = [_Act(kind="read_file", path=f"f{i}.py") for i in range(30)]
    prompt = build_advisor_prompt(actions, "/repo")
    assert "and 10 more" in prompt


def test_session_surfaces_warnings_on_first_action_result(monkeypatch):
    """End-to-end: enabled advisor's warnings ride the first action_result."""
    monkeypatch.setenv("HARNESS_ADVISOR", "1")
    tmpdir = tempfile.mkdtemp()
    try:
        real_tmp = os.path.realpath(tmpdir)
        cfg = HarnessConfig(repo=real_tmp, swarm_adapter="demo")
        session = ConversationalSession(cfg)

        class AdvisoryPilot:
            def __init__(self):
                self.calls = 0

            def complete(self, prompt, system=None):
                # The advisor call carries the advisor system prompt; answer
                # it with a warning array. Pilot turns get the normal envelope.
                if system and "advisor" in system:
                    return FakeResponse(text=json.dumps(["writing outside src/"]))
                self.calls += 1
                if self.calls == 1:
                    return FakeResponse(text=json.dumps({
                        "say": "Writing a file",
                        "actions": [{"kind": "write_file", "path": "hello.txt",
                                     "content": "hi"}],
                    }))
                return FakeResponse(text=json.dumps({"say": "Done", "actions": []}))

        session.pilot = AdvisoryPilot()
        events = list(session.send("start"))
        results = [e for e in events if e.kind == "action_result"]
        assert results, "expected at least one action_result"
        assert results[0].data.get("advisor_warnings") == ["writing outside src/"]
        # Warnings surface exactly once.
        assert all("advisor_warnings" not in e.data for e in results[1:])
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_session_makes_no_advisor_call_when_disabled(monkeypatch):
    monkeypatch.delenv("HARNESS_ADVISOR", raising=False)
    tmpdir = tempfile.mkdtemp()
    try:
        real_tmp = os.path.realpath(tmpdir)
        cfg = HarnessConfig(repo=real_tmp, swarm_adapter="demo")
        session = ConversationalSession(cfg)

        advisor_calls = []

        class PlainPilot:
            def __init__(self):
                self.calls = 0

            def complete(self, prompt, system=None):
                if system and "advisor" in system:
                    advisor_calls.append(prompt)
                    return FakeResponse(text="[]")
                self.calls += 1
                if self.calls == 1:
                    return FakeResponse(text=json.dumps({
                        "say": "Writing a file",
                        "actions": [{"kind": "write_file", "path": "hello.txt",
                                     "content": "hi"}],
                    }))
                return FakeResponse(text=json.dumps({"say": "Done", "actions": []}))

        session.pilot = PlainPilot()
        events = list(session.send("start"))
        assert advisor_calls == []
        results = [e for e in events if e.kind == "action_result"]
        assert all("advisor_warnings" not in e.data for e in results)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
