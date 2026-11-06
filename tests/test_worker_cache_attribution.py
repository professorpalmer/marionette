"""Regression tests for worker/swarm token attribution to the parent session.

A background worker's spend (in, out and cached prompt tokens) must reach the
parent session meters on the patch path AND on the 'no changes produced' path.
server.py's cache_savings_usd reads _tokens_cached off the pilot, and cost
prices _tokens_out at the output rate, so a dropped counter hides real spend.

These tests exercise:
  (a) WorkerResult carries a tokens_cached field with a safe default,
  (b) the real background worker branches (patch and no patch) add the
      worker's in, out and cached tokens to the parent meters without
      adding cached tokens to _tokens_used a second time,
  (c) _add_worker_tokens_from_artifacts sums tokens_cached across artifacts
      and feeds the parent's _tokens_cached (not _tokens_used).
"""

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.worker import WorkerResult


def test_worker_result_carries_tokens_cached_field():
    """The dataclass must expose tokens_cached with a safe default (0) so
    positional construction and older callers keep working, and must round-trip
    an explicit value."""
    # Default
    r_default = WorkerResult(ok=True)
    assert r_default.tokens_cached == 0

    # Explicit
    r_explicit = WorkerResult(ok=True, tokens_cached=123)
    assert r_explicit.tokens_cached == 123


def _run_worker(monkeypatch, tmp_path, result):
    """Drive the real background worker branch. Stub only the edit engine,
    the git status probe and the git patch apply."""
    session = ConversationalSession(
        HarnessConfig(driver="stub-oracle-v2", state_dir=str(tmp_path))
    )
    job_id = "job_attr"
    session._register_local_job(
        job_id, goal="edit a.py", role="implement", engine="native",
    )
    monkeypatch.setattr(
        "harness.worktree_seed._list_git_status_porcelain_paths",
        lambda _repo: [],
    )
    monkeypatch.setattr(session, "_run_edit_worker_bounded", lambda *a, **k: result)
    monkeypatch.setattr(
        session, "_apply_worker_patch", lambda *a, **k: (False, [], "stub apply"),
    )
    session._run_provider_worker_background(job_id, "edit a.py")
    return session


def test_success_patch_attribution_meters_parent_tokens(monkeypatch, tmp_path):
    """The patch branch adds in, out and cached to the parent meters.
    Cached tokens are a subset of tokens_in, so _tokens_used is in + out."""
    res = WorkerResult(
        ok=True,
        patch="--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n",
        files_changed=["a.py"],
        tokens_in=1000,
        tokens_out=200,
        tokens_cached=750,
    )
    s = _run_worker(monkeypatch, tmp_path, res)
    assert (s._tokens_in, s._tokens_out, s._tokens_cached, s._tokens_used) == (
        1000, 200, 750, 1200,
    )


def test_no_patch_attribution_meters_parent_tokens(monkeypatch, tmp_path):
    """A worker with no patch still spent tokens. The failure branch meters
    them on the session and on the queued job result."""
    res = WorkerResult(
        ok=False,
        error="no changes produced",
        summary="no changes produced",
        tokens_in=400,
        tokens_out=50,
        tokens_cached=300,
    )
    s = _run_worker(monkeypatch, tmp_path, res)
    assert (s._tokens_in, s._tokens_out, s._tokens_cached, s._tokens_used) == (
        400, 50, 300, 450,
    )
    item = s._swarm_results.get_nowait()["result"]
    assert (item["tokens_in"], item["tokens_out"], item["tokens_cached"]) == (
        400, 50, 300,
    )


def test_add_worker_tokens_from_artifacts_sums_tokens_cached():
    """Multi-worker artifact summing must round-trip tokens_cached into the
    parent's _tokens_cached (not _tokens_used) so cache reads inside spawned
    puppetmaster jobs show up in the parent session's cache_savings_usd."""
    cfg = HarnessConfig()
    session = ConversationalSession(cfg)

    artifacts = [
        {"task_id": "t1", "tokens_in": 500, "tokens_out": 60, "tokens_cached": 400},
        {"payload": {"task_id": "t2", "tokens_in": 300, "tokens_out": 40, "tokens_cached": 250}},
    ]
    sum_in, sum_out, sum_cached = session._add_worker_tokens_from_artifacts(artifacts)

    assert sum_in == 800
    assert sum_out == 100
    assert sum_cached == 650
    assert session._tokens_cached == 650
    # tokens_used must NOT include tokens_cached (would double-count against
    # tokens_in which already contains cached reads).
    assert session._tokens_used == 900
