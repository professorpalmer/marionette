"""get_json empty_as_none applies to qs_args as well as qs_arg."""
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
