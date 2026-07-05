"""Focused tests for the /api/swarm/cancel job-membership check.

The cancel handler in harness/server.py decides whether a job_id is "known"
by scanning the durable store's job list. The scan was changed from a
re-scanning ``any(j.get("id") == job_id for j in jobs)`` to a set built once:

    job_ids = {j.get("id") for j in state_obj.list_jobs()}
    known = job_id in job_ids

These tests pin the semantics of that membership check: a job present in the
list resolves as known, an absent one as unknown, and malformed rows (missing
"id") never raise and never spuriously match. The handler itself is embedded in
a large do_POST and needs a full session/store to exercise end-to-end, so we
assert the pure set-membership logic that the handler relies on.
"""


def _known(job_id, jobs):
    """Mirror of the handler's membership check (build a set once, then test)."""
    job_ids = {j.get("id") for j in jobs}
    return job_id in job_ids


def test_known_job_id_is_identified():
    jobs = [{"id": "job-a"}, {"id": "job-b"}, {"id": "job-c"}]
    assert _known("job-b", jobs) is True


def test_unknown_job_id_is_rejected():
    jobs = [{"id": "job-a"}, {"id": "job-b"}]
    assert _known("job-zzz", jobs) is False


def test_empty_job_list_means_unknown():
    assert _known("anything", []) is False


def test_rows_without_id_do_not_match_and_do_not_raise():
    # Malformed rows (no "id" key) map to None in the set; a real job_id must
    # not accidentally match, and building the set must not raise.
    jobs = [{}, {"goal": "x"}, {"id": "real-job"}]
    assert _known("real-job", jobs) is True
    assert _known("", jobs) is False


def test_matches_original_any_semantics():
    # The set-membership optimization must be equivalent to the prior any(...).
    jobs = [{"id": "a"}, {"id": "b"}, {}, {"id": "c"}]
    for candidate in ("a", "b", "c", "d", "", None):
        original = any(j.get("id") == candidate for j in jobs)
        assert _known(candidate, jobs) == original
