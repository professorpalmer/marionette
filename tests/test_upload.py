"""Upload endpoint + run-with-images param: integration against a live server
instance on an ephemeral port. Uses the stub driver and a fake vision sidecar so
no keys are needed."""
import json
import os
import sys
import urllib.request
from contextlib import contextmanager

import pytest

from http_server_support import (
    TestThreadingHTTPServer as _TestThreadingHTTPServer,
    harness_http_server,
    wait_server_ready as _wait_server_ready,
)

_SERVE_THREAD_NAME = "test-upload-httpd"
_HTTP_TIMEOUT = 15.0 if sys.platform == "win32" else 10.0


@contextmanager
def _upload_http_server():
    """Configure stub driver, reload harness.server, start ephemeral HTTP server."""
    os.environ["HARNESS_DRIVER"] = "stub-oracle-v2"
    os.environ["HARNESS_BUDGET"] = "2"
    import importlib
    import harness.server as srv
    importlib.reload(srv)

    with harness_http_server(srv, thread_name=_SERVE_THREAD_NAME) as running:
        yield running


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


_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000154a24f3f0000000049454e44ae426082"
)
_BOUNDARY = "----harnessboundary"


@pytest.mark.parametrize("ctype,max_bytes,status,error_words", [
    # The 16-byte cap refuses the body with 413 before the server reads it.
    pytest.param(f"multipart/form-data; boundary={_BOUNDARY}", "16", 413, None,
                 id="oversized_body"),
    pytest.param("multipart/form-data", None, 400, None,
                 id="multipart_without_boundary"),
    pytest.param(f"multipart/form-dataX; boundary={_BOUNDARY}", None, 400, None,
                 id="multipart_substring_wrong_media_type"),
    # RFC 7578: a quoted boundary must use double quotes.
    pytest.param(f"multipart/form-data; boundary='{_BOUNDARY}'", None, 400,
                 ("quoted", "boundary"), id="single_quoted_boundary"),
    pytest.param(f'multipart/form-data; boundary="{_BOUNDARY}"', None, None, None,
                 id="double_quoted_boundary_accepted"),
])
def test_upload_request_rules(monkeypatch, tmp_path, ctype, max_bytes, status, error_words):
    from harness.api.files import check_upload_request, save_upload

    if max_bytes is None:
        monkeypatch.delenv("HARNESS_UPLOAD_MAX_BYTES", raising=False)
    else:
        monkeypatch.setenv("HARNESS_UPLOAD_MAX_BYTES", max_bytes)
    body, _ctype = _multipart("file", "x.png", _PNG)
    early = check_upload_request(ctype, len(body))
    if status is None:
        assert early is None
        code, res = save_upload(body, ctype, str(tmp_path))
        assert code == 200
        assert res["saved"] and res["saved"][0]["path"].endswith(".png")
        return
    assert early is not None
    assert early[0] == status
    if error_words:
        assert any(word in early[1]["error"].lower() for word in error_words)


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
