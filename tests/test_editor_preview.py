"""Editor preview modes: /api/file/raw MIME, binary calm payload, workspace tree."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import urllib.error
import urllib.request

from http.server import ThreadingHTTPServer


def _start_server(repo_path: str):
    os.environ["HARNESS_DRIVER"] = "stub-oracle-v2"
    os.environ["HARNESS_BUDGET"] = "2"
    os.environ["HARNESS_REPO"] = repo_path

    import importlib
    import harness.server as srv

    importlib.reload(srv)
    srv._cfg.repo = repo_path

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port, srv


def _get(base: str, path: str, token: str | None):
    headers = {}
    if token:
        headers["X-Harness-Token"] = token
    req = urllib.request.Request(base + path, headers=headers, method="GET")
    return urllib.request.urlopen(req, timeout=10)


def test_editor_preview_raw_html_and_binary_and_tree():
    temp_dir = tempfile.mkdtemp()
    try:
        html_path = os.path.join(temp_dir, "preview.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write("<html><body>hi</body></html>")
        bin_path = os.path.join(temp_dir, "blob.bin")
        with open(bin_path, "wb") as f:
            f.write(b"abc\x00def")
        nested = os.path.join(temp_dir, "sub", "note.txt")
        os.makedirs(os.path.dirname(nested), exist_ok=True)
        with open(nested, "w", encoding="utf-8") as f:
            f.write("nested")

        httpd, port, srv = _start_server(temp_dir)
        try:
            base = f"http://127.0.0.1:{port}"
            token = srv._TOKEN

            # /api/file/raw forces text/html for .html preview iframes
            res = _get(base, "/api/file/raw?path=preview.html", token)
            assert res.status == 200
            ctype = res.headers.get("Content-Type", "")
            assert ctype.startswith("text/html")
            assert b"<html>" in res.read()

            # Binary workspace files return calm metadata (not raw bytes) on read
            payload = json.load(_get(base, "/api/file/read?path=blob.bin", token))
            assert payload.get("binary") is True
            assert payload.get("ok") is False
            assert payload.get("path") == "blob.bin"

            # File tree is workspace-relative, sorted, forward-slash paths
            tree = json.load(_get(base, "/api/workspace/files", token))
            assert "blob.bin" in tree["files"]
            assert "preview.html" in tree["files"]
            assert "sub/note.txt" in tree["files"]
            assert tree["files"] == sorted(tree["files"])

            # Raw preview refuses path escape
            try:
                _get(base, "/api/file/raw?path=../outside.html", token)
                assert False, "raw escape should be rejected"
            except urllib.error.HTTPError as e:
                assert e.code in (400, 403)
        finally:
            httpd.shutdown()
            httpd.server_close()
    finally:
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)
