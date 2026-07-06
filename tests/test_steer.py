"""Tests for mid-turn steering (out-of-band user messages) backend functionality."""
import json
import os
import threading
import tempfile
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import pytest
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


def test_steer_queue_round_trip():
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    s = ConversationalSession(cfg)
    
    assert s.drain_steer() == []
    
    s.enqueue_steer("First steer message")
    s.enqueue_steer("Second steer message")
    
    assert s.drain_steer() == ["First steer message", "Second steer message"]
    assert s.drain_steer() == []


class _SteeringPilot:
    def __init__(self, session):
        self.session = session
        self.calls = 0

    def complete(self, prompt, *, system=None):
        from pmharness.drivers.openai_compat import DriverResponse
        self.calls += 1
        if self.calls == 1:
            # Enqueue a steer mid-turn
            self.session.enqueue_steer("Pay attention to the formatting rules please.")
            txt = '{"say":"Starting initial check...","actions":[{"kind":"read_file","path":"AGENTS.md"}]}'
        else:
            txt = '{"say":"Done checking files.","actions":[]}'
        return DriverResponse(text=txt, tokens_out=10, latency_ms=1.0)


def test_drive_turn_with_mid_turn_steer():
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    s = ConversationalSession(cfg)
    s.pilot = _SteeringPilot(s)
    
    events = list(s.send("Let's audit the repository"))
    
    # Assert steer event was emitted
    kinds = [e.kind for e in events]
    assert "steer" in kinds
    assert kinds[-1] == "assistant_done"
    
    # Check that the OUT-OF-BAND user message was injected into history
    found_marker = False
    for msg in s._history:
        if msg["role"] == "user" and "[OUT-OF-BAND USER MESSAGE" in msg["content"]:
            assert "Pay attention to the formatting rules please." in msg["content"]
            found_marker = True
            
    assert found_marker, "Out of band steer message was not found in history"
    
    # Assert strict role alternation holds in history (excluding system prompt at index 0)
    history_roles = [msg["role"] for msg in s._history if msg["role"] != "system"]
    for idx in range(1, len(history_roles)):
        assert history_roles[idx] != history_roles[idx - 1], f"Strict alternation broken: consecutive {history_roles[idx]} at index {idx}"


class _FinalizingPilot:
    """First call yields NO actions (the model is finalizing its answer). A steer
    is enqueued at that exact moment -- there is no tool result to piggyback on, so
    the finalization-time drain must deliver it as a next-turn user message and
    re-ask the model rather than terminating. The second call (after the steer is
    delivered) finishes cleanly."""
    def __init__(self, session):
        self.session = session
        self.calls = 0

    def complete(self, prompt, *, system=None):
        from pmharness.drivers.openai_compat import DriverResponse
        self.calls += 1
        if self.calls == 1:
            # The model is done talking and emits no actions; a steer arrives now.
            self.session.enqueue_steer("Actually, also check the README.")
            txt = '{"say":"Here is my answer.","actions":[]}'
        else:
            txt = '{"say":"Acknowledged, checked the README.","actions":[]}'
        return DriverResponse(text=txt, tokens_out=10, latency_ms=1.0)


def test_steer_during_finalization_reasks_model():
    cfg = HarnessConfig(driver="stub-oracle-v2", state_dir=tempfile.mkdtemp())
    s = ConversationalSession(cfg)
    s.pilot = _FinalizingPilot(s)

    events = list(s.send("Give me your final answer"))
    kinds = [e.kind for e in events]

    # The steer must surface and the model must be re-asked (two pilot calls),
    # not terminated on the first no-action turn.
    assert "steer" in kinds, "the steer must surface as an event"
    assert s.pilot.calls == 2, "the model must be re-asked after the finalization-time steer"
    assert kinds[-1] == "assistant_done", "the run must still end cleanly after delivery"

    # The steer must be delivered as a genuine next-turn user message.
    found_marker = False
    for msg in s._history:
        if msg["role"] == "user" and "[OUT-OF-BAND USER MESSAGE" in msg.get("content", ""):
            assert "Actually, also check the README." in msg["content"]
            found_marker = True
    assert found_marker, "the finalization-time steer was not delivered as a user message"

    # No steer may be left stranded as pending.
    assert s.drain_steer() == [], "the steer must not remain pending"


def test_api_session_steer_auth(tmp_path):
    import harness.server as srv
    
    # Start local testing server
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    
    # Set mock configurations on srv
    srv._cfg.state_dir = str(tmp_path)
    srv._sessions.path = str(tmp_path / "harness_sessions.json")
    srv._sessions._sessions = []
    
    try:
        # 1. POST without token should fail with 403
        req_no_token = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/session/steer",
            data=json.dumps({"text": "Hello without token"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        try:
            urllib.request.urlopen(req_no_token, timeout=5)
            assert False, "should have failed with 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403
            
        # 2. POST with token but empty/missing text should fail with 400
        req_empty_text = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/session/steer",
            data=json.dumps({"text": ""}).encode(),
            headers={"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN},
            method="POST"
        )
        try:
            urllib.request.urlopen(req_empty_text, timeout=5)
            assert False, "should have failed with 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
            
        # 3. POST with token and valid text should succeed and enqueue steer
        req_valid = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/session/steer",
            data=json.dumps({"text": "Adjust course left"}).encode(),
            headers={"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN},
            method="POST"
        )
        resp = urllib.request.urlopen(req_valid, timeout=5)
        assert resp.status == 200
        resp_data = json.loads(resp.read().decode())
        assert resp_data["ok"] is True
        
        # Verify that srv._pilot has enqueued the steer message
        assert srv._pilot.drain_steer() == ["Adjust course left"]
        
    finally:
        httpd.shutdown()


def test_api_session_steer_image_path_traversal_blocked(tmp_path):
    """Steer image attachments must be validated like queue/chat/run."""
    import harness.server as srv
    import tempfile

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    srv._cfg.state_dir = str(tmp_path)
    srv._sessions.path = str(tmp_path / "harness_sessions.json")
    srv._sessions._sessions = []

    try:
        bad_path = os.path.join(tempfile.gettempdir(), "steer_bad_outside.png")
        with open(bad_path, "wb") as f:
            f.write(b"fake png")

        req_bad = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/session/steer",
            data=json.dumps({"text": "look at this", "images": [bad_path]}).encode(),
            headers={"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN},
            method="POST",
        )
        try:
            urllib.request.urlopen(req_bad, timeout=5)
            assert False, "should have been rejected with 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
            data = json.loads(e.read().decode())
            assert "Invalid image path" in data["error"]

        good_path = os.path.join(srv._UPLOAD_DIR, "steer_valid.png")
        os.makedirs(srv._UPLOAD_DIR, exist_ok=True)
        with open(good_path, "wb") as f:
            f.write(b"fake png")

        req_good = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/session/steer",
            data=json.dumps({"text": "adjust left", "images": [good_path]}).encode(),
            headers={"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN},
            method="POST",
        )
        resp = urllib.request.urlopen(req_good, timeout=5)
        assert resp.status == 200
        assert json.loads(resp.read().decode())["ok"] is True
    finally:
        try:
            os.remove(bad_path)
        except OSError:
            pass
        try:
            os.remove(good_path)
        except OSError:
            pass
        httpd.shutdown()
