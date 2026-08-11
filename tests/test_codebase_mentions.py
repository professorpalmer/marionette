"""@codebase mention: first-class token + CodeGraph resolve honesty."""
from __future__ import annotations

import os
import tempfile
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock, patch

from harness.mention_context import (
    codebase_mention_query,
    expand_codebase_mention,
    extract_mention_tokens,
    format_codebase_mention_block,
    format_codebase_mention_failure,
    format_codebase_mention_skip,
    is_codebase_mention,
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


def test_extract_mention_tokens_codebase():
    assert extract_mention_tokens("use @codebase please") == ["codebase"]
    assert extract_mention_tokens("@codebase:AuthService") == ["codebase:AuthService"]
    assert extract_mention_tokens('@codebase:"spaced query"') == ["codebase:spaced query"]
    assert extract_mention_tokens("@codebase @folder:src @symbol:Foo") == [
        "codebase",
        "folder:src",
        "symbol:Foo",
    ]
    # Must not treat bare file token "codebase" as a path when typed as @codebase
    assert is_codebase_mention("codebase")
    assert is_codebase_mention("codebase:Auth")
    assert not is_codebase_mention("src/codebase.ts")
    assert codebase_mention_query("codebase") == ""
    assert codebase_mention_query("codebase:Auth") == "Auth"


def test_format_codebase_mention_honesty_helpers():
    block = format_codebase_mention_block("codebase", "CG BODY")
    assert "--- Codebase: @codebase ---" in block
    assert "CG BODY" in block
    assert "skipped" in format_codebase_mention_skip(
        "codebase:Auth", reason="CodeGraph unavailable",
    )
    assert "@codebase:Auth" in format_codebase_mention_skip(
        "codebase:Auth", reason="x",
    )
    assert "failed to resolve" in format_codebase_mention_failure(
        "codebase", error="boom",
    )


def test_expand_codebase_mention_skip_when_unavailable():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = os.path.realpath(tmpdir)
        with patch("puppetmaster.codegraph.codegraph_available", return_value=False):
            block = expand_codebase_mention(repo, "codebase")
        assert "--- Codebase: @codebase ---" in block
        assert "skipped" in block
        assert "CodeGraph unavailable" in block


def test_expand_codebase_mention_skip_when_not_ready():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = os.path.realpath(tmpdir)
        with patch("puppetmaster.codegraph.codegraph_available", return_value=True), patch(
            "puppetmaster.codegraph.codegraph_ready", return_value=False
        ):
            block = expand_codebase_mention(repo, "codebase:Auth")
        assert "@codebase:Auth" in block
        assert "skipped" in block
        assert "not ready" in block


def test_expand_codebase_mention_skip_when_empty():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = os.path.realpath(tmpdir)
        with patch("puppetmaster.codegraph.codegraph_available", return_value=True), patch(
            "puppetmaster.codegraph.codegraph_ready", return_value=True
        ), patch(
            "puppetmaster.codegraph.codegraph_context", return_value=None
        ):
            block = expand_codebase_mention(repo, "codebase", task_fallback="find Auth")
        assert "skipped" in block
        assert "no context" in block


def test_expand_codebase_mention_success():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = os.path.realpath(tmpdir)
        with patch("puppetmaster.codegraph.codegraph_available", return_value=True), patch(
            "puppetmaster.codegraph.codegraph_ready", return_value=True
        ), patch(
            "puppetmaster.codegraph.codegraph_context",
            return_value="- **Foo** in bar.py",
        ) as mock_ctx, patch(
            "puppetmaster.codegraph.codegraph_prompt_section",
            return_value="Shared CodeGraph context for this task:\n```\n- **Foo**\n```",
        ):
            block = expand_codebase_mention(repo, "codebase:Foo")
        mock_ctx.assert_called_once_with(task="Foo", cwd=repo)
        assert "--- Codebase: @codebase:Foo ---" in block
        assert "Shared CodeGraph context" in block
        assert "skipped" not in block
        assert "failed" not in block


def test_expand_codebase_mention_failure_honesty():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = os.path.realpath(tmpdir)
        with patch("puppetmaster.codegraph.codegraph_available", return_value=True), patch(
            "puppetmaster.codegraph.codegraph_ready", return_value=True
        ), patch(
            "puppetmaster.codegraph.codegraph_context",
            side_effect=RuntimeError("cg boom"),
        ):
            block = expand_codebase_mention(repo, "codebase")
        assert "failed to resolve" in block
        assert "cg boom" in block


def test_at_codebase_resolution_on_send_success():
    with tempfile.TemporaryDirectory() as tmpdir:
        real_tmp = os.path.realpath(tmpdir)
        mock_pilot = MagicMock()
        mock_pilot.send.return_value = []
        mock_pilot.drain_swarm_results.return_value = []

        with patch("harness.server._pilot", mock_pilot), patch(
            "harness.server._pilot_preflight", return_value=None
        ), patch(
            "puppetmaster.codegraph.codegraph_available", return_value=True
        ), patch(
            "puppetmaster.codegraph.codegraph_ready", return_value=True
        ), patch(
            "puppetmaster.codegraph.codegraph_context",
            return_value="- **Auth** in auth.py",
        ), patch(
            "puppetmaster.codegraph.codegraph_prompt_section",
            return_value="Shared CodeGraph context:\n```\n- **Auth**\n```",
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

                msg = "Explain @codebase:Auth"
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
                assert "Referenced codebase:" in sent_msg
                assert "--- Codebase: @codebase:Auth ---" in sent_msg
                assert "Shared CodeGraph context" in sent_msg
                assert "Explain @codebase:Auth" in sent_msg
                # Must not fall through to symbol search for "codebase:Auth"
                assert "Referenced symbols:" not in sent_msg
            finally:
                httpd.shutdown()


def test_at_codebase_resolution_on_send_skip_unavailable():
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
                    "/api/chat?message=" + urllib.parse.quote("use @codebase"),
                    headers,
                )
                while True:
                    line = res.readline().decode()
                    if not line or '{"kind": "done"}' in line or '{"kind": "error"' in line:
                        break

                sent_msg = mock_pilot.send.call_args[0][0]
                assert "Referenced codebase:" in sent_msg
                assert "--- Codebase: @codebase ---" in sent_msg
                assert "skipped" in sent_msg
                assert "CodeGraph unavailable" in sent_msg
                # Never silent bare-token fallthrough into symbol search
                assert "Referenced symbols:" not in sent_msg
            finally:
                httpd.shutdown()
