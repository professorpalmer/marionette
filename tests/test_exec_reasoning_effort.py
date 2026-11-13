"""`harness exec --reasoning-effort` reaches the endpoint wire and the workers."""
from __future__ import annotations

import http.server
import json
import os
import threading

import pytest

from harness import exec_cmd
from harness import providers as prov
from harness.local_model_manager import LocalModelManager, reset_manager_for_tests

EFFORT_ENVS = ("HARNESS_CODEX_REASONING_EFFORT", "HARNESS_ENDPOINT_REASONING_EFFORT",
               "HARNESS_SWARM_REASONING_EFFORT", "PUPPETMASTER_WORKER_EFFORT")


class _Capture(http.server.BaseHTTPRequestHandler):
    bodies: list = []

    def do_POST(self):
        size = int(self.headers.get("Content-Length") or 0)
        type(self).bodies.append(json.loads(self.rfile.read(size)))
        reply = json.dumps({"id": "x", "object": "chat.completion", "model": "qwen", "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args):
        pass


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    """A loopback endpoint saved as an external, plus the bodies it receives."""
    for name in EFFORT_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    _Capture.bodies = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Capture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    reset_manager_for_tests()
    mgr = LocalModelManager(root=str(tmp_path / "local-models"), catalog={
        "version": 1, "models": [],
        "runtime": {"id": "llama.cpp", "release": "t", "binary": "llama-server", "assets": {}}})
    state = mgr._state()
    state["externals"] = [{
        "id": "loop", "vendor": "", "base_url": "http://127.0.0.1:%d/v1" % server.server_address[1],
        "models": ["qwen"], "selected_model": "qwen", "healthy": True, "has_key": False,
        "kind": "loopback", "requires_key": False}]
    mgr._save(state)
    monkeypatch.setattr("harness.local_model_manager.get_manager", lambda: mgr)
    yield _Capture.bodies
    server.shutdown()


def test_endpoint_pilot_sends_the_exec_effort_on_the_wire(endpoint, monkeypatch):
    monkeypatch.setenv("HARNESS_ENDPOINT_REASONING_EFFORT", "high")
    prov.build_pilot("local:loop/qwen").chat([{"role": "user", "content": "hi"}])
    assert endpoint[-1]["reasoning_effort"] == "high"


def test_endpoint_pilot_sends_no_effort_unless_exec_set_one(endpoint):
    prov.build_pilot("local:loop/qwen").chat([{"role": "user", "content": "hi"}])
    assert "reasoning_effort" not in endpoint[-1]


def test_exec_flag_sets_pilot_endpoint_and_worker_effort(monkeypatch, tmp_path):
    for name in EFFORT_ENVS:
        monkeypatch.delenv(name, raising=False)
    seen = {}

    class Probe:
        def __init__(self, cfg):
            pass

        def send(self, message, resume=False):
            seen.update({name: os.environ.get(name) for name in EFFORT_ENVS})
            return iter(())

        def has_pending_swarms(self):
            return False

        def drain_swarm_results(self):
            return iter(())

    monkeypatch.setattr("harness.conversation.ConversationalSession", Probe)
    assert exec_cmd.run_exec(["task", "--repo", str(tmp_path), "--reasoning-effort", "high"]) == 0
    assert seen == {name: "high" for name in EFFORT_ENVS}


def test_exec_effort_outside_the_worker_range_leaves_workers_alone(monkeypatch, tmp_path):
    for name in EFFORT_ENVS:
        monkeypatch.delenv(name, raising=False)
    exec_cmd._set_reasoning_effort("max")
    assert os.environ["HARNESS_ENDPOINT_REASONING_EFFORT"] == "max"
    assert "PUPPETMASTER_WORKER_EFFORT" not in os.environ
    for name in EFFORT_ENVS:
        monkeypatch.delenv(name, raising=False)
