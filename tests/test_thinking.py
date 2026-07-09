"""Tests for the Thinking reasoning channel features."""
import pytest
from harness.pilot import parse_pilot_turn, PilotTurn, PilotError
from harness.conversation import ConversationalSession, ConvEvent
from harness.config import HarnessConfig
import tempfile


def test_parse_pilot_turn_thinking():
    # Populate thinking from "thinking"
    t1 = parse_pilot_turn('{"thinking":"My thoughts", "say":"Hello", "actions":[]}')
    assert t1.thinking == "My thoughts"
    assert t1.say == "Hello"

    # Populate thinking from "reasoning"
    t2 = parse_pilot_turn('{"reasoning":"My reasoning", "say":"Hello", "actions":[]}')
    assert t2.thinking == "My reasoning"

    # Populate thinking from "thought"
    t3 = parse_pilot_turn('{"thought":"My thought", "say":"Hello", "actions":[]}')
    assert t3.thinking == "My thought"

    # Default to empty string when absent
    t4 = parse_pilot_turn('{"say":"Hello", "actions":[]}')
    assert t4.thinking == ""


def test_clean_say_applied_to_thinking():
    # A thinking with an [INFO] line or USER:( echo should be cleaned using clean_say
    # clean_say strips log lines like [INFO] ... or USER: (run_command ...completed)
    raw_thinking = "Analyzing code...\n[INFO] background task running\nUSER: (run_command 'ls' completed with exit code 0)\nLet's write a file."
    t = parse_pilot_turn(f'{{"thinking":{repr(raw_thinking)}, "say":"Done", "actions":[]}}')
    
    # We test that the conversation cleaning behaves as expected.
    from harness.text_clean import clean_say
    cleaned = clean_say(t.thinking)
    assert "[INFO]" not in cleaned
    assert "USER: (" not in cleaned
    assert "Analyzing code..." in cleaned
    assert "Let's write a file." in cleaned


class _ThinkingScriptedPilot:
    name = "thinking_scripted"
    def __init__(self, with_thinking=True):
        self.with_thinking = with_thinking
        self.calls = 0

    def complete(self, prompt, *, system=None):
        from pmharness.drivers.openai_compat import DriverResponse
        self.calls += 1
        if self.with_thinking:
            txt = '{"thinking":"I think I should stop.", "say":"I am stopping.", "actions":[]}'
        else:
            txt = '{"say":"I am stopping.", "actions":[]}'
        return DriverResponse(text=txt, tokens_out=10, latency_ms=1.0)


def test_conversation_does_not_emit_thinking_event_when_present():
    # Post-answer reasoning UI was removed: even when the pilot JSON includes
    # a thinking field, we never emit a ConvEvent("thinking", ...) -- the
    # answer is enough and streaming would otherwise paint reasoning after it.
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    s = ConversationalSession(cfg)
    s.pilot = _ThinkingScriptedPilot(with_thinking=True)
    events = list(s.send("Test thinking"))

    thinking_events = [e for e in events if e.kind == "thinking"]
    assert len(thinking_events) == 0

    # Verify thinking is NOT appended to _history
    for h in s._history:
        if h.get("role") == "assistant":
            assert h["content"] == "I am stopping."
            assert "I think" not in h["content"]


def test_conversation_does_not_emit_thinking_event_when_absent():
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    s = ConversationalSession(cfg)
    s.pilot = _ThinkingScriptedPilot(with_thinking=False)
    events = list(s.send("Test thinking"))

    thinking_events = [e for e in events if e.kind == "thinking"]
    assert len(thinking_events) == 0


def test_openai_compat_reasoning_param(monkeypatch):
    from pmharness.drivers.openai_compat import OpenAICompatDriver
    # Setup mock urllib.request.urlopen
    import urllib.request
    import json

    requests_made = []

    class MockResponse:
        def __init__(self, data):
            self.data = data
        def read(self):
            return self.data
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            pass

    def mock_urlopen(req, timeout=None):
        nonlocal requests_made
        body_data = req.data
        body_dict = json.loads(body_data.decode("utf-8"))
        requests_made.append(body_dict)

        resp_obj = {
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Hello world",
                },
                "finish_reason": "stop"
            }],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5}
        }
        return MockResponse(json.dumps(resp_obj).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
    monkeypatch.setenv("MOCK_API_KEY", "sk-mock-key")

    # Default: reasoning disabled (no token spend on unused post-answer traces)
    driver_default = OpenAICompatDriver(
        name="mock-driver",
        model="mock-model",
        base_url="http://mock-api",
        api_key_env="MOCK_API_KEY",
    )
    assert driver_default.enable_reasoning is False
    driver_default.chat([{"role": "user", "content": "hi"}])
    assert len(requests_made) == 1
    assert "reasoning" not in requests_made[0]

    # Explicit opt-in still sends the OpenRouter-style field
    requests_made.clear()
    driver_on = OpenAICompatDriver(
        name="mock-driver",
        model="mock-model",
        base_url="http://mock-api",
        api_key_env="MOCK_API_KEY",
        enable_reasoning=True,
    )
    driver_on.chat([{"role": "user", "content": "hi"}])
    assert len(requests_made) == 1
    assert requests_made[0]["reasoning"] == {"max_tokens": 1024}

