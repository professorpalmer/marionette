import pytest
import json
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.autobudget import AutoBudget
from harness.worker import ProviderWorker, WorkerResult


def test_provider_worker_stamps_tokens_out_from_budget():
    """Regression: ProviderWorker.run() must report metered token spend on EVERY
    return path (not only when routed through run_native_edit). tokens_out was
    documented but always 0, breaking cost accounting for direct callers."""
    budget = AutoBudget(max_tokens=10_000)
    worker = ProviderWorker("/tmp/does-not-matter", "goal", budget=budget)
    budget.tokens_used = 1234  # simulate metered spend during the inner run

    # Inner run returns a result that (like the real return sites) leaves
    # tokens_out unset; the run() wrapper must stamp it from the budget.
    worker._run_impl = lambda: WorkerResult(ok=True, summary="done")
    result = worker.run()
    assert result.tokens_out == 1234


def test_provider_worker_preserves_explicit_tokens_out():
    """If the inner result already carries tokens_out (e.g. the agentic engine
    set it), the wrapper must not clobber it with the budget total."""
    budget = AutoBudget(max_tokens=10_000)
    worker = ProviderWorker("/tmp/does-not-matter", "goal", budget=budget)
    budget.tokens_used = 999
    worker._run_impl = lambda: WorkerResult(ok=True, tokens_out=42)
    result = worker.run()
    assert result.tokens_out == 42

def test_add_worker_tokens_from_artifacts_basic():
    cfg = HarnessConfig()
    session = ConversationalSession(cfg)
    assert session._tokens_used == 0

    # Top-level shapes
    artifacts = [
        {"task_id": "task_1", "tokens_in": 100, "tokens_out": 20},
        {"task_id": "task_2", "tokens_in": 150, "tokens_out": 30}
    ]
    sum_in, sum_out = session._add_worker_tokens_from_artifacts(artifacts)
    assert sum_in == 250
    assert sum_out == 50
    assert session._tokens_used == 300


def test_add_worker_tokens_from_artifacts_nested():
    cfg = HarnessConfig()
    session = ConversationalSession(cfg)
    assert session._tokens_used == 0

    # Nested payload shapes
    artifacts = [
        {
            "payload": {
                "task_id": "task_1",
                "tokens_in": 120,
                "tokens_out": 15
            }
        },
        {
            "payload": {
                "task_id": "task_2",
                "tokens_in": 80,
                "tokens_out": 10
            }
        }
    ]
    sum_in, sum_out = session._add_worker_tokens_from_artifacts(artifacts)
    assert sum_in == 200
    assert sum_out == 25
    assert session._tokens_used == 225


def test_add_worker_tokens_from_artifacts_dedupe_task_id():
    cfg = HarnessConfig()
    session = ConversationalSession(cfg)
    assert session._tokens_used == 0

    # Same task_id should deduplicate and take maximum tokens seen
    artifacts = [
        {"task_id": "task_1", "tokens_in": 100, "tokens_out": 20},
        {
            "payload": {
                "task_id": "task_1",
                "tokens_in": 150,
                "tokens_out": 20
            }
        },
        {"task_id": "task_2", "tokens_in": 50, "tokens_out": 5}
    ]
    sum_in, sum_out = session._add_worker_tokens_from_artifacts(artifacts)
    assert sum_in == 200  # task_1 (150) + task_2 (50)
    assert sum_out == 25  # task_1 (20) + task_2 (5)
    assert session._tokens_used == 225


def test_add_worker_tokens_from_artifacts_no_task_id():
    cfg = HarnessConfig()
    session = ConversationalSession(cfg)
    assert session._tokens_used == 0

    # Artifacts without task_id should deduplicate by their token values to prevent double counting
    artifacts = [
        {"tokens_in": 100, "tokens_out": 20},
        {"tokens_in": 100, "tokens_out": 20},
        {"tokens_in": 50, "tokens_out": 5}
    ]
    sum_in, sum_out = session._add_worker_tokens_from_artifacts(artifacts)
    assert sum_in == 150
    assert sum_out == 25
    assert session._tokens_used == 175


def test_add_worker_tokens_from_artifacts_defensive_parsing():
    cfg = HarnessConfig()
    session = ConversationalSession(cfg)
    assert session._tokens_used == 0

    # Malformed list elements, strings, missing/invalid keys
    artifacts = [
        "not-a-dict",
        {"tokens_in": "invalid", "tokens_out": 10},
        {"payload": "invalid-payload"},
        None,
        {"task_id": "task_abc", "tokens_in": 10, "tokens_out": None}
    ]
    sum_in, sum_out = session._add_worker_tokens_from_artifacts(artifacts)
    assert sum_in == 10
    assert sum_out == 10  # from "invalid" tokens_in, 10 tokens_out
    assert session._tokens_used == 20

    # Raw JSON string input
    json_str = '[{"task_id": "task_json", "tokens_in": 40, "tokens_out": 5}]'
    sum_in2, sum_out2 = session._add_worker_tokens_from_artifacts(json_str)
    assert sum_in2 == 40
    assert sum_out2 == 5
    assert session._tokens_used == 65

    # Invalid JSON string should not crash
    sum_in3, sum_out3 = session._add_worker_tokens_from_artifacts("{invalid-json}")
    assert sum_in3 == 0
    assert sum_out3 == 0
    assert session._tokens_used == 65


def test_add_worker_tokens_from_artifacts_simulate_parallel():
    cfg = HarnessConfig()
    session = ConversationalSession(cfg)
    assert session._tokens_used == 0

    # Simulate run_parallel across 3 separate worker jobs
    job1_artifacts = [
        {"task_id": "parallel_1", "tokens_in": 100, "tokens_out": 10}
    ]
    job2_artifacts = [
        {
            "payload": {
                "task_id": "parallel_2",
                "tokens_in": 200,
                "tokens_out": 20
            }
        }
    ]
    job3_artifacts = [
        {"task_id": "parallel_3", "tokens_in": 300, "tokens_out": 30}
    ]

    sum1_in, sum1_out = session._add_worker_tokens_from_artifacts(job1_artifacts)
    sum2_in, sum2_out = session._add_worker_tokens_from_artifacts(job2_artifacts)
    sum3_in, sum3_out = session._add_worker_tokens_from_artifacts(job3_artifacts)

    assert sum1_in == 100
    assert sum1_out == 10
    assert sum2_in == 200
    assert sum2_out == 20
    assert sum3_in == 300
    assert sum3_out == 30

    assert session._tokens_used == 660
