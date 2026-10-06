"""Tests for the chat/autopilot message stash -- the fix for a real data-loss
bug: a large paste rides in the SSE GET's URL query string (EventSource is
GET-only), which can exceed the HTTP request-line limit and get silently
dropped. POST /api/chat/stash lets the client hand the payload over out of
band and reference it from the GET stream via a short ?mid= id instead.
"""
import json
import pytest
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock, patch


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


def test_stash_requires_token():
    httpd, port, srv = _server()
    try:
        try:
            _post(port, "/api/chat/stash", {"message": "hello"}, {"Content-Type": "application/json"})
            assert False, "should have been rejected"
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        httpd.shutdown()


def test_stash_round_trip_basic():
    httpd, port, srv = _server()
    try:
        headers = {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN}
        res = _post(port, "/api/chat/stash", {"message": "hello world"}, headers)
        assert res.status == 200
        data = json.loads(res.read().decode())
        mid = data["id"]
        assert mid

        # Stored, and pop returns it exactly once.
        stashed = srv._stash_pop(mid)
        assert stashed is not None
        assert stashed["message"] == "hello world"

        # Second pop of the same id: gone (consumed).
        assert srv._stash_pop(mid) is None
    finally:
        httpd.shutdown()


def test_stash_caps_retained_entries():
    from harness.api import sessions

    httpd, port, srv = _server()
    try:
        sessions._CHAT_STASH.clear()
        ids = [srv._stash_put(f"msg-{i}") for i in range(sessions._CHAT_STASH_MAX + 10)]
        assert len(sessions._CHAT_STASH) <= sessions._CHAT_STASH_MAX
        # The oldest ids should have been evicted; the newest should remain.
        assert ids[-1] in sessions._CHAT_STASH
        assert ids[0] not in sessions._CHAT_STASH
    finally:
        httpd.shutdown()


def test_get_chat_mid_resolves_stashed_message(owned_server):
    """A message far too large to ever fit in a URL still reaches the pilot
    when stashed and referenced via ?mid=, proving the data-loss bug is fixed."""
    import harness.server as srv

    mock_pilot = owned_server._pilot
    mock_pilot.send = MagicMock(side_effect=lambda *_a, **_k: (_ for _ in ()))
    mock_pilot.drain_swarm_results = MagicMock(return_value=[])

    with patch("harness.server._pilot", mock_pilot), \
         patch("harness.server._pilot_preflight", return_value=None):

        httpd, port, srv_inst = _server()
        try:
            headers = {"Content-Type": "application/json", "X-Harness-Token": srv_inst._TOKEN}

            assert srv_inst._runners.get(srv_inst._sessions.active) is mock_pilot

            # A transcript far larger than a URL could ever carry (well past
            # the stdlib http.server request-line limit).
            huge_message = "A" * 200_000

            res = _post(port, "/api/chat/stash", {"message": huge_message}, headers)
            assert res.status == 200
            mid = json.loads(res.read().decode())["id"]

            # The mid is tiny -- this URL is nowhere near any length limit,
            # unlike embedding huge_message directly would be.
            url = f"/api/chat?mid={mid}"
            assert len(url) < 200

            res = _get(port, url, headers)
            while True:
                line = res.readline().decode()
                if not line or '{"kind": "done"}' in line or '{"kind": "error"' in line:
                    break

            mock_pilot.send.assert_called_once()
            sent_msg = mock_pilot.send.call_args[0][0]
            assert sent_msg == huge_message
            receipt, = mock_pilot.input_receipts()
            assert receipt["original_text"] == huge_message
            assert mock_pilot.send.call_args.kwargs["input_id"] == receipt["id"]

            # The stash entry was consumed (popped), not left to leak forever.
            from harness.api import sessions
            assert mid not in sessions._CHAT_STASH
        finally:
            httpd.shutdown()


def test_get_chat_unknown_mid_refuses_without_consuming_input(owned_server):
    """Expired staging refuses admission and leaves existing input intact."""
    import harness.server as srv

    mock_pilot = owned_server._pilot
    mock_pilot.send = MagicMock(side_effect=lambda *_a, **_k: (_ for _ in ()))
    mock_pilot.drain_swarm_results = MagicMock(return_value=[])

    with patch("harness.server._pilot", mock_pilot), \
         patch("harness.server._pilot_preflight", return_value=None):

        httpd, port, srv_inst = _server()
        try:
            headers = {"Content-Type": "application/json", "X-Harness-Token": srv_inst._TOKEN}

            assert srv_inst._runners.get(srv_inst._sessions.active) is mock_pilot

            queued = mock_pilot.enqueue_prompt("  keep this draft\n")
            before = mock_pilot.input_receipts()
            with pytest.raises(urllib.error.HTTPError) as error:
                _get(port, "/api/chat?mid=doesnotexist&message=do+not+send", headers)
            assert error.value.code == 409
            payload = json.loads(error.value.read())
            assert payload["code"] == "input_stash_expired"
            assert payload["ok"] is False
            mock_pilot.send.assert_not_called()
            assert mock_pilot.input_receipts() == before
            assert mock_pilot.list_prompts()[0]["id"] == queued["id"]

        finally:
            httpd.shutdown()


def test_stash_missing_message_and_images_rejected():
    httpd, port, srv = _server()
    try:
        headers = {"Content-Type": "application/json", "X-Harness-Token": srv._TOKEN}
        try:
            _post(port, "/api/chat/stash", {}, headers)
            assert False, "should have been rejected"
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        httpd.shutdown()
