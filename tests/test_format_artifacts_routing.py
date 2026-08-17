"""format_artifacts must forward ROUTING pin attribution fields to the GUI."""

from types import SimpleNamespace

from harness.state import DurableState


def _routing_artifact(**payload):
    return SimpleNamespace(
        id="art-1",
        type="ROUTING",
        confidence=1.0,
        created_by="router",
        task_id="task-1",
        payload=payload,
    )


def _verification_artifact(**payload):
    return SimpleNamespace(
        id="verify-1",
        type="verification",
        confidence=1.0,
        created_by="worker",
        task_id="task-1",
        payload=payload,
    )


def test_format_artifacts_forwards_policy_provider_adapter():
    ds = DurableState.__new__(DurableState)
    out = ds.format_artifacts([
        _routing_artifact(
            model_id="agentic/meta/muse-spark-1.1",
            adapter_model_name="meta/muse-spark-1.1",
            policy="explicit_pin",
            provider="openrouter",
            adapter="agentic",
            estimated_cost_usd=0.01,
            reason="pinned by caller",
        ),
    ])
    assert len(out) == 1
    row = out[0]
    assert row["model"] == "agentic/meta/muse-spark-1.1"
    assert row["adapter_model_name"] == "meta/muse-spark-1.1"
    assert row["policy"] == "explicit_pin"
    assert row["provider"] == "openrouter"
    assert row["adapter"] == "agentic"
    assert row["detail"] == "pinned by caller"


def test_format_artifacts_omits_missing_pin_fields_as_none():
    ds = DurableState.__new__(DurableState)
    out = ds.format_artifacts([
        _routing_artifact(model_id="cheap-model", estimated_cost_usd=0.02),
    ])
    row = out[0]
    assert row["policy"] is None
    assert row["provider"] is None
    assert row["adapter"] is None
    assert row["adapter_model_name"] is None


def test_format_failed_verification_projects_bounded_redacted_source_reason():
    ds = DurableState.__new__(DurableState)
    out = ds.format_artifacts([
        _verification_artifact(
            result="failed",
            failure="codex_turn_failed",
            turn_failure_message="Spend cap reached token=super-secret-value " + ("x" * 600),
            message="lower-priority message",
            stderr="lower-priority stderr",
        ),
    ])

    row = out[0]
    assert row["failure"] == "codex_turn_failed"
    assert row["detail"].startswith("Spend cap reached token=REDACTED")
    assert "super-secret-value" not in row["detail"]
    assert "lower-priority" not in row["detail"]
    assert len(row["detail"]) == 500


def test_format_failed_verification_falls_back_to_message_then_stderr():
    ds = DurableState.__new__(DurableState)
    message_row, stderr_row = ds.format_artifacts([
        _verification_artifact(result="failed", failure="no_model", message="No eligible model"),
        _verification_artifact(result="blocked", failure="auth_failure", stderr="Provider login required"),
    ])

    assert message_row["detail"] == "No eligible model"
    assert stderr_row["detail"] == "Provider login required"
