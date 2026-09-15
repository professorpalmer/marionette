"""get_json empty_as_none applies to qs_args as well as qs_arg."""
from types import SimpleNamespace

from harness.http_routes import get_json


class _Handler:
    def __init__(self):
        self.sent = None

    def _send(self, status, body):
        self.sent = (status, body)
        return status, body


def test_empty_as_none_on_qs_args():
    captured = {}

    def api_fn(schedule_id, limit, svc):
        captured["id"] = schedule_id
        captured["limit"] = limit
        return 200, {"ok": True}

    handle = get_json(
        api_fn, services=lambda: object(), qs_args=("id", "limit"), empty_as_none=True
    )
    handle(_Handler(), None, {"id": [""], "limit": [""]})
    assert captured["id"] is None
    assert captured["limit"] is None


def test_empty_as_none_on_qs_arg():
    captured = {}

    def api_fn(job_id, svc):
        captured["job_id"] = job_id
        return 200, []

    handle = get_json(
        api_fn, services=lambda: object(), qs_arg="job_id", empty_as_none=True
    )
    handle(_Handler(), None, {"job_id": [""]})
    assert captured["job_id"] is None


def test_session_queue_route_passes_requested_session_id(monkeypatch):
    from harness import http_routes
    from harness.api import session_control

    captured = {}

    def queue_read(session_id, service):
        captured["session_id"] = session_id
        captured["service"] = service
        return 200, {"ok": True}

    class _Services:
        def __getattr__(self, _name):
            return lambda: object()

    monkeypatch.setattr(session_control, "get_session_queue", queue_read)
    routes = http_routes.build_get_routes(_Services())
    handler = _Handler()
    routes["/api/session/queue"](
        handler,
        SimpleNamespace(query="session_id=B"),
        {"session_id": ["B"]},
    )

    assert captured["session_id"] == "B"
    assert captured["service"] is not None
