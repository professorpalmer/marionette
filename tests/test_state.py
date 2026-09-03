"""Job event include modes on DurableState / JobStore.events_since."""
from __future__ import annotations

from harness.state import DurableState, JobStore, normalize_event_include, read_job_events_since


def _heartbeat_mix():
    return [
        {"id": 1, "event": "host.started", "payload": {"reason": "first"}},
        {"id": 2, "event": "run.heartbeat", "payload": {"n": 1}},
        {"id": 3, "event": "task.lease_renewed", "payload": {"task_id": "t1"}},
        {"id": 4, "event": "job.stalled", "payload": {"reason": "dead"}},
        {"id": 5, "event": "worker.gate_failed", "payload": {"reason": "no diff"}},
    ]


class _CursorStore:
    def __init__(self, events):
        self._events = list(events)
        self.lifecycle_calls = []

    def read_events_since(self, job_id, since=0):
        return [e for e in self._events if int(e.get("id") or 0) > int(since or 0)]

    def event_cursor(self, job_id):
        return max((int(e.get("id") or 0) for e in self._events), default=0)


class _LifecycleStore(_CursorStore):
    def read_lifecycle_events(self, job_id, since=0, include="lifecycle"):
        self.lifecycle_calls.append((job_id, since, include))
        return [{"id": 99, "event": "host.started", "payload": {"via": "store"}}]


def test_normalize_unknown_include_is_lifecycle():
    assert normalize_event_include("nope") == "lifecycle"
    assert normalize_event_include("") == "lifecycle"
    assert normalize_event_include("all") == "all"
    assert normalize_event_include("quiet") == "quiet"


def test_events_since_default_drops_heartbeat():
    ds = DurableState.__new__(DurableState)
    ds.store = _CursorStore(_heartbeat_mix())
    payload = ds.events_since("job_1")
    names = [e.get("event") for e in payload["events"]]
    assert "run.heartbeat" not in names
    assert "task.lease_renewed" not in names
    assert "host.started" in names
    assert "job.stalled" in names
    assert payload["cursor"] == 5


def test_events_since_include_all_keeps_heartbeat():
    ds = DurableState.__new__(DurableState)
    ds.store = _CursorStore(_heartbeat_mix())
    names = [e.get("event") for e in ds.events_since("job_1", include="all")["events"]]
    assert "run.heartbeat" in names
    assert "task.lease_renewed" in names


def test_events_since_unknown_include_matches_lifecycle():
    ds = DurableState.__new__(DurableState)
    ds.store = _CursorStore(_heartbeat_mix())
    default = [e.get("event") for e in ds.events_since("job_1")["events"]]
    unknown = [e.get("event") for e in ds.events_since("job_1", include="nope")["events"]]
    assert default == unknown
    assert "run.heartbeat" not in unknown


def test_events_since_prefers_store_read_lifecycle_events():
    store = _LifecycleStore(_heartbeat_mix())
    ds = DurableState.__new__(DurableState)
    ds.store = store
    payload = ds.events_since("job_1", cursor=2, include="quiet")
    assert store.lifecycle_calls == [("job_1", 2, "quiet")]
    assert payload["events"][0]["payload"]["via"] == "store"


def test_read_job_events_since_quiet_keeps_gate_failed():
    names = [
        e.get("event")
        for e in read_job_events_since(_CursorStore(_heartbeat_mix()), "job_1", include="quiet")
    ]
    assert "run.heartbeat" not in names
    assert "worker.gate_failed" in names
    assert "host.started" in names


def test_jobstore_alias_is_durable_state():
    assert JobStore is DurableState


def test_get_job_events_http_passes_include():
    from harness.api.jobs import get_job_events, make_job_services

    seen = {}

    class _Durable:
        def events_since(self, job_id, cursor=0, include="lifecycle"):
            seen["job_id"] = job_id
            seen["cursor"] = cursor
            seen["include"] = include
            return {"events": [{"event": "host.started"}], "cursor": 4}

    class _Sess:
        def state(self):
            return _Durable()

    svc = make_job_services(
        get_session=lambda: _Sess(),
        scoped_jobs_with_stores=lambda **_k: ([{"id": "job_1"}], None, None),
    )

    def _owned(job_id, _svc):
        return True

    import harness.api.jobs as jobs_mod
    orig = jobs_mod._job_access_owned
    jobs_mod._job_access_owned = _owned
    try:
        code, payload = get_job_events(
            {"job_id": ["job_1"], "include": ["all"], "since": ["2"]},
            svc,
        )
    finally:
        jobs_mod._job_access_owned = orig
    assert code == 200
    assert seen == {"job_id": "job_1", "cursor": 2, "include": "all"}
    assert payload["events"][0]["event"] == "host.started"


def test_events_since_fail_soft_empty_without_helpers():
    class _Dead:
        pass

    ds = DurableState.__new__(DurableState)
    ds.store = _Dead()
    payload = ds.events_since("job_1")
    assert payload == {"events": [], "cursor": 0}
