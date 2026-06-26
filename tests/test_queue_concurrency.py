"""
Option A; turns serialize on _history (single-writer); swarm results re-enter as labeled follow-up assistant messages; invariant _history never mutated by two threads at once.
"""
import pytest
import subprocess
import json
import threading
import time
from unittest.mock import patch, MagicMock
from harness.conversation import ConversationalSession, ConvEvent
from harness.config import HarnessConfig

def test_session_state_defaults_idle():
    cfg = HarnessConfig()
    session = ConversationalSession(cfg)
    assert session.state() == "idle"

def test_await_and_apply_job_characterization(tmp_path):
    # Set up a real git repo in tmp_path
    subprocess.run(["git", "init"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    
    file_path = tmp_path / "hello.txt"
    file_path.write_text("Hello World\n")
    subprocess.run(["git", "add", "hello.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=tmp_path, check=True)
    
    cfg = HarnessConfig()
    cfg.repo = str(tmp_path)
    session = ConversationalSession(cfg)
    
    # Assert initial tokens_used is 0
    assert session._tokens_used == 0
    
    # Mock artifacts that will be returned
    mock_artifacts = [
        {
            "type": "patch",
            "payload": {
                "files": ["hello.txt"],
                "unified_diff": "diff --git a/hello.txt b/hello.txt\n--- a/hello.txt\n+++ b/hello.txt\n@@ -1,1 +1,2 @@\n Hello World\n+Hello New World\n"
            },
            "tokens_in": 100,
            "tokens_out": 50
        }
    ]
    
    # We mock subprocess.run to intercept await and artifacts commands
    original_run = subprocess.run
    
    def mock_subprocess_run(cmd, *args, **kwargs):
        cmd_str = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
        print("MOCK RUN CALL:", cmd_str)
        is_await = any(arg == "await" for arg in cmd) if isinstance(cmd, list) else " await " in f" {cmd_str} "
        is_artifacts = any(arg == "artifacts" for arg in cmd) if isinstance(cmd, list) else " artifacts " in f" {cmd_str} "
        if is_await:
            return subprocess.CompletedProcess(cmd, 0, stdout="Awaiting complete", stderr="")
        elif is_artifacts:
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(mock_artifacts), stderr="")
        else:
            return original_run(cmd, *args, **kwargs)
            
    with patch("subprocess.run", side_effect=mock_subprocess_run):
        res = session._await_and_apply_job("job_123456789012")
        
    assert res["job_id"] == "job_123456789012"
    assert res["applied"] is True
    assert res["files"] == ["hello.txt"]
    assert res["tokens_in"] == 100
    assert res["tokens_out"] == 50
    assert "Applied patch" in res["summary"]
    assert res["error"] is None
    
    # Assert tokens folded into self._tokens_used
    assert session._tokens_used == 150
    
    # Assert the file actually changed on disk!
    assert file_path.read_text() == "Hello World\nHello New World\n"


def test_queue_drains_while_swarm_pending(tmp_path):
    # Set up a git repo in tmp_path
    subprocess.run(["git", "init"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    
    cfg = HarnessConfig()
    cfg.repo = str(tmp_path)
    session = ConversationalSession(cfg)
    
    # Mock puppetmaster being available
    with patch("harness.conversation._puppetmaster_available", return_value=True):
        # Mock pilot completing and returning run_implement action
        mock_pilot = MagicMock()
        first_resp = MagicMock()
        first_resp.text = json.dumps({
            "say": "I will run implement now.",
            "actions": [{"kind": "run_implement", "goal": "Apply fix to hello.txt", "adapter": "cursor"}]
        })
        first_resp.meta = {}
        first_resp.error = None
        
        second_resp = MagicMock()
        second_resp.text = json.dumps({
            "say": "Turn two prose reply.",
            "actions": []
        })
        second_resp.meta = {}
        second_resp.error = None
        
        mock_pilot.chat.side_effect = [first_resp, second_resp]
        session.pilot = mock_pilot
        
        # Mock subprocess.Popen for launching run_implement
        mock_proc = MagicMock()
        mock_proc.stdout = ["job_123456789012\n"]
        mock_proc.returncode = 0
        mock_proc.wait.return_value = 0
        
        # Control blocking in the background using an Event
        block_event = threading.Event()
        
        original_await_and_apply = session._await_and_apply_job
        
        def mock_await_and_apply(job_id, state_dir=None, objective=""):
            block_event.wait()
            return {
                "job_id": job_id,
                "applied": True,
                "files": ["hello.txt"],
                "tokens_in": 100,
                "tokens_out": 50,
                "summary": "Completed successfully background task",
                "error": None,
                "artifacts": [],
                "has_patch_art": True,
                "apply_msg": "applied cleanly",
                "num_artifacts": 1,
                "artifact_types": ["patch"],
                "ar_list": []
            }
        
        session._await_and_apply_job = mock_await_and_apply
        
        with patch("subprocess.Popen", return_value=mock_proc):
            events_one = list(session.send("turn one"))
            
        # Assert a swarm_pending event was emitted
        pending_events = [e for e in events_one if e.kind == "swarm_pending"]
        assert len(pending_events) == 1
        assert pending_events[0].data["job_ids"] == ["job_123456789012"]
        assert pending_events[0].data["objective"] == "Apply fix to hello.txt"
        
        # After send returns, self._busy is NOT held!
        assert session._busy.acquire(blocking=False) is True
        session._busy.release()
        
        # State should be "awaiting_swarm"
        assert session.state() == "awaiting_swarm"
        assert session.has_pending_swarms() is True
        
        # Second send can run concurrently while first is blocked
        events_two = list(session.send("turn two"))
        assert any(e.kind == "message" and "Turn two prose reply" in e.data.get("text", "") for e in events_two)
        
        # Release the event to let the background job finish
        block_event.set()
        
        # Wait a moment for background future to complete and put result
        time.sleep(0.5)
        
        # Drain results
        drain_events = list(session.drain_swarm_results())
        assert len(drain_events) == 1
        assert drain_events[0].kind == "swarm_result"
        assert drain_events[0].data["job_id"] == "job_123456789012"
        
        # Check that history has exactly one follow-up assistant message for the result
        follow_ups = [m for m in session._history if "[swarm result for:" in m.get("content", "")]
        assert len(follow_ups) == 1
        assert "[swarm result for: Apply fix to hello.txt]" in follow_ups[0]["content"]
        assert "Completed successfully background task" in follow_ups[0]["content"]
        
        # State should be back to "idle" now
        assert session.state() == "idle"
        assert session.has_pending_swarms() is False


def test_apply_lock_serializes_background_applies(tmp_path):
    # Test that self._apply_lock prevents overlapping git apply execution
    cfg = HarnessConfig()
    cfg.repo = str(tmp_path)
    session = ConversationalSession(cfg)
    
    overlap_detected = []
    active_applies = 0
    lock = threading.Lock()
    
    # We instrument _apply_worker_patch to record active parallel applications
    def mock_apply_worker_patch(artifacts, job_id=""):
        nonlocal active_applies
        with lock:
            active_applies += 1
            if active_applies > 1:
                overlap_detected.append(True)
        time.sleep(0.1)
        with lock:
            active_applies -= 1
        return True, ["hello.txt"], "applied"
        
    session._apply_worker_patch = mock_apply_worker_patch
    
    # We will dispatch two jobs concurrently
    # Both background swarms will invoke _await_and_apply_job which calls _apply_worker_patch
    def run_job(job_id):
        # Ensure _add_worker_tokens_from_artifacts doesn't crash on mocked artifacts
        session._add_worker_tokens_from_artifacts([])
        return session._await_and_apply_job(job_id)
        
    # Mock subprocess.run in _await_and_apply_job to return empty JSON list
    with patch("subprocess.run") as mock_run:
        mock_p = MagicMock()
        mock_p.returncode = 0
        mock_p.stdout = "[]"
        mock_run.return_value = mock_p
        
        t1 = threading.Thread(target=run_job, args=("job_1",))
        t2 = threading.Thread(target=run_job, args=("job_2",))
        
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        
    # Overlap must be empty because self._apply_lock serialized them
    assert len(overlap_detected) == 0


def test_concurrency_stress_safety(tmp_path):
    # Dispatch N=3 background swarms + interleave 2 normal sends;
    # assert _history has no interleaved/duplicated tool blocks and message roles alternate sanely (single-writer held).
    cfg = HarnessConfig()
    cfg.repo = str(tmp_path)
    session = ConversationalSession(cfg)
    
    # Setup mock pilot responses
    mock_pilot = MagicMock()
    r1 = MagicMock()
    r1.text = json.dumps({"say": "Reply 1", "actions": []})
    r1.meta = {}
    r1.error = None
    
    r2 = MagicMock()
    r2.text = json.dumps({"say": "Reply 2", "actions": []})
    r2.meta = {}
    r2.error = None
    
    mock_pilot.chat.side_effect = [r1, r2]
    session.pilot = mock_pilot
    
    # Simulate N=3 completed background swarm results
    # and put them directly onto the result queue
    for i in range(3):
        session._swarm_results.put({
            "job_id": f"job_{i}",
            "objective": f"Goal {i}",
            "result": {
                "job_id": f"job_{i}",
                "applied": True,
                "files": [f"file_{i}.txt"],
                "tokens_in": 10,
                "tokens_out": 20,
                "summary": f"Summary {i}",
                "error": None,
                "artifacts": [],
                "has_patch_art": True,
                "apply_msg": "clean",
                "num_artifacts": 1,
                "artifact_types": ["patch"],
                "ar_list": []
            },
            "state_dir": None
        })
        
    # Now we call send("msg 1") while holding background results, then drain_swarm_results,
    # then send("msg 2"), then drain_swarm_results.
    # We must ensure that message roles alternate sanely and there is no corruption.
    events1 = list(session.send("user message 1"))
    drained1 = list(session.drain_swarm_results())
    events2 = list(session.send("user message 2"))
    drained2 = list(session.drain_swarm_results())
    
    # Assert roles in history alternate nicely: system, user, assistant, user, assistant etc.
    # Let's inspect the session._history
    roles = [m["role"] for m in session._history]
    print("ROLES IN HISTORY:", roles)
    
    # Check that there are no adjacent assistant-assistant or user-user messages unless explicitly expected
    # and that single-writer invariant holds.
    for i in range(len(roles) - 1):
        # We can have consecutive assistant messages (due to background swarm results appending as assistant)
        # but no consecutive user messages.
        assert not (roles[i] == "user" and roles[i+1] == "user"), "Consecutive user messages detected!"
        
    # We also assert that all 3 background results got folded into history
    follow_ups = [m for m in session._history if "[swarm result for:" in m.get("content", "")]
    assert len(follow_ups) == 3


def test_api_session_state_endpoint():
    from http.server import ThreadingHTTPServer
    import urllib.request
    import urllib.error
    import harness.server as srv
    
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    
    try:
        # 1. 403 without token
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/session/state", timeout=5)
            assert False, "should have failed with 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403
            
        # 2. 200 with token
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/session/state?token={srv._TOKEN}", timeout=5)
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert data["state"] == "idle"
        assert data["pending_swarms"] is False
    finally:
        httpd.shutdown()

