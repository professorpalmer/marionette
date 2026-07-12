from __future__ import annotations

import json
import os
import shutil
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


def _post(base: str, path: str, body: dict, token: str | None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Harness-Token"] = token
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=10)


def test_file_mutate_endpoints_path_containment():
    temp_dir = tempfile.mkdtemp()
    try:
        # Seed files/dirs inside the workspace
        with open(os.path.join(temp_dir, "keep.txt"), "w", encoding="utf-8") as f:
            f.write("keep")
        os.makedirs(os.path.join(temp_dir, "subdir"))
        with open(os.path.join(temp_dir, "subdir", "nested.txt"), "w", encoding="utf-8") as f:
            f.write("nested")

        httpd, port, srv = _start_server(temp_dir)
        try:
            base = f"http://127.0.0.1:{port}"
            token = srv._TOKEN

            # Auth required
            for endpoint, body in (
                ("/api/file/mkdir", {"path": "auth-dir"}),
                ("/api/file/rename", {"path": "keep.txt", "new_name": "x.txt"}),
                ("/api/file/delete", {"path": "keep.txt"}),
            ):
                try:
                    _post(base, endpoint, body, token=None)
                    assert False, f"{endpoint} should require auth"
                except urllib.error.HTTPError as e:
                    assert e.code == 403

            # mkdir creates a directory
            res = json.load(_post(base, "/api/file/mkdir", {"path": "newdir"}, token))
            assert res["ok"] is True
            assert os.path.isdir(os.path.join(temp_dir, "newdir"))

            # mkdir refuse escape
            try:
                _post(base, "/api/file/mkdir", {"path": "../outside-dir"}, token)
                assert False, "mkdir should block escape"
            except urllib.error.HTTPError as e:
                assert e.code in (403, 400)

            # mkdir refuse existing
            try:
                _post(base, "/api/file/mkdir", {"path": "newdir"}, token)
                assert False, "mkdir should conflict on existing"
            except urllib.error.HTTPError as e:
                assert e.code == 409

            # rename via new_name
            res = json.load(
                _post(
                    base,
                    "/api/file/rename",
                    {"path": "keep.txt", "new_name": "kept.txt"},
                    token,
                )
            )
            assert res["ok"] is True
            assert res["from"] == "keep.txt"
            assert res["to"] == "kept.txt"
            assert os.path.isfile(os.path.join(temp_dir, "kept.txt"))
            assert not os.path.exists(os.path.join(temp_dir, "keep.txt"))

            # rename refuse overwrite
            with open(os.path.join(temp_dir, "other.txt"), "w", encoding="utf-8") as f:
                f.write("other")
            try:
                _post(
                    base,
                    "/api/file/rename",
                    {"path": "kept.txt", "new_name": "other.txt"},
                    token,
                )
                assert False, "rename should refuse overwrite"
            except urllib.error.HTTPError as e:
                assert e.code == 409

            # rename via from/to
            res = json.load(
                _post(
                    base,
                    "/api/file/rename",
                    {"from": "subdir/nested.txt", "to": "newdir/moved.txt"},
                    token,
                )
            )
            assert res["ok"] is True
            assert os.path.isfile(os.path.join(temp_dir, "newdir", "moved.txt"))

            # rename refuse escape
            try:
                _post(
                    base,
                    "/api/file/rename",
                    {"from": "kept.txt", "to": "../escaped.txt"},
                    token,
                )
                assert False, "rename should block escape"
            except urllib.error.HTTPError as e:
                assert e.code in (403, 400)

            # delete file
            res = json.load(_post(base, "/api/file/delete", {"path": "other.txt"}, token))
            assert res["ok"] is True
            assert not os.path.exists(os.path.join(temp_dir, "other.txt"))

            # delete non-empty directory
            res = json.load(_post(base, "/api/file/delete", {"path": "newdir"}, token))
            assert res["ok"] is True
            assert not os.path.exists(os.path.join(temp_dir, "newdir"))

            # delete refuse escape
            try:
                _post(base, "/api/file/delete", {"path": "../kept.txt"}, token)
                assert False, "delete should block escape"
            except urllib.error.HTTPError as e:
                assert e.code in (403, 400)

            # delete refuse workspace root
            try:
                _post(base, "/api/file/delete", {"path": "."}, token)
                assert False, "delete should refuse workspace root"
            except urllib.error.HTTPError as e:
                assert e.code in (400, 403)

        finally:
            httpd.shutdown()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
