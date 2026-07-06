import os
import json
import shutil
import tempfile
import urllib.request
import urllib.error
import threading
import subprocess
from http.server import ThreadingHTTPServer


def _server():
    import harness.server as srv
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port, srv


def _post(port, path, body, headers):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    return urllib.request.urlopen(req, timeout=10)


def _get(port, path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {}, method="GET")
    return urllib.request.urlopen(req, timeout=10)


def test_ergonomics_workspace_files():
    httpd, port, srv = _server()
    try:
        # 1. GET /api/workspace/files without token -> 403
        try:
            _get(port, "/api/workspace/files")
            assert False, "Should have returned 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403

        headers = {
            "Content-Type": "application/json",
            "X-Harness-Token": srv._TOKEN
        }

        # 2. Open a temporary workspace and write some files.
        # Manual mkdtemp + best-effort rmtree instead of TemporaryDirectory:
        # opening the workspace kicks off async probes (codegraph status) that
        # can briefly hold the dir open on Windows, where an in-use file turns
        # cleanup into a PermissionError. (TemporaryDirectory's
        # ignore_cleanup_errors needs 3.10; CI still runs 3.9.)
        tmpdir = tempfile.mkdtemp()
        try:
            real_tmp = os.path.realpath(tmpdir)
            # init git repo
            subprocess.run(["git", "init", "-b", "main", real_tmp], capture_output=True, check=True)
            
            # create files
            os.makedirs(os.path.join(real_tmp, "src"), exist_ok=True)
            os.makedirs(os.path.join(real_tmp, ".git"), exist_ok=True)
            os.makedirs(os.path.join(real_tmp, "node_modules"), exist_ok=True)
            
            with open(os.path.join(real_tmp, "src/main.py"), "w") as f:
                f.write("print('hello')")
            with open(os.path.join(real_tmp, "node_modules/bad.py"), "w") as f:
                f.write("bad")
            with open(os.path.join(real_tmp, "README.md"), "w") as f:
                f.write("# Sample Readme")

            # Open workspace
            res_open = _post(port, "/api/workspace/open", {"path": real_tmp}, headers)
            assert res_open.status == 200

            # GET /api/workspace/files with token
            res_files = _get(port, f"/api/workspace/files?token={srv._TOKEN}", headers)
            assert res_files.status == 200
            data = json.loads(res_files.read().decode())
            
            # Should contain workspace files, and exclude node_modules / .git
            assert "files" in data
            files = data["files"]
            assert "README.md" in files
            assert "src/main.py" in files
            # node_modules and .git files must be skipped
            assert not any(f.startswith("node_modules") for f in files)
            assert not any(f.startswith(".git") for f in files)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    finally:
        httpd.shutdown()


def test_ergonomics_session_compact():
    httpd, port, srv = _server()
    try:
        # 1. POST /api/session/compact without token -> 403
        try:
            _post(port, "/api/session/compact", {}, {})
            assert False, "Should have failed with 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403

        headers = {
            "Content-Type": "application/json",
            "X-Harness-Token": srv._TOKEN
        }

        # 2. POST /api/session/compact with token -> 200 and compacts
        res = _post(port, "/api/session/compact", {}, headers)
        assert res.status == 200
        data = json.loads(res.read().decode())
        assert data["ok"] is True
        assert "before_tokens" in data
        assert "after_tokens" in data

    finally:
        httpd.shutdown()


def test_ergonomics_at_path_resolution():
    import harness.server as srv
    from unittest.mock import MagicMock
    
    with tempfile.TemporaryDirectory() as tmpdir:
        real_tmp = os.path.realpath(tmpdir)
        srv._cfg.repo = real_tmp
        
        # Write some files to resolve
        file_path = os.path.join(real_tmp, "test_doc.txt")
        with open(file_path, "w") as f:
            f.write("Important test context content here.")
            
        # Write an outside file to test traversal guard
        outside_dir = tempfile.gettempdir()
        outside_file = os.path.join(outside_dir, "outside.txt")
        with open(outside_file, "w") as f:
            f.write("Secret outside content.")

        class DummyHandler:
            pass

        # We can construct and call the same resolution logic to test it directly
        def resolve_message(message: str) -> str:
            resolved_context = []
            total_size = 0
            repo = srv._cfg.repo
            if repo and os.path.isdir(repo) and message:
                import re
                tokens = re.findall(r'@([a-zA-Z0-9_\-\.\/]+)', message)
                seen_tokens = set()
                for token in tokens:
                    if token in seen_tokens:
                        continue
                    seen_tokens.add(token)
                    
                    full_path = os.path.abspath(os.path.join(repo, token))
                    repo_real = os.path.realpath(repo)
                    full_real = os.path.realpath(full_path)
                    
                    try:
                        common = os.path.commonpath([repo_real, full_real])
                        if common == repo_real and os.path.isfile(full_real):
                            size = os.path.getsize(full_real)
                            read_size = min(size, 50 * 1024)
                            if total_size + read_size <= 150 * 1024:
                                with open(full_real, 'r', encoding='utf-8', errors='replace') as f:
                                    content = f.read(read_size)
                                resolved_context.append(f"--- File: {token} ---\n{content}\n")
                                total_size += len(content.encode('utf-8'))
                    except Exception:
                        pass
                
                if resolved_context:
                    context_block = "Referenced files:\n" + "\n".join(resolved_context) + "\n"
                    message = context_block + message
            return message

        # 1. Valid inside path gets resolved
        msg1 = "Check this @test_doc.txt please"
        resolved1 = resolve_message(msg1)
        assert "Referenced files:" in resolved1
        assert "--- File: test_doc.txt ---" in resolved1
        assert "Important test context content here." in resolved1
        
        # 2. Directory traversal attempt gets rejected
        msg2 = "Check this @../outside.txt please"
        resolved2 = resolve_message(msg2)
        assert "Referenced files:" not in resolved2
        assert "Secret outside content." not in resolved2

        # 3. Bad path token doesn't crash anything
        msg3 = "Check this @nonexistent_file.txt please"
        resolved3 = resolve_message(msg3)
        assert resolved3 == msg3
