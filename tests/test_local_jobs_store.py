"""Session-partitioned local-jobs store: size bounds, scoping, no clobber, caps.

Synthetic fixtures only; never reads a real ~/.pmharness state dir.
"""
import json
import os
import threading
from types import SimpleNamespace

from harness.conversation_jobs import _worker_provenance_text
from harness.local_jobs import LocalJobsMixin
from harness.local_jobs_store import COMMAND_RECEIPT_CAP, HISTORY_CAP, local_jobs_store
from harness.provenance_sanitize import LIVE_DIRTY_SAMPLE_CAP, bound_live_dirty_provenance

DIRTY = 22_900


class Runner(LocalJobsMixin):
    def __init__(self, path, session_id="A"):
        self.config = SimpleNamespace(repo=str(path), driver="stub")
        self.state_dir = str(path)
        self._local_jobs = {}
        self._local_jobs_lock = threading.Lock()
        self._local_job_cancels = {}
        self._local_jobs_path = str(path / "swarm_local_jobs.json")
        self._display_transcript = []
        self.harness_session_id = session_id
        self._load_local_jobs()


def dirty_paths(prefix, n=DIRTY):
    return [f"{prefix}/pkg{i // 100:03d}/module_{i:05d}.py" for i in range(n)]


def analysis_row(i, session="A"):
    before = dirty_paths("src")
    after = before[:-3] + ["src/new_a.py", "src/new_b.py"]
    prov = {
        "live_dirty_paths_before": before,
        "live_dirty_paths_after": after,
        "managed_worktree_mode": "managed",
        "managed_worktree_path": f"/tmp/wt-{i}",
        "requested_mode": "analysis",
        "worktree_diff_empty": True,
    }
    return {
        "id": f"local-analysis-{i:02d}",
        "session_id": session,
        "role": "analysis",
        "status": "completed",
        "created_at": float(i),
        "updated_at": float(i),
        "tasks": [{"id": f"local-analysis-{i:02d}-w0", "status": "completed"}],
        "actions": [],
        "worker_provenance": prov,
        "artifacts": [{
            "id": f"local-analysis-{i:02d}-result",
            "type": "analysis",
            "headline": "finding",
            "worker_provenance": json.loads(json.dumps(prov)),
        }],
    }


def command_row(i, session="A", status="completed"):
    return {
        "id": f"local-cmd-{i:04d}",
        "session_id": session,
        "role": "command",
        "job_kind": "run_command",
        "status": status,
        "created_at": float(i),
        "updated_at": float(i),
        "terminal_receipt": {"status": status, "exit_code": 0} if status == "completed" else None,
        "launch_checkpoint": None,
        "tasks": [],
        "artifacts": [],
        "actions": [],
    }


def write_legacy(path, rows):
    path.write_text(json.dumps({"jobs": rows}), encoding="utf-8")


def stored(path):
    return json.loads(path.read_text(encoding="utf-8"))["jobs"]


def test_bound_provenance_keeps_counts_samples_and_delta_idempotently():
    before = dirty_paths("src")
    after = before[:-3] + ["src/new_a.py", "src/new_b.py"]
    raw = {"live_dirty_paths_before": before, "live_dirty_paths_after": after}
    bounded = bound_live_dirty_provenance(raw)
    assert bounded["dirty_count_before"] == DIRTY
    assert bounded["dirty_count_after"] == DIRTY - 1
    assert bounded["live_dirty_paths_before"] == before[:LIVE_DIRTY_SAMPLE_CAP]
    assert bounded["live_dirty_added"] == ["src/new_a.py", "src/new_b.py"]
    assert bounded["live_dirty_removed"] == before[-3:]
    assert bounded["live_dirty_added_count"] == 2
    assert bounded["live_dirty_removed_count"] == 3
    assert bound_live_dirty_provenance(bounded) == bounded
    # Pilot-facing text is unchanged by the bound.
    for expects_diff in (True, False):
        assert (_worker_provenance_text(bounded, expects_diff=expects_diff)
                == _worker_provenance_text(raw, expects_diff=expects_diff))
    # Same wording the full lists produced: exact counts, first 12 paths, "+N more".
    text = _worker_provenance_text(bounded)
    assert f"had {DIRTY} pre-existing dirty paths before ({before[0]}, " in text
    assert f", +{DIRTY - 12} more) and {DIRTY - 1} after ({after[0]}, " in text
    assert text.endswith(f", +{DIRTY - 1 - 12} more).")


def test_legacy_file_shrinks_on_next_persist(tmp_path, capsys):
    path = tmp_path / "swarm_local_jobs.json"
    rows = [analysis_row(i) for i in range(20)] + [analysis_row(20 + i, "B") for i in range(2)]
    write_legacy(path, rows)
    before_bytes = os.path.getsize(path)

    runner = Runner(tmp_path, "A")
    after_bytes = os.path.getsize(path)
    with capsys.disabled():
        print(f"\nlocal-jobs fixture: {before_bytes} bytes -> {after_bytes} bytes")
    assert before_bytes > 40 * 1024 * 1024
    assert after_bytes < 400 * 1024
    jobs = stored(path)
    assert len(jobs) == 22  # other sessions' rows are migrated, never dropped
    for job in jobs:
        prov = job["worker_provenance"]
        assert prov["dirty_count_before"] == DIRTY
        assert len(prov["live_dirty_paths_before"]) == LIVE_DIRTY_SAMPLE_CAP
        assert len(prov["live_dirty_paths_after"]) == LIVE_DIRTY_SAMPLE_CAP
        art = job["artifacts"][0]["worker_provenance"]
        assert "live_dirty_paths_before" not in art and art["dirty_count_before"] == DIRTY
    assert runner._local_jobs["local-analysis-00"]["worker_provenance"]["dirty_count_after"] == DIRTY - 1

    # Idempotent: reloading the migrated file changes nothing on disk.
    stamp = os.stat(path).st_mtime_ns
    Runner(tmp_path, "A")
    assert os.stat(path).st_mtime_ns == stamp and stored(path) == jobs


def test_finish_never_copies_path_lists_onto_the_result_artifact(tmp_path):
    runner = Runner(tmp_path, "A")
    runner._register_local_job("local-big", "audit", role="analysis",
                               cwd=str(tmp_path), engine="native", skip_routing_preview=True)
    before = dirty_paths("src")
    runner._finish_local_job("local-big", ok=True, summary="finding", worker_provenance={
        "live_dirty_paths_before": before, "live_dirty_paths_after": before,
        "worktree_diff_empty": True,
    })
    job = stored(tmp_path / "swarm_local_jobs.json")[0]
    assert job["worker_provenance"]["dirty_count_before"] == DIRTY
    assert len(job["worker_provenance"]["live_dirty_paths_before"]) == LIVE_DIRTY_SAMPLE_CAP
    art = next(a for a in job["artifacts"] if a["id"] == "local-big-result")
    assert not any(key.startswith("live_dirty_") and isinstance(value, list)
                   for key, value in art["worker_provenance"].items())
    assert len(json.dumps(job)) < 32 * 1024


def test_runner_loads_only_its_session_and_mirrors_others_read_only(tmp_path):
    path = tmp_path / "swarm_local_jobs.json"
    write_legacy(path, [command_row(1, "A"), command_row(2, "B"),
                        dict(command_row(3, "B"), status="running", terminal_receipt=None)])
    runner = Runner(tmp_path, "A")
    assert set(runner._local_jobs) == {"local-cmd-0001"}
    # Another session's rows stay visible to all-scope metadata reads...
    index = runner.local_metadata_handle()
    assert {"local-cmd-0002", "local-cmd-0003"} <= set(index.rows)
    assert set(runner._foreign_local_jobs) == {"local-cmd-0002", "local-cmd-0003"}
    # ...but are not healed by a runner that does not own them.
    by_id = {j["id"]: j for j in stored(path)}
    assert by_id["local-cmd-0003"]["status"] == "running"


def test_unbound_runner_loads_its_partition_when_bound(tmp_path):
    write_legacy(tmp_path / "swarm_local_jobs.json", [command_row(1, "A"), command_row(2, "B")])
    runner = Runner(tmp_path, "")
    assert runner._local_jobs == {}
    runner.harness_session_id = "B"
    assert set(runner._local_jobs) == {"local-cmd-0002"}
    assert set(runner._foreign_local_jobs) == {"local-cmd-0001"}


def test_concurrent_runners_never_clobber_each_other(tmp_path):
    a = Runner(tmp_path, "A")
    b = Runner(tmp_path, "B")
    a._register_local_job("local-a1", "a one", cwd=str(tmp_path), skip_routing_preview=True)
    b._register_local_job("local-b1", "b one", cwd=str(tmp_path), skip_routing_preview=True)
    a._register_local_job("local-a2", "a two", cwd=str(tmp_path), skip_routing_preview=True)
    # B's in-memory view never saw A's rows; its write must still keep them.
    b._finish_local_job("local-b1", ok=True, summary="done")
    ids = {j["id"] for j in stored(tmp_path / "swarm_local_jobs.json")}
    assert ids == {"local-a1", "local-a2", "local-b1"}
    assert set(a._local_jobs) == {"local-a1", "local-a2"}
    assert set(b._local_jobs) == {"local-b1"}

    # Parallel writers: every row from every session lands.
    def work(runner, prefix):
        for i in range(25):
            runner._register_local_job(f"{prefix}-{i}", "w", cwd=str(tmp_path), skip_routing_preview=True)
    threads = [threading.Thread(target=work, args=(r, p)) for r, p in ((a, "local-pa"), (b, "local-pb"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    ids = {j["id"] for j in stored(tmp_path / "swarm_local_jobs.json")}
    assert {f"local-pa-{i}" for i in range(25)} <= ids
    assert {f"local-pb-{i}" for i in range(25)} <= ids
    # A refreshes its all-scope mirror of B on its own persist.
    a._persist_local_jobs()
    assert "local-pb-24" in a._foreign_local_jobs
    assert "local-pb-24" in a.local_metadata_handle().rows


def test_external_write_is_merged_not_overwritten(tmp_path):
    path = tmp_path / "swarm_local_jobs.json"
    runner = Runner(tmp_path, "A")
    runner._register_local_job("local-a1", "a", cwd=str(tmp_path), skip_routing_preview=True)
    rows = stored(path) + [command_row(9, "C")]
    write_legacy(path, rows)
    runner._register_local_job("local-a2", "a", cwd=str(tmp_path), skip_routing_preview=True)
    assert {j["id"] for j in stored(path)} == {"local-a1", "local-a2", "local-cmd-0009"}


def test_terminal_command_receipts_are_capped_per_session(tmp_path):
    rows = [command_row(i, "A") for i in range(COMMAND_RECEIPT_CAP + 100)]
    rows += [dict(command_row(10_000 + i, "A"), status="running", terminal_receipt=None,
                  launch_checkpoint={"pid": 1}) for i in range(3)]
    rows += [command_row(20_000 + i, "B") for i in range(10)]
    write_legacy(tmp_path / "swarm_local_jobs.json", rows)
    runner = Runner(tmp_path, "A")
    runner._persist_local_jobs()
    jobs = stored(tmp_path / "swarm_local_jobs.json")
    mine = [j for j in jobs if j["session_id"] == "A"]
    terminal = [j for j in mine if j["status"] == "completed"]
    assert len(terminal) == COMMAND_RECEIPT_CAP
    # Newest receipts survive; unresolved rows are never pruned.
    assert min(j["updated_at"] for j in terminal) == 100.0
    assert sum(1 for j in mine if j["status"] == "unknown") == 3
    assert sum(1 for j in jobs if j["session_id"] == "B") == 10


def test_provider_history_cap_is_per_session(tmp_path):
    runner_a = Runner(tmp_path, "A")
    for i in range(HISTORY_CAP + 5):
        runner_a._local_jobs[f"local-p{i:04d}"] = dict(
            command_row(i, "A"), id=f"local-p{i:04d}", role="implement", job_kind="")
    runner_a._persist_local_jobs()
    runner_b = Runner(tmp_path, "B")
    runner_b._register_local_job("local-b", "b", cwd=str(tmp_path), skip_routing_preview=True)
    jobs = stored(tmp_path / "swarm_local_jobs.json")
    assert sum(1 for j in jobs if j["session_id"] == "A") == HISTORY_CAP
    assert "local-b" in {j["id"] for j in jobs}


def test_cold_switch_parses_only_its_own_partition(tmp_path, monkeypatch):
    rows = [analysis_row(i, "A") for i in range(3)] + [command_row(1, "B")]
    write_legacy(tmp_path / "swarm_local_jobs.json", rows)
    Runner(tmp_path, "A")
    store = local_jobs_store(str(tmp_path / "swarm_local_jobs.json"))
    import harness.local_jobs_store as module
    parsed = []
    real_loads = module.json.loads

    def counting_loads(text, *args, **kwargs):
        parsed.append(len(text))
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(module.json, "loads", counting_loads)
    rows_b, status, _ = store.load("B")
    assert status == "ok" and [r["id"] for r in rows_b] == ["local-cmd-0001"]
    # Only B's partition text is parsed, never the whole file.
    assert parsed and max(parsed) < os.path.getsize(tmp_path / "swarm_local_jobs.json") // 2


def test_failed_disk_write_leaves_cache_on_last_durable_rows(tmp_path, monkeypatch):
    from harness.local_jobs_store import LocalJobsStore
    store = LocalJobsStore(str(tmp_path / "swarm_local_jobs.json"))
    store.write("s1", [{"id": "j1", "session_id": "s1", "status": "running"}])

    def refuse(*_a, **_k):
        raise OSError("injected disk fault")

    monkeypatch.setattr("harness.local_jobs_store.os.replace", refuse)
    try:
        store.write("s1", [{"id": "j1", "session_id": "s1", "status": "completed"}])
    except OSError:
        pass
    monkeypatch.undo()
    rows, _, _ = store.load("s1")
    assert [r["status"] for r in rows] == ["running"]
