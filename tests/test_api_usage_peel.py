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
        active_session_total=lambda ids, arts, reg: None,
        sum_job_set_savings=lambda ids, arts, reg: (0.0, 0.0),
        cache_savings=lambda cached, pin: 0.0,
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
    assert "jobs" in payload
    assert store  # cached
