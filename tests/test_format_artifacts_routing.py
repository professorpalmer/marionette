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


def test_format_artifacts_forwards_policy_provider_adapter():
    ds = DurableState.__new__(DurableState)
    out = ds.format_artifacts([
        _routing_artifact(
            model_id="meta/muse-spark-1.1",
            policy="explicit_pin",
            provider="openrouter",
            adapter="agentic",
            estimated_cost_usd=0.01,
            reason="pinned by caller",
        ),
    ])
    assert len(out) == 1
    row = out[0]
    assert row["model"] == "meta/muse-spark-1.1"
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
