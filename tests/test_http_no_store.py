"""Every backend reply is Cache-Control: no-store, so no chat, image or file
reply can enter the renderer's disk cache."""
from __future__ import annotations

import io
from http.client import HTTPResponse

import pytest

pytestmark = pytest.mark.usefixtures("owned_server")


class _Connection:
    def __init__(self, incoming=b""):
        self.incoming = incoming
        self.output = bytearray()

    def makefile(self, *args, **kwargs):
        return io.BytesIO(self.incoming)

    def sendall(self, data):
        self.output.extend(data)

    def shutdown(self, *args):
        pass

    def close(self):
        pass


def _request(srv, path):
    wire = (f"GET {path} HTTP/1.0\r\nHost: 127.0.0.1\r\n"
            f"X-Harness-Token: {srv._TOKEN}\r\n\r\n").encode()
    conn = _Connection(wire)
    srv.Handler(conn, ("127.0.0.1", 12345), object())
    response = HTTPResponse(_Connection(bytes(conn.output)))
    response.begin()
    return response


@pytest.mark.parametrize("path", ["/api/sessions", "/api/does-not-exist"])
def test_routes_reply_no_store(owned_server, path):
    response = _request(owned_server, path)
    assert response.getheader("Cache-Control") == "no-store"
    assert len(response.msg.get_all("Cache-Control")) == 1


def _bare_handler(srv):
    handler = srv.Handler.__new__(srv.Handler)
    handler.wfile = io.BytesIO()
    handler.request_version = "HTTP/1.1"
    handler.requestline = "GET / HTTP/1.1"
    handler.command = "GET"
    handler.path = "/"
    handler.client_address = ("127.0.0.1", 1)
    handler.close_connection = True
    return handler


def _headers(handler):
    return handler._headers_buffer if hasattr(handler, "_headers_buffer") else []


def test_route_cache_header_becomes_no_store(owned_server):
    handler = _bare_handler(owned_server)
    handler.send_response(200)
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Cache-Control", "max-age=60")
    written = b"".join(_headers(handler)).decode()
    handler.end_headers()
    assert written.count("Cache-Control") == 1
    assert "Cache-Control: no-store" in written


def test_keep_alive_reply_resets_header_state(owned_server):
    handler = _bare_handler(owned_server)
    handler.send_response(200)
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    handler.send_response(200)
    handler.end_headers()
    out = handler.wfile.getvalue().decode()
    assert out.count("Cache-Control: no-store") == 2
