"""A synchronous run_swarm carries its Puppetmaster cost receipt.

ConversationalSession.state() is the UI status string, not the ledger. The
receipt read used it, so the store was never found and no receipt was sent.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from harness.pilot import PilotAction
from harness.send_loop_dispatch import dispatch_swarm_action


def test_sync_swarm_attaches_the_pm_cost_receipt(monkeypatch):
    import harness.financial_receipt as receipts
    import harness.send_loop_dispatch as dispatch

    result = SimpleNamespace(
        job_id="job_ok", adapter="agentic", mode="swarm", num_artifacts=1,
        artifact_types=["finding"], auth_failure="", summary="1 finding",
        artifacts=[{"type": "finding", "headline": "harness/keys.py line 40 drops keys",
                    "body": "harness/keys.py reads only state keys.json; see harness/keys.py line 40. " * 3}],
    )
    store = SimpleNamespace(list_artifacts=lambda job_id: [])
    session = SimpleNamespace(
        config=SimpleNamespace(repo="/repo/wiki"),
        _session_job_ids=[],
        _register_local_job=MagicMock(),
        _finish_local_job=MagicMock(),
        _append_action_result=MagicMock(),
        _display_transcript=[],
        state=lambda: "idle",
        durable=SimpleNamespace(store=store, format_artifacts=lambda rows: rows),
    )
    loaded = []

    def load(pm_store, job_id):
        loaded.append((pm_store, job_id))
        return {"job_id": job_id, "actual_cost": {"measured_cost_usd": 0.25, "measured_runs": 1, "priced_tasks": 1,
                                                  "total_marginal_cost_usd": 0.25}}

    monkeypatch.setattr(receipts, "load_pm_cost_report", load)
    monkeypatch.delenv("HARNESS_ALLOW_DEMO_SWARM", raising=False)
    monkeypatch.setattr(dispatch, "_non_git_workspace_error", lambda *_a, **_k: None)
    monkeypatch.setattr(dispatch, "stream_swarm", lambda session, intent, q, *_a, **_k: q.put(("done", result)))

    events = list(dispatch_swarm_action(
        session, PilotAction(kind="run_swarm", goal="audit keys", roles=["explore"]), "a-1", True,
        counters={"swarms": 0, "demo_swarms": 0}, turn_findings=[],
    ))

    assert loaded == [(store, "job_ok")]
    action_receipt = next(e for e in events if e.kind == "action_result").data["financial_receipt"]
    badge_receipt = next(e for e in events if e.kind == "swarm_result").data["result"]["financial_receipt"]
    assert action_receipt == badge_receipt
    assert action_receipt["job_id"] == "job_ok" and action_receipt["owner"] == "pm"
    assert action_receipt["spend_usd"] == 0.25
