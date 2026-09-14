from types import SimpleNamespace

import pytest

from harness.api.streams import (
    _admit_owned_stream_input,
    _stream_session_pilot,
    _stream_turn_config,
)
from harness.input_receipts import InputReceiptError
from harness.sessions import SessionStore


def test_explicit_stream_owner_survives_view_switch():
    owner = SimpleNamespace(harness_session_id="a")
    other = SimpleNamespace(harness_session_id="b")
    runners = {"a": owner, "b": other}
    svc = SimpleNamespace(get_pilot=lambda: other, sessions=SimpleNamespace(active="b"),
                          get_runners=lambda: runners, pilot_swap_lock=None)
    assert _stream_session_pilot(svc, "a") is owner
    assert _admit_owned_stream_input(svc, owner, "a", "hello", [], "")[2] == "hello"
    runners["a"] = SimpleNamespace(harness_session_id="a")
    with pytest.raises(InputReceiptError):
        _admit_owned_stream_input(svc, owner, "a", "hello", [], "")
    runners["a"] = other
    with pytest.raises(InputReceiptError):
        _stream_session_pilot(svc, "a")
    with pytest.raises(InputReceiptError):
        _stream_session_pilot(svc, "missing")


def test_session_historical_value_roundtrip_and_legacy(tmp_path):
    path = str(tmp_path / "sessions.json")
    store = SessionStore(path)
    sid = store.create()["id"]
    store.accumulate_meters(sid, input_tokens=100, estimated_cost_usd=0,
                            nominal_cost_usd=2, cache_savings_usd=3,
                            billing="plan", value_complete=True)
    row = SessionStore(path).list()[0]
    assert row["estimated_cost_usd"] == 0
    assert row["nominal_cost_usd"] == 2
    assert row["cache_savings_usd"] == 3
    assert row["plan_calls"] == 1
    assert row["list_price_complete"] is True
    store.accumulate_meters(sid, input_tokens=10, estimated_cost_usd=1)
    row = SessionStore(path).list()[0]
    assert row["estimated_cost_usd"] == 1
    assert row["nominal_cost_usd"] == 3
    assert row["list_price_complete"] is False


def test_owned_queue_route_uses_registry_not_active_view():
    from harness.api.session_control import _owned_input_route
    owner = SimpleNamespace(harness_session_id="a")
    other = SimpleNamespace(harness_session_id="b")
    svc = SimpleNamespace(get_pilot=lambda: other, get_runners=lambda: {"a": owner},
                          get_sessions=lambda: SimpleNamespace(active="b"),
                          pilot_swap_lock=None, gate_active_pilot_ready=lambda: {"error": "view loading"})
    route = _owned_input_route(lambda body, services, pilot: (200, {"owner": pilot}))
    assert route({"session_id": "a"}, svc) == (200, {"owner": owner})


def test_session_total_carries_historical_plan_value(tmp_path, monkeypatch):
    import harness.server
    from harness.api import usage_meters
    store = SessionStore(str(tmp_path / "sessions.json"))
    sid = store.create()["id"]
    store.accumulate_meters(sid, input_tokens=100, nominal_cost_usd=2,
                            cache_savings_usd=3, billing="plan", value_complete=True)
    monkeypatch.setattr(usage_meters, "_sessions", lambda: store)
    total = usage_meters._active_session_total([], lambda _: [], {})
    assert total["est_cost_usd"] == 0
    assert total["nominal_cost_usd"] == 2
    assert total["cache_savings_gross_usd"] == 3
    assert total["cost_source"] == "plan_estimated"
    assert total["list_price_complete"] is True


def test_finalize_uses_owner_state_directory(tmp_path, monkeypatch):
    import harness.server as server
    owner = SimpleNamespace(harness_session_id="a", config=SimpleNamespace(state_dir=str(tmp_path / "a")))
    monkeypatch.setattr(server, "_runners", {"a": owner})
    monkeypatch.setattr(server, "_cfg", SimpleNamespace(state_dir=str(tmp_path / "b")))
    saved = []
    monkeypatch.setattr(server, "persist_live_transcript", lambda pilot, path, sid, **kw: saved.append((path, sid)))
    server._persist_turn_transcript({"pilot": owner, "session_id": "a", "config": owner.config})
    assert saved == [(str(tmp_path / "a"), "a")]


def test_explicit_stream_legacy_service_accepts_only_current_owner():
    owner = SimpleNamespace(harness_session_id="a")
    svc = SimpleNamespace(get_pilot=lambda: owner, get_runners=None)
    assert _stream_session_pilot(svc, "a") is owner
    with pytest.raises(InputReceiptError):
        _stream_session_pilot(svc, "b")


def test_stream_turn_config_uses_live_config_only_for_active_owner():
    active = SimpleNamespace(harness_session_id="a", config=SimpleNamespace(repo="old-a"))
    background = SimpleNamespace(harness_session_id="b", config=SimpleNamespace(repo="owner-b"))
    live = SimpleNamespace(repo="live-a")
    svc = SimpleNamespace(
        cfg=live,
        sessions=SimpleNamespace(active="a"),
        get_pilot=lambda: active,
    )
    assert _stream_turn_config(svc, active, "a") is live
    assert _stream_turn_config(svc, background, "b") is background.config


def test_explicit_stream_resolves_its_own_cold_runner(tmp_path, monkeypatch):
    from harness.deferred_attach import DeferredPilotPlaceholder
    placeholder = DeferredPilotPlaceholder(session_id="a", state_dir=str(tmp_path))
    owner = SimpleNamespace(harness_session_id="a")
    runners = {"a": placeholder}
    def ready():
        runners["a"] = owner
        return owner
    monkeypatch.setattr(placeholder, "ensure_ready", ready)
    svc = SimpleNamespace(get_pilot=lambda: None, get_runners=lambda: runners)
    assert _stream_session_pilot(svc, "a") is owner
