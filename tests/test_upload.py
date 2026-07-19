"""Upload endpoint + run-with-images param: integration against a live server
instance on an ephemeral port. Uses the stub driver and a fake vision sidecar so
no keys are needed."""
import io
import json
import os
import threading
import time
import urllib.request
import urllib.error
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
        import harness.server as _srv
        # config reachable (GET now requires the auth token)
        _cfg_req = urllib.request.Request(base + "/api/config",
                                          headers={"X-Harness-Token": _srv._TOKEN}, method="GET")
        cfg = json.load(urllib.request.urlopen(_cfg_req, timeout=10))
        assert cfg["driver"] == "stub-oracle-v2"
        # upload a tiny PNG (1x1)
        png = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
                            "890000000a49444154789c6360000002000154a24f3f0000000049454e44ae426082")
        body, ctype = _multipart("file", "x.png", png)
        import harness.server as _srv
        req = urllib.request.Request(base + "/api/upload", data=body,
                                     headers={"Content-Type": ctype,
                                              "X-Harness-Token": _srv._TOKEN},
                                     method="POST")
        res = json.load(urllib.request.urlopen(req, timeout=10))
        assert res["saved"] and res["saved"][0]["path"].endswith(".png")
        assert os.path.exists(res["saved"][0]["path"])
    finally:
        httpd.shutdown()


def test_upload_rejects_oversized_body():
    """A body whose Content-Length exceeds the cap is refused with 413 BEFORE
    it is parsed into memory -- the memory-exhaustion guard."""
    httpd, port = _start_server(None)
    old_cap = os.environ.get("HARNESS_UPLOAD_MAX_BYTES")
    os.environ["HARNESS_UPLOAD_MAX_BYTES"] = "16"  # tiny cap for the test
    try:
        base = f"http://127.0.0.1:{port}"
        png = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
                            "890000000a49444154789c6360000002000154a24f3f0000000049454e44ae426082")
        body, ctype = _multipart("file", "x.png", png)  # well over 16 bytes
        import harness.server as _srv
        req = urllib.request.Request(base + "/api/upload", data=body,
                                     headers={"Content-Type": ctype,
                                              "X-Harness-Token": _srv._TOKEN},
                                     method="POST")
        try:
            urllib.request.urlopen(req, timeout=10)
            assert False, "expected HTTP 413 for oversized upload"
        except urllib.error.HTTPError as e:
            assert e.code == 413
    finally:
        if old_cap is None:
            os.environ.pop("HARNESS_UPLOAD_MAX_BYTES", None)
        else:
            os.environ["HARNESS_UPLOAD_MAX_BYTES"] = old_cap
        httpd.shutdown()


def test_upload_rejects_multipart_without_boundary():
    httpd, port = _start_server(None)
    try:
        base = f"http://127.0.0.1:{port}"
        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000a49444154789c6360000002000154a24f3f0000000049454e44ae426082"
        )
        body, _ctype = _multipart("file", "x.png", png)

        import harness.server as _srv
        bad_ctype = "multipart/form-data"  # missing required boundary
        req = urllib.request.Request(
            base + "/api/upload",
            data=body,
            headers={"Content-Type": bad_ctype, "X-Harness-Token": _srv._TOKEN},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            assert False, "expected HTTP 400 for missing boundary"
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        httpd.shutdown()


def test_upload_rejects_content_type_with_multipart_substring_but_wrong_media_type():
    httpd, port = _start_server(None)
    try:
        base = f"http://127.0.0.1:{port}"
        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000a49444154789c6360000002000154a24f3f0000000049454e44ae426082"
        )
        body, _ctype = _multipart("file", "x.png", png)

        import harness.server as _srv
        # Has "multipart/form-data" as a substring but is not the exact media
        # type required by our strict parser.
        bad_ctype = "multipart/form-dataX; boundary=----harnessboundary"
        req = urllib.request.Request(
            base + "/api/upload",
            data=body,
            headers={"Content-Type": bad_ctype, "X-Harness-Token": _srv._TOKEN},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            assert False, "expected HTTP 400 for wrong media type"
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        httpd.shutdown()


def test_upload_accepts_quoted_boundary_variant():
    httpd, port = _start_server(None)
    try:
        base = f"http://127.0.0.1:{port}"
        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000a49444154789c6360000002000154a24f3f0000000049454e44ae426082"
        )
        body, _ctype = _multipart("file", "x.png", png)

        import harness.server as _srv
        boundary = "----harnessboundary"
        good_ctype = f'multipart/form-data; boundary="{boundary}"'
        req = urllib.request.Request(
            base + "/api/upload",
            data=body,
            headers={"Content-Type": good_ctype, "X-Harness-Token": _srv._TOKEN},
            method="POST",
        )
        res = json.load(urllib.request.urlopen(req, timeout=10))
        assert res["saved"] and res["saved"][0]["path"].endswith(".png")
    finally:
        httpd.shutdown()


def test_upload_rejects_single_quoted_boundary():
    """RFC 7578: boundary parameter must use double quotes, not single quotes."""
    httpd, port = _start_server(None)
    try:
        base = f"http://127.0.0.1:{port}"
        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000a49444154789c6360000002000154a24f3f0000000049454e44ae426082"
        )
        body, _ctype = _multipart("file", "x.png", png)

        import harness.server as _srv
        # Single-quoted boundary should be rejected (non-RFC)
        boundary = "----harnessboundary"
        bad_ctype = f"multipart/form-data; boundary='{boundary}'"
        req = urllib.request.Request(
            base + "/api/upload",
            data=body,
            headers={"Content-Type": bad_ctype, "X-Harness-Token": _srv._TOKEN},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            assert False, "expected HTTP 400 for single-quoted boundary"
        except urllib.error.HTTPError as e:
            assert e.code == 400
            data = json.loads(e.read().decode())
            assert "quoted" in data["error"].lower() or "boundary" in data["error"].lower()
    finally:
        httpd.shutdown()
