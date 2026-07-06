"""Tests for read-only L0-L3 memory layer snapshot helpers."""
import json
import os
import shutil
import tempfile
import threading
import urllib.request

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.memory_layers import (
    JOURNAL_FILENAME,
    LAYER_IDS,
    estimate_l0_hot_chars,
    layer_snapshot_at,
    latest_layer_snapshot,
    measure_l1_session,
    measure_l2_workspace,
    measure_l3_cold,
    record_memory_layer_snapshot,
    snapshot_memory_layers,
)
from harness.spill_registry import register_spill


class _FakeConversation:
    def __init__(self, history):
        self._history = history


def test_l0_increases_when_history_grows():
    small = _FakeConversation(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ]
    )
    large = _FakeConversation(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello there"},
            {"role": "user", "content": "x" * 500},
        ]
    )
    assert estimate_l0_hot_chars(large) > estimate_l0_hot_chars(small)


def test_l1_reflects_registered_spill(tmp_path):
    state = str(tmp_path)
    path = os.path.join(state, "pmharness-results", "call1.txt")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("spilled content")
    register_spill(state, "sess1", "call1", path, len("spilled content"))

    layer = measure_l1_session(state, "sess1")
    assert layer["bytes"] > 0
    assert layer["entries"] >= 1
    assert layer["components"]["spill_entries"] >= 1


def test_l2_missing_stores_return_zeros(tmp_path):
    layer = measure_l2_workspace(str(tmp_path), repo=str(tmp_path))
    assert layer == {"bytes": 0, "entries": 0, "components": {}}


def test_l3_missing_stores_return_zeros(tmp_path):
    layer = measure_l3_cold(str(tmp_path), "sess1")
    assert layer["bytes"] == 0
    assert layer["entries"] == 0
    assert "components" in layer


def test_snapshot_shape(tmp_path):
    conv = _FakeConversation([{"role": "system", "content": "s"}, {"role": "user", "content": "q"}])
    snap = snapshot_memory_layers(conv, str(tmp_path), "default", repo=str(tmp_path))
    assert set(snap.keys()) == set(LAYER_IDS) | {"snapshot_at"}
    for layer_id in LAYER_IDS:
        assert "bytes" in snap[layer_id]
        assert "entries" in snap[layer_id]
    assert snap["L0"]["bytes"] > 0
    assert isinstance(snap["snapshot_at"], str)


def test_l1_turn_context_lines_counted(tmp_path):
    state = str(tmp_path)
    from harness.turn_context import record_turn_context

    record_turn_context(state, "s1", 1, repo=str(tmp_path))
    record_turn_context(state, "s1", 2, repo=str(tmp_path))
    layer = measure_l1_session(state, "s1")
    assert layer["components"]["turn_context_lines"] == 2
    assert layer["bytes"] > 0


def test_measurement_never_raises_on_bad_paths():
    conv = _FakeConversation([])
    snap = snapshot_memory_layers(conv, "", "default", repo="")
    for layer_id in LAYER_IDS:
        assert snap[layer_id]["bytes"] == 0
        assert snap[layer_id]["entries"] == 0


def test_record_and_layer_snapshot_at(tmp_path):
    state = str(tmp_path)
    snap = snapshot_memory_layers(_FakeConversation([]), state, "s1")
    record_memory_layer_snapshot(state, "s1", 1, snap)
    read_back = layer_snapshot_at(state, "s1", 1)
    assert read_back is not None
    assert read_back["L0"]["bytes"] == snap["L0"]["bytes"]


def test_latest_layer_snapshot_empty_when_missing(tmp_path):
    assert latest_layer_snapshot(str(tmp_path), "s1") == {}


def test_send_records_memory_layers_journal():
    tmpdir = tempfile.mkdtemp()
    try:
        cfg = HarnessConfig(state_dir=tmpdir, repo=os.path.realpath(tmpdir))
        session = ConversationalSession(cfg)
        session.harness_session_id = "mem-layer-session"

        class DonePilot:
            def complete(self, prompt, system=None):
                return type("R", (), {"text": json.dumps({"say": "ok", "actions": []}), "error": "", "tokens_in": 1, "tokens_out": 1})()

        session.pilot = DonePilot()
        list(session.send("hello"))
        journal = os.path.join(tmpdir, JOURNAL_FILENAME)
        assert os.path.isfile(journal)
        snap = latest_layer_snapshot(tmpdir, "mem-layer-session")
        assert snap.get("L0", {}).get("bytes", 0) > 0
        assert layer_snapshot_at(tmpdir, "mem-layer-session", 1) is not None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_usage_api_includes_memory_layers():
    tmp_dir = tempfile.mkdtemp()
    try:
        import harness.server as srv

        srv._session.state_dir = tmp_dir
        srv._pilot.state_dir = tmp_dir
        srv._pilot.harness_session_id = "usage-mem-session"
        snap = snapshot_memory_layers(srv._pilot, tmp_dir, "usage-mem-session")
        record_memory_layer_snapshot(tmp_dir, "usage-mem-session", 1, snap)

        from http.server import ThreadingHTTPServer

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            headers = {"X-Harness-Token": srv._TOKEN}
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/usage",
                headers=headers,
                method="GET",
            )
            usage = json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
            layers = usage["session"].get("memory_layers")
            assert isinstance(layers, dict)
            assert layers.get("L0", {}).get("bytes", 0) >= 0
        finally:
            httpd.shutdown()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_usage_api_missing_journal_degrades_to_empty_dict():
    tmp_dir = tempfile.mkdtemp()
    try:
        import harness.server as srv

        srv._session.state_dir = tmp_dir
        srv._pilot.state_dir = tmp_dir
        srv._pilot.harness_session_id = "empty-mem-session"

        from http.server import ThreadingHTTPServer

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            headers = {"X-Harness-Token": srv._TOKEN}
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/usage",
                headers=headers,
                method="GET",
            )
            usage = json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
            assert usage["session"].get("memory_layers") == {}
        finally:
            httpd.shutdown()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
