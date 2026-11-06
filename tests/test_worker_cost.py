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

def test_provider_worker_reports_tokens_in_split():
    """Regression for audit finding #5: prompt tokens must survive to the result.
    The inner session meters _tokens_in (prompt) and _tokens_used (in+out total);
    run() must split them so tokens_in is not silently dropped and tokens_out is
    only the completion share."""
    budget = AutoBudget(max_tokens=10_000)
    worker = ProviderWorker("/tmp/does-not-matter", "goal", budget=budget)
    budget.tokens_used = 1000  # total metered (in + out)
    worker._session_tokens_in = 300
    worker._run_impl = lambda: WorkerResult(ok=True, summary="done")
    result = worker.run()
    assert result.tokens_in == 300
    assert result.tokens_out == 700  # 1000 total - 300 prompt


def test_provider_worker_preserves_explicit_tokens_in():
    """If the inner result already carries tokens_in (agentic engine set it),
    the wrapper must not overwrite it."""
    budget = AutoBudget(max_tokens=10_000)
    worker = ProviderWorker("/tmp/does-not-matter", "goal", budget=budget)
    budget.tokens_used = 500
    worker._session_tokens_in = 999  # must be ignored -- result already has one
    worker._run_impl = lambda: WorkerResult(ok=True, tokens_in=120, tokens_out=380)
    result = worker.run()
    assert result.tokens_in == 120
    assert result.tokens_out == 380


@pytest.mark.parametrize("artifacts,sum_in,sum_out,tokens_used", [
    pytest.param(
        [
            {"task_id": "task_1", "tokens_in": 100, "tokens_out": 20},
            {"task_id": "task_2", "tokens_in": 150, "tokens_out": 30},
        ],
        250, 50, 300, id="basic",
    ),
    pytest.param(
        [
            {"payload": {"task_id": "task_1", "tokens_in": 120, "tokens_out": 15}},
            {"payload": {"task_id": "task_2", "tokens_in": 80, "tokens_out": 10}},
        ],
        200, 25, 225, id="nested",
    ),
    # One task_id counts once, with the maximum tokens seen.
    pytest.param(
        [
            {"task_id": "task_1", "tokens_in": 100, "tokens_out": 20},
            {"payload": {"task_id": "task_1", "tokens_in": 150, "tokens_out": 20}},
            {"task_id": "task_2", "tokens_in": 50, "tokens_out": 5},
        ],
        200, 25, 225, id="dedupe_task_id",
    ),
    # Without a task_id, rows with the same token values count once.
    pytest.param(
        [
            {"tokens_in": 100, "tokens_out": 20},
            {"tokens_in": 100, "tokens_out": 20},
            {"tokens_in": 50, "tokens_out": 5},
        ],
        150, 25, 175, id="no_task_id",
    ),
    # Old worker artifacts have no tokens_cached key: it counts as 0.
    pytest.param(
        [
            {"task_id": "t1", "tokens_in": 100, "tokens_out": 10},
            {"payload": {"task_id": "t2", "tokens_in": 200, "tokens_out": 20}},
        ],
        300, 30, 330, id="mixed_shapes_without_tokens_cached",
    ),
])
def test_add_worker_tokens_from_artifacts(artifacts, sum_in, sum_out, tokens_used):
    cfg = HarnessConfig()
    session = ConversationalSession(cfg)
    assert session._tokens_used == 0

    got_in, got_out, got_cached = session._add_worker_tokens_from_artifacts(artifacts)
    assert got_in == sum_in
    assert got_out == sum_out
    assert got_cached == 0
    assert session._tokens_cached == 0
    assert session._tokens_used == tokens_used


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
    sum_in, sum_out, sum_cached = session._add_worker_tokens_from_artifacts(artifacts)
    assert sum_in == 10
    assert sum_out == 10  # from "invalid" tokens_in, 10 tokens_out
    assert sum_cached == 0
    assert session._tokens_used == 20

    # Raw JSON string input
    json_str = '[{"task_id": "task_json", "tokens_in": 40, "tokens_out": 5}]'
    sum_in2, sum_out2, sum_cached2 = session._add_worker_tokens_from_artifacts(json_str)
    assert sum_in2 == 40
    assert sum_out2 == 5
    assert sum_cached2 == 0
    assert session._tokens_used == 65

    # Invalid JSON string should not crash
    sum_in3, sum_out3, sum_cached3 = session._add_worker_tokens_from_artifacts("{invalid-json}")
    assert sum_in3 == 0
    assert sum_out3 == 0
    assert sum_cached3 == 0
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

    sum1_in, sum1_out, sum1_cached = session._add_worker_tokens_from_artifacts(job1_artifacts)
    sum2_in, sum2_out, sum2_cached = session._add_worker_tokens_from_artifacts(job2_artifacts)
    sum3_in, sum3_out, sum3_cached = session._add_worker_tokens_from_artifacts(job3_artifacts)

    assert sum1_in == 100
    assert sum1_out == 10
    assert sum1_cached == 0
    assert sum2_in == 200
    assert sum2_out == 20
    assert sum2_cached == 0
    assert sum3_in == 300
    assert sum3_out == 30
    assert sum3_cached == 0

    assert session._tokens_used == 660


def test_shared_child_budget_noop_attempt_reports_zero_output():
    """Ambient child with zero delta must not inherit cumulative parent spend.

    First attempt spends 30 (10 in → 20 out); a no-op second attempt reports 0
    output while the parent cumulative total stays at 30.
    """
    from harness.worker import ambient_budget

    parent = AutoBudget(max_tokens=10_000).start()

    def run_attempt(goal: str, tokens_in: int, total_spend: int) -> WorkerResult:
        worker = ProviderWorker("/tmp/repo", goal)

        def _run_impl() -> WorkerResult:
            if total_spend:
                worker.budget.add_tokens(total_spend)
            worker._session_tokens_in = tokens_in
            return WorkerResult(ok=True, summary="done")

        worker._run_impl = _run_impl
        return worker.run()

    with ambient_budget(parent):
        first = run_attempt("first", tokens_in=10, total_spend=30)
        second = run_attempt("second", tokens_in=0, total_spend=0)

    assert first.tokens_in == 10
    assert first.tokens_out == 20
    assert second.tokens_in == 0
    assert second.tokens_out == 0
    assert parent.tokens_used == 30
