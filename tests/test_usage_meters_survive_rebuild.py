"""Boot cost meters must survive pilot rebuild on workspace switch."""
from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

_METER_ATTRS = (
    "_tokens_used",
    "_tokens_in",
    "_tokens_out",
    "_tokens_cached",
    "_worker_cost_usd",
    "_worker_tokens_in",
    "_worker_tokens_out",
)


def _spin_server():
    import harness.server as srv
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return srv, httpd, port


def _get_usage(port, token):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/usage?token={token}",
        headers={"X-Harness-Token": token},
    )
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read().decode("utf-8"))


def _snapshot_meters(pilot):
    return {attr: getattr(pilot, attr, 0) for attr in _METER_ATTRS}


def _restore_meters(pilot, snap):
    for attr, val in snap.items():
        setattr(pilot, attr, val)


def test_rebuild_pilot_preserves_usage_meters():
    srv, httpd, port = _spin_server()
    saved = _snapshot_meters(srv._pilot)
    try:
        srv._pilot._tokens_used = 12_000
        srv._pilot._tokens_in = 8_000
        srv._pilot._tokens_out = 4_000
        srv._pilot._tokens_cached = 1_500
        srv._pilot._worker_cost_usd = 0.42
        srv._pilot._worker_tokens_in = 900
        srv._pilot._worker_tokens_out = 300

        before = _get_usage(port, srv._TOKEN)
        assert before["session"]["tokens_used"] == 12_000
        assert before["session"]["est_cost_usd"] > 0

        srv._rebuild_pilot_and_session()

        after = _get_usage(port, srv._TOKEN)
        assert after["session"]["tokens_used"] == 12_000
        assert getattr(srv._pilot, "_tokens_in") == 8_000
        assert getattr(srv._pilot, "_tokens_out") == 4_000
        assert getattr(srv._pilot, "_tokens_cached") == 1_500
        assert getattr(srv._pilot, "_worker_cost_usd") == 0.42
        assert getattr(srv._pilot, "_worker_tokens_in") == 900
        assert getattr(srv._pilot, "_worker_tokens_out") == 300
        assert after["session"]["est_cost_usd"] > 0
    finally:
        # Global singleton -- restore so later /api/usage tests see a clean pilot.
        _restore_meters(srv._pilot, saved)
        httpd.shutdown()
