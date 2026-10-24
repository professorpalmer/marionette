"""Puppetmaster stores for the worker jobs of encrypted sessions.

A worker job keeps its goal, prompts, and results in a Puppetmaster store.
For an encrypted session, each job gets its own store in the session's
sealed folder, not the shared store. Marionette deletes that store when the
job ends, after the result is in the encrypted transcript. While the job
runs, the store is plain text on disk.

A crash can leave a store. The next backend start deletes it, and a session
delete removes the whole sealed folder.

Only the agentic adapter runs: it calls HTTP providers and keeps all job
data in the store. CLI adapters (Codex, Claude Code, Cursor) keep their own
plain history. Flow runs keep their state after the run. Encrypted sessions
refuse both.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import threading

from . import seal

JOBS_DIR = "jobs"
FLOW_TOOLS = frozenset({"run_flow", "flow_control"})

FLOW_REFUSED = (
    "Encrypted sessions cannot run flows: a flow run keeps its state on disk "
    "after the run. Use run_swarm, run_implement, or run_parallel."
)


def adapter_refused(adapter: str) -> str:
    return (
        f"Encrypted sessions run workers only on the agentic adapter. "
        f"Adapter {adapter!r} keeps its own plain history."
    )


def is_sealed(session_id: str) -> bool:
    return bool(session_id) and seal.is_sealed(session_id)


def new_job_store(session_id: str, prefix: str) -> str:
    """Make an empty store folder for one job of a sealed session."""
    root = os.path.join(seal.sealed_data_dir(session_id), JOBS_DIR)
    os.makedirs(root, mode=0o700, exist_ok=True)
    return tempfile.mkdtemp(prefix=prefix, dir=root)


def remove_job_store(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


def remove_job_store_after(thread, path: str) -> None:
    """Delete ``path`` when ``thread`` (the job's runner) has ended.

    A runner that still writes can make the folders again, so a store is
    never deleted under a live runner.
    """
    if thread is None or not thread.is_alive():
        remove_job_store(path)
        return

    def wait_then_remove() -> None:
        thread.join()
        remove_job_store(path)

    threading.Thread(target=wait_then_remove, name="sealed-store-reaper", daemon=True).start()


def sweep_job_stores() -> int:
    """Delete the job stores of every sealed session. Return how many.

    Call this only at backend start, before a worker job can run.
    """
    base = os.path.join(seal.state_dir(), "sealed")
    try:
        sessions = os.listdir(base)
    except OSError:
        return 0
    removed = 0
    for sid in sessions:
        jobs = os.path.join(base, sid, JOBS_DIR)
        try:
            names = os.listdir(jobs)
        except OSError:
            continue
        for name in names:
            remove_job_store(os.path.join(jobs, name))
            removed += 1
    return removed
