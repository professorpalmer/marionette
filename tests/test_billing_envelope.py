"""Hermetic monthly billing envelope + /api/usage without a runner."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from harness.api.usage import UsageServices, get_usage, post_usage
from harness.billing_envelope import (
    apply_settings,
    load_envelope,
    observe_spend,
    utc_month_key,
)


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
        boot_session_cost=lambda pin, pout: 0.42,
        scoped_jobs_with_stores=lambda repo_root=None: ([], None, None),
        job_in_cost_window=lambda created: True,
        swarm_registry=lambda: [],
        job_swarm_accounting=lambda arts, reg: (0, 0.0),
        tokens_cached_swarm=lambda arts: 0,
        job_savings_fields=lambda jid: {},
        active_session_total=lambda ids, arts, reg: None,
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
        get_pilot=lambda: (_ for _ in ()).throw(RuntimeError("no runner")),
    ), store


def test_usage_without_runner_returns_envelope(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    svc, _ = _svc()
    code, payload = get_usage("", svc)
    assert code == 200
    assert "session" in payload
    env = payload["envelope"]
    assert env["month_key"] == utc_month_key()
    assert env["month_key"] == datetime.now(timezone.utc).strftime("%Y-%m")
    assert env["spent_usd"] == 0.42
    assert env["cap"] is None
    assert env["remaining"] is None
    assert env["blocked"] is False
    assert env["auto_reload"] == {"enabled": False, "amount": 0.0}
    assert env["last_reload"] is None


def test_usage_survives_missing_services(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    svc, _ = _svc()
    svc.boot_usage_meters = lambda: (_ for _ in ()).throw(RuntimeError("no meters"))
    code, payload = get_usage("", svc)
    assert code == 200
    assert payload["envelope"]["month_key"] == utc_month_key()
    assert payload["envelope"]["spent_usd"] == 0.0


def test_cap_blocks_when_spent_reaches_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    status, body = post_usage({"cap": 0.10})
    assert status == 200
    assert body["envelope"]["cap"] == 0.10
    assert body["envelope"]["blocked"] is False

    env = observe_spend(0.10)
    assert env["blocked"] is True
    assert env["remaining"] == 0.0
    assert env["spent_usd"] == 0.10

    svc, _ = _svc()
    code, payload = get_usage("", svc)
    assert code == 200
    assert payload["envelope"]["blocked"] is True
    assert payload["envelope"]["spent_usd"] >= 0.10


def test_auto_reload_persists_intent_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    status, body = post_usage({
        "cap": 1.0,
        "auto_reload": {"enabled": True, "amount": 5.0},
    })
    assert status == 200
    auto = body["envelope"]["auto_reload"]
    assert auto["enabled"] is True
    assert auto["amount"] == 5.0
    assert body["envelope"]["last_reload"] is None

    env = apply_settings({})  # reload from disk
    assert env["auto_reload"]["enabled"] is True
    assert env["auto_reload"]["amount"] == 5.0

    blocked = observe_spend(1.5)
    assert blocked["blocked"] is True
    last = blocked["last_reload"]
    assert last is not None
    assert last["amount"] == 5.0
    assert last["charged"] is False
    assert last["at"]
    # Cap is unchanged — no card charge / no reload of funds.
    assert blocked["cap"] == 1.0
    disk = load_envelope()
    assert disk["auto_reload"]["enabled"] is True
    assert disk["last_reload"]["charged"] is False


def test_month_key_rolls_spent(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    july = datetime(2026, 7, 31, 23, 0, tzinfo=timezone.utc)
    apply_settings({"cap": 20.0}, now=july)
    observe_spend(4.0, now=july)
    stored = load_envelope(now=july)
    assert stored["month_key"] == "2026-07"
    assert stored["spent_usd"] == 4.0

    august = datetime(2026, 8, 1, 0, 1, tzinfo=timezone.utc)
    rolled = load_envelope(now=august)
    assert rolled["month_key"] == "2026-08"
    assert rolled["spent_usd"] == 0.0
    assert rolled["cap"] == 20.0
