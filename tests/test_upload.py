"""Upload endpoint + run-with-images param: integration against a live server
instance on an ephemeral port. Uses the stub driver and a fake vision sidecar so
no keys are needed."""
import io
import json
import os
import threading
import time
import urllib.request
import tempfile
from http.server import ThreadingHTTPServer


def _start_server(monkeypatch_env):
    # configure env BEFORE importing the server module so its import-time config sticks
    os.environ["HARNESS_DRIVER"] = "stub-oracle-v2"
    os.environ["HARNESS_BUDGET"] = "2"
    import importlib
    import harness.server as srv
    importlib.reload(srv)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port


def _multipart(field, filename, data, ctype="image/png"):
    boundary = "----harnessboundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def test_upload_then_config_roundtrip():
    httpd, port = _start_server(None)
    try:
        base = f"http://127.0.0.1:{port}"
        # config reachable
        cfg = json.load(urllib.request.urlopen(base + "/api/config", timeout=10))
        assert cfg["driver"] == "stub-oracle-v2"
        # upload a tiny PNG (1x1)
        png = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
                            "890000000a49444154789c6360000002000154a24f3f0000000049454e44ae426082")
        body, ctype = _multipart("file", "x.png", png)
        req = urllib.request.Request(base + "/api/upload", data=body,
                                     headers={"Content-Type": ctype}, method="POST")
        res = json.load(urllib.request.urlopen(req, timeout=10))
        assert res["saved"] and res["saved"][0]["path"].endswith(".png")
        assert os.path.exists(res["saved"][0]["path"])
    finally:
        httpd.shutdown()
