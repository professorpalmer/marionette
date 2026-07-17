"""Focused unit tests for .cursor/rules/state-scoping.mdc invariants."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from harness.api.jobs import JobServices, get_artifacts, post_swarm_cancel
from harness.api.sessions import handle_session_delete, remove_session_transcript
from harness.api.workspace import _persistable_recent_path, record_recent_workspace
from harness.server import _job_status_is_terminal
from harness.sessions import SessionStore, _is_ephemeral_root, save_transcript


def _noop(*_a, **_k):
    return None


def _job_services(*, get_pilot, get_session, cfg_repo: str = "/repo") -> JobServices:
    return JobServices(
        cfg=SimpleNamespace(repo=cfg_repo),
        sessions=SimpleNamespace(),
        get_pilot=get_pilot,
        get_session=get_session,
        diag=_noop,
        scoped_jobs_snapshot=lambda **_k: [],
        scoped_jobs_with_stores=lambda **_k: ([], None, None),
        retry_on_locked=lambda fn: fn(),
        swarm_registry=lambda: [],
        job_status_is_terminal=lambda _s: False,
        slim_swarm_list_artifacts=lambda *_a, **_k: [],
        job_swarm_accounting=lambda *_a, **_k: (0, 0, 0),
        task_swarm_accounting=lambda *_a, **_k: {},
        routing_saved_usd=lambda *_a, **_k: 0.0,
        cache_saved_usd_swarm=lambda *_a, **_k: 0.0,
        tokens_cached_swarm=lambda *_a, **_k: 0,
        job_dead_run_failure=lambda *_a, **_k: None,
        job_savings_fields=lambda *_a, **_k: {},
        repo_session_stamped_meters=lambda *_a, **_k: {},
        session_cost_split=lambda *_a, **_k: 0.0,
        cache_savings=lambda *_a, **_k: 0.0,
        tool_output_savings_fields=lambda *_a, **_k: {},
        cost_source_label=lambda *_a, **_k: "",
    )


class _FakeStore:
    def __init__(self, jobs, *, cancelable: bool = True):
        self._jobs = list(jobs)
        self.cancelled: list[str] = []
        self._cancelable = cancelable

    def list_jobs(self):
        return list(self._jobs)

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
    def __init__(self, local_ids=None):
        self._local_ids = set(local_ids or [])

    def cancel_local_job(self, job_id: str) -> bool:
        return job_id in self._local_ids


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
    harness = _FakeStore([{"id": "harness-job"}])

    class _CliStore(_FakeStore):
        def list_artifacts(self, job_id: str):
            if job_id == "cli-job":
                return [{"type": "verification", "payload": {"check": "ok"}}]
            return []

    cli_store = _CliStore([{"id": "cli-job"}])

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


def test_unknown_job_is_unknown_for_cancel_and_empty_for_artifacts(monkeypatch):
    harness = _FakeStore([])
    cli_store = _FakeStore([])

    class _HarnessState(_FakeState):
        def job_artifacts(self, _job_id: str):
            return []

    monkeypatch.setattr(
        "harness.cli_job_merge.open_cli_durable_state",
        lambda _repo="": SimpleNamespace(store=cli_store, job_artifacts=lambda _j: []),
    )
    svc = _job_services(
        get_pilot=lambda: _FakePilot(),
        get_session=lambda: _FakeSession(_HarnessState(harness)),
    )
    code, body = post_swarm_cancel({"job_id": "missing"}, svc)
    assert code == 404
    art_code, arts = get_artifacts("missing", svc)
    assert art_code == 200
    assert arts == []


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
    transcript = state_dir / "transcripts" / f"{sid}.json"
    assert transcript.is_file()

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


def test_remove_session_transcript_is_idempotent(tmp_path):
    state_dir = str(tmp_path)
    remove_session_transcript("no-such-sid", state_dir=state_dir)
    save_transcript(state_dir, "abc", {"display": []})
    path = tmp_path / "transcripts" / "abc.json"
    assert path.is_file()
    remove_session_transcript("abc", state_dir=state_dir)
    assert not path.exists()
    remove_session_transcript("abc", state_dir=state_dir)  # no raise
