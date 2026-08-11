"""@folder mention: bounded listing, workspace confinement, truncation honesty."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock, patch

from harness.mention_context import (
    expand_folder_mention,
    folder_entry_cap,
    resolve_repo_dir,
)


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
    return urllib.request.urlopen(req, timeout=10)


def test_resolve_repo_dir_fail_closed():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = os.path.realpath(tmpdir)
        os.makedirs(os.path.join(repo, "src", "lib"))
        assert resolve_repo_dir(repo, "src/lib") == os.path.realpath(
            os.path.join(repo, "src", "lib")
        )
        assert resolve_repo_dir(repo, "folder:src/lib") == os.path.realpath(
            os.path.join(repo, "src", "lib")
        )
        assert resolve_repo_dir(repo, "../outside") is None
        assert resolve_repo_dir(repo, "folder:../outside") is None
        assert resolve_repo_dir(repo, "missing") is None
        # File path is not a directory
        open(os.path.join(repo, "readme.txt"), "w").write("x")
        assert resolve_repo_dir(repo, "readme.txt") is None


def test_expand_folder_mention_truncation_honesty(monkeypatch):
    monkeypatch.setenv("HARNESS_FOLDER_MENTION_CAP", "3")
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = os.path.realpath(tmpdir)
        folder = os.path.join(repo, "pkg")
        os.makedirs(folder)
        for name in ("a.py", "b.py", "c.py", "d.py", "e.py"):
            open(os.path.join(folder, name), "w").write("x")

        block = expand_folder_mention(repo, "folder:pkg", entry_cap=3)
        assert block is not None
        assert "--- Folder: pkg ---" in block
        assert "pkg/a.py" in block
        assert "pkg/b.py" in block
        assert "pkg/c.py" in block
        assert "pkg/d.py" not in block
        assert "truncated" in block
        assert "showing 3 of 5" in block
        assert folder_entry_cap({"HARNESS_FOLDER_MENTION_CAP": "3"}) == 3


def test_expand_folder_mention_empty_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = os.path.realpath(tmpdir)
        os.makedirs(os.path.join(repo, "empty"))
        block = expand_folder_mention(repo, "folder:empty")
        assert block is not None
        assert "(empty directory)" in block


def test_expand_folder_mention_outside_returns_none():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = os.path.realpath(tmpdir)
        assert expand_folder_mention(repo, "folder:../etc") is None
        assert expand_folder_mention(repo, "folder:/etc") is None


def test_workspace_files_includes_folders():
    httpd, port, srv = _server()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            real_tmp = os.path.realpath(tmpdir)
            os.makedirs(os.path.join(real_tmp, "src", "nested"))
            open(os.path.join(real_tmp, "src", "a.py"), "w").write("x")
            open(os.path.join(real_tmp, "src", "nested", "b.py"), "w").write("x")
            srv._cfg.repo = real_tmp
            headers = {"X-Harness-Token": srv._TOKEN}
            res = _get(port, "/api/workspace/files", headers)
            data = json.loads(res.read().decode())
            assert "folders" in data
            assert "src" in data["folders"]
            assert "src/nested" in data["folders"]
            assert "src/a.py" in data["files"]
    finally:
        httpd.shutdown()


def test_at_folder_resolution_on_send():
    import harness.server as srv

    with tempfile.TemporaryDirectory() as tmpdir:
        real_tmp = os.path.realpath(tmpdir)
        pkg = os.path.join(real_tmp, "pkg")
        os.makedirs(pkg)
        open(os.path.join(pkg, "one.py"), "w").write("ONE")
        open(os.path.join(pkg, "two.py"), "w").write("TWO")

        mock_pilot = MagicMock()
        mock_pilot.send.return_value = []
        mock_pilot.drain_swarm_results.return_value = []

        with patch("harness.server._pilot", mock_pilot), patch(
            "harness.server._pilot_preflight", return_value=None
        ):
            httpd, port, srv_inst = _server()
            try:
                srv_inst._cfg.repo = real_tmp
                headers = {
                    "Content-Type": "application/json",
                    "X-Harness-Token": srv_inst._TOKEN,
                }
                sess = srv_inst._sessions.create()
                srv_inst._sessions._active = sess["id"]

                res = _get(
                    port,
                    "/api/chat?message=Look+at+@folder:pkg",
                    headers,
                )
                while True:
                    line = res.readline().decode()
                    if not line or '{"kind": "done"}' in line or '{"kind": "error"' in line:
                        break

                mock_pilot.send.assert_called_once()
                sent_msg = mock_pilot.send.call_args[0][0]
                assert "Referenced folders:" in sent_msg
                assert "--- Folder: pkg ---" in sent_msg
                assert "pkg/one.py" in sent_msg
                assert "pkg/two.py" in sent_msg
                assert "Look at @folder:pkg" in sent_msg
                # Bounded listing — file contents are NOT dumped
                assert "ONE" not in sent_msg
            finally:
                httpd.shutdown()


def test_at_folder_resolution_confinement():
    import harness.server as srv

    with tempfile.TemporaryDirectory() as tmpdir:
        real_tmp = os.path.realpath(tmpdir)
        mock_pilot = MagicMock()
        mock_pilot.send.return_value = []
        mock_pilot.drain_swarm_results.return_value = []

        with patch("harness.server._pilot", mock_pilot), patch(
            "harness.server._pilot_preflight", return_value=None
        ), patch(
            "puppetmaster.codegraph.codegraph_available", return_value=False
        ):
            httpd, port, srv_inst = _server()
            try:
                srv_inst._cfg.repo = real_tmp
                headers = {
                    "Content-Type": "application/json",
                    "X-Harness-Token": srv_inst._TOKEN,
                }
                sess = srv_inst._sessions.create()
                srv_inst._sessions._active = sess["id"]

                res = _get(
                    port,
                    "/api/chat?message=@folder:../outside",
                    headers,
                )
                while True:
                    line = res.readline().decode()
                    if not line or '{"kind": "done"}' in line or '{"kind": "error"' in line:
                        break

                sent_msg = mock_pilot.send.call_args[0][0]
                assert "Referenced folders:" not in sent_msg
                assert "@folder:../outside" in sent_msg or "outside" in sent_msg
            finally:
                httpd.shutdown()
