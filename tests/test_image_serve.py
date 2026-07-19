"""GET /api/image: serves an uploaded image back to the browser so SENT message
thumbnails have a durable src (the composer's blob: preview URL is revoked
right after send and never survives a reload). Integration against a live
server instance on an ephemeral port, mirroring tests/test_upload.py."""
import json
import os
import threading
import urllib.request
import urllib.error
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
    return httpd, port


_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000154a24f3f0000000049454e44ae426082"
)


def test_image_serve_existing_file_under_upload_dir():
    httpd, port = _start_server()
    try:
        import harness.server as srv
        os.makedirs(srv._UPLOAD_DIR, exist_ok=True)
        path = os.path.join(srv._UPLOAD_DIR, "test_serve.png")
        with open(path, "wb") as f:
            f.write(_PNG)
        base = f"http://127.0.0.1:{port}"
        req = urllib.request.Request(
            base + "/api/image?path=" + path,
            headers={"X-Harness-Token": srv._TOKEN},
            method="GET",
        )
        resp = urllib.request.urlopen(req, timeout=10)
        assert resp.status == 200
        assert resp.headers.get("Content-Type") == "image/png"
        assert resp.read() == _PNG
    finally:
        httpd.shutdown()


def test_image_serve_rejects_path_outside_upload_dir():
    """Path traversal / arbitrary-file-read guard: a real file that exists but
    lives OUTSIDE _UPLOAD_DIR must be rejected with 403, never streamed."""
    httpd, port = _start_server()
    try:
        import harness.server as srv
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        outside_path = os.path.join(repo_root, "pyproject.toml")
        if not os.path.exists(outside_path):
            outside_path = "/etc/hosts"
        assert os.path.exists(outside_path)
        base = f"http://127.0.0.1:{port}"
        req = urllib.request.Request(
            base + "/api/image?path=" + outside_path,
            headers={"X-Harness-Token": srv._TOKEN},
            method="GET",
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            assert False, "expected HTTP 403 for a path outside the upload dir"
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        httpd.shutdown()


def test_image_serve_missing_file_under_upload_dir_is_404():
    httpd, port = _start_server()
    try:
        import harness.server as srv
        os.makedirs(srv._UPLOAD_DIR, exist_ok=True)
        missing_path = os.path.join(srv._UPLOAD_DIR, "does-not-exist-xyz.png")
        assert not os.path.exists(missing_path)
        base = f"http://127.0.0.1:{port}"
        req = urllib.request.Request(
            base + "/api/image?path=" + missing_path,
            headers={"X-Harness-Token": srv._TOKEN},
            method="GET",
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            assert False, "expected HTTP 404 for a missing file"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        httpd.shutdown()


def test_image_serve_rejects_non_image_extension():
    """Even under _UPLOAD_DIR, a non-image extension (e.g. a smuggled .py or
    .txt file) must be rejected -- this endpoint only ever serves images."""
    httpd, port = _start_server()
    try:
        import harness.server as srv
        os.makedirs(srv._UPLOAD_DIR, exist_ok=True)
        path = os.path.join(srv._UPLOAD_DIR, "not_an_image.txt")
        with open(path, "w") as f:
            f.write("hello")
        base = f"http://127.0.0.1:{port}"
        req = urllib.request.Request(
            base + "/api/image?path=" + path,
            headers={"X-Harness-Token": srv._TOKEN},
            method="GET",
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            assert False, "expected HTTP 403 for a non-image extension"
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        httpd.shutdown()


def test_image_serve_requires_token():
    httpd, port = _start_server()
    try:
        import harness.server as srv
        os.makedirs(srv._UPLOAD_DIR, exist_ok=True)
        path = os.path.join(srv._UPLOAD_DIR, "test_serve_auth.png")
        with open(path, "wb") as f:
            f.write(_PNG)
        base = f"http://127.0.0.1:{port}"
        req = urllib.request.Request(base + "/api/image?path=" + path, method="GET")
        try:
            urllib.request.urlopen(req, timeout=10)
            assert False, "expected HTTP 403 without a token"
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        httpd.shutdown()


def test_image_serve_requires_header_auth_not_query_string():
    """Verify images authenticate via X-Harness-Token header, not query string.
    
    Query-string tokens are no longer accepted (removed for security hardening).
    Only header-based auth works.
    """
    httpd, port = _start_server()
    try:
        import harness.server as srv
        os.makedirs(srv._UPLOAD_DIR, exist_ok=True)
        path = os.path.join(srv._UPLOAD_DIR, "test_serve_header_auth.png")
        with open(path, "wb") as f:
            f.write(_PNG)
        base = f"http://127.0.0.1:{port}"
        
        # 1. Query-string token should be rejected (even with token value)
        req = urllib.request.Request(
            base + f"/api/image?path={path}&token={srv._TOKEN}",
            method="GET"
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            assert False, "expected HTTP 403 with query-string token (not supported)"
        except urllib.error.HTTPError as e:
            assert e.code == 403
        
        # 2. Header-based token should work
        req = urllib.request.Request(
            base + "/api/image?path=" + path,
            headers={"X-Harness-Token": srv._TOKEN},
            method="GET",
        )
        resp = urllib.request.urlopen(req, timeout=10)
        assert resp.status == 200
        assert resp.read() == _PNG
    finally:
        httpd.shutdown()
