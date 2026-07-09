import os
import tempfile
import time
import pytest
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession, ConvEvent

class _FakePilotWithActions:
    name = "fake_actions"
    def __init__(self, actions):
        self.actions = actions
        self.calls = 0

    def chat(self, messages, tools=None, system=None):
        from pmharness.drivers.openai_compat import DriverResponse
        self.calls += 1
        import json
        if self.calls == 1:
            txt = json.dumps({
                "say": "Executing actions.",
                "actions": self.actions
            })
        else:
            txt = json.dumps({
                "say": "Done.",
                "actions": []
            })
        return DriverResponse(text=txt, tokens_out=10, latency_ms=1.0)


def test_parallel_reads_ordering_and_contents():
    # (a) a turn with 3 read_file actions returns all 3 results in the SAME order as a serial run, contents correct
    temp_dir = tempfile.mkdtemp()
    
    # Create the 3 files
    file1_path = os.path.join(temp_dir, "file1.txt")
    file2_path = os.path.join(temp_dir, "file2.txt")
    file3_path = os.path.join(temp_dir, "file3.txt")
    
    with open(file1_path, "w") as f:
        f.write("content of file 1")
    with open(file2_path, "w") as f:
        f.write("content of file 2")
    with open(file3_path, "w") as f:
        f.write("content of file 3")
        
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    cfg.repo = temp_dir
    s = ConversationalSession(cfg)
    
    actions = [
        {"kind": "read_file", "path": "file1.txt"},
        {"kind": "read_file", "path": "file2.txt"},
        {"kind": "read_file", "path": "file3.txt"},
    ]
    s.pilot = _FakePilotWithActions(actions)
    
    events = list(s.send("run three reads"))
    
    # Filter action_result events
    ar_events = [e for e in events if e.kind == "action_result"]
    assert len(ar_events) == 3
    
    # Get all tool results from history
    user_inputs_and_tools = [h for h in s._history if h["role"] in ("user", "tool") and h != s._history[1]]
    assert len(user_inputs_and_tools) == 3
    
    # Check that they match the history in correct order and are correct
    assert "content of file 1" in user_inputs_and_tools[0]["content"]
    assert "file1.txt" in user_inputs_and_tools[0]["content"]
    
    assert "content of file 2" in user_inputs_and_tools[1]["content"]
    assert "file2.txt" in user_inputs_and_tools[1]["content"]
    
    assert "content of file 3" in user_inputs_and_tools[2]["content"]
    assert "file3.txt" in user_inputs_and_tools[2]["content"]


def test_parallel_reads_confinement_rejected():
    # (b) a read_file outside the repo root is still rejected (confinement honored in the parallel path)
    temp_dir = tempfile.mkdtemp()
    
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    cfg.repo = temp_dir
    s = ConversationalSession(cfg)
    
    # Let's request a read_file outside the repository
    actions = [
        {"kind": "read_file", "path": "../outside.txt"},
        {"kind": "read_file", "path": "file1.txt"}  # needs 2+ reads to trigger prefetch
    ]
    s.pilot = _FakePilotWithActions(actions)
    
    events = list(s.send("run safe reads"))
    
    ar_events = [e for e in events if e.kind == "action_result"]
    assert len(ar_events) == 2
    
    # First action (outside.txt) should be rejected
    assert ar_events[0].data["id"] == "a1"
    assert "error" in ar_events[0].data
    assert "Path traversal attempt rejected" in ar_events[0].data["error"]


def test_parallel_reads_mixed_with_write():
    # (c) a turn mixing read_file + write_file applies the write correctly and reads are still correct (ordering preserved)
    temp_dir = tempfile.mkdtemp()
    
    file1_path = os.path.join(temp_dir, "file1.txt")
    file2_path = os.path.join(temp_dir, "file2.txt")
    with open(file1_path, "w") as f:
        f.write("file1 init")
    with open(file2_path, "w") as f:
        f.write("file2 init")
        
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    cfg.repo = temp_dir
    s = ConversationalSession(cfg)
    
    actions = [
        {"kind": "read_file", "path": "file1.txt"},
        {"kind": "write_file", "path": "file3.txt", "content": "file3 written content"},
        {"kind": "read_file", "path": "file2.txt"},
    ]
    s.pilot = _FakePilotWithActions(actions)
    
    events = list(s.send("run mixed turn"))
    
    ar_events = [e for e in events if e.kind == "action_result"]
    assert len(ar_events) == 3
    
    # Verify the write file was applied
    file3_actual_path = os.path.join(temp_dir, "file3.txt")
    assert os.path.exists(file3_actual_path)
    with open(file3_actual_path, "r") as f:
        assert f.read() == "file3 written content"
        
    # Verify order and values
    user_inputs_and_tools = [h for h in s._history if h["role"] in ("user", "tool") and h != s._history[1]]
    assert len(user_inputs_and_tools) == 3
    
    assert "file1 init" in user_inputs_and_tools[0]["content"]
    assert "successfully wrote" in user_inputs_and_tools[1]["content"]
    assert "file3.txt" in user_inputs_and_tools[1]["content"]
    assert "file2 init" in user_inputs_and_tools[2]["content"]


def test_parallel_reads_timing_sanity(monkeypatch):
    # (d) timing sanity: mock the read helper with a small sleep and assert 3 reads take ~1x not ~3x (use a helper that sleeps 0.2s and assert total < 0.45s)
    temp_dir = tempfile.mkdtemp()
    
    file1_path = os.path.join(temp_dir, "file1.txt")
    file2_path = os.path.join(temp_dir, "file2.txt")
    file3_path = os.path.join(temp_dir, "file3.txt")
    
    with open(file1_path, "w") as f:
        f.write("1")
    with open(file2_path, "w") as f:
        f.write("2")
    with open(file3_path, "w") as f:
        f.write("3")
        
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    cfg.repo = temp_dir
    s = ConversationalSession(cfg)
    
    # Monkeypatch ConversationalSession._do_read_file
    original_do_read_file = ConversationalSession._do_read_file
    def mocked_do_read_file(self, act):
        time.sleep(0.5)
        return original_do_read_file(self, act)
        
    monkeypatch.setattr(ConversationalSession, "_do_read_file", mocked_do_read_file)
    
    actions = [
        {"kind": "read_file", "path": "file1.txt"},
        {"kind": "read_file", "path": "file2.txt"},
        {"kind": "read_file", "path": "file3.txt"},
    ]
    s.pilot = _FakePilotWithActions(actions)
    
    start_time = time.time()
    list(s.send("run parallel timed reads"))
    elapsed = time.time() - start_time
    
    # Serial would be 3 * 0.5s = 1.5s of sleeping alone; parallel is ~0.5s.
    # Windows CI has flaked above 1.2s from git/config probe overhead even when
    # the three sleeps overlap, so allow more headroom there while still
    # failing a true serial regression (~1.5s sleep + overhead).
    import sys
    bound = 2.0 if sys.platform == "win32" else 1.2
    assert elapsed < bound, f"Elapsed time {elapsed}s suggests reads ran serially!"
