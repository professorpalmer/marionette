"""JSON POST body size cap on Handler._read_json (DoS gate -> HTTP 413)."""
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer


def _start_server():
    os.environ["HARNESS_DRIVER"] = "stub-oracle-v2"
    os.environ["HARNESS_BUDGET"] = "2"
    import importlib
    import harness.server as srv
    importlib.reload(srv)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port, srv


def test_json_body_under_limit_ok():
    httpd, port, srv = _start_server()
    try:
        body = json.dumps({}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/settings",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Harness-Token": srv._TOKEN,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.status == 200
            payload = json.load(resp)
            assert isinstance(payload, dict)
    finally:
        httpd.shutdown()


def test_json_body_over_limit_413():
    old = os.environ.get("HARNESS_JSON_BODY_MAX_BYTES")
    os.environ["HARNESS_JSON_BODY_MAX_BYTES"] = "32"
    httpd, port, srv = _start_server()
    try:
        body = json.dumps({"pad": "x" * 64}).encode()
        assert len(body) > 32
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/settings",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Harness-Token": srv._TOKEN,
            },
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            assert False, "expected HTTP 413 for oversized JSON body"
        except urllib.error.HTTPError as e:
            assert e.code == 413
            payload = e.read().decode()
            assert "too large" in payload.lower()
    finally:
        if old is None:
            os.environ.pop("HARNESS_JSON_BODY_MAX_BYTES", None)
        else:
            os.environ["HARNESS_JSON_BODY_MAX_BYTES"] = old
        httpd.shutdown()
