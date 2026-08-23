"""Receipt-first spend for local jobs and consumed PM cost reports (#102)."""
from __future__ import annotations

from harness.financial_receipt import (
    LABEL_ESTIMATED,
    LABEL_FORECAST,
    LABEL_SAVINGS,
    LABEL_UNAVAILABLE,
    apply_local_receipt,
    build_local_financial_receipt,
    consume_pm_cost_report,
    format_job_identifier,
    format_savings_label,
    format_spend_label,
    project_historical_one_worker_spend,
)
from harness.local_job_swarm_view import project_local_job_for_swarm_live


def test_job_identifier_is_copyable_and_neutral():
    assert format_job_identifier("local-e0d790a9") == "Job local-e0d790a9"
    assert format_job_identifier("job_41123fa7ff1b") == "Job job_41123fa7ff1b"


def test_labels_name_their_basis():
    assert format_spend_label(0.1095, "estimated").startswith(LABEL_ESTIMATED)
    assert "~$" in format_spend_label(0.1095, "estimated")
    assert format_spend_label(0.02, "provider").startswith("Provider-reported")
    assert "~" not in format_spend_label(0.02, "provider")
    assert format_spend_label(None, "unavailable") == LABEL_UNAVAILABLE
    assert format_savings_label(0.0278).startswith(LABEL_SAVINGS)


def test_local_one_worker_receipt_equals_job_and_worker():
    job = {
        "id": "local-abc",
        "est_cost_usd": 0.0185,
        "artifacts": [{
            "type": "ROUTING",
            "est_cost_usd": 0.0185,
        }],
        "routing_saved_usd": 0.0278,
        "tasks": [{"id": "local-abc-w0", "status": "completed"}],
    }
    receipt = build_local_financial_receipt(
        "local-abc",
        spend_usd=0.1095,
        estimated=True,
        cost_provenance="static",
        tokens=10948,
        artifacts=job["artifacts"],
        routing_saved_usd=0.0278,
    )
    apply_local_receipt(job, receipt)
    assert job["est_cost_usd"] == job["tasks"][0]["est_cost_usd"] == 0.1095
    assert job["financial_receipt"]["route_forecast_usd"] == 0.0185
    assert job["financial_receipt"]["spend_usd"] == 0.1095
    assert job["financial_receipt"]["spend_usd"] != job["financial_receipt"]["route_forecast_usd"]
    assert job["financial_receipt"]["savings_label"].startswith(LABEL_SAVINGS)
    assert job["financial_receipt"]["forecast_label"].startswith(LABEL_FORECAST)


def test_historical_one_worker_reuses_job_estimate_not_forecast():
    row = project_local_job_for_swarm_live({
        "id": "local-e0d790a9",
        "goal": "edit",
        "status": "completed",
        "est_cost_usd": 0.1095,
        "estimated": True,
        "tokens": 10948,
        "artifacts": [{"type": "ROUTING", "est_cost_usd": 0.0185}],
        "tasks": [{"id": "local-e0d790a9-w0", "status": "completed", "role": "implement"}],
    })
    assert row["tasks"][0]["est_cost_usd"] == 0.1095
    assert row["tasks"][0]["est_cost_usd"] != 0.0185
    assert row["est_cost_usd"] == row["tasks"][0]["est_cost_usd"]


def test_multi_worker_without_task_receipts_does_not_invent_split():
    job = {
        "id": "local-multi",
        "est_cost_usd": 0.40,
        "estimated": True,
        "tokens": 20000,
        "tasks": [
            {"id": "w0", "status": "completed"},
            {"id": "w1", "status": "completed"},
        ],
    }
    receipt = build_local_financial_receipt(
        "local-multi",
        spend_usd=0.40,
        estimated=True,
        tokens=20000,
    )
    apply_local_receipt(job, receipt)
    assert job["est_cost_usd"] == 0.40
    assert "est_cost_usd" not in job["tasks"][0]
    assert "est_cost_usd" not in job["tasks"][1]
    projected = project_historical_one_worker_spend(job)
    assert "est_cost_usd" not in projected["tasks"][0]


def test_consume_pm_report_does_not_use_preflight_as_spend():
    report = {
        "job_id": "job_41123fa7ff1b",
        "cost_basis": "preflight_routing_estimate",
        "total_estimated_cost_usd": 0.0185,
        "actual_cost": {
            "cost_basis": "measured_usage_x_registry_price",
            "total_marginal_cost_usd": 0.1095,
            "measured_cost_usd": 0.1095,
            "estimated_cost_usd": 0.0,
            "measured_runs": 1,
            "estimated_runs": 0,
            "priced_tasks": 1,
            "unpriced_tasks": 0,
            "by_model": {"m": {"billing": "metered", "calls": 1, "tokens_in": 1, "tokens_out": 1, "marginal_cost_usd": 0.1095}},
            "tasks": [],
        },
        "counterfactual": {"avoided_usd": 0.0278, "reference_model_id": "flagship"},
    }
    receipt = consume_pm_cost_report(report)
    assert receipt["identifier"] == "Job job_41123fa7ff1b"
    assert receipt["spend_usd"] == 0.1095
    assert receipt["route_forecast_usd"] == 0.0185
    assert receipt["spend_usd"] != receipt["route_forecast_usd"]
    assert receipt["estimated_savings_usd"] == 0.0278
    assert receipt["savings_label"].startswith(LABEL_SAVINGS)
    assert receipt["pm_report"] is report


def test_finish_local_job_stamps_matching_job_and_worker_receipt(tmp_path):
    from harness.config import HarnessConfig
    from harness.conversation import ConversationalSession

    sess = ConversationalSession(HarnessConfig(state_dir=str(tmp_path)))
    sess._register_local_job("local-fin", "do work", role="implement")
    sess._finish_local_job(
        "local-fin",
        ok=True,
        summary="done",
        tokens=10948,
        est_cost_usd=0.1095,
        engine="agentic",
        model="cheap-model",
    )
    job = sess._local_jobs["local-fin"]
    receipt = job["financial_receipt"]
    assert receipt["identifier"] == "Job local-fin"
    assert receipt["spend_usd"] == job["est_cost_usd"] == job["tasks"][0]["est_cost_usd"]
    assert receipt["spend_usd"] == 0.1095
    assert receipt["spend_basis"] == "provider"
    assert job["tasks"][0]["cost_provenance"] == "provider"
