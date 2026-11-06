"""TEMPORARY update-skew compat: legacy Electron streaming GET query-token auth.

Pre-v0.9.95 installed Electron main processes send the SSE auth token as
``?token=`` on the streaming GET routes (/api/chat, /api/auto, /api/run). After
an in-app update flipped the backend to header-only auth, those shells 403'd on
every turn ("[aborted] Connection closed before the turn finished"). The
backend now accepts query-token auth ONLY for those exact routes, only on a GET
from a confirmed loopback peer, with the token compared constant-time and never
echoed. Everything else keeps rejecting query tokens.

These tests lock the narrow scope in -- and must be DELETED together with the
shim once pre-v0.9.95 app shells are out of support (see the removal criterion
next to ``legacy_stream_query_token_ok`` in harness/server.py).
"""
import json
import threading
import urllib.error
import urllib.request

import pytest


@pytest.fixture(scope="module")
def served():
    import harness.server as srv
    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield srv, port
    finally:
        httpd.shutdown()
        httpd.server_close()


def _request(port, path, method="GET"):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    return urllib.request.urlopen(req, timeout=10)


def _stub_streams(monkeypatch, srv):
    """Replace the heavyweight SSE stream bodies with a 200 marker response.

    Auth is what's under test: reaching the stub proves the request passed the
    centralized do_GET gate; a 403 proves it did not.
    """
    def _stub(self, *args, **kwargs):
        return self._send(200, json.dumps({"ok": True, "via": "stream-stub"}))

    monkeypatch.setattr(srv.Handler, "_stream_chat", _stub, raising=True)
    monkeypatch.setattr(srv.Handler, "_stream_run", _stub, raising=True)
    monkeypatch.setattr(srv.Handler, "_stream_auto", _stub, raising=True)


# ---- pure gate function -----------------------------------------------------

def _gate(srv, **overrides):
    params = dict(
        method="GET",
        path="/api/chat",
        query="token=tok-secret",
        peer_address="127.0.0.1",
        expected_token="tok-secret",
    )
    params.update(overrides)
    return srv.legacy_stream_query_token_ok(**params)


def test_gate_accepts_loopback_get_with_correct_token_on_legacy_routes(capsys):
    import harness.server as srv
    for path in ("/api/chat", "/api/auto", "/api/run"):
        assert _gate(srv, path=path) is True
    assert _gate(srv, peer_address="::1") is True
    err = capsys.readouterr().err
    assert "[auth deprecation] legacy stream query-token" in err
    assert "/api/chat" in err or "/api/auto" in err or "/api/run" in err
    assert "tok-secret" not in err
    assert "prefer X-Harness-Token" in err


def test_gate_rejects_wrong_or_missing_token():
    import harness.server as srv
    assert _gate(srv, query="token=wrong") is False
    assert _gate(srv, query="") is False
    assert _gate(srv, query="message=hi") is False
    assert _gate(srv, expected_token="") is False


def test_gate_rejects_non_loopback_peer_even_with_correct_token():
    import harness.server as srv
    assert _gate(srv, peer_address="10.0.0.5") is False
    assert _gate(srv, peer_address="192.168.1.20") is False
    assert _gate(srv, peer_address="") is False


def test_gate_rejects_other_paths_and_verbs():
    import harness.server as srv
    assert _gate(srv, path="/api/memory") is False
    assert _gate(srv, path="/api/chat/events") is False
    assert _gate(srv, path="/api/config") is False
    assert _gate(srv, method="POST") is False
    assert _gate(srv, method="DELETE") is False


# ---- end-to-end through the Handler gate ------------------------------------

def test_legacy_stream_get_with_query_token_passes_auth_gate(monkeypatch, served):
    srv, port = served
    _stub_streams(monkeypatch, srv)
    for path in ("/api/chat", "/api/auto", "/api/run"):
        resp = _request(port, f"{path}?token={srv._TOKEN}")
        assert resp.status == 200, f"{path} must accept legacy loopback query auth"
        assert json.loads(resp.read())["via"] == "stream-stub"


def test_legacy_stream_get_with_wrong_query_token_403s_and_redacts(monkeypatch, served):
    srv, port = served
    _stub_streams(monkeypatch, srv)
    supplied = "attacker-guess-token"
    with pytest.raises(urllib.error.HTTPError) as ei:
        _request(port, f"/api/chat?token={supplied}")
    assert ei.value.code == 403
    body = ei.value.read().decode("utf-8", errors="ignore")
    assert supplied not in body, "supplied token must never be echoed"
    assert srv._TOKEN not in body, "real token must never be emitted"


def test_query_token_still_403s_on_non_stream_get_apis(served):
    srv, port = served
    for path in ("/api/memory", "/api/config", "/api/chat/events"):
        with pytest.raises(urllib.error.HTTPError) as ei:
            _request(port, f"{path}?token={srv._TOKEN}")
        assert ei.value.code == 403, f"{path} must keep rejecting query tokens"
        body = ei.value.read().decode("utf-8", errors="ignore")
        assert srv._TOKEN not in body


def test_query_token_still_403s_on_post_verbs(served):
    srv, port = served
    with pytest.raises(urllib.error.HTTPError) as ei:
        _request(port, f"/api/chat?token={srv._TOKEN}", method="POST")
    assert ei.value.code == 403, "query auth must never unlock POST"
    body = ei.value.read().decode("utf-8", errors="ignore")
    assert srv._TOKEN not in body


def test_header_auth_on_stream_routes_still_works(monkeypatch, served):
    srv, port = served
    _stub_streams(monkeypatch, srv)
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/chat",
        headers={"X-Harness-Token": srv._TOKEN},
        method="GET",
    )
    resp = urllib.request.urlopen(req, timeout=10)
    assert resp.status == 200
