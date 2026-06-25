"""Intent repair: a verbose model that wraps JSON in prose is salvaged on one
retry; a hopeless model fails cleanly after the repair budget."""
import json
from harness.repair import drive_with_repair
from pmharness.drivers.base import DriverResponse, SYSTEM_PROMPT


class _ProseFirstDriver:
    """Emits chain-of-thought prose on attempt 1 (no JSON), valid JSON on retry.
    Mirrors the Kimi failure mode."""
    name = "prose-first"
    def __init__(self): self.calls = 0
    def complete(self, prompt, *, system=SYSTEM_PROMPT):
        self.calls += 1
        if self.calls == 1:
            return DriverResponse(text="Let me think step by step about this task...",
                                  tokens_out=120, model=self.name)
        return DriverResponse(text='{"action":"answer","rationale":"ok"}',
                              tokens_out=8, model=self.name)


class _HopelessDriver:
    name = "hopeless"
    def complete(self, prompt, *, system=SYSTEM_PROMPT):
        return DriverResponse(text="I cannot produce JSON, ever.", tokens_out=10, model=self.name)


def test_repair_salvages_prose_first_model():
    d = _ProseFirstDriver()
    intent, resp, repairs = drive_with_repair(d, "What is JSON?", SYSTEM_PROMPT)
    assert intent is not None
    assert intent.action == "answer"
    assert repairs == 1
    assert d.calls == 2
    # token accounting accumulated across both attempts
    assert resp.tokens_out == 128


def test_repair_gives_up_cleanly_on_hopeless_model():
    d = _HopelessDriver()
    intent, resp, repairs = drive_with_repair(d, "x", SYSTEM_PROMPT)
    assert intent is None
    assert "invalid intent" in (resp.error or "")
    assert repairs == 1


def test_valid_first_try_no_repair():
    class Good:
        name="good"
        def complete(self, p, *, system=SYSTEM_PROMPT):
            return DriverResponse(text='{"action":"stop","rationale":"done"}', tokens_out=8, model="good")
    intent, resp, repairs = drive_with_repair(Good(), "x", SYSTEM_PROMPT)
    assert intent.action == "stop" and repairs == 0
