""" /api/file/read must truncate by UTF-8 bytes, not text-mode characters. """
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer


def _start_server(repo_path):
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
    return srv, httpd, port


def test_file_read_utf8_truncation_is_byte_accurate():
    """A file over 1 MiB of multibyte UTF-8 must return <= 1 MiB encoded content."""
    temp_dir = tempfile.mkdtemp()
    try:
        # 400_000 CJK ideographs = 1_200_000 UTF-8 bytes (> 1 MiB).
        # Text-mode f.read(1_048_576) would return 1_048_576 *characters*
        # (= 3_145_728 bytes) and still claim truncated — the bug.
        big = "中" * 400_000
        path = os.path.join(temp_dir, "utf8_big.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(big)
        assert os.path.getsize(path) > 1024 * 1024

        srv, httpd, port = _start_server(temp_dir)
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/file/read?path="
                + urllib.parse.quote("utf8_big.txt"),
                headers={"X-Harness-Token": srv._TOKEN},
            )
            res = json.load(urllib.request.urlopen(req, timeout=30))
            assert res["ok"] is True
            assert res["truncated"] is True
            encoded = res["content"].encode("utf-8")
            assert len(encoded) <= 1024 * 1024
            # Must have actually truncated (not returned the whole file).
            assert len(encoded) < os.path.getsize(path)
            # Content should be a prefix of the original (no replacement garbage).
            assert big.startswith(res["content"])
        finally:
            httpd.shutdown()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
