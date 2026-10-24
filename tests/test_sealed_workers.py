"""Worker jobs of encrypted sessions: own store, agentic only, no flows."""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from types import SimpleNamespace

import pytest

import pmharness.bridge as bridge
from harness import flows, seal, sealed_workers
from pmharness.intent import DriverIntent


@pytest.fixture
def sealed(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    seal.lock()
    seal._sealed_ids.clear()
    master = secrets.token_bytes(32)
    os.makedirs(seal.seal_dir(), exist_ok=True)
    with open(seal.config_path(), "w", encoding="utf-8") as fh:
        json.dump({"v": 1, "enabled": True, "key_id": seal.key_id(master).hex()}, fh)
    seal.unlock(master)
    seal.create_session("sealedsid")
    yield "sealedsid"
    seal.lock()
    seal._sealed_ids.clear()


def test_job_store_is_in_the_sealed_folder_and_goes_with_the_session(sealed):
    store = sealed_workers.new_job_store(sealed, "swarm-")
    assert store.startswith(os.path.join(seal.sealed_data_dir(sealed), sealed_workers.JOBS_DIR))
    assert os.path.isdir(store)
    seal.erase_session(sealed)
    assert not os.path.exists(store)


def test_sweep_removes_job_stores_only(sealed):
    stores = [sealed_workers.new_job_store(sealed, "pmh-edit-") for _ in range(2)]
    other = os.path.join(seal.sealed_data_dir(sealed), "todos.json")
    with open(other, "w") as fh:
        fh.write("kept")
    assert sealed_workers.sweep_job_stores() == 2
    assert not any(os.path.exists(s) for s in stores)
    assert os.path.exists(other)


def test_store_is_not_removed_under_a_live_runner(sealed):
    store = sealed_workers.new_job_store(sealed, "swarm-")
    release = threading.Event()
    runner = threading.Thread(target=release.wait, daemon=True)
    runner.start()
    sealed_workers.remove_job_store_after(runner, store)
    time.sleep(0.2)
    assert os.path.isdir(store)
    release.set()
    deadline = time.time() + 5
    while os.path.exists(store) and time.time() < deadline:
        time.sleep(0.05)
    assert not os.path.exists(store)


def test_flow_launch_is_refused(sealed):
    session = SimpleNamespace(harness_session_id=sealed)
    with pytest.raises(flows.FlowCallError, match="cannot run flows"):
        flows.launch(session, graph={"nodes": []}, flow_input="", continue_from="", goal="g", repo="")


def test_schema_hides_flow_tools_in_a_sealed_session(sealed, tmp_path, monkeypatch):
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession

    monkeypatch.setattr(ConversationalSession, "_worker_delegation_available", lambda self: True)

    def names(sid):
        session = ConversationalSession(HarnessConfig(repo=str(tmp_path)))
        session.harness_session_id = sid
        return {(item.get("function") or item).get("name") for item in session._build_visible_tools_schema()}

    plain, sealed_names = names("plainsid"), names(sealed)
    assert "run_swarm" in sealed_names and "run_implement" in sealed_names
    assert {"run_flow", "flow_control"} <= plain
    assert plain - sealed_names == {"run_flow", "flow_control"}
    assert not sealed_names & sealed_workers.FLOW_TOOLS


def test_dispatch_adapter_refusal(sealed):
    from harness.send_loop_dispatch import _sealed_adapter_refusal

    pin = SimpleNamespace(adapter="codex")
    plain = SimpleNamespace(harness_session_id="plainsid")
    sealed_session = SimpleNamespace(harness_session_id=sealed)
    assert _sealed_adapter_refusal(plain, SimpleNamespace(adapter="claude-code"), None) is None
    assert _sealed_adapter_refusal(sealed_session, SimpleNamespace(adapter=""), None) == ""
    assert _sealed_adapter_refusal(sealed_session, SimpleNamespace(adapter="agentic"), None) == ""
    assert "claude-code" in _sealed_adapter_refusal(sealed_session, SimpleNamespace(adapter="claude-code"), None)
    assert "codex" in _sealed_adapter_refusal(sealed_session, SimpleNamespace(adapter=""), pin)


def test_edit_worker_runs_agentic_only_with_no_fallback(sealed, tmp_path, monkeypatch):
    from harness import edit_engines
    from harness.config import HarnessConfig
    from harness.worker import WorkerResult

    config = HarnessConfig(repo=str(tmp_path))
    refused = edit_engines.run_edit_worker(config, "g", requested_adapter="cursor", session_id=sealed)
    assert not refused.ok and "only on the agentic adapter" in refused.summary

    monkeypatch.setattr(edit_engines, "agentic_platform_enabled", lambda: True)
    monkeypatch.setattr(edit_engines, "agentic_available", lambda: True)
    calls = []
    monkeypatch.setattr(edit_engines, "run_agentic_edit", lambda *a, **k: calls.append("agentic") or WorkerResult(
        ok=False, error=edit_engines.AGENTIC_UNAVAILABLE, summary="down"))
    monkeypatch.setattr(edit_engines, "run_native_edit", lambda *a, **k: calls.append("native"))
    monkeypatch.setattr(edit_engines, "run_cursor_edit", lambda *a, **k: calls.append("cursor"))
    result = edit_engines.run_edit_worker(config, "g", session_id=sealed)
    assert calls == ["agentic"] and not result.ok


def _swarm(monkeypatch, tmp_path, adapter="agentic"):
    monkeypatch.setenv("HARNESS_REPO", str(tmp_path))
    monkeypatch.setattr("harness.swarm_worker_route.resolve_product_worker_adapter", lambda: adapter)
    monkeypatch.setattr("harness.swarm_worker_allowlist.resolve_swarm_worker_allowlist", lambda **_k: {
        "allowed_adapters": ["agentic", "cursor"], "prefer_plan_billed": False, "primary_adapter": "agentic",
        "allowed_model_ids": [],
    })
    monkeypatch.setattr(bridge, "_warn_if_unindexed", lambda *_a, **_k: None)
    monkeypatch.setattr(bridge, "_browser_swarm_enabled", lambda _goal: True)
    specs = []

    class Orchestrator:
        def __init__(self, store):
            self.store = store

        def run(self, goal, specs=(), **_k):
            specs_seen.extend(specs)
            return SimpleNamespace(job=SimpleNamespace(id="job_x", status="complete"), status="complete",
                                   mode="inline", artifacts=[], summary="ok")

    specs_seen = specs
    monkeypatch.setattr("puppetmaster.orchestrator.Orchestrator", Orchestrator)
    return specs


def test_sealed_swarm_runs_agentic_without_browser_or_cursor(sealed, tmp_path, monkeypatch):
    specs = _swarm(monkeypatch, tmp_path)
    intent = DriverIntent(action="run_swarm", goal="audit", roles=["explore"])
    bridge.execute_intent(intent, state_dir=str(tmp_path / "store"), sealed=True)
    assert specs and all(s.adapter == "agentic" for s in specs)
    assert all(s.payload["allowed_adapters"] == ["agentic"] for s in specs)
    assert all(s.payload["allow_browser"] is False for s in specs)

    specs.clear()
    bridge.execute_intent(intent, state_dir=str(tmp_path / "store2"))
    assert specs[0].payload["allowed_adapters"] == ["agentic", "cursor"]
    assert specs[0].payload["allow_browser"] is True


@pytest.mark.parametrize("case", ["cursor_route", "prewalk", "daemon", "codex_pin"])
def test_sealed_swarm_refusals(case, sealed, tmp_path, monkeypatch):
    specs = _swarm(monkeypatch, tmp_path, adapter="cursor" if case == "cursor_route" else "agentic")
    kwargs = {}
    intent = DriverIntent(action="run_swarm", goal="audit", roles=["explore"])
    if case == "prewalk":
        intent = DriverIntent(action="run_prewalk", goal="audit")
    if case == "daemon":
        kwargs["worker_mode"] = "daemon"
    if case == "codex_pin":
        intent = DriverIntent(action="run_swarm", goal="audit", roles=["explore"], model="codex/gpt-6.1-sol")
        monkeypatch.setattr("harness.swarm_model_pin.resolve_swarm_model_pin", lambda *_a, **_k: {
            "adapter": "codex", "pin_fields": {"pinned_model": "codex/gpt-6.1-sol"}})
    expected = {"cursor_route": "'cursor' keeps", "prewalk": "run_prewalk", "daemon": "'daemon' workers",
                "codex_pin": "'codex' keeps"}[case]
    with pytest.raises(ValueError, match=expected):
        bridge.execute_intent(intent, state_dir=str(tmp_path / "store"), sealed=True, **kwargs)
    assert specs == []


def test_sealed_swarm_cost_receipt_comes_from_the_sealed_store(sealed, monkeypatch):
    import harness.financial_receipt as receipts
    import harness.send_loop_dispatch as dispatch
    from unittest.mock import MagicMock

    from harness.pilot import PilotAction

    result = SimpleNamespace(
        job_id="job_ok", adapter="agentic", mode="swarm", num_artifacts=0, artifact_types=[],
        auth_failure="", summary="", artifacts=[],
    )
    session = SimpleNamespace(
        config=SimpleNamespace(repo="/repo/wiki"), harness_session_id=sealed, _session_job_ids=[],
        _register_local_job=MagicMock(), _finish_local_job=MagicMock(), _append_action_result=MagicMock(),
        _display_transcript=[], durable=None,
    )
    loaded = []
    monkeypatch.setattr(receipts, "load_pm_cost_report", lambda store, job_id: loaded.append(store.db_path) or {
        "job_id": job_id, "actual_cost": {"measured_cost_usd": 0.5, "measured_runs": 1, "priced_tasks": 1}})
    monkeypatch.setattr(dispatch, "_non_git_workspace_error", lambda *_a, **_k: None)
    monkeypatch.setattr(dispatch, "stream_swarm", lambda session, intent, q, *_a, **_k: q.put(("done", result)))

    events = list(dispatch.dispatch_swarm_action(
        session, PilotAction(kind="run_swarm", goal="audit keys", roles=["explore"]), "a-1", True,
        counters={"swarms": 0, "demo_swarms": 0}, turn_findings=[],
    ))

    jobs = os.path.join(seal.sealed_data_dir(sealed), sealed_workers.JOBS_DIR)
    assert len(loaded) == 1 and str(loaded[0]).startswith(jobs)
    receipt = next(e for e in events if e.kind == "action_result").data["financial_receipt"]
    assert receipt["job_id"] == "job_ok" and receipt["spend_usd"] == 0.5
