"""Pilot envelope parsing + conversational loop (the product UX)."""
import tempfile
import pytest
pytestmark = pytest.mark.swarm
from harness.pilot import (parse_pilot_turn, PilotTurn, PilotError,
                           PilotAction, _coerce_actions)
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


def test_parse_clean_envelope():
    t = parse_pilot_turn('{"say":"Looking into it.","actions":[{"kind":"run_swarm","goal":"Map auth"}]}')
    assert t.say == "Looking into it."
    assert t.has_actions and t.actions[0].goal == "Map auth"


def test_parse_say_only_no_actions():
    t = parse_pilot_turn('{"say":"API means Application Programming Interface.","actions":[]}')
    assert t.say.startswith("API means")
    assert not t.has_actions


def test_parse_bare_prose_is_say_only():
    t = parse_pilot_turn("Just a greeting, no JSON here.")
    assert "greeting" in t.say
    assert not t.has_actions


def test_parse_json_in_fences_with_prose():
    raw = 'Sure!\n```json\n{"say":"On it","actions":[{"kind":"run_swarm","goal":"Audit X"}]}\n```'
    t = parse_pilot_turn(raw)
    assert t.actions[0].goal == "Audit X"


def test_action_requires_goal():
    try:
        _coerce_actions([{"kind": "run_swarm", "goal": ""}])
        assert False, "should have raised"
    except PilotError:
        pass


def test_coerce_relocate_promotes_workspace_root_to_path():
    acts = _coerce_actions([{
        "kind": "relocate_session",
        "workspace_root": "C:\\tmp\\kotoba",
    }])
    assert len(acts) == 1
    assert acts[0].path == "C:\\tmp\\kotoba"
    assert acts[0].arguments.get("workspace_root") == "C:\\tmp\\kotoba"


def test_roles_string_coerced_to_list():
    acts = _coerce_actions([{"kind": "run_swarm", "goal": "g", "roles": "explore"}])
    assert acts[0].roles == ["explore"]


class _ScriptedPilot:
    """A fake pilot: says it'll investigate, fires one swarm, then explains+stops."""
    name = "scripted"
    def __init__(self): self.calls = 0
    def complete(self, prompt, *, system=None):
        from pmharness.drivers.openai_compat import DriverResponse
        self.calls += 1
        if self.calls == 1:
            txt = '{"say":"I will map the loop.","actions":[{"kind":"run_swarm","goal":"Map the loop"}]}'
        else:
            txt = '{"say":"Done -- the loop has 3 stages.","actions":[]}'
        return DriverResponse(text=txt, tokens_out=10, latency_ms=1.0)


def test_conversational_loop_drives_swarm_then_finishes(monkeypatch):
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    s = ConversationalSession(cfg)
    s.pilot = _ScriptedPilot()  # inject fake pilot
    events = list(s.send("How does the loop work?"))
    kinds = [e.kind for e in events]
    assert "message" in kinds          # pilot prose
    assert "action_start" in kinds     # fired a swarm (collapsible card)
    assert "action_result" in kinds    # swarm returned artifacts
    assert kinds[-1] == "assistant_done"
    # the swarm produced real demo artifacts
    ar = [e for e in events if e.kind == "action_result"][0]
    assert ar.data["num"] > 0
