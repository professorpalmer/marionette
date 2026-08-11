"""Post-Stop cooperative quarantine for implement/local disk mutations.

After interrupt/Stop, abandoned in-process write/edit/patch paths must refuse
further live-worktree mutations (fail-closed on the existing cancel Event /
stop-idle flags). Owned process groups stay PG-killed; this covers the
cooperative Python-thread gap.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace
from pathlib import Path

import pytest

from harness.busy_control import BusyControlMixin
from harness.conversation import ConversationalSession
from harness.conversation_jobs import ConversationJobsMixin
from harness.pilot import PilotAction
from harness.tool_dispatch import ToolDispatchMixin


class _QuarantineHost(BusyControlMixin, ToolDispatchMixin, ConversationJobsMixin):
    def __init__(self, repo: str):
        self.config = SimpleNamespace(repo=repo)
        self.harness_session_id = "sess-quarantine-test"
        self._cancel = threading.Event()
        self._interrupt_requested = False
        self._stop_holds_idle = False
        self._steer_boundary_drop_on_acquire = False
        self._interrupted_swarms = False
        self._state = "executing"
        self._busy = threading.Lock()
        self._busy.acquire()
        self._busy_since = 1.0
        self._busy_gen = 1
        self._busy_meta = threading.Lock()
        self._local_jobs_lock = threading.Lock()
        self._local_jobs = {
            "local-implement": {
                "id": "local-implement",
                "status": "running",
                "role": "implement",
            },
        }
        self._local_job_cancels = {
            "local-implement": threading.Event(),
        }
        self._session_job_ids = []
        self.cancelled_local: list[str] = []
        self._pending_owned_command_orphan_notice = None
        self._display_transcript: list = []
        self._last_checkpoint_id = None
        self._last_ast_preview = None
        # Bind the real apply gate without constructing a full session.
        self._apply_worker_patch = ConversationalSession._apply_worker_patch.__get__(
            self, _QuarantineHost,
        )

    @property
    def durable(self):
        return SimpleNamespace(store=None)

    def cancel(self) -> None:
        self._cancel.set()
        self._interrupted_swarms = True

    def cancel_local_job(self, job_id: str) -> bool:
        self.cancelled_local.append(job_id)
        ev = self._local_job_cancels.get(job_id)
        if ev is not None:
            ev.set()
        job = self._local_jobs.get(job_id)
        if not job or job.get("status") != "running":
            return False
        job["status"] = "cancelled"
        return True

    def _local_job_cancelled(self, job_id: str) -> bool:
        ev = self._local_job_cancels.get(job_id)
        return bool(ev is not None and ev.is_set())

    def _drain_session_jobs_dual_store(self, job_ids=None):
        return []

    def _kill_owned_command_procs_on_interrupt(self) -> dict:
        return {"killed": 0, "signaled": [], "orphaned": []}


@pytest.fixture
def host(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "seed.txt").write_text("before\n", encoding="utf-8")
    return _QuarantineHost(str(repo)), repo


def test_interrupt_arms_cooperative_quarantine_before_idle(host):
    session, _repo = host
    assert session._cooperative_disk_mutations_quarantined() is False
    session.interrupt()
    assert session._cancel.is_set()
    assert session._interrupt_requested is True
    assert session._stop_holds_idle is True
    assert session._cooperative_disk_mutations_quarantined() is True
    assert session._local_job_cancels["local-implement"].is_set()
    refused = session._refuse_quarantined_disk_mutation()
    assert refused is not None
    assert refused[1] == "cancelled"
    assert "cooperative quarantine" in refused[2]


def test_write_file_refuses_after_interrupt(host):
    session, repo = host
    session.interrupt()
    act = PilotAction(kind="write_file", path="seed.txt", content="after-stop\n")
    ok, status, msg = session._do_write_file(act, write=True)
    assert ok is False
    assert status == "cancelled"
    assert "cooperative quarantine" in msg
    assert (repo / "seed.txt").read_text(encoding="utf-8") == "before\n"


def test_write_file_dry_run_still_validates_after_interrupt(host):
    session, repo = host
    session.interrupt()
    act = PilotAction(kind="write_file", path="seed.txt", content="after-stop\n")
    ok, status, val = session._do_write_file(act, write=False)
    assert ok is True and status == "success" and val == 0
    assert (repo / "seed.txt").read_text(encoding="utf-8") == "before\n"


def test_edit_file_refuses_after_interrupt(host):
    session, repo = host
    session.interrupt()
    act = PilotAction(
        kind="edit_file",
        path="seed.txt",
        old_str="before\n",
        new_str="after-stop\n",
    )
    ok, status, msg = session._do_edit_file(act, write=True)
    assert ok is False
    assert status == "cancelled"
    assert "cooperative quarantine" in msg
    assert (repo / "seed.txt").read_text(encoding="utf-8") == "before\n"


def test_apply_worker_patch_refuses_when_job_cancelled(host, monkeypatch):
    session, repo = host
    session.cancel_local_job("local-implement")
    assert session._local_job_cancelled("local-implement") is True

    # If quarantine were skipped, git apply would run — stub to prove we never reach it.
    def boom(*_a, **_k):
        raise AssertionError("git apply must not run after job cancel")

    monkeypatch.setattr("subprocess.run", boom)

    artifacts = [{
        "type": "patch",
        "payload": {
            "unified_diff": (
                "diff --git a/seed.txt b/seed.txt\n"
                "--- a/seed.txt\n"
                "+++ b/seed.txt\n"
                "@@ -1 +1 @@\n"
                "-before\n"
                "+after-stop\n"
            ),
            "files": ["seed.txt"],
        },
    }]
    applied, files, msg = session._apply_worker_patch(
        artifacts, "local-implement",
    )
    assert applied is False
    assert files == []
    assert "cooperative quarantine" in msg
    assert (repo / "seed.txt").read_text(encoding="utf-8") == "before\n"


def test_automatic_patch_apply_refused_on_session_quarantine(host):
    session, _repo = host
    assert session._automatic_patch_apply_refused("local-implement") is None
    session.interrupt()
    refused = session._automatic_patch_apply_refused("local-implement")
    assert refused is not None
    assert "cooperative quarantine" in refused


def test_new_turn_clears_quarantine_so_writes_resume(host):
    session, repo = host
    session.interrupt()
    assert session._cooperative_disk_mutations_quarantined() is True
    # Mirror send() ownership: clear cancel then acquire-time mark.
    session._cancel.clear()
    session._mark_busy_acquired()
    assert session._cooperative_disk_mutations_quarantined() is False
    act = PilotAction(kind="write_file", path="seed.txt", content="next-turn\n")
    ok, status, _val = session._do_write_file(act, write=True)
    assert ok is True and status == "success"
    assert (repo / "seed.txt").read_text(encoding="utf-8") == "next-turn\n"
