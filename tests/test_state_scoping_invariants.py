"""Focused unit tests for .cursor/rules/state-scoping.mdc invariants."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from harness.api.jobs import make_job_services, get_artifacts, post_swarm_cancel
from harness.job_scoping import job_label_for_session, stamp_task_payload
from puppetmaster.models import Artifact, ArtifactType, Task
from puppetmaster.store_factory import create_store
from harness.api.sessions import handle_session_delete, remove_session_transcript
from harness.api.workspace import _persistable_recent_path, record_recent_workspace
from harness.server import _job_status_is_terminal
from harness.sessions import SessionStore, _is_ephemeral_root, save_transcript


def _noop(*_a, **_k):
    return None


def _job_services(*, get_pilot, get_session, cfg_repo: str = "/repo"):
    return make_job_services(
        cfg=SimpleNamespace(repo=cfg_repo),
        sessions=SimpleNamespace(),
        get_pilot=get_pilot,
        get_session=get_session,
        diag=_noop,
    )


class _FakeStore:
    def __init__(self, jobs, *, cancelable: bool = True):
        self._jobs = list(jobs)
        self.cancelled: list[str] = []
        self._cancelable = cancelable

    def list_jobs(self):
        return list(self._jobs)

    def list_tasks(self, job_id: str):
        return []

    def cancel_job(self, job_id: str):
        if not self._cancelable:
            raise RuntimeError("cancel_job unavailable")
        self.cancelled.append(job_id)


class _FakeState:
    def __init__(self, store: _FakeStore):
        self.store = store

    def list_jobs(self):
        return self.store.list_jobs()


class _FakeSession:
    def __init__(self, state: _FakeState):
        self._state = state

    def state(self):
        return self._state


class _FakePilot:
    def __init__(self, local_ids=None, *, session_id="", local_jobs=None, registered=None):
        self.harness_session_id = session_id
        self._session_job_ids = list(registered or [])
        self._local_jobs: dict = {}
        for jid in local_ids or []:
            self._local_jobs[jid] = {"id": jid, "session_id": session_id}
        for job in local_jobs or []:
            self._local_jobs[job["id"]] = dict(job)

    def get_local_job(self, job_id: str):
        job = self._local_jobs.get(job_id)
        return dict(job) if job else None

    def cancel_local_job(self, job_id: str) -> bool:
        return job_id in self._local_jobs


def _track_request_cancel(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        "puppetmaster.cancellation.request_cancel",
        lambda job_id: calls.append(job_id),
    )
    return calls


# ---------------------------------------------------------------------------
# 1) Ephemeral / temp roots must not persist as boot-restorable state
# ---------------------------------------------------------------------------


def test_is_ephemeral_root_true_under_temp_when_not_pytest(tmp_path, monkeypatch):
    import harness.sessions as sessions_mod

    fake_tmp = tmp_path / "faketmp"
    temp_repo = fake_tmp / "worker-wt"
    temp_repo.mkdir(parents=True)
    real_repo = tmp_path / "real-project"
    real_repo.mkdir()

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(sessions_mod.tempfile, "gettempdir", lambda: str(fake_tmp))

    assert _is_ephemeral_root(str(temp_repo)) is True
    assert _is_ephemeral_root(str(real_repo)) is False
    assert _is_ephemeral_root("") is False


def test_is_ephemeral_root_skipped_under_pytest(tmp_path, monkeypatch):
    import harness.sessions as sessions_mod

    fake_tmp = tmp_path / "faketmp"
    temp_repo = fake_tmp / "fixture"
    temp_repo.mkdir(parents=True)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_state_scoping_invariants.py::x")
    monkeypatch.setattr(sessions_mod.tempfile, "gettempdir", lambda: str(fake_tmp))
    assert _is_ephemeral_root(str(temp_repo)) is False


def test_persistable_recent_path_rejects_temp_roots(tmp_path, monkeypatch):
    import tempfile as _tempfile

    fake_tmp = tmp_path / "faketmp"
    temp_repo = fake_tmp / "pmh-edit-xyz"
    temp_repo.mkdir(parents=True)
    real_repo = tmp_path / "user-project"
    real_repo.mkdir()

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(_tempfile, "gettempdir", lambda: str(fake_tmp))

    assert _persistable_recent_path(str(temp_repo), lambda _p: False) is False
    assert _persistable_recent_path(str(real_repo), lambda _p: False) is True
    assert _persistable_recent_path(str(real_repo), lambda _p: True) is False


def test_record_recent_scrubs_ephemeral_and_keeps_prior_repo(tmp_path, monkeypatch):
    import tempfile as _tempfile

    import harness.api.workspace as ws_api
    import harness.server as srv

    fake_tmp = tmp_path / "faketmp"
    temp_repo = fake_tmp / "tmpxyz"
    temp_repo.mkdir(parents=True)
    # Keep the "real" repo outside the faked temp tree (and off pytest tmp when
    # PYTEST_CURRENT_TEST is cleared — macOS /var/folders is itself ephemeral).
    real_repo = os.path.join(os.getcwd(), "pytest-real-repo-state-scoping")
    os.makedirs(real_repo, exist_ok=True)

    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(_tempfile, "gettempdir", lambda: str(fake_tmp))
    monkeypatch.setattr(srv, "_is_app_install_root", lambda _p: False)

    try:
        # Ensure workspace recent deps are bound (server import side-effect).
        assert ws_api._recent_deps is not None
        record_recent_workspace(real_repo)
        record_recent_workspace(str(temp_repo))

        data = json.loads((tmp_path / "workspace.json").read_text(encoding="utf-8"))
        assert data["repo"] == real_repo
        assert str(temp_repo) not in data["recents"]
        assert real_repo in data["recents"]
    finally:
        import shutil

        shutil.rmtree(real_repo, ignore_errors=True)


# ---------------------------------------------------------------------------
# 2) Active-session promotion stays same-workspace
# ---------------------------------------------------------------------------


def test_pick_next_active_stays_same_workspace(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    repo_a.mkdir()
    repo_b.mkdir()

    older_a = store.create("A-old", repo=str(repo_a), workspace_root=str(repo_a))
    store.create("B", repo=str(repo_b), workspace_root=str(repo_b))
    newer_a = store.create("A-new", repo=str(repo_a), workspace_root=str(repo_a))
    # time.time() can collide within one second; pin order for max(created).
    for row in store._sessions:
        if row["id"] == older_a["id"]:
            row["created"] = 1.0
        elif row["id"] == newer_a["id"]:
            row["created"] = 3.0
        else:
            row["created"] = 2.0

    picked = store._pick_next_active(str(repo_a))
    assert picked == newer_a["id"]
    assert picked != older_a["id"]

    # No same-workspace sibling → None (never yank to repo_b).
    assert store._pick_next_active(str(tmp_path / "missing")) is None


def test_delete_active_promotes_same_workspace_sibling_only(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    repo_a.mkdir()
    repo_b.mkdir()

    peer_a = store.create("A-peer", repo=str(repo_a), workspace_root=str(repo_a))
    store.create("B-newest-global", repo=str(repo_b), workspace_root=str(repo_b))
    active_a = store.create("A-active", repo=str(repo_a), workspace_root=str(repo_a))

    assert store.active == active_a["id"]
    new_active = store.delete(active_a["id"])
    assert new_active == peer_a["id"]


# ---------------------------------------------------------------------------
# 3) Job reads AND actions resolve the same dual store set
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _silence_request_cancel(monkeypatch):
    monkeypatch.setattr(
        "puppetmaster.cancellation.request_cancel",
        lambda _job_id: None,
    )


def test_cancel_and_artifacts_both_resolve_cli_store(monkeypatch):
    label = job_label_for_session("sess-x")
    harness = _FakeStore([{"id": "harness-job", "label": label}])

    class _CliStore(_FakeStore):
        def list_artifacts(self, job_id: str):
            if job_id == "cli-job":
                return [{"type": "verification", "payload": {"check": "ok"}}]
            return []

    cli_store = _CliStore([{"id": "cli-job", "label": label}])

    class _HarnessState(_FakeState):
        def job_artifacts(self, job_id: str):
            return []

        def format_artifacts(self, raw):
            return list(raw)

    class _CliState:
        store = cli_store

        def job_artifacts(self, job_id: str):
            return [{"type": "verification", "headline": "cli"}]

    monkeypatch.setattr(
        "harness.cli_job_merge.open_cli_durable_state",
        lambda _repo="": _CliState(),
    )
    svc = _job_services(
        get_pilot=lambda: _FakePilot(),
        get_session=lambda: _FakeSession(_HarnessState(harness)),
    )

    cancel_code, cancel_body = post_swarm_cancel({"job_id": "cli-job"}, svc)
    assert cancel_code == 200
    assert cancel_body["ok"] is True
    assert cancel_body["durable"] is True

    art_code, arts = get_artifacts("cli-job", svc)
    assert art_code == 200
    assert arts
    assert arts[0]["type"] == "verification"


def test_known_unowned_job_artifacts_and_cancel_look_unknown(monkeypatch):
    harness = _FakeStore([{"id": "foreign-row", "goal": "unstamped leftover"}])
    reads = {"harness": 0}
    tripped = _track_request_cancel(monkeypatch)

    class _HarnessState(_FakeState):
        def job_artifacts(self, job_id: str):
            reads["harness"] += 1
            return [{"type": "finding", "headline": "should not leak"}]

    monkeypatch.setattr(
        "harness.cli_job_merge.open_cli_durable_state",
        lambda _repo="": None,
    )
    svc = _job_services(
        get_pilot=lambda: _FakePilot(),
        get_session=lambda: _FakeSession(_HarnessState(harness)),
    )
    cancel_code, cancel_body = post_swarm_cancel({"job_id": "foreign-row"}, svc)
    assert cancel_code == 404
    assert cancel_body == {"ok": False, "error": "unknown job_id", "job_id": "foreign-row"}
    assert harness.cancelled == []
    assert tripped == []
    art_code, arts = get_artifacts("foreign-row", svc)
    assert art_code == 200
    assert arts == []
    assert reads["harness"] == 0


def test_known_unowned_cli_job_artifacts_and_cancel_look_unknown(monkeypatch):
    harness = _FakeStore([])
    cli_store = _FakeStore([{"id": "cli-foreign", "goal": "unstamped leftover"}])

    class _HarnessState(_FakeState):
        def job_artifacts(self, job_id: str):
            return [{"type": "finding", "headline": "should not leak"}]

    monkeypatch.setattr(
        "harness.cli_job_merge.open_cli_durable_state",
        lambda _repo="": SimpleNamespace(
            store=cli_store,
            job_artifacts=lambda _j: [{"type": "finding", "headline": "cli leak"}],
        ),
    )
    svc = _job_services(
        get_pilot=lambda: _FakePilot(),
        get_session=lambda: _FakeSession(_HarnessState(harness)),
    )
    cancel_code, cancel_body = post_swarm_cancel({"job_id": "cli-foreign"}, svc)
    assert cancel_code == 404
    assert cancel_body == {"ok": False, "error": "unknown job_id", "job_id": "cli-foreign"}
    assert cli_store.cancelled == []
    art_code, arts = get_artifacts("cli-foreign", svc)
    assert art_code == 200
    assert arts == []


def test_unknown_job_is_unknown_for_cancel_and_empty_for_artifacts(monkeypatch):
    harness = _FakeStore([])
    cli_store = _FakeStore([])
    reads = {"harness": 0, "cli": 0}
    tripped = _track_request_cancel(monkeypatch)

    class _HarnessState(_FakeState):
        def job_artifacts(self, _job_id: str):
            reads["harness"] += 1
            return [{"type": "finding", "headline": "should not leak"}]

    def _cli_artifacts(_job_id: str):
        reads["cli"] += 1
        return [{"type": "finding", "headline": "cli leak"}]

    monkeypatch.setattr(
        "harness.cli_job_merge.open_cli_durable_state",
        lambda _repo="": SimpleNamespace(store=cli_store, job_artifacts=_cli_artifacts),
    )
    svc = _job_services(
        get_pilot=lambda: _FakePilot(),
        get_session=lambda: _FakeSession(_HarnessState(harness)),
    )
    code, body = post_swarm_cancel({"job_id": "missing"}, svc)
    assert code == 404
    assert tripped == []
    art_code, arts = get_artifacts("missing", svc)
    assert art_code == 200
    assert arts == []
    assert reads["harness"] == 0
    assert reads["cli"] == 0


def _job_status(store, job_id: str) -> str:
    job = store.get_job(job_id)
    raw = getattr(job, "status", None)
    return str(getattr(raw, "value", raw) or "")


def _seed_sibling_store(tmp_path, *, owned: bool, with_artifact: bool = True):
    sibling = tmp_path / "sibling-state"
    store = create_store("sqlite", str(sibling))
    job = store.create_job("sibling job")
    payload = (
        stamp_task_payload({}, session_id="sess-x", origin="marionette")
        if owned
        else {"note": "unstamped leftover"}
    )
    task = Task(
        job_id=job.id,
        role="implement",
        instruction="do work",
        adapter="agentic",
        payload=payload,
    )
    store.save_task(task)
    store.update_job_status(job.id, "running")
    if with_artifact:
        store.save_artifact(Artifact(
            job_id=job.id,
            task_id=task.id,
            type=ArtifactType.FINDING,
            created_by="worker",
            payload={"claim": "sibling finding"},
            confidence=0.9,
            evidence=["test"],
        ))
    return store, sibling, job.id


def _sibling_action_svc(monkeypatch, sibling_dir, *, registered=None):
    monkeypatch.setenv("HARNESS_CLI_CROSS_PROJECT", "1")
    monkeypatch.setattr(
        "harness.cli_job_merge.open_cli_durable_state",
        lambda _repo="": None,
    )
    monkeypatch.setattr(
        "puppetmaster.state.list_project_state_dirs",
        lambda: [sibling_dir],
    )

    def _forbidden_dual(*_a, **_k):
        raise AssertionError("cancel_job_dual_store must not run after a primary miss")

    monkeypatch.setattr("harness.job_cancel.cancel_job_dual_store", _forbidden_dual)

    class _HarnessState(_FakeState):
        def job_artifacts(self, _job_id: str):
            return []

        def format_artifacts(self, raw):
            return list(raw)

    harness = _FakeStore([])
    pilot = _FakePilot()
    if registered is not None:
        pilot._session_job_ids = list(registered)
    return _job_services(
        get_pilot=lambda: pilot,
        get_session=lambda: _FakeSession(_HarnessState(harness)),
    ), harness


def test_foreign_sibling_artifacts_hidden(tmp_path, monkeypatch):
    store, sibling_dir, job_id = _seed_sibling_store(tmp_path, owned=False)
    svc, _harness = _sibling_action_svc(monkeypatch, sibling_dir, registered=[job_id])
    art_code, arts = get_artifacts(job_id, svc)
    assert art_code == 200
    assert arts == []
    assert store.list_artifacts(job_id)


def test_owned_sibling_task_stamp_artifacts_returned(tmp_path, monkeypatch):
    _store, sibling_dir, job_id = _seed_sibling_store(tmp_path, owned=True)
    svc, _harness = _sibling_action_svc(monkeypatch, sibling_dir)
    art_code, arts = get_artifacts(job_id, svc)
    assert art_code == 200
    assert arts
    assert any(
        "sibling finding" in str(row.get("headline") or "")
        for row in arts
        if isinstance(row, dict)
    )


def test_foreign_sibling_cancel_refused_and_artifacts_hidden(tmp_path, monkeypatch):
    store, sibling_dir, job_id = _seed_sibling_store(tmp_path, owned=False)
    svc, _harness = _sibling_action_svc(monkeypatch, sibling_dir, registered=[job_id])
    tripped = _track_request_cancel(monkeypatch)
    before = _job_status(store, job_id)
    cancel_code, cancel_body = post_swarm_cancel({"job_id": job_id}, svc)
    assert cancel_code == 404
    assert cancel_body == {"ok": False, "error": "unknown job_id", "job_id": job_id}
    assert tripped == []
    assert _job_status(store, job_id) == before
    art_code, arts = get_artifacts(job_id, svc)
    assert art_code == 200
    assert arts == []


def test_owned_sibling_cancel_succeeds_and_artifacts_returned(tmp_path, monkeypatch):
    store, sibling_dir, job_id = _seed_sibling_store(tmp_path, owned=True)
    svc, harness = _sibling_action_svc(monkeypatch, sibling_dir)
    tripped = _track_request_cancel(monkeypatch)
    art_code, arts = get_artifacts(job_id, svc)
    assert art_code == 200
    assert arts
    cancel_code, cancel_body = post_swarm_cancel({"job_id": job_id}, svc)
    assert cancel_code == 200
    assert cancel_body["ok"] is True
    assert cancel_body["marked"] is True
    assert tripped == [job_id]
    assert _job_status(store, job_id) == "cancelled"
    assert harness.cancelled == []


# ---------------------------------------------------------------------------
# 4) stall/fail/error/cancel map to terminal (harness helper)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    ["stalled", "failed", "error", "cancelled", "JobStatus.FAILED", "complete"],
)
def test_terminal_bucket_covers_stall_fail_error_cancel(status):
    assert _job_status_is_terminal(status) is True


@pytest.mark.parametrize("status", ["running", "in_progress", "pending", "queued"])
def test_non_terminal_statuses_stay_live(status):
    assert _job_status_is_terminal(status) is False


# ---------------------------------------------------------------------------
# 5) Session delete removes metadata + transcript
# ---------------------------------------------------------------------------


def test_session_delete_removes_metadata_and_transcript(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    store = SessionStore(str(tmp_path / "harness_sessions.json"))
    meta = store.create("Doomed", repo=str(tmp_path), workspace_root=str(tmp_path))
    sid = meta["id"]
    save_transcript(str(state_dir), sid, {"display": [{"role": "user", "text": "hi"}]})
    from harness.compaction_archive import append_compaction_archive, compaction_archive_path

    append_compaction_archive(str(state_dir), sid, [{"role": "user", "content": "elided"}])
    transcript = state_dir / "transcripts" / f"{sid}.json"
    archive = state_dir / "transcripts" / f"{sid}.archive.json"
    assert transcript.is_file()
    assert archive.is_file()
    assert compaction_archive_path(str(state_dir), sid) == str(archive)

    class _Runners:
        def drop(self, _sid):
            return None

    svc = SimpleNamespace(
        sessions=store,
        runners=_Runners(),
        sessions_state_dir=lambda: str(state_dir),
        get_pilot=lambda: SimpleNamespace(load_history=lambda _h: None),
        attach_view=lambda *_a, **_k: None,
        sync_pilot_session_id=lambda: None,
        diag=lambda *_a, **_k: None,
    )
    code, body = handle_session_delete(sid, svc)
    assert code == 200
    assert body["ok"] is True
    assert sid not in {s["id"] for s in store.rows()}
    assert not transcript.exists()
    assert not archive.exists()


def test_remove_session_transcript_is_idempotent(tmp_path):
    state_dir = str(tmp_path)
    remove_session_transcript("no-such-sid", state_dir=state_dir)
    save_transcript(state_dir, "abc", {"display": []})
    path = tmp_path / "transcripts" / "abc.json"
    assert path.is_file()
    remove_session_transcript("abc", state_dir=state_dir)
    assert not path.exists()
    remove_session_transcript("abc", state_dir=state_dir)  # no raise


# ---------------------------------------------------------------------------
# 6) Test-session pollution: live ~/.pmharness must stay untouched under pytest
# ---------------------------------------------------------------------------


def test_force_throwaway_harness_state_dir_overrides_live_preset(monkeypatch, tmp_path):
    """Contaminated HARNESS_STATE_DIR under ~/.pmharness must be overwritten."""
    from conftest import force_throwaway_harness_state_dir, _path_under_dir

    fake_home = tmp_path / "userhome"
    live_state = fake_home / ".pmharness" / "state"
    live_state.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(live_state))

    effective = force_throwaway_harness_state_dir()
    assert effective
    assert effective != str(live_state)
    assert not _path_under_dir(effective, str(fake_home / ".pmharness"))
    assert os.path.isdir(effective)


def test_force_throwaway_harness_state_dir_preserves_non_live_preset(
    monkeypatch, tmp_path
):
    """A non-live preset (CI / explicit temp root) must not be rewritten."""
    from conftest import force_throwaway_harness_state_dir

    preset = tmp_path / "explicit-state"
    preset.mkdir()
    monkeypatch.setenv("HARNESS_STATE_DIR", str(preset))
    assert force_throwaway_harness_state_dir() == str(preset)


def test_tmp_path_session_store_persists_under_pytest(tmp_path):
    """Defense-in-depth must not block legitimate temp SessionStore writes."""
    path = tmp_path / "harness_sessions.json"
    store = SessionStore(str(path))
    store.create(title="Cold")
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert any(s.get("title") == "Cold" for s in data.get("sessions") or [])


def test_session_store_skips_writes_into_live_pmharness_under_pytest(
    tmp_path, monkeypatch
):
    """When PYTEST_CURRENT_TEST is set, refuse writes into real ~/.pmharness."""
    fake_home = tmp_path / "userhome"
    live_state = fake_home / ".pmharness" / "state"
    live_state.mkdir(parents=True)
    durable = live_state / "harness_sessions.json"
    prior = {
        "sessions": [
            {
                "id": "keepme",
                "title": "Keep",
                "created": 1.0,
                "active": True,
                "workspace_root": str(tmp_path / "real-project"),
            }
        ],
        "active": "keepme",
    }
    durable.write_text(json.dumps(prior), encoding="utf-8")
    before = durable.read_bytes()

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv(
        "PYTEST_CURRENT_TEST",
        "test_state_scoping_invariants.py::test_session_store_skips_writes",
    )

    store = SessionStore(str(durable))
    store.create(title="Cold")
    assert durable.read_bytes() == before
    assert "Cold" not in durable.read_text(encoding="utf-8")


def test_srv_sessions_create_cold_does_not_mutate_live_durable(tmp_path, monkeypatch):
    """Preset live HARNESS_STATE_DIR + srv._sessions.create('Cold') must not
    rewrite the durable harness_sessions.json under the live tree.

    Covers the audit root cause: module-global SessionStore path binding plus
    a contaminated shell env. Autouse rebind + write guard both apply.
    """
    fake_home = tmp_path / "userhome"
    live_state = fake_home / ".pmharness" / "state"
    live_state.mkdir(parents=True)
    durable = live_state / "harness_sessions.json"
    prior = {
        "sessions": [
            {
                "id": "keepme",
                "title": "Keep",
                "created": 1.0,
                "active": True,
                "workspace_root": str(tmp_path / "real-project"),
            }
        ],
        "active": "keepme",
    }
    durable.write_text(json.dumps(prior), encoding="utf-8")
    before = durable.read_bytes()

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    # Contaminated shell: looks like a post-import live anchor.
    monkeypatch.setenv("HARNESS_STATE_DIR", str(live_state))

    from conftest import _path_under_dir
    import harness.server as srv

    # Autouse fixture must have rebound off the live tree already.
    live_root = str(fake_home / ".pmharness")
    assert not _path_under_dir(srv._sessions.path, live_root)

    srv._sessions.create(title="Cold")
    assert durable.read_bytes() == before

    # Even a poisoned rebound must not mutate live durable under pytest.
    poisoned = SessionStore(str(durable))
    old = srv._sessions
    srv._sessions = poisoned
    try:
        srv._sessions.create(title="Cold")
    finally:
        srv._sessions = old
    assert durable.read_bytes() == before
