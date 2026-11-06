"""/api/file/* endpoints: read, write, raw, mutate, editor preview."""
from __future__ import annotations

import os
import json
import threading
import urllib.request
import urllib.error
import urllib.parse
import tempfile
import shutil
from http.server import ThreadingHTTPServer
import subprocess


def _start_server(repo_path, monkeypatch):
    monkeypatch.setenv("HARNESS_DRIVER", "stub-oracle-v2")
    monkeypatch.setenv("HARNESS_BUDGET", "2")
    monkeypatch.setenv("HARNESS_REPO", repo_path)

    import importlib
    import harness.server as srv
    importlib.reload(srv)

    monkeypatch.setattr(srv._cfg, "repo", repo_path)

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


def _get(base: str, path: str, token: str | None):
    headers = {}
    if token:
        headers["X-Harness-Token"] = token
    req = urllib.request.Request(base + path, headers=headers, method="GET")
    return urllib.request.urlopen(req, timeout=10)


def test_file_edit_endpoints(monkeypatch):
    temp_dir = tempfile.mkdtemp()
    
    # Initialize ephemeral git repo for checkpoints to work
    subprocess.run(["git", "init", "-b", "main"], cwd=temp_dir, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=temp_dir, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=temp_dir, capture_output=True)
    
    # Create an initial file and commit it so snapshot has a baseline
    init_file = os.path.join(temp_dir, "init.txt")
    with open(init_file, "w") as f:
        f.write("Initial content")
    subprocess.run(["git", "add", "init.txt"], cwd=temp_dir, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=temp_dir, capture_output=True)
    
    httpd, port, srv = _start_server(temp_dir, monkeypatch)
    try:
        base = f"http://127.0.0.1:{port}"
        token = srv._TOKEN
        
        # Test 1: Endpoint 403 without token (GET)
        try:
            urllib.request.urlopen(base + "/api/file/read?path=init.txt", timeout=10)
            assert False, "Should have raised 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403
            
        # Test 2: Endpoint 403 without token (POST)
        try:
            req = urllib.request.Request(
                base + "/api/file/write",
                data=json.dumps({"path": "init.txt", "content": "hack"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            urllib.request.urlopen(req, timeout=10)
            assert False, "Should have raised 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403
            
        # Test 3: Read returns confined file content
        req = urllib.request.Request(
            base + f"/api/file/read?path=init.txt",
            headers={"X-Harness-Token": token}
        )
        res = json.load(urllib.request.urlopen(req, timeout=10))
        assert res["ok"] is True
        assert res["path"] == "init.txt"
        assert res["content"] == "Initial content"
        assert res["truncated"] is False
        
        # Test 4: Traverse escapes (realpath check) should be rejected
        req = urllib.request.Request(
            base + f"/api/file/read?path=../something.txt",
            headers={"X-Harness-Token": token}
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            assert False, "Should have blocked traversal"
        except urllib.error.HTTPError as e:
            assert e.code in (403, 400)
            
        # Test 5: Rejects writes with traversal or inside .git
        req = urllib.request.Request(
            base + "/api/file/write",
            data=json.dumps({"path": "../traversal.txt", "content": "hack"}).encode(),
            headers={"Content-Type": "application/json", "X-Harness-Token": token},
            method="POST"
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            assert False, "Should have blocked traversal"
        except urllib.error.HTTPError as e:
            assert e.code in (403, 400)
            
        req = urllib.request.Request(
            base + "/api/file/write",
            data=json.dumps({"path": ".git/config", "content": "hack"}).encode(),
            headers={"Content-Type": "application/json", "X-Harness-Token": token},
            method="POST"
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            assert False, "Should have blocked .git write"
        except urllib.error.HTTPError as e:
            assert e.code in (403, 400)
            
        # Test 6: Write writes file atomically + takes a checkpoint first
        req = urllib.request.Request(
            base + "/api/file/write",
            data=json.dumps({"path": "new_file.txt", "content": "New edited content"}).encode(),
            headers={"Content-Type": "application/json", "X-Harness-Token": token},
            method="POST"
        )
        write_res = json.load(urllib.request.urlopen(req, timeout=10))
        assert write_res["ok"] is True
        assert write_res["bytes"] == len("New edited content")
        
        # Check file was written
        new_file_abs = os.path.join(temp_dir, "new_file.txt")
        assert os.path.exists(new_file_abs)
        with open(new_file_abs, "r") as f:
            assert f.read() == "New edited content"
            
        # Check checkpoint was created!
        from harness.checkpoints import CheckpointStore
        store = CheckpointStore(temp_dir)
        checkpoints = store.list()
        assert len(checkpoints) > 0
        assert checkpoints[0]["trigger"] == "manual_edit"
        assert "before manual edit new_file.txt" in checkpoints[0]["label"]

        # Test 7: Absolute path under repo is accepted for read
        abs_init = os.path.join(temp_dir, "init.txt")
        req = urllib.request.Request(
            base + "/api/file/read?path=" + urllib.parse.quote(abs_init),
            headers={"X-Harness-Token": token},
        )
        abs_res = json.load(urllib.request.urlopen(req, timeout=10))
        assert abs_res["ok"] is True
        assert abs_res["content"] == "Initial content"

        # Test 8: Absolute path outside repo is denied
        outside = os.path.realpath(os.path.join(temp_dir, "..", "outside-escape.txt"))
        req = urllib.request.Request(
            base + "/api/file/read?path=" + urllib.parse.quote(outside),
            headers={"X-Harness-Token": token},
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            assert False, "Should have blocked absolute escape"
        except urllib.error.HTTPError as e:
            assert e.code in (403, 400)

        # Test 9: Binary read returns metadata (not a blank 404)
        bin_path = os.path.join(temp_dir, "blob.bin")
        with open(bin_path, "wb") as f:
            f.write(b"hello\x00world")
        req = urllib.request.Request(
            base + "/api/file/read?path=blob.bin",
            headers={"X-Harness-Token": token},
        )
        bin_res = json.load(urllib.request.urlopen(req, timeout=10))
        assert bin_res["ok"] is False
        assert bin_res["binary"] is True
        assert bin_res["size"] == 11
        assert bin_res["name"] == "blob.bin"

        # Test 10: /api/file/raw streams bytes with content-type
        req = urllib.request.Request(
            base + "/api/file/raw?path=blob.bin",
            headers={"X-Harness-Token": token},
        )
        raw_res = urllib.request.urlopen(req, timeout=10)
        assert raw_res.status == 200
        assert raw_res.read() == b"hello\x00world"

        # Test 11: /api/file/raw denies escape
        req = urllib.request.Request(
            base + "/api/file/raw?path=" + urllib.parse.quote(outside),
            headers={"X-Harness-Token": token},
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            assert False, "raw should block escape"
        except urllib.error.HTTPError as e:
            assert e.code in (403, 400)

    finally:
        httpd.shutdown()
        shutil.rmtree(temp_dir)


def test_file_mutate_endpoints_path_containment(monkeypatch):
    temp_dir = tempfile.mkdtemp()
    try:
        # Seed files/dirs inside the workspace
        with open(os.path.join(temp_dir, "keep.txt"), "w", encoding="utf-8") as f:
            f.write("keep")
        os.makedirs(os.path.join(temp_dir, "subdir"))
        with open(os.path.join(temp_dir, "subdir", "nested.txt"), "w", encoding="utf-8") as f:
            f.write("nested")

        httpd, port, srv = _start_server(temp_dir, monkeypatch)
        try:
            base = f"http://127.0.0.1:{port}"
            token = srv._TOKEN

            # Auth required
            for endpoint, body in (
                ("/api/file/mkdir", {"path": "auth-dir"}),
                ("/api/file/rename", {"path": "keep.txt", "new_name": "x.txt"}),
                ("/api/file/delete", {"path": "keep.txt"}),
                ("/api/file/reveal", {"path": "keep.txt"}),
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

            # reveal is path-contained and returns ok for an existing file
            from unittest import mock
            with mock.patch("harness.file_reveal.reveal_in_file_manager", return_value=None) as reveal_mock:
                res = json.load(_post(base, "/api/file/reveal", {"path": "kept.txt"}, token))
                assert res["ok"] is True
                assert reveal_mock.called

            # reveal auth required (also covered in the auth loop above)
            # reveal refuse escape
            try:
                _post(base, "/api/file/reveal", {"path": "../outside.txt"}, token)
                assert False, "reveal should block escape"
            except urllib.error.HTTPError as e:
                assert e.code in (400, 403)

        finally:
            httpd.shutdown()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_file_read_utf8_truncation_is_byte_accurate(monkeypatch):
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

        httpd, port, srv = _start_server(temp_dir, monkeypatch)
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


def test_editor_preview_raw_html_and_binary_and_tree(monkeypatch):
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

        httpd, port, srv = _start_server(temp_dir, monkeypatch)
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
        shutil.rmtree(temp_dir, ignore_errors=True)
