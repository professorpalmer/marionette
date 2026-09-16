"""Characterization tests for usage API peel."""
from __future__ import annotations

from types import SimpleNamespace

from harness.api.usage import UsageServices, get_context_usage, get_usage


def _svc(*, driver="m1", repo="", meters=None, cache=None, pilot=None):
    meters = meters or {
        "_tokens_used": 100,
        "_tokens_cached": 10,
        "_worker_tokens_in": 0,
        "_worker_tokens_out": 0,
        "_worker_cost_usd": 0.0,
        "_provider_cost_usd": 0.0,
    }
    store = {} if cache is None else cache

    return UsageServices(
        cfg=SimpleNamespace(driver=driver, repo=repo),
        boot_repos=lambda: set(),
        boot_usage_meters=lambda: dict(meters),
        usage_cache_get=lambda k: store.get(k),
        usage_cache_put=lambda k, p: store.__setitem__(k, p),
        boot_session_cost=lambda pin, pout: 0.01,
        scoped_jobs_with_stores=lambda repo_root=None: ([], None, None),
        job_in_cost_window=lambda created: True,
        swarm_registry=lambda: [],
        job_swarm_accounting=lambda arts, reg: (0, 0.0),
        tokens_cached_swarm=lambda arts: 0,
        job_savings_fields=lambda jid: {},
        active_session_total=lambda ids, arts, reg, reports: None,
        sum_job_set_savings=lambda ids, arts, reg, **kw: (0.0, 0.0),
        sum_job_set_savings_detail=lambda ids, arts, reg, **kw: {
            "routing_saved_usd": 0.0,
            "cache_saved_usd_swarm": 0.0,
            "routing_savings_basis": "unknown",
            "routing_tokens_compared": 0,
        },
        cache_savings=lambda cached, pin: 0.0,
        cache_savings_gross=lambda cached, pin: 0.0,
        boot_cost_source=lambda: "estimated",
        tool_output_savings_fields=lambda pin, process_wide=False: {},
        persist_boot_usage=lambda **kw: None,
        retry_on_locked=lambda fn: fn(),
        diag=lambda *a, **k: None,
        get_pilot=lambda: pilot or SimpleNamespace(
            get_context_usage=lambda: {"used": 1, "limit": 10}
        ),
    ), store


def test_get_context_usage_ok():
    svc, _ = _svc()
    code, payload = get_context_usage(svc)
    assert code == 200
    assert payload["used"] == 1


def test_get_context_usage_error():
    class _Bad:
        def get_context_usage(self):
            raise RuntimeError("boom")

    svc, _ = _svc(pilot=_Bad())
    code, payload = get_context_usage(svc)
    assert code == 500
    assert "boom" in payload["error"]


def test_get_usage_cache_hit():
    store = {"hit": {"session": {"tokens_used": 9}, "jobs": []}}
    svc, _ = _svc(cache=store)

    # Force cache key to collide by stubbing meters + empty repos so we
    # can put a known key: monkey via pre-seeded get that always hits.
    svc.usage_cache_get = lambda k: {"session": {"tokens_used": 42}, "jobs": []}
    code, payload = get_usage("", svc)
    assert code == 200
    assert payload["session"]["tokens_used"] == 42


def test_get_usage_builds_session_pill(monkeypatch):
    monkeypatch.setattr(
        "pmharness.registry.resolve_price",
        lambda driver: (1.0, 2.0),
        raising=False,
    )
    svc, store = _svc(driver="m1", repo="")
    code, payload = get_usage("", svc)
    assert code == 200
    assert payload["session"]["driver"] == "m1"
    assert payload["session"]["price_in"] == 1.0
    assert payload["session"]["tokens_used"] == 100
    assert isinstance(payload["session"].get("pilot_by_model"), list)
    assert isinstance(payload["session"].get("swarm_by_model"), list)
    assert "jobs" in payload
    assert store  # cached


def test_context_usage_deferred_transition(tmp_path):
    from threading import Event
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession
    from harness.deferred_attach import DeferredPilotPlaceholder, schedule_deferred_build

    shell = DeferredPilotPlaceholder(session_id="child", state_dir=str(tmp_path))
    svc, _ = _svc(pilot=shell)
    release = Event()
    real = ConversationalSession(HarnessConfig(max_context_tokens=1000))
    real.harness_session_id = "child"
    def build():
        assert release.wait(5)
        return real
    thread = schedule_deferred_build(build, on_done=shell.mark_ready, on_error=shell.mark_failed)
    try:
        code, payload = get_context_usage(svc)
        assert (code, payload) == (200, {"available": False, "reason": "building", "session_id": "child"})
    finally:
        release.set()
        thread.join(5)
    code, payload = get_context_usage(svc)
    assert code == 200
    assert payload == {**real.get_context_usage(), "available": True, "session_id": "child"}


def test_context_usage_failed_build_remains_error(tmp_path):
    from harness.deferred_attach import DeferredPilotPlaceholder
    shell = DeferredPilotPlaceholder(session_id="child", state_dir=str(tmp_path))
    shell.mark_failed(RuntimeError("construction failed"))
    svc, _ = _svc(pilot=shell)
    code, payload = get_context_usage(svc)
    assert code == 500
    assert "construction failed" in payload["error"]


def test_context_usage_scoped_registry_lookup_never_uses_active_pilot():
    from harness.session_runners import SessionRunnerRegistry
    runners = SessionRunnerRegistry()
    child = SimpleNamespace(harness_session_id="child", get_context_usage=lambda: {"total": 73})
    runners.get_or_create("child", lambda: child)
    svc, _ = _svc()
    svc.get_runner = runners.get
    def forbidden():
        raise AssertionError("scoped request must not resolve the active pilot")
    svc.get_pilot = forbidden
    assert get_context_usage(svc, "child") == (200, {"available": True, "session_id": "child", "total": 73})
    assert get_context_usage(svc, "missing") == (200, {"available": False, "session_id": "missing", "reason": "no_runner"})
    assert runners.ids() == ["child"]


def test_context_usage_route_during_real_cold_attach(tmp_path, monkeypatch):
    import threading
    import json
    from copy import deepcopy
    import harness.server as srv
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession
    from harness.http_routes import build_get_routes
    from harness.session_runners import SessionRunnerRegistry

    monkeypatch.setenv("HARNESS_DEFER_COLD_ATTACH", "1")
    cfg = deepcopy(srv._cfg)
    cfg.state_dir = str(tmp_path)
    monkeypatch.setattr(srv, "_cfg", cfg)
    monkeypatch.setattr(srv, "_runners", SessionRunnerRegistry())
    monkeypatch.setattr(srv, "_pilot", None)
    # Keep bind side effects out of this route/attach test.
    monkeypatch.setattr(srv, "_bind_pilot_services", lambda pilot: None)
    real = ConversationalSession(HarnessConfig(state_dir=str(tmp_path), max_context_tokens=1000))
    release = threading.Event()
    def build(*, config=None):
        assert release.wait(5)
        return real
    monkeypatch.setattr(srv, "_build_conversational_pilot", build)
    class Handler:
        def _send(self, status, body):
            return status, json.loads(body)
    route = build_get_routes(srv._route_services())["/api/context/usage"]
    child_id = srv._sessions.create(title="cold-child")["id"]
    shell = srv._attach_view(child_id, defer_cold_build=True, load_transcript_on_create=False)
    try:
        assert route(Handler(), None, {"session_id": [child_id]}) == (
            200, {"available": False, "reason": "building", "session_id": child_id})
        assert route(Handler(), None, {"session_id": ["other"]}) == (
            200, {"available": False, "reason": "no_runner", "session_id": "other"})
    finally:
        release.set()
        shell.ensure_ready(timeout=5)
    status, payload = route(Handler(), None, {"session_id": [child_id]})
    assert status == 200
    assert payload["available"] is True
    assert payload["session_id"] == child_id
    assert payload["total"] == real.get_context_usage()["total"]


def test_session_usage_filters_foreign_session_jobs_before_aggregation(tmp_path, monkeypatch):
    from puppetmaster.store_factory import create_store
    store = create_store('sqlite', tmp_path / 'store')
    mine = store.create_job('mine', origin='marionette', session_id='session-a')
    other = store.create_job('other', origin='marionette', session_id='session-b')
    svc, _ = _svc(pilot=SimpleNamespace(harness_session_id='session-a'))
    svc.scoped_jobs_with_stores = lambda repo_root=None: ([
        dict(id=j.id, source='harness', accounting_owned=True, session_id=sid, origin='marionette')
        for j, sid in [(mine, 'session-a'), (other, 'session-b')]
    ], store, None)
    seen = []
    def total(ids, arts, registry, reports):
        seen.extend(key[-1] for key in ids)
        return dict(session_id='session-a', est_cost_usd=0, input_tokens=0, output_tokens=0)
    svc.active_session_total = total
    get_usage('', svc)
    assert seen == [mine.id]
