"""Artifacts endpoint resilience: a transient SQLite 'database is locked' must not
500 GET /api/artifacts. Hermetic -- no real store."""
import json
import sqlite3
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from harness import server
from harness.job_scoping import job_label_for_session


_OWNED_JOB_ID = "j1"
_OWNED_LABEL = job_label_for_session("sess-artifacts", origin="marionette")


class _OwnedStore:
    """Harness store row so GET /api/artifacts admits this id as owned."""

    def get_job(self, job_id):
        if job_id != _OWNED_JOB_ID:
            return None
        return {"id": job_id, "origin": "marionette", "label": _OWNED_LABEL}

    def list_jobs(self):
        return [self.get_job(_OWNED_JOB_ID)]

    def list_tasks(self, _job_id):
        return []


class _State:
    def __init__(self, artifacts=None, exc=None):
        self._artifacts = artifacts
        self._exc = exc
        self.store = _OwnedStore()

    def job_artifacts(self, job_id):
        if self._exc is not None:
            raise self._exc
        return self._artifacts if self._artifacts is not None else []


class _Session:
    def __init__(self, state):
        self._state = state

    def state(self):
        return self._state


def _server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port


def test_api_artifacts_returns_payload(monkeypatch):
    monkeypatch.setattr(
        server, "_session",
        _Session(_State(artifacts=[{"type": "finding", "headline": "ok"}])))
    httpd, port = _server()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/artifacts?job_id=j1",
            method="GET",
            headers={"X-Harness-Token": server._TOKEN},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        assert data == [{"type": "finding", "headline": "ok"}]
    finally:
        httpd.shutdown()


def test_api_artifacts_degrades_on_locked(monkeypatch):
    monkeypatch.setattr(
        server, "_session",
        _Session(_State(exc=sqlite3.OperationalError("database is locked"))))
    monkeypatch.setattr("time.sleep", lambda _s: None)
    httpd, port = _server()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/artifacts?job_id=j1",
            method="GET",
            headers={"X-Harness-Token": server._TOKEN},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        assert resp.status == 200
        assert json.loads(resp.read().decode()) == []
    finally:
        httpd.shutdown()



def test_api_artifacts_empty_job_id_is_400(monkeypatch):
    monkeypatch.setattr(server, "_session", _Session(_State(artifacts=[])))
    httpd, port = _server()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/artifacts?job_id=",
            method="GET",
            headers={"X-Harness-Token": server._TOKEN},
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            assert False, "expected 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
            data = json.loads(e.read().decode())
            assert data == {"error": "missing job id"}
    finally:
        httpd.shutdown()
