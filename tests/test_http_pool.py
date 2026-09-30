"""Provider keep-alive pool: reuse only provably healthy, fully read connections."""
import json
import socket
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest

from pmharness.drivers import http_pool


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    connections = set()

    def log_message(self, *a):
        pass

    def _remember(self):
        _Handler.connections.add(self.client_address)

    def do_POST(self):
        self._remember()
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        if self.path == "/error":
            out = b'{"error": "nope"}'
            self.send_response(429)
        elif self.path == "/close":
            out = b"bye"
            self.send_response(200)
            self.send_header("Connection", "close")
        else:
            out = json.dumps({"echo": body.decode(), "ua": self.headers.get("User-Agent")}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        self._remember()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for i in range(3):
            line = f"data: {i}\n\n".encode()
            self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
        self.wfile.write(b"0\r\n\r\n")


@pytest.fixture
def server():
    http_pool.reset()
    _Handler.connections = set()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()
    http_pool.reset()


def _post(url, data=b"{}"):
    return urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")


def test_sequential_requests_reuse_one_connection(server):
    for i in range(5):
        with http_pool.urlopen(_post(server + "/x", json.dumps({"i": i}).encode()), timeout=5) as resp:
            assert json.loads(resp.read())["echo"] == json.dumps({"i": i})
            assert resp.status == 200
    assert len(_Handler.connections) == 1
    assert http_pool.stats()["reused"] == 4


def test_streamed_chunked_body_read_to_the_end_is_reused(server):
    for _ in range(2):
        with http_pool.urlopen(urllib.request.Request(server + "/stream"), timeout=5) as resp:
            lines = [line for line in resp if line.strip()]
        assert lines == [b"data: 0\n", b"data: 1\n", b"data: 2\n"]
    assert len(_Handler.connections) == 1


def test_abandoned_stream_is_never_reused(server):
    with http_pool.urlopen(urllib.request.Request(server + "/stream"), timeout=5) as resp:
        resp.readline()
    with http_pool.urlopen(urllib.request.Request(server + "/stream"), timeout=5) as resp:
        resp.read()
    assert len(_Handler.connections) == 2


def test_server_close_and_shut_down_sockets_are_discarded(server):
    with http_pool.urlopen(_post(server + "/close"), timeout=5) as resp:
        assert resp.read() == b"bye"
    assert http_pool.stats()["idle"] == 0
    with http_pool.urlopen(_post(server + "/x"), timeout=5) as resp:
        sock = resp.fp.raw._sock
        resp.read()
    sock.shutdown(socket.SHUT_RD)  # what the drivers' cancel helpers do, late
    with http_pool.urlopen(_post(server + "/x"), timeout=5) as resp:
        resp.read()
    assert len(_Handler.connections) == 3


def test_http_errors_match_urllib_and_keep_the_connection(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        http_pool.urlopen(_post(server + "/error"), timeout=5)
    assert exc.value.code == 429
    assert json.loads(exc.value.read()) == {"error": "nope"}
    with http_pool.urlopen(_post(server + "/x"), timeout=5) as resp:
        assert json.loads(resp.read())["ua"].startswith("Python-urllib/")
    assert len(_Handler.connections) == 1


def test_a_replaced_urlopen_still_intercepts(server):
    with patch("urllib.request.urlopen", side_effect=RuntimeError("patched")):
        with pytest.raises(RuntimeError, match="patched"):
            http_pool.urlopen(_post(server + "/x"), timeout=5)
