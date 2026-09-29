"""A long-lived runner keeps no more settled job rows in memory than the store keeps on disk."""
import json
import os

from harness.local_jobs_store import HISTORY_CAP
from tests.test_command_jobs import _Session


def test_runner_forgets_settled_rows_the_store_capped(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    s = _Session(str(tmp_path), str(repo))
    total = HISTORY_CAP + 60
    for i in range(total):
        jid = f"job-{i:04d}"
        s._register_local_job(jid, f"goal {i}", skip_routing_preview=True)
        s._finish_local_job(jid, ok=True, summary="done")
    s._register_local_job("job-live", "still running", skip_routing_preview=True)
    assert len(s._local_jobs) <= HISTORY_CAP + 1
    assert "job-live" in s._local_jobs and "job-live" in s._local_job_cancels
    assert f"job-{total - 1:04d}" in s._local_jobs
    assert "job-0000" not in s._local_jobs and "job-0000" not in s._local_job_cancels
    with open(os.path.join(str(tmp_path), "swarm_local_jobs.json"), encoding="utf-8") as f:
        on_disk = {r["id"] for r in json.load(f)["jobs"]}
    assert set(s._local_jobs) == on_disk
    assert set(s._local_metadata.rows) == on_disk
