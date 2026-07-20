"""run_due proofs with injected fakes: due fires, claims, halt mapping, cancel.

No real Puppetmaster, no network -- session_factory/budget_factory are stubs,
mirroring the _fake_result / _NeverDonePilot injection style of test_auto.py.
"""
import subprocess
import time
from datetime import datetime
from pathlib import Path

import pytest

from harness.schedule_core import Schedule, fire_at_timestamp
from harness.schedule_store import ScheduleStore
from harness.scheduler import (
    Notifier,
    SchedulerDaemon,
    check_schedule_workspace,
    run_due,
    run_one_now,
)


class _Event:
    def __init__(self, kind, data):
        self.kind = kind
        self.data = data


class _FakeSession:
    """Yields two auto_status events then a terminal auto_halt, like run_auto."""

    def __init__(self, reason, cycles, tokens, swarms, *, slow=False, check_cancel=False):
        self._reason = reason
        self._cycles = cycles
        self._tokens = tokens
        self._swarms = swarms
        self._slow = slow
        self._check_cancel = check_cancel
        self._cancel = False
        self.cancel_calls = 0

    def cancel(self):
        self.cancel_calls += 1
        self._cancel = True

    def run_auto(self, objective, budget=None, *, require_codegraph=True):
        yield _Event("auto_status", {"cycle": 1, "snapshot": {
            "tokens_used": 1, "swarms_used": 0}})
        if self._slow:
            time.sleep(0.2)
        if self._check_cancel and self._cancel:
            yield _Event("auto_halt", {"reason": "cancelled", "snapshot": {
                "tokens_used": self._tokens, "swarms_used": self._swarms}})
            return
        yield _Event("auto_status", {"cycle": self._cycles, "snapshot": {
            "tokens_used": self._tokens, "swarms_used": self._swarms}})
        if self._check_cancel and self._cancel:
            yield _Event("auto_halt", {"reason": "cancelled", "snapshot": {
                "tokens_used": self._tokens, "swarms_used": self._swarms}})
            return
        yield _Event("auto_halt", {"reason": self._reason, "snapshot": {
            "tokens_used": self._tokens, "swarms_used": self._swarms}})


class _CountingNotifier(Notifier):
    def __init__(self):
        self.calls = []

    def notify(self, schedule, run):
        self.calls.append((schedule.id, run))


class _FakeBudget:
    pass


def _git_repo(tmp_path, name="repo"):
    root = tmp_path / name
    root.mkdir()
    subprocess.run(
        ["git", "init"], cwd=str(root), check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return str(root)


_OK_REASON = "pilot reports objective met (no further investigation)"


def _session_factory(reason=_OK_REASON, cycles=2, tokens=42, swarms=3, **kw):
    return lambda sched: _FakeSession(reason, cycles, tokens, swarms, **kw)


def _budget_factory(sched):
    return _FakeBudget()


def _store(tmp_path):
    return ScheduleStore(str(tmp_path / "s.sqlite"))


def _now():
    # A minute that "* * * * *" always matches.
    return datetime(2024, 1, 1, 12, 0)


def _add_due(store, tmp_path, name="due", cron="* * * * *", **kw):
    repo = kw.pop("repo", None)
    if repo is None:
        repo = _git_repo(tmp_path, name=f"repo-{name}")
    return store.add(Schedule(
        id="", name=name, objective="o", cron=cron, repo=repo, **kw,
    ))


def test_only_due_enabled_run(tmp_path):
    store = _store(tmp_path)
    due = _add_due(store, tmp_path, "due")
    # A never-firing schedule (Feb 30 does not exist) is not due.
    not_due = _add_due(store, tmp_path, "nd", cron="0 0 30 2 *")
    notifier = _CountingNotifier()

    runs = run_due(store, _now(), notifier=notifier,
                   session_factory=_session_factory(),
                   budget_factory=_budget_factory)

    real = [r for r in runs if r.get("status") != "blocked"]
    assert len(real) == 1
    assert real[0]["schedule_id"] == due.id
    assert len(store.list_runs(not_due.id)) == 0


def test_run_records_snapshot_fields(tmp_path):
    store = _store(tmp_path)
    s = _add_due(store, tmp_path, "x")
    notifier = _CountingNotifier()

    run_due(store, _now(), notifier=notifier,
            session_factory=_session_factory(reason=_OK_REASON,
                                             cycles=5, tokens=999, swarms=7),
            budget_factory=_budget_factory)

    recorded = store.list_runs(s.id)
    assert len(recorded) == 1
    r = recorded[0]
    assert r["status"] == "ok"
    assert r["halt_reason"] == _OK_REASON
    assert r["cycles"] == 5
    assert r["tokens_used"] == 999
    assert r["swarms_used"] == 7
    # last_run updated on the schedule row.
    assert store.get(s.id).last_status == "ok"
    assert store.get(s.id).last_run_at > 0
    assert store.get(s.id).last_fire_at == fire_at_timestamp(_now())


def test_disabled_skipped(tmp_path):
    store = _store(tmp_path)
    s = _add_due(store, tmp_path, "off", enabled=False)
    runs = run_due(store, _now(), notifier=_CountingNotifier(),
                   session_factory=_session_factory(),
                   budget_factory=_budget_factory)
    assert runs == []
    assert store.list_runs(s.id) == []


def test_raising_factory_isolated_as_error(tmp_path):
    store = _store(tmp_path)
    bad = _add_due(store, tmp_path, "bad")
    good = _add_due(store, tmp_path, "good")
    notifier = _CountingNotifier()

    def factory(sched):
        if sched.id == bad.id:
            raise RuntimeError("kaboom")
        return _FakeSession(_OK_REASON, 1, 5, 0)

    runs = run_due(store, _now(), notifier=notifier,
                   session_factory=factory, budget_factory=_budget_factory)

    real = [r for r in runs if r.get("status") != "blocked"]
    assert len(real) == 2  # bad one did not abort the good one
    bad_run = store.list_runs(bad.id)[0]
    assert bad_run["status"] == "error"
    assert "kaboom" in bad_run["halt_reason"]
    good_run = store.list_runs(good.id)[0]
    assert good_run["status"] == "ok"


def test_notifier_called_once_per_run(tmp_path):
    store = _store(tmp_path)
    _add_due(store, tmp_path, "a")
    _add_due(store, tmp_path, "b")
    notifier = _CountingNotifier()

    run_due(store, _now(), notifier=notifier,
            session_factory=_session_factory(),
            budget_factory=_budget_factory)

    assert len(notifier.calls) == 2


def test_same_minute_double_tick_one_run(tmp_path):
    store = _store(tmp_path)
    s = _add_due(store, tmp_path, "once")
    now = _now()
    run_due(store, now, notifier=_CountingNotifier(),
            session_factory=_session_factory(),
            budget_factory=_budget_factory)
    # Second tick in the same minute (daemon 30s cadence).
    run_due(store, now.replace(second=30), notifier=_CountingNotifier(),
            session_factory=_session_factory(),
            budget_factory=_budget_factory)
    assert len(store.list_runs(s.id)) == 1


def test_daemon_run_now_overlap_blocked(tmp_path):
    store = _store(tmp_path)
    s = _add_due(store, tmp_path, "overlap")
    # Hold a live claim as if the daemon is mid-run.
    claim = store.try_claim(
        s.id, fire_at=fire_at_timestamp(_now()), owner="daemon",
        lease_seconds=600,
    )
    assert claim is not None
    run = run_one_now(
        store, s.id,
        notifier=_CountingNotifier(),
        session_factory=_session_factory(),
        budget_factory=_budget_factory,
        owner="cli",
    )
    assert run is not None
    assert run["status"] == "blocked"


def test_halt_status_mapping_ceilings(tmp_path):
    store = _store(tmp_path)
    s = _add_due(store, tmp_path, "ceil")
    run_due(
        store, _now(), notifier=_CountingNotifier(),
        session_factory=_session_factory(reason="token ceiling reached (1/1)"),
        budget_factory=_budget_factory,
    )
    assert store.list_runs(s.id)[0]["status"] == "token_ceiling"
    assert store.get(s.id).last_status == "token_ceiling"


def test_invalid_cron_status(tmp_path):
    store = _store(tmp_path)
    # Persist a schedule then corrupt cron via raw SQL (bypasses add validation).
    s = _add_due(store, tmp_path, "badcron")
    store._conn.execute(
        "UPDATE schedules SET cron = ? WHERE id = ?", ("bogus", s.id),
    )
    store._conn.commit()
    runs = run_due(store, _now(), notifier=_CountingNotifier(),
                   session_factory=_session_factory(),
                   budget_factory=_budget_factory)
    assert runs == []
    assert store.get(s.id).last_status == "invalid_cron"
    assert store.get(s.id).display_status() == "invalid_cron"


def test_home_temp_nongit_refusal(tmp_path, monkeypatch):
    store = _store(tmp_path)
    home = Path.home()
    s_home = store.add(Schedule(
        id="", name="home", objective="o", cron="* * * * *", repo=str(home),
    ))
    run_due(store, _now(), notifier=_CountingNotifier(),
            session_factory=_session_factory(),
            budget_factory=_budget_factory)
    assert store.list_runs(s_home.id)[0]["status"] == "refused"
    assert "home" in store.list_runs(s_home.id)[0]["halt_reason"].lower()

    # Non-git directory (force _git_work_tree false — tmp may sit in a parent repo).
    plain = tmp_path / "notgit"
    plain.mkdir()
    monkeypatch.setattr(
        "harness.implement_guards._git_work_tree", lambda path: False,
    )
    monkeypatch.setattr(
        "harness.implement_guards.check_implement_workspace",
        lambda repo, goal="": f"REFUSED: {repo} is not a git repository",
    )
    s_ng = store.add(Schedule(
        id="", name="ng", objective="o", cron="* * * * *", repo=str(plain),
    ))
    run_due(store, _now(), notifier=_CountingNotifier(),
            session_factory=_session_factory(),
            budget_factory=_budget_factory)
    ng_runs = [r for r in store.list_runs(s_ng.id) if r["status"] != "running"]
    assert ng_runs and ng_runs[0]["status"] == "refused"

    # Temp/ephemeral: clear pytest marker so the guard applies.
    temp_repo = tmp_path / "temprepo"
    temp_repo.mkdir()
    subprocess.run(
        ["git", "init"], cwd=str(temp_repo), check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    reason = check_schedule_workspace(str(temp_repo))
    assert reason is not None
    assert "temp" in reason.lower() or "ephemeral" in reason.lower()


def test_nested_git_workspace_allowed(tmp_path):
    root = Path(_git_repo(tmp_path, "outer"))
    nested = root / "pkg" / "sub"
    nested.mkdir(parents=True)
    assert check_schedule_workspace(str(nested)) is None


def test_disable_cancellation(tmp_path):
    import threading

    store = _store(tmp_path)
    s = _add_due(store, tmp_path, "cancel-me")
    sessions = []
    release = threading.Event()

    class _GateSession(_FakeSession):
        def run_auto(self, objective, budget=None, *, require_codegraph=True):
            yield _Event("auto_status", {"cycle": 1, "snapshot": {
                "tokens_used": 1, "swarms_used": 0}})
            release.wait(timeout=2.0)
            if self._cancel:
                yield _Event("auto_halt", {"reason": "cancelled", "snapshot": {
                    "tokens_used": 0, "swarms_used": 0}})
                return
            yield _Event("auto_halt", {"reason": _OK_REASON, "snapshot": {
                "tokens_used": 0, "swarms_used": 0}})

    def factory(sched):
        sess = _GateSession(_OK_REASON, 2, 1, 0, check_cancel=True)
        sessions.append(sess)
        return sess

    result = {}

    def runner():
        result["run"] = run_due(
            store, _now(), notifier=_CountingNotifier(),
            session_factory=factory, budget_factory=_budget_factory,
        )

    t = threading.Thread(target=runner)
    t.start()
    # Wait until claim is visible, then disable (requests cancel).
    for _ in range(100):
        got = store.get(s.id)
        if got and got.claim_owner:
            break
        time.sleep(0.02)
    assert store.get(s.id).claim_owner
    assert store.set_enabled(s.id, False)
    # Allow the generator to proceed to the next event so _run_one sees cancel.
    time.sleep(0.05)
    release.set()
    t.join(timeout=5)
    assert not t.is_alive()
    assert sessions and sessions[0].cancel_calls >= 1
    runs = store.list_runs(s.id)
    assert runs
    assert runs[0]["status"] == "cancelled"


def test_daemon_stop_cancels_and_interruptible_wait(tmp_path):
    store = _store(tmp_path)
    s = _add_due(store, tmp_path, "dstop")
    store.try_claim(
        s.id, fire_at=1.0, owner="daemon-active", lease_seconds=600,
    )
    daemon = SchedulerDaemon(store)
    daemon._active["schedule_id"] = s.id
    started = time.time()
    # stop() should request cancel; interruptible wait should return quickly.
    daemon.stop()
    assert store.cancel_requested(s.id)
    daemon._interruptible_wait(30)
    assert time.time() - started < 2.0


def test_keyboard_interrupt_leaves_recoverable_state(tmp_path):
    store = _store(tmp_path)
    s = _add_due(store, tmp_path, "crash")
    holder = {"schedule_id": "pre", "run_id": "pre"}

    class _BoomSession(_FakeSession):
        def run_auto(self, objective, budget=None, *, require_codegraph=True):
            yield _Event("auto_status", {"cycle": 1, "snapshot": {}})
            raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        run_due(
            store, _now(), notifier=_CountingNotifier(),
            session_factory=lambda sched: _BoomSession("x", 1, 0, 0),
            budget_factory=_budget_factory,
            active_schedule_holder=holder,
        )
    # In-memory holder cleared; durable claim left running for recovery.
    assert "schedule_id" not in holder
    assert "run_id" not in holder
    got = store.get(s.id)
    assert got.last_status == "running"
    assert got.claim_owner
    runs = store.list_runs(s.id)
    assert runs and runs[0]["status"] == "running"
    # Stale recovery cleans it up.
    claim = store.try_claim(
        s.id, fire_at=fire_at_timestamp(_now()) + 60,
        owner="recover", lease_seconds=60, now=time.time() + 10_000,
    )
    assert claim is not None
    prior = [r for r in store.list_runs(s.id) if r["status"] == "interrupted"]
    assert prior


def test_short_max_seconds_schedule_stays_fenced_while_live(tmp_path):
    from harness.schedule_store import DEFAULT_LEASE_SECONDS, claim_lease_seconds

    store = _store(tmp_path)
    s = store.add(Schedule(
        id="", name="short", objective="o", cron="* * * * *",
        repo=_git_repo(tmp_path, "short-repo"), max_seconds=5,
    ))
    release = __import__("threading").Event()
    renewed = []

    class _SlowSession(_FakeSession):
        def run_auto(self, objective, budget=None, *, require_codegraph=True):
            yield _Event("auto_status", {"cycle": 1, "snapshot": {
                "tokens_used": 1, "swarms_used": 0}})
            release.wait(timeout=2.0)
            yield _Event("auto_halt", {"reason": _OK_REASON, "snapshot": {
                "tokens_used": 1, "swarms_used": 0}})

    import threading

    def runner():
        run_due(
            store, _now(), notifier=_CountingNotifier(),
            session_factory=lambda sched: _SlowSession(_OK_REASON, 1, 1, 0),
            budget_factory=_budget_factory,
        )

    t = threading.Thread(target=runner)
    t.start()
    for _ in range(100):
        got = store.get(s.id)
        if got and got.claim_owner:
            renewed.append(got.claim_lease_until)
            break
        time.sleep(0.02)
    live = store.get(s.id)
    assert live and live.claim_owner
    assert claim_lease_seconds(5) >= DEFAULT_LEASE_SECONDS
    assert live.claim_lease_until >= time.time()
    # Competing claim while live must lose.
    assert store.try_claim(
        s.id, fire_at=fire_at_timestamp(_now()) + 120,
        owner="intruder", lease_seconds=claim_lease_seconds(5),
    ) is None
    release.set()
    t.join(timeout=5)
    assert not t.is_alive()
    assert store.get(s.id).claim_owner == ""


def test_preexisting_cancel_invokes_before_first_next(tmp_path):
    """Cancel already set before the event loop must call session.cancel before next()."""
    store = _store(tmp_path)
    s = _add_due(store, tmp_path, "pre-cancel")
    sessions = []

    class _TrackSession(_FakeSession):
        def __init__(self):
            super().__init__(_OK_REASON, 1, 0, 0)
            self.advanced = False
            self.cancel_before_advance = False

        def cancel(self):
            self.cancel_calls += 1
            if not self.advanced:
                self.cancel_before_advance = True

        def run_auto(self, objective, budget=None, *, require_codegraph=True):
            self.advanced = True
            yield _Event("auto_halt", {"reason": "cancelled", "snapshot": {
                "tokens_used": 0, "swarms_used": 0}})

    def factory(sched):
        # After try_claim cleared the flag; re-request before the iterator loop.
        store.request_cancel(sched.id)
        sess = _TrackSession()
        sessions.append(sess)
        return sess

    notifier = _CountingNotifier()
    runs = run_due(
        store, _now(), notifier=notifier,
        session_factory=factory, budget_factory=_budget_factory,
    )
    assert sessions and sessions[0].cancel_calls >= 1
    assert sessions[0].cancel_before_advance is True
    assert runs and runs[0]["status"] == "cancelled"


def test_renew_claim_false_returns_superseded_without_notify(tmp_path):
    """Lost ownership mid-run must never notify or return a false ok."""
    store = _store(tmp_path)
    s = _add_due(store, tmp_path, "lose-renew")
    notifier = _CountingNotifier()
    sessions = []

    def renew_always_lose(schedule_id, run_id, lease_seconds, now=None):
        return False

    store.renew_claim = renew_always_lose  # type: ignore[method-assign]

    class _MultiEvent(_FakeSession):
        def run_auto(self, objective, budget=None, *, require_codegraph=True):
            yield _Event("auto_status", {"cycle": 1, "snapshot": {
                "tokens_used": 1, "swarms_used": 0}})
            yield _Event("auto_halt", {"reason": _OK_REASON, "snapshot": {
                "tokens_used": 1, "swarms_used": 0}})

    def factory(sched):
        sess = _MultiEvent(_OK_REASON, 1, 1, 0)
        sessions.append(sess)
        return sess

    runs = run_due(
        store, _now(), notifier=notifier,
        session_factory=factory, budget_factory=_budget_factory,
    )
    assert runs and runs[0]["status"] == "superseded"
    assert runs[0]["halt_reason"] == "ownership_lost"
    assert runs[0]["status"] != "ok"
    assert notifier.calls == []
    assert sessions and sessions[0].cancel_calls >= 1


def test_stale_owner_complete_through_run_one_is_superseded(tmp_path):
    """Successor claim during a live run: stale owner must not notify ok."""
    import threading

    store = _store(tmp_path)
    s = _add_due(store, tmp_path, "stale-owner")
    notifier = _CountingNotifier()
    release = threading.Event()
    claimed = threading.Event()
    sessions = []

    class _GateSession(_FakeSession):
        def run_auto(self, objective, budget=None, *, require_codegraph=True):
            claimed.set()
            yield _Event("auto_status", {"cycle": 1, "snapshot": {
                "tokens_used": 1, "swarms_used": 0}})
            release.wait(timeout=2.0)
            yield _Event("auto_halt", {"reason": _OK_REASON, "snapshot": {
                "tokens_used": 9, "swarms_used": 2}})

    def factory(sched):
        sess = _GateSession(_OK_REASON, 1, 9, 2)
        sessions.append(sess)
        return sess

    result = {}

    def runner():
        result["runs"] = run_due(
            store, _now(), notifier=notifier,
            session_factory=factory, budget_factory=_budget_factory,
        )

    t = threading.Thread(target=runner)
    t.start()
    assert claimed.wait(timeout=2.0)
    live = store.get(s.id)
    assert live and live.claim_owner
    old_run = live.claim_run_id
    # Expire lease and let a successor claim while the original is still running.
    # Heartbeat interval defaults to <=60s, so a sub-second expire+claim window
    # still reproduces ownership loss before the next heartbeat tick.
    with store._lock:
        store._conn.execute(
            "UPDATE schedules SET claim_lease_until = ? WHERE id = ?",
            (time.time() - 1, s.id),
        )
        store._conn.commit()
    nxt = store.try_claim(
        s.id,
        fire_at=fire_at_timestamp(_now()) + 120,
        owner="successor",
        lease_seconds=600,
    )
    assert nxt is not None
    assert nxt["run_id"] != old_run
    successor_before = store.get(s.id)
    assert successor_before.claim_owner == "successor"
    assert successor_before.claim_run_id == nxt["run_id"]
    release.set()
    t.join(timeout=5)
    assert not t.is_alive()
    runs = result["runs"]
    assert runs and runs[0]["status"] == "superseded"
    assert runs[0]["halt_reason"] == "ownership_lost"
    assert notifier.calls == []
    # Durable successor state untouched.
    after = store.get(s.id)
    assert after.claim_owner == "successor"
    assert after.claim_run_id == nxt["run_id"]
    assert after.last_status == "running"
    old_row = [r for r in store.list_runs(s.id) if r["id"] == old_run][0]
    assert old_row["status"] == "interrupted"


def test_lease_heartbeat_renews_during_blocked_provider(tmp_path, monkeypatch):
    """Heartbeat keeps the claim fenced while run_auto blocks inside next()."""
    import threading

    import harness.scheduler as sched_mod
    from harness.schedule_store import claim_lease_seconds as real_claim_lease

    store = _store(tmp_path)
    s = _add_due(store, tmp_path, "hb")
    # Short lease + fast heartbeat so a blocked provider would expire without
    # independent renewals; successor must still lose while the worker is live.
    monkeypatch.setattr(sched_mod, "claim_lease_seconds", lambda max_seconds=0: 2)
    monkeypatch.setattr(sched_mod, "_heartbeat_interval", lambda lease_seconds: 0.2)

    release = threading.Event()
    claimed = threading.Event()
    lease_samples = []

    class _BlockedSession(_FakeSession):
        def run_auto(self, objective, budget=None, *, require_codegraph=True):
            claimed.set()
            # Block inside next() longer than the short lease without yielding.
            release.wait(timeout=3.0)
            yield _Event("auto_halt", {"reason": _OK_REASON, "snapshot": {
                "tokens_used": 1, "swarms_used": 0}})

    result = {}

    def runner():
        result["runs"] = run_due(
            store, _now(), notifier=_CountingNotifier(),
            session_factory=lambda sched: _BlockedSession(_OK_REASON, 1, 1, 0),
            budget_factory=_budget_factory,
        )

    t = threading.Thread(target=runner)
    t.start()
    assert claimed.wait(timeout=2.0)
    live = store.get(s.id)
    assert live and live.claim_owner
    old_run = live.claim_run_id
    # Observe lease extending past the bare 2s ceiling while blocked.
    deadline = time.time() + 2.5
    while time.time() < deadline:
        got = store.get(s.id)
        if got:
            lease_samples.append(got.claim_lease_until)
        time.sleep(0.15)
    assert max(lease_samples) > min(lease_samples)
    # Successor cannot steal while heartbeat keeps the fence fresh.
    assert store.try_claim(
        s.id,
        fire_at=fire_at_timestamp(_now()) + 120,
        owner="intruder",
        lease_seconds=real_claim_lease(0),
    ) is None
    release.set()
    t.join(timeout=5)
    assert not t.is_alive()
    assert result["runs"] and result["runs"][0]["status"] == "ok"
    assert store.get(s.id).claim_owner == ""
    assert store.list_runs(s.id)[0]["id"] == old_run


def test_blocked_worker_after_successor_cannot_corrupt_state(tmp_path, monkeypatch):
    """Ownership-loss regression: late resume after successor claim is no-op."""
    import threading

    import harness.scheduler as sched_mod

    store = _store(tmp_path)
    s = _add_due(store, tmp_path, "late")

    class _NoHeartbeat:
        ownership_lost = False

        def start(self):
            return None

        def stop(self):
            return False

    # Disable heartbeat so a blocked worker can lose the lease on purpose.
    monkeypatch.setattr(
        sched_mod, "_ClaimLeaseHeartbeat", lambda *a, **k: _NoHeartbeat(),
    )
    release = threading.Event()
    claimed = threading.Event()
    notifier = _CountingNotifier()

    class _BlockedSession(_FakeSession):
        def run_auto(self, objective, budget=None, *, require_codegraph=True):
            claimed.set()
            release.wait(timeout=3.0)
            yield _Event("auto_halt", {"reason": _OK_REASON, "snapshot": {
                "tokens_used": 99, "swarms_used": 9}})

    result = {}

    def runner():
        result["runs"] = run_due(
            store, _now(), notifier=notifier,
            session_factory=lambda sched: _BlockedSession(_OK_REASON, 1, 99, 9),
            budget_factory=_budget_factory,
        )

    t = threading.Thread(target=runner)
    t.start()
    assert claimed.wait(timeout=2.0)
    old_run = store.get(s.id).claim_run_id
    with store._lock:
        store._conn.execute(
            "UPDATE schedules SET claim_lease_until = ? WHERE id = ?",
            (time.time() - 1, s.id),
        )
        store._conn.commit()
    nxt = store.try_claim(
        s.id,
        fire_at=fire_at_timestamp(_now()) + 999,
        owner="successor",
        lease_seconds=600,
    )
    assert nxt is not None
    release.set()
    t.join(timeout=5)
    assert not t.is_alive()
    assert result["runs"][0]["status"] == "superseded"
    assert result["runs"][0]["halt_reason"] == "ownership_lost"
    assert notifier.calls == []
    after = store.get(s.id)
    assert after.claim_owner == "successor"
    assert after.claim_run_id == nxt["run_id"]
    assert after.last_fire_at == 0.0 or after.last_status == "running"
    old_row = [r for r in store.list_runs(s.id) if r["id"] == old_run][0]
    assert old_row["status"] == "interrupted"
    assert old_row["tokens_used"] == 0


def test_complete_claim_false_on_refusal_returns_superseded(tmp_path):
    """Workspace-refusal path must honor complete_claim False (no success notify)."""
    store = _store(tmp_path)
    # No git repo -> refused after claim.
    s = store.add(Schedule(
        id="", name="refuse", objective="o", cron="* * * * *",
        repo=str(tmp_path / "not-a-git-repo"),
    ))
    (tmp_path / "not-a-git-repo").mkdir()
    notifier = _CountingNotifier()
    real_complete = store.complete_claim

    def always_lose(*a, **k):
        return False

    store.complete_claim = always_lose  # type: ignore[method-assign]
    run = run_one_now(
        store, s.id, notifier=notifier,
        session_factory=_session_factory(), budget_factory=_budget_factory,
    )
    store.complete_claim = real_complete  # type: ignore[method-assign]
    assert run is not None
    assert run["status"] == "superseded"
    assert run["halt_reason"] == "ownership_lost"
    assert notifier.calls == []
