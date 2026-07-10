"""Tests for per-turn output token budget directive (+Nk / +Nk!)."""
from __future__ import annotations

import json
import shutil
import tempfile

import pytest

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.turn_budget import parse_turn_budget, turn_budget_enabled


@pytest.mark.parametrize(
    "text,expected",
    [
        ("+50k", {"total": 50_000, "hard": False}),
        ("+1.5m!", {"total": 1_500_000, "hard": True}),
        ("+500", {"total": 500, "hard": False}),
        ("Please keep it short +5k thanks", {"total": 5_000, "hard": False}),
        ("+2K!", {"total": 2_000, "hard": True}),
    ],
)
def test_parse_turn_budget_matches(text, expected):
    assert parse_turn_budget(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "$+5k",
        "v+2",
        "+0",
        "+-5k",
        "",
        "price is 5k dollars",
    ],
)
def test_parse_turn_budget_non_matches(text):
    assert parse_turn_budget(text) is None


def test_parse_turn_budget_never_raises():
    assert parse_turn_budget(None) is None  # type: ignore[arg-type]
    parse_turn_budget("+\u0000k")


def test_turn_budget_env_toggle(monkeypatch):
    monkeypatch.setenv("HARNESS_TURN_BUDGET", "off")
    assert not turn_budget_enabled()
    monkeypatch.setenv("HARNESS_TURN_BUDGET", "1")
    assert turn_budget_enabled()


class _BudgetPilot:
    name = "budget-pilot"

    def __init__(self, turns, tokens_out=60):
        self.turns = turns
        self.tokens_out = tokens_out
        self.step = 0
        self.system_prompts: list[str] = []
        self.user_contents: list[str] = []

    def chat(self, messages, tools=None, system=None):
        from pmharness.drivers.openai_compat import DriverResponse

        self.system_prompts.append(system or "")
        for m in messages or []:
            if isinstance(m, dict) and m.get("role") == "user":
                self.user_contents.append(str(m.get("content") or ""))
        txt = self.turns[min(self.step, len(self.turns) - 1)]
        self.step += 1
        return DriverResponse(text=txt, tokens_out=self.tokens_out, latency_ms=1.0)

    def complete(self, prompt, *, system=None):
        from pmharness.drivers.openai_compat import DriverResponse

        self.system_prompts.append(system or "")
        txt = self.turns[min(self.step, len(self.turns) - 1)]
        self.step += 1
        return DriverResponse(text=txt, tokens_out=self.tokens_out, latency_ms=1.0)


def test_hard_turn_budget_stops_loop_early():
    temp_dir = tempfile.mkdtemp()
    try:
        cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=temp_dir)
        cfg.repo = temp_dir
        session = ConversationalSession(cfg)
        turns = [
            json.dumps({"say": "step 1", "actions": [{"kind": "run_command", "command": "echo 1"}]}),
            json.dumps({"say": "step 2", "actions": [{"kind": "run_command", "command": "echo 2"}]}),
            json.dumps({"say": "done", "actions": []}),
        ]
        session.pilot = _BudgetPilot(turns, tokens_out=60)
        events = list(session.send("Work on this +100!"))
        done = next(e for e in events if e.kind == "assistant_done")
        assert done.data.get("turn_budget_exhausted") is True
        assert done.data["turns"] == 2
        usage = session.get_context_usage()
        assert usage.get("turn_budget_exhausted") is True
        assert usage.get("turn_budget_total") == 100
        assert usage.get("turn_output_tokens") == 120
    finally:
        shutil.rmtree(temp_dir)


def test_advisory_budget_note_in_system_prompt():
    """Budget note rides the user trailer under append-only (default auto)."""
    temp_dir = tempfile.mkdtemp()
    try:
        cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=temp_dir)
        cfg.repo = temp_dir
        session = ConversationalSession(cfg)
        pilot = _BudgetPilot(
            [json.dumps({"say": "ok", "actions": []})],
            tokens_out=5,
        )
        session.pilot = pilot
        list(session.send("Summarize +50k"))
        assert pilot.step == 1
        needle = "output budget for this turn: 50000 tokens"
        # Append-only keeps system frozen; note is on the user turn trailer.
        assert not any(needle in system for system in pilot.system_prompts)
        assert any(needle in content for content in pilot.user_contents)
    finally:
        shutil.rmtree(temp_dir)
