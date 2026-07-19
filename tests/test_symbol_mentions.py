import os
import json
import tempfile
import urllib.request
import urllib.error
import threading
from http.server import ThreadingHTTPServer
from unittest.mock import patch, MagicMock


def _server():
    import harness.server as srv
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port, srv


def _get(port, path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {}, method="GET")
    return urllib.request.urlopen(req, timeout=10)


def test_symbols_endpoint_basic():
    httpd, port, srv = _server()
    try:
        try:
            _get(port, "/api/workspace/symbols?q=foo")
            assert False, "Should have returned 403"
        except urllib.error.HTTPError as e:
            assert e.code == 403

        headers = {
            "Content-Type": "application/json",
            "X-Harness-Token": srv._TOKEN
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            real_tmp = os.path.realpath(tmpdir)
            srv._cfg.repo = real_tmp

            with patch("puppetmaster.codegraph.codegraph_available", return_value=True), \
                 patch("puppetmaster.codegraph.codegraph_ready", return_value=True), \
                 patch("puppetmaster.codegraph.codegraph_query") as mock_query:
                
                mock_query.return_value = {
                    "ok": True,
                    "stdout": json.dumps([
                        {
                            "node": {
                                "name": "my_foo_function",
                                "kind": "function",
                                "filePath": "src/main.py",
                                "startLine": 10,
                                "endLine": 15
                            }
                        }
                    ])
                }

                res = _get(port, "/api/workspace/symbols?q=foo", headers)
                assert res.status == 200
                data = json.loads(res.read().decode())
                assert "symbols" in data
                assert len(data["symbols"]) == 1
                sym = data["symbols"][0]
                assert sym["name"] == "my_foo_function"
                assert sym["kind"] == "function"
                assert sym["path"] == "src/main.py"
                assert sym["line"] == 10

                mock_query.assert_called_once_with(search="foo", cwd=real_tmp, limit=20)

    finally:
        httpd.shutdown()


def test_symbols_endpoint_error_handling():
    httpd, port, srv = _server()
    try:
        headers = {
            "Content-Type": "application/json",
            "X-Harness-Token": srv._TOKEN
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            real_tmp = os.path.realpath(tmpdir)
            srv._cfg.repo = real_tmp

            srv._cfg.repo = None
            res_none = _get(port, "/api/workspace/symbols?q=foo", headers)
            assert res_none.status == 200
            data_none = json.loads(res_none.read().decode())
            assert data_none["symbols"] == []

            srv._cfg.repo = real_tmp

            with patch("puppetmaster.codegraph.codegraph_available", return_value=True), \
                 patch("puppetmaster.codegraph.codegraph_ready", return_value=True), \
                 patch("puppetmaster.codegraph.codegraph_query", side_effect=ValueError("Test crash")):
                
                res = _get(port, "/api/workspace/symbols?q=foo", headers)
                assert res.status == 200
                data = json.loads(res.read().decode())
                assert data["symbols"] == []
                assert "error" in data
                assert "Test crash" in data["error"]

    finally:
        httpd.shutdown()


def test_at_symbol_resolution_on_send():
    import harness.server as srv
    from unittest.mock import patch, MagicMock
    
    with tempfile.TemporaryDirectory() as tmpdir:
        real_tmp = os.path.realpath(tmpdir)
        srv._cfg.repo = real_tmp

        file_path = os.path.join(real_tmp, "src/math.py")
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "w") as f:
            f.write("\n" * 9)
            f.write("def myfunc():\n")
            f.write("    return 42\n")

        mock_pilot = MagicMock()
        mock_pilot.send.return_value = []
        mock_pilot.drain_swarm_results.return_value = []
        
        with patch("harness.server._pilot", mock_pilot), \
             patch("harness.server._pilot_preflight", return_value=None), \
             patch("puppetmaster.codegraph.codegraph_available", return_value=True), \
             patch("puppetmaster.codegraph.codegraph_ready", return_value=True), \
             patch("puppetmaster.codegraph.codegraph_query") as mock_query:

            mock_query.return_value = {
                "ok": True,
                "stdout": json.dumps([
                    {
                        "node": {
                            "name": "myfunc",
                            "kind": "function",
                            "filePath": "src/math.py",
                            "startLine": 10,
                            "endLine": 12
                        }
                    }
                ])
            }

            httpd, port, srv_inst = _server()
            try:
                srv_inst._cfg.repo = real_tmp
                headers = {
                    "Content-Type": "application/json",
                    "X-Harness-Token": srv_inst._TOKEN
                }

                # Start the active session so title-derivation doesn't cause issues
                sess = srv_inst._sessions.create()
                srv_inst._sessions._active = sess["id"]

                res = _get(port, "/api/chat?message=Check+out+@symbol:myfunc", headers)
                
                # Consume line by line until we hit 'done'
                while True:
                    line = res.readline().decode()
                    if not line or '{"kind": "done"}' in line or '{"kind": "error"' in line:
                        break

                mock_pilot.send.assert_called_once()
                sent_msg = mock_pilot.send.call_args[0][0]
                
                assert "Referenced symbols:" in sent_msg
                assert "--- Symbol: myfunc (src/math.py:10) ---" in sent_msg
                assert "def myfunc():" in sent_msg
                assert "return 42" in sent_msg
                assert "Check out @symbol:myfunc" in sent_msg

            finally:
                httpd.shutdown()


def test_at_symbol_resolution_confinement():
    import harness.server as srv
    from unittest.mock import patch, MagicMock
    
    with tempfile.TemporaryDirectory() as tmpdir:
        real_tmp = os.path.realpath(tmpdir)
        srv._cfg.repo = real_tmp

        mock_pilot = MagicMock()
        mock_pilot.send.return_value = []
        mock_pilot.drain_swarm_results.return_value = []
        
        with patch("harness.server._pilot", mock_pilot), \
             patch("harness.server._pilot_preflight", return_value=None), \
             patch("puppetmaster.codegraph.codegraph_available", return_value=True), \
             patch("puppetmaster.codegraph.codegraph_ready", return_value=True), \
             patch("puppetmaster.codegraph.codegraph_query") as mock_query:

            mock_query.return_value = {
                "ok": True,
                "stdout": json.dumps([
                    {
                        "node": {
                            "name": "outside_func",
                            "kind": "function",
                            "filePath": "../outside.py",
                            "startLine": 1,
                            "endLine": 5
                        }
                    }
                ])
            }

            httpd, port, srv_inst = _server()
            try:
                srv_inst._cfg.repo = real_tmp
                headers = {
                    "Content-Type": "application/json",
                    "X-Harness-Token": srv_inst._TOKEN
                }

                # Start active session
                sess = srv_inst._sessions.create()
                srv_inst._sessions._active = sess["id"]

                res = _get(port, "/api/chat?message=@symbol:outside_func", headers)
                
                # Consume line by line until we hit 'done'
                while True:
                    line = res.readline().decode()
                    if not line or '{"kind": "done"}' in line or '{"kind": "error"' in line:
                        break

                sent_msg = mock_pilot.send.call_args[0][0]
                assert "Referenced symbols:" not in sent_msg
                assert "@symbol:outside_func" in sent_msg

            finally:
                httpd.shutdown()
