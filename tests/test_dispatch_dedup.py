"""In-flight objective dedup guard (audit finding #2).

The "one objective -> one worker / disjoint file sets" rule used to live only in
the system prompt. These tests lock in the code-level enforcement that stops the
same objective from being dispatched to two concurrent workers, which is the
PATCH-DID-NOT-APPLY re-dispatch loop the durable memory flagged."""

import threading

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


def _session():
    cfg = HarnessConfig(driver="stub-oracle-v2")
    return ConversationalSession(cfg)


def test_claim_rejects_duplicate_until_released():
    s = _session()
    goal = "Add a retry-with-backoff helper to net.py"
    assert s._claim_objective(goal) is True      # first claim wins
    assert s._claim_objective(goal) is False     # duplicate rejected while in flight
    s._release_objective(goal)
    assert s._claim_objective(goal) is True       # legitimately dispatchable again


def test_claim_normalizes_whitespace_and_case():
    s = _session()
    assert s._claim_objective("Fix   the  Bug") is True
    # Same objective, different spacing/case -> still a collision.
    assert s._claim_objective("fix the bug") is False
    assert s._claim_objective("FIX THE BUG") is False


def test_claim_normalizes_path_separators():
    s = _session()
    assert s._claim_objective(r"Rewrite at C:\Ashita\addons\kotoba") is True
    assert s._claim_objective("Rewrite at C:/Ashita/addons/kotoba.") is False


def test_empty_objective_is_never_deduped():
    s = _session()
    # Nothing meaningful to collide on -- empty/whitespace claims always succeed.
    assert s._claim_objective("") is True
    assert s._claim_objective("") is True
    assert s._claim_objective("   ") is True


def test_release_unknown_objective_is_safe():
    s = _session()
    s._release_objective("never claimed")  # must not raise


def test_concurrent_claims_have_exactly_one_winner():
    s = _session()
    goal = "Refactor the token accounting path"
    results = []
    results_lock = threading.Lock()
    start = threading.Event()

    def worker():
        start.wait()
        won = s._claim_objective(goal)
        with results_lock:
            results.append(won)

    threads = [threading.Thread(target=worker) for _ in range(32)]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join()

    assert sum(1 for r in results if r) == 1  # exactly one thread claimed it
    assert len(results) == 32


def test_external_then_local_same_goal_second_claim_fails():
    """Regression: twin run_implement (cursor/external + local agentic) used to
    both dispatch because only the local path claimed. Shared claim must block
    the second path the way run_implement now does before branching."""
    s = _session()
    goal = "Fix three bugs in backend/app/main.py that break the paste URL flow"
    # First dispatch (external PM path) claims.
    assert s._claim_objective(goal) is True
    # Second identical run_implement (local path) must be rejected.
    assert s._claim_objective(goal) is False
    s._release_objective(goal)
    assert s._claim_objective(goal) is True
