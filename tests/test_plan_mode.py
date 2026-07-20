import tempfile
import pytest
from harness.pilot import PILOT_SYSTEM, PLAN_SYSTEM_SUFFIX
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession

class _FakePilotWithActions:
    """A fake pilot that can emit specific actions."""
    name = "fake_actions"
    def __init__(self, actions):
        self.actions = actions
        self.system_received = None
        self.calls = 0

    def chat(self, messages, tools=None, system=None):
        from pmharness.drivers.openai_compat import DriverResponse
        self.system_received = system
        self.calls += 1
        import json
        if self.calls == 1:
            txt = json.dumps({
                "say": "Here is what I will do.",
                "actions": self.actions
            })
        else:
            txt = json.dumps({
                "say": "Done.",
                "actions": []
            })
        return DriverResponse(text=txt, tokens_out=10, latency_ms=1.0)


def test_plan_mode_system_prompt():
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    s = ConversationalSession(cfg)
    fake = _FakePilotWithActions([])
    s.pilot = fake
    
    # Capture original system prompt
    original_sys = s._history[0]["content"]
    
    # send(plan=False): system stays clean; no plan suffix on user turn
    list(s.send("test message", plan=False))
    assert fake.system_received is not None
    assert PLAN_SYSTEM_SUFFIX not in fake.system_received
    assert PLAN_SYSTEM_SUFFIX not in (s._history[-1].get("content") or "")
    
    # send(plan=True): plan suffix rides on the user turn (not system) so
    # append-only / prompt-cache prefixes stay byte-stable.
    list(s.send("test message", plan=True))
    assert fake.system_received is not None
    assert PLAN_SYSTEM_SUFFIX not in fake.system_received
    assert any(
        PLAN_SYSTEM_SUFFIX in (m.get("content") or "")
        for m in s._history
        if m.get("role") == "user"
    )
    assert s._history[0]["content"] == original_sys or s._history[0]["content"].startswith(
        original_sys.split("\n")[0]
    )


def test_plan_mode_filters_edit_actions():
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    cfg.repo = tempfile.mkdtemp() # set workspace so read/write doesn't error out instantly
    s = ConversationalSession(cfg)
    
    # write_file, run_command, run_implement, run_parallel, read_file
    actions = [
        {"kind": "write_file", "path": "test.txt", "content": "hello"},
        {"kind": "run_command", "command": "echo test"},
        {"kind": "run_implement", "goal": "implement test"},
        {"kind": "run_parallel", "goals": ["parallel 1"]},
        {"kind": "read_file", "path": "test.txt"}
    ]
    fake = _FakePilotWithActions(actions)
    s.pilot = fake
    
    events = list(s.send("do work", plan=True))
    
    # Verify that skipped events were emitted with error message "(plan mode: skipped <kind>)"
    action_results = [e for e in events if e.kind == "action_result"]
    assert len(action_results) == 5
    
    # First 4 are skipped (write_file, run_command, run_implement, run_parallel)
    assert "skipped write_file" in action_results[0].data["error"]
    assert "skipped run_command" in action_results[1].data["error"]
    assert "skipped run_implement" in action_results[2].data["error"]
    assert "skipped run_parallel" in action_results[3].data["error"]
    
    # 5th (read_file) is executed, fails because test.txt doesn't exist, but is NOT skipped as plan mode skipped
    assert "File not found" in action_results[4].data["error"]


def test_plan_mode_filters_mcp_mutate_actions():
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    cfg.repo = tempfile.mkdtemp()
    s = ConversationalSession(cfg)
    s._mcp = type("M", (), {
        "call": staticmethod(lambda *a, **k: (_ for _ in ()).throw(AssertionError("call"))),
        "manage": staticmethod(lambda *a, **k: (_ for _ in ()).throw(AssertionError("manage"))),
        "discovered_tools": staticmethod(lambda: []),
    })()
    actions = [
        {"kind": "call_mcp", "tool": "fake.echo", "arguments": {"text": "x"}},
        {"kind": "manage_mcp", "action": "list"},
        {"kind": "read_file", "path": "missing.txt"},
    ]
    s.pilot = _FakePilotWithActions(actions)
    events = list(s.send("plan mcp", plan=True))
    action_results = [e for e in events if e.kind == "action_result"]
    assert "skipped call_mcp" in action_results[0].data["error"]
    assert "skipped manage_mcp" in action_results[1].data["error"]
    assert "File not found" in action_results[2].data["error"]


def test_plan_mode_filters_browser_actions():
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    cfg.repo = tempfile.mkdtemp()
    s = ConversationalSession(cfg)

    class _BoomBrowser:
        def browser_navigate(self, *a, **k):
            raise AssertionError("browser must not run in plan mode")

        def browser_click(self, *a, **k):
            raise AssertionError("browser must not run in plan mode")

    s._browser = _BoomBrowser()
    actions = [
        {"kind": "browser_navigate", "url": "https://example.com"},
        {"kind": "browser_click", "arguments": {"ref": "e1"}},
        {"kind": "read_file", "path": "missing.txt"},
    ]
    s.pilot = _FakePilotWithActions(actions)
    events = list(s.send("plan browse", plan=True))
    action_results = [e for e in events if e.kind == "action_result"]
    assert "skipped browser_navigate" in action_results[0].data["error"]
    assert "skipped browser_click" in action_results[1].data["error"]
    assert "File not found" in action_results[2].data["error"]
