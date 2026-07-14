""" /api/workspace/files must keep the alphabetical head, not walk-order bias. """
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer


def _server():
    import harness.server as srv

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port, srv


def _get(port, path, headers=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", headers=headers or {}, method="GET"
    )
    return urllib.request.urlopen(req, timeout=30)


def test_workspace_files_cap_is_alphabetical_head(monkeypatch):
    """With a tiny cap, kept files must be the alphabetical head of the full set."""
    monkeypatch.setenv("HARNESS_WORKSPACE_FILES_CAP", "5")
    httpd, port, srv = _server()
    tmpdir = tempfile.mkdtemp()
    try:
        real_tmp = os.path.realpath(tmpdir)
        # Create zzz/ first so os.walk visits it before aaa/ — walk-biased
        # capping would keep zzz/* and drop early alphabet entries.
        os.makedirs(os.path.join(real_tmp, "zzz"), exist_ok=True)
        os.makedirs(os.path.join(real_tmp, "aaa"), exist_ok=True)
        os.makedirs(os.path.join(real_tmp, "mmm"), exist_ok=True)
        for name in (
            "zzz/late1.txt",
            "zzz/late2.txt",
            "zzz/late3.txt",
            "aaa/early1.txt",
            "aaa/early2.txt",
            "mmm/mid.txt",
            "root_z.txt",
            "root_a.txt",
        ):
            with open(os.path.join(real_tmp, name.replace("/", os.sep)), "w") as fh:
                fh.write("x")

        srv._cfg.repo = real_tmp
        headers = {"X-Harness-Token": srv._TOKEN}
        res = _get(port, f"/api/workspace/files?token={srv._TOKEN}", headers)
        data = json.loads(res.read().decode())

        assert data["total"] == 8
        assert data["truncated"] is True
        assert data["capped"] == 5
        files = data["files"]
        assert len(files) == 5
        assert files == sorted(files)
        # Alphabetical head — not the walk-first zzz/* sample.
        assert files == [
            "aaa/early1.txt",
            "aaa/early2.txt",
            "mmm/mid.txt",
            "root_a.txt",
            "root_z.txt",
        ]
        assert "zzz/late1.txt" not in files
    finally:
        httpd.shutdown()
        shutil.rmtree(tmpdir, ignore_errors=True)
        monkeypatch.delenv("HARNESS_WORKSPACE_FILES_CAP", raising=False)
