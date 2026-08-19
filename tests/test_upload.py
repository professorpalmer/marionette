"""Upload endpoint + run-with-images param: integration against a live server
instance on an ephemeral port. Uses the stub driver and a fake vision sidecar so
no keys are needed."""
import json
import os
import sys
import threading
import time
import urllib.request
import urllib.error
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from typing import Optional

_JOIN_TIMEOUT = 5.0
_READY_TIMEOUT = 5.0
_SERVE_THREAD_NAME = "test-upload-httpd"
_HTTP_TIMEOUT = 15.0 if sys.platform == "win32" else 10.0


class _TestThreadingHTTPServer(ThreadingHTTPServer):
    """Request-handler threads must not outlive teardown (Windows CI flake)."""

    daemon_threads = True


def _wait_server_ready(port: int, token: str) -> None:
    """Block until the accept loop answers a side-effect-light GET."""
    deadline = time.monotonic() + _READY_TIMEOUT
    last_err: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/config",
                headers={"X-Harness-Token": token},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:
            last_err = exc
            time.sleep(0.05)
    raise RuntimeError(
        f"upload test server not ready on 127.0.0.1:{port}: {last_err!r}"
    )


@contextmanager
def _upload_http_server():
    """Configure stub driver, reload harness.server, start ephemeral HTTP server."""
    os.environ["HARNESS_DRIVER"] = "stub-oracle-v2"
    os.environ["HARNESS_BUDGET"] = "2"
    import importlib
    import harness.server as srv
    importlib.reload(srv)

    httpd = _TestThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(
        target=httpd.serve_forever,
        name=_SERVE_THREAD_NAME,
        daemon=True,
    )
    thread.start()
    try:
        _wait_server_ready(port, srv._TOKEN)
        yield srv, port
    finally:
        try:
            httpd.shutdown()
        finally:
            try:
                httpd.server_close()
            except OSError:
                pass
            thread.join(timeout=_JOIN_TIMEOUT)


def _multipart(field, filename, data, ctype="image/png"):
    boundary = "----harnessboundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def test_upload_then_config_roundtrip():
    with _upload_http_server() as (srv, port):
        base = f"http://127.0.0.1:{port}"
        _cfg_req = urllib.request.Request(
            base + "/api/config",
            headers={"X-Harness-Token": srv._TOKEN},
            method="GET",
        )
        cfg = json.load(urllib.request.urlopen(_cfg_req, timeout=_HTTP_TIMEOUT))
        assert cfg["driver"] == "stub-oracle-v2"
        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000a49444154789c6360000002000154a24f3f0000000049454e44ae426082"
        )
        body, ctype = _multipart("file", "x.png", png)
        req = urllib.request.Request(
            base + "/api/upload",
            data=body,
            headers={"Content-Type": ctype, "X-Harness-Token": srv._TOKEN},
            method="POST",
        )
        res = json.load(urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT))
        assert res["saved"] and res["saved"][0]["path"].endswith(".png")
        assert os.path.exists(res["saved"][0]["path"])


def test_upload_rejects_oversized_body():
    """A body whose Content-Length exceeds the cap is refused with 413 BEFORE
    it is parsed into memory -- the memory-exhaustion guard."""
    with _upload_http_server() as (srv, port):
        old_cap = os.environ.get("HARNESS_UPLOAD_MAX_BYTES")
        os.environ["HARNESS_UPLOAD_MAX_BYTES"] = "16"  # tiny cap for the test
        try:
            base = f"http://127.0.0.1:{port}"
            png = bytes.fromhex(
                "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
                "890000000a49444154789c6360000002000154a24f3f0000000049454e44ae426082"
            )
            body, ctype = _multipart("file", "x.png", png)  # well over 16 bytes
            req = urllib.request.Request(
                base + "/api/upload",
                data=body,
                headers={"Content-Type": ctype, "X-Harness-Token": srv._TOKEN},
                method="POST",
            )
            try:
                urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT)
                assert False, "expected HTTP 413 for oversized upload"
            except urllib.error.HTTPError as e:
                assert e.code == 413
        finally:
            if old_cap is None:
                os.environ.pop("HARNESS_UPLOAD_MAX_BYTES", None)
            else:
                os.environ["HARNESS_UPLOAD_MAX_BYTES"] = old_cap


def test_upload_rejects_multipart_without_boundary():
    with _upload_http_server() as (srv, port):
        base = f"http://127.0.0.1:{port}"
        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000a49444154789c6360000002000154a24f3f0000000049454e44ae426082"
        )
        body, _ctype = _multipart("file", "x.png", png)

        bad_ctype = "multipart/form-data"  # missing required boundary
        req = urllib.request.Request(
            base + "/api/upload",
            data=body,
            headers={"Content-Type": bad_ctype, "X-Harness-Token": srv._TOKEN},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT)
            assert False, "expected HTTP 400 for missing boundary"
        except urllib.error.HTTPError as e:
            assert e.code == 400


def test_upload_rejects_content_type_with_multipart_substring_but_wrong_media_type():
    with _upload_http_server() as (srv, port):
        base = f"http://127.0.0.1:{port}"
        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000a49444154789c6360000002000154a24f3f0000000049454e44ae426082"
        )
        body, _ctype = _multipart("file", "x.png", png)

        # Has "multipart/form-data" as a substring but is not the exact media
        # type required by our strict parser.
        bad_ctype = "multipart/form-dataX; boundary=----harnessboundary"
        req = urllib.request.Request(
            base + "/api/upload",
            data=body,
            headers={"Content-Type": bad_ctype, "X-Harness-Token": srv._TOKEN},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT)
            assert False, "expected HTTP 400 for wrong media type"
        except urllib.error.HTTPError as e:
            assert e.code == 400


def test_upload_accepts_quoted_boundary_variant():
    with _upload_http_server() as (srv, port):
        base = f"http://127.0.0.1:{port}"
        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000a49444154789c6360000002000154a24f3f0000000049454e44ae426082"
        )
        body, _ctype = _multipart("file", "x.png", png)

        boundary = "----harnessboundary"
        good_ctype = f'multipart/form-data; boundary="{boundary}"'
        req = urllib.request.Request(
            base + "/api/upload",
            data=body,
            headers={"Content-Type": good_ctype, "X-Harness-Token": srv._TOKEN},
            method="POST",
        )
        res = json.load(urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT))
        assert res["saved"] and res["saved"][0]["path"].endswith(".png")


def test_upload_rejects_single_quoted_boundary():
    """RFC 7578: boundary parameter must use double quotes, not single quotes."""
    with _upload_http_server() as (srv, port):
        base = f"http://127.0.0.1:{port}"
        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000a49444154789c6360000002000154a24f3f0000000049454e44ae426082"
        )
        body, _ctype = _multipart("file", "x.png", png)

        # Single-quoted boundary should be rejected (non-RFC)
        boundary = "----harnessboundary"
        bad_ctype = f"multipart/form-data; boundary='{boundary}'"
        req = urllib.request.Request(
            base + "/api/upload",
            data=body,
            headers={"Content-Type": bad_ctype, "X-Harness-Token": srv._TOKEN},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT)
            assert False, "expected HTTP 400 for single-quoted boundary"
        except urllib.error.HTTPError as e:
            assert e.code == 400
            data = json.loads(e.read().decode())
            assert "quoted" in data["error"].lower() or "boundary" in data["error"].lower()


def test_save_upload_accepts_markdown(tmp_path):
    from harness.api.files import save_upload

    body, ctype = _multipart("file", "paper.md", b"# title\n", "text/markdown")
    status, payload = save_upload(body, ctype, str(tmp_path))
    assert status == 200
    assert payload["saved"]
    assert payload["saved"][0]["path"].endswith(".md")
    assert os.path.exists(payload["saved"][0]["path"])


def test_save_upload_accepts_zip(tmp_path):
    from harness.api.files import save_upload

    body, ctype = _multipart(
        "file", "bundle.zip", b"PK\x03\x04payload", "application/zip"
    )
    status, payload = save_upload(body, ctype, str(tmp_path))
    assert status == 200
    assert payload["saved"]
    assert payload["saved"][0]["path"].endswith(".zip")
    assert os.path.exists(payload["saved"][0]["path"])


def test_save_upload_rejects_exe_and_extensionless(tmp_path):
    from harness.api.files import save_upload

    body, ctype = _multipart("file", "payload.exe", b"MZ", "application/octet-stream")
    status, payload = save_upload(body, ctype, str(tmp_path))
    assert status == 400
    assert payload.get("saved") == []
    assert "cannot be attached" in (payload.get("error") or "")

    body, ctype = _multipart(
        "file", "authority-spoof", b"not an image", "application/octet-stream"
    )
    status, payload = save_upload(body, ctype, str(tmp_path))
    assert status == 400
    assert not any(
        str(item.get("path") or "").endswith(".png")
        for item in payload.get("saved") or []
    )
