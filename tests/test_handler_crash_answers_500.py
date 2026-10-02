"""An unhandled route exception answers 500 instead of dropping the socket.

Before: the exception escaped to socketserver, which closed the connection
with no response. The desktop app treats a reset socket as a possible backend
change, so a single handler bug could knock the whole UI into Reconnect.
"""
from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import harness.server as srv


def test_route_exception_answers_json_500(monkeypatch):
    def boom(handler, u, q):
        raise AttributeError("placeholder has no lock")

    routes = dict(srv._get_routes())
    routes["/api/boom"] = boom
    monkeypatch.setattr(srv, "_get_routes", lambda: routes)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
        conn.request("GET", "/api/boom", headers={"X-Harness-Token": srv._TOKEN})
        resp = conn.getresponse()
        assert resp.status == 500
        body = json.loads(resp.read())
        assert body["code"] == "handler_failed"
        assert "placeholder has no lock" not in body["error"]
    finally:
        httpd.shutdown()
        httpd.server_close()
