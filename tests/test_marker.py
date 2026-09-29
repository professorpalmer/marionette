import os
import json
import time
import threading
import pytest
from harness.server import serve


def test_marker_and_reuse_logic(tmp_path, monkeypatch, capsys):
    # Isolate all app state (marker/token/workspace) under tmp_path via the
    # state-home env the server now honors -- backend.json lives directly in it.
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))

    # 1. Start a background server on an ephemeral port (0)
    t1 = threading.Thread(target=serve, kwargs={"host": "127.0.0.1", "port": 0}, daemon=True)
    t1.start()

    # Wait for the marker to be created and populated
    marker_path = os.path.join(str(tmp_path), "backend.json")
    for _ in range(50):
        if os.path.exists(marker_path):
            try:
                with open(marker_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("port"):
                    break
            except Exception:
                pass
        time.sleep(0.1)
    else:
        pytest.fail("Marker file was not written by background serve()")

    with open(marker_path, "r", encoding="utf-8") as f:
        m1 = json.load(f)

    port1 = m1["port"]
    assert port1 > 0
    assert m1["pid"] == os.getpid()

    # Clear captured stdout from startup
    capsys.readouterr()

    # 2. (a) A second serve() with a healthy marker present does NOT start a second server
    # It should return cleanly.
    serve(host="127.0.0.1", port=port1, force=False)

    # Assert that the reuse message was printed and it returned cleanly
    captured = capsys.readouterr()
    assert "pm-harness already running" in captured.out
    assert f"http://127.0.0.1:{port1} — reusing" in captured.out

    # 3. (b) --force bypasses it and attempts to bind (causing SystemExit(2) since port is in use)
    with pytest.raises(SystemExit) as exc_info:
        serve(host="127.0.0.1", port=port1, force=True)
    assert exc_info.value.code == 2

    # 4. (c) A stale marker (unreachable port) is replaced and the server starts
    # We manually write a stale/unreachable marker
    stale_port = 59999
    with open(marker_path, "w", encoding="utf-8") as f:
        json.dump({
            "port": stale_port,
            "pid": 99999,
            "at": int(time.time() * 1000)
        }, f)

    # Start another background server with port=0
    t2 = threading.Thread(target=serve, kwargs={"host": "127.0.0.1", "port": 0, "force": False}, daemon=True)
    t2.start()

    # Wait for the marker to be updated (it should change from 59999 to the new ephemeral port)
    for _ in range(50):
        if os.path.exists(marker_path):
            try:
                with open(marker_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("port") and data["port"] != stale_port:
                    m2 = data
                    break
            except Exception:
                pass
        time.sleep(0.1)
    else:
        pytest.fail("Stale marker was not replaced")

    assert m2["port"] != stale_port
    assert m2["port"] > 0
    assert m2["pid"] == os.getpid()


def test_reuse_probe_treats_a_slow_live_backend_as_running(tmp_path, monkeypatch, capsys):
    # A busy backend that accepts the connection but answers slowly must be
    # reused, not treated as a stale marker (binding its port would fail).
    import socket
    import urllib.request

    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    with open(os.path.join(str(tmp_path), "backend.json"), "w", encoding="utf-8") as f:
        json.dump({"port": 59999, "pid": os.getpid()}, f)
    probed = []

    def slow(url, timeout=None):
        probed.append(url)
        raise socket.timeout("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", slow)
    serve(host="127.0.0.1", port=59999, force=False)
    assert probed and probed[0].endswith("/api/endpoint")
    assert "already running" in capsys.readouterr().out
