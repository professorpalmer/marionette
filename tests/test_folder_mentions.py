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
    format_folder_mention_skip,
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


def test_format_folder_mention_skip():
    skip = format_folder_mention_skip("folder:pkg", reason="not found in workspace")
    assert "--- Folder: pkg ---" in skip
    assert "... skipped: not found in workspace" in skip
    assert format_folder_mention_skip("src/lib", reason="budget") == (
        "--- Folder: src/lib ---\n... skipped: budget\n"
    )


def test_workspace_files_folder_cap_is_flagged(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from harness.api.files import FileServices, get_workspace_files

    monkeypatch.setenv("HARNESS_WORKSPACE_FOLDERS_CAP", "2")
    (tmp_path / "aaa").mkdir()
    (tmp_path / "bbb").mkdir()
    (tmp_path / "ccc").mkdir()
    (tmp_path / "aaa" / "f.txt").write_text("x", encoding="utf-8")
    svc = FileServices(
        cfg=SimpleNamespace(repo=str(tmp_path)),
        sessions=None,
        upload_dir=str(tmp_path),
    )
    code, data = get_workspace_files(svc)
    assert code == 200
    assert data["folders_truncated"] is True
    assert data["folders_total"] == 3
    assert data["folders_capped"] == 2
    assert data["folders"] == ["aaa", "bbb"]


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


def test_at_folder_resolution_on_send(owned_server):
    import harness.server as srv

    with tempfile.TemporaryDirectory() as tmpdir:
        real_tmp = os.path.realpath(tmpdir)
        pkg = os.path.join(real_tmp, "pkg")
        os.makedirs(pkg)
        open(os.path.join(pkg, "one.py"), "w").write("ONE")
        open(os.path.join(pkg, "two.py"), "w").write("TWO")

        mock_pilot = owned_server._pilot
        mock_pilot.send = MagicMock(side_effect=lambda *_a, **_k: (_ for _ in ()))
        mock_pilot.drain_swarm_results = MagicMock(return_value=[])

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
                assert srv_inst._runners.get(srv_inst._sessions.active) is mock_pilot

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
                receipt, = mock_pilot.input_receipts()
                assert receipt["original_text"] == "Look at @folder:pkg"
                assert mock_pilot.send.call_args.kwargs["input_id"] == receipt["id"]
                assert sent_msg != receipt["original_text"]
                assert "Referenced folders:" in sent_msg
                assert "--- Folder: pkg ---" in sent_msg
                assert "pkg/one.py" in sent_msg
                assert "pkg/two.py" in sent_msg
                assert "Look at @folder:pkg" in sent_msg
                # Bounded listing — file contents are NOT dumped
                assert "ONE" not in sent_msg
            finally:
                httpd.shutdown()


def test_at_folder_resolution_confinement(owned_server):
    import harness.server as srv

    with tempfile.TemporaryDirectory() as tmpdir:
        real_tmp = os.path.realpath(tmpdir)
        mock_pilot = owned_server._pilot
        mock_pilot.send = MagicMock(side_effect=lambda *_a, **_k: (_ for _ in ()))
        mock_pilot.drain_swarm_results = MagicMock(return_value=[])

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
                assert srv_inst._runners.get(srv_inst._sessions.active) is mock_pilot

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
                receipt, = mock_pilot.input_receipts()
                assert receipt["original_text"] == "@folder:../outside"
                assert mock_pilot.send.call_args.kwargs["input_id"] == receipt["id"]
                assert sent_msg != receipt["original_text"]
                assert "Referenced folders:" in sent_msg
                assert "--- Folder: ../outside ---" in sent_msg
                assert "... skipped: not found in workspace" in sent_msg
                assert "@folder:../outside" in sent_msg or "outside" in sent_msg
            finally:
                httpd.shutdown()


def test_at_folder_budget_skip_honesty(monkeypatch, owned_server):
    """Over-budget @folder must emit a skip note, never silent-drop."""
    monkeypatch.setattr(
        "harness.mention_context.MENTION_TOTAL_BUDGET",
        40,
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        real_tmp = os.path.realpath(tmpdir)
        pkg = os.path.join(real_tmp, "pkg")
        os.makedirs(pkg)
        # Listing alone exceeds the tiny budget once the honesty header is included.
        for name in ("a.py", "b.py", "c.py", "d.py", "e.py"):
            open(os.path.join(pkg, name), "w").write("x")

        mock_pilot = owned_server._pilot
        mock_pilot.send = MagicMock(side_effect=lambda *_a, **_k: (_ for _ in ()))
        mock_pilot.drain_swarm_results = MagicMock(return_value=[])

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
                assert srv_inst._runners.get(srv_inst._sessions.active) is mock_pilot

                res = _get(
                    port,
                    "/api/chat?message=@folder:pkg",
                    headers,
                )
                while True:
                    line = res.readline().decode()
                    if not line or '{"kind": "done"}' in line or '{"kind": "error"' in line:
                        break

                sent_msg = mock_pilot.send.call_args[0][0]
                receipt, = mock_pilot.input_receipts()
                assert receipt["original_text"] == "@folder:pkg"
                assert mock_pilot.send.call_args.kwargs["input_id"] == receipt["id"]
                assert sent_msg != receipt["original_text"]
                assert "Referenced folders:" in sent_msg
                assert "... skipped:" in sent_msg
                assert "budget exhausted" in sent_msg
            finally:
                httpd.shutdown()
