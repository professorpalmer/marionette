"""@file mention: quoted spaced paths, confinement, truncation/IO honesty."""
from __future__ import annotations

import os
import tempfile
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock, patch

from harness.mention_context import (
    extract_mention_tokens,
    format_file_mention_failure,
    format_file_mention_skip,
    read_file_mention,
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


def test_extract_mention_tokens_quoted_and_bare():
    assert extract_mention_tokens('see @src/a.ts and @"my file.ts"') == [
        "src/a.ts",
        "my file.ts",
    ]
    assert extract_mention_tokens('@folder:"my docs" @folder:src') == [
        "folder:my docs",
        "folder:src",
    ]
    assert extract_mention_tokens("@symbol:Foo @symbol:\"Bar Baz\"") == [
        "symbol:Foo",
        "symbol:Bar Baz",
    ]
    # Unquoted tokens still stop at whitespace (legacy).
    assert extract_mention_tokens("@src/a.ts more") == ["src/a.ts"]


def test_read_file_mention_truncation_honesty():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "big.txt")
        payload = ("x" * (50 * 1024 + 100)).encode("utf-8")
        with open(path, "wb") as fh:
            fh.write(payload)
        block, added = read_file_mention(path, "big.txt", total_size=0)
        assert "--- File: big.txt ---" in block
        assert "truncated" in block
        assert "50KB per-file cap" in block
        assert added == 50 * 1024
        assert "x" * 50 in block


def test_read_file_mention_budget_skip_honesty():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "late.txt")
        with open(path, "w") as fh:
            fh.write("never attached")
        block, added = read_file_mention(
            path, "late.txt", total_size=150 * 1024,
        )
        assert added == 0
        assert "skipped" in block
        assert "budget exhausted" in block
        assert "never attached" not in block


def test_read_file_mention_io_failure_honesty():
    missing = os.path.join(tempfile.gettempdir(), "no-such-mention-file-xyz.txt")
    if os.path.exists(missing):
        os.unlink(missing)
    block, added = read_file_mention(missing, "missing.txt", total_size=0)
    assert added == 0
    assert "failed to read" in block
    assert format_file_mention_failure("t", error="boom") == (
        "--- File: t ---\n... failed to read: boom\n"
    )
    assert "budget exhausted" in format_file_mention_skip(
        "t", reason="mention context budget exhausted (150KB total across @-mentions)"
    )


def test_at_file_quoted_spaced_path_on_send():
    with tempfile.TemporaryDirectory() as tmpdir:
        real_tmp = os.path.realpath(tmpdir)
        spaced = os.path.join(real_tmp, "cool file.txt")
        with open(spaced, "w") as fh:
            fh.write("SPACED CONTENTS")

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

                msg = 'Look at @"cool file.txt"'
                res = _get(
                    port,
                    "/api/chat?message=" + urllib.parse.quote(msg),
                    headers,
                )
                while True:
                    line = res.readline().decode()
                    if not line or '{"kind": "done"}' in line or '{"kind": "error"' in line:
                        break

                mock_pilot.send.assert_called_once()
                sent_msg = mock_pilot.send.call_args[0][0]
                assert "Referenced files:" in sent_msg
                assert "--- File: cool file.txt ---" in sent_msg
                assert "SPACED CONTENTS" in sent_msg
                assert 'Look at @"cool file.txt"' in sent_msg
            finally:
                httpd.shutdown()


def test_at_file_confinement_rejects_outside():
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
                    "/api/chat?message=" + urllib.parse.quote('@"../outside"'),
                    headers,
                )
                while True:
                    line = res.readline().decode()
                    if not line or '{"kind": "done"}' in line or '{"kind": "error"' in line:
                        break

                sent_msg = mock_pilot.send.call_args[0][0]
                assert "Referenced files:" not in sent_msg
            finally:
                httpd.shutdown()


def test_at_file_truncation_honesty_on_send():
    with tempfile.TemporaryDirectory() as tmpdir:
        real_tmp = os.path.realpath(tmpdir)
        big = os.path.join(real_tmp, "huge.txt")
        with open(big, "w") as fh:
            fh.write("Z" * (50 * 1024 + 40))

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

                res = _get(port, "/api/chat?message=@huge.txt", headers)
                while True:
                    line = res.readline().decode()
                    if not line or '{"kind": "done"}' in line or '{"kind": "error"' in line:
                        break

                sent_msg = mock_pilot.send.call_args[0][0]
                assert "Referenced files:" in sent_msg
                assert "truncated" in sent_msg
                assert "50KB per-file cap" in sent_msg
            finally:
                httpd.shutdown()
