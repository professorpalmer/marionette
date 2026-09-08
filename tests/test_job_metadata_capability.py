"""Public-runtime startup and bounded metadata capability boundary."""
import http.client
import json
import os
import sys
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from harness.job_metadata_capability import bounded_metadata_available, UNAVAILABLE_REASON


@pytest.mark.parametrize("force_missing", [False, True])
def test_server_startup_and_registry(tmp_path, monkeypatch, force_missing):
    import harness.server as server
    from harness.session_runners import SessionRunnerRegistry

    import harness.job_metadata_view as view_module

    actual = bounded_metadata_available()
    expected = os.environ.get("PM_METADATA_EXPECT_SUPPORTED")
    if expected is not None:
        assert actual == (expected == "1")
    if force_missing:
        monkeypatch.setattr(view_module, "bounded_metadata_available", lambda: False)
    supported = actual and not force_missing
    if not supported:
        def forbidden(*args, **kwargs):
            pytest.fail("unsupported metadata invoked reader, discovery, or native observer")
        monkeypatch.setattr(view_module, "create_metadata_reader", forbidden)
        monkeypatch.setattr(view_module, "discover_sources", forbidden)

    reg = SessionRunnerRegistry()
    assert reg.metadata_view.supported is supported
    assert (reg.metadata_view.reader() is not None) is supported
    runner = SimpleNamespace(config=SimpleNamespace(repo=str(tmp_path)),
                             state_dir=str(tmp_path / "store"))
    if not supported:
        runner._local_metadata = SimpleNamespace(describe=forbidden)
    assert reg.get_or_create("A", lambda: runner) is runner
    reg.set_active_view("A")
    assert reg.get("A") is runner
    assert reg.active_view_id == "A"
    assert reg.status("A") == "idle"
    monkeypatch.setattr(server, "_runners", reg)
    monkeypatch.setattr(server, "_GET_ROUTES", None)
    monkeypatch.setattr(server, "_POST_JSON_ROUTES", None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    def request(method, path, body=None, token=True):
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
        headers = {"X-Harness-Token": server._TOKEN} if token else {}
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = conn.getresponse()
        result = response.status, json.loads(response.read())
        conn.close()
        return result

    try:
        status, payload = request("GET", "/api/jobs/metadata/view")
        assert status == 200
        assert payload["context"]["session_id"] == "A"
        assert payload["availability"] == "unavailable"
        assert payload["missing"] == (["sources_not_refreshed"] if supported else [UNAVAILABLE_REASON])
        generation = payload["context"]["view_generation"]
        assert request("GET", "/api/sessions/runners")[0] == 200
        assert request("GET", "/api/jobs/metadata/view?unknown=")[0] == 400
        assert request("POST", "/api/jobs/metadata/view/refresh", {})[0] == 400
        assert request("POST", "/api/jobs/metadata/view/refresh", {"view_generation": "stale"})[0] == 409
        endpoints = [("GET", "", None), ("GET", "/detail", None),
                     ("GET", "/local", None), ("GET", "/local/detail", None),
                     ("POST", "/pins", {})]
        for method, suffix, body in endpoints + [("GET", "/view", None),
                ("POST", "/view/refresh", {"view_generation": generation})]:
            assert request(method, "/api/jobs/metadata" + suffix, body, token=False)[0] == 403
        for method, suffix, body in endpoints:
            status, result = request(method, "/api/jobs/metadata" + suffix, body)
            if supported:
                assert status == 400
                assert result["code"] == "invalid_read_request"
            else:
                assert status == 503
                assert result == dict(code="metadata_unavailable", availability="unavailable",
                                      missing=[UNAVAILABLE_REASON])
        if not supported:
            assert request("POST", "/api/jobs/metadata/view/refresh",
                           {"view_generation": generation}) == (200, payload)
            transition = reg.metadata_view.invalidate()
            reg.metadata_view.restore(transition)
            reg.metadata_view.invalidate_sources()
            reg.metadata_view.replace_root(str(tmp_path / "another"))
            assert reg.metadata_view.describe()["missing"] == [UNAVAILABLE_REASON]
            assert reg.metadata_view.capture().session_id == "A"
            assert reg.metadata_view.capture().generation != generation
        if not actual:
            assert "harness.job_readmodel" not in sys.modules
            assert "harness.api.job_readmodel" not in sys.modules
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)
    assert reg.detach_view("A")
    assert reg.get("A") is runner


def test_capability_does_not_mask_broken_imports(monkeypatch):
    import harness.job_metadata_capability as capability

    monkeypatch.setattr(capability, "find_spec", lambda name: object())
    def broken(name):
        raise RuntimeError("broken PM installation")
    monkeypatch.setattr(capability, "import_module", broken)
    with pytest.raises(RuntimeError, match="broken PM installation"):
        capability.bounded_metadata_available()


def test_capability_requires_both_store_contracts(monkeypatch):
    import harness.job_metadata_capability as capability

    if not bounded_metadata_available():
        # Public 1.23 has no identity module; no new API may be imported.
        assert capability.find_spec("puppetmaster.identity") is None
        return
    from puppetmaster.sqlite_store import SQLiteSwarmStore
    from puppetmaster.store import SwarmStore
    for store in (SQLiteSwarmStore, SwarmStore):
        with monkeypatch.context() as patch:
            patch.setattr(store, "list_job_summaries", None)
            assert not capability.bounded_metadata_available()
    assert capability.bounded_metadata_available()
