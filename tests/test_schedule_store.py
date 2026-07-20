"""ScheduleStore proofs: CRUD, claims, WAL fencing, state path, run log."""
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import pytest

from harness.schedule_core import Schedule
from harness.schedule_store import (
    DEFAULT_LEASE_SECONDS,
    DEFAULT_RUN_HISTORY_KEEP,
    REMOVE_CANCEL_REQUESTED,
    REMOVE_REMOVED,
    REMOVE_STALE_RECOVERED,
    ScheduleStore,
    _pytest_test_db_path,
    claim_lease_seconds,
    default_db_path,
    run_history_keep,
)


def _mk(name="n", cron="* * * * *", enabled=True, repo=""):
    return Schedule(
        id="", name=name, objective="obj", cron=cron, enabled=enabled, repo=repo,
    )


def test_add_autogen_id_and_created_at(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.sqlite"))
    s = store.add(_mk())
    assert s.id and len(s.id) == 8
    assert s.created_at > 0
    assert s.enabled_at > 0


def test_add_get_list(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.sqlite"))
    a = store.add(_mk("a"))
    b = store.add(_mk("b"))
    assert {x.id for x in store.list()} == {a.id, b.id}
    got = store.get(a.id)
    assert got is not None and got.name == "a"
    assert store.get("nope") is None


def test_enabled_only_filter(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.sqlite"))
    a = store.add(_mk("on", enabled=True))
    b = store.add(_mk("off", enabled=False))
    ids = {x.id for x in store.list(enabled_only=True)}
    assert a.id in ids and b.id not in ids


def test_remove(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.sqlite"))
    a = store.add(_mk())
    assert store.remove(a.id) == REMOVE_REMOVED
    assert store.get(a.id) is None
    assert store.remove(a.id) is False


def test_set_enabled(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.sqlite"))
    a = store.add(_mk(enabled=True))
    assert store.set_enabled(a.id, False) is True
    assert store.get(a.id).enabled is False
    assert store.set_enabled("nope", True) is False


def test_update_last_run(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.sqlite"))
    a = store.add(_mk())
    ts = time.time()
    assert store.update_last_run(a.id, "ok", ts) is True
    got = store.get(a.id)
    assert got.last_status == "ok"
    assert abs(got.last_run_at - ts) < 1.0


def test_record_and_list_runs(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.sqlite"))
    a = store.add(_mk())
    store.record_run(a.id, 1.0, 2.0, "ok", halt_reason="done",
                     cycles=3, tokens_used=100, swarms_used=2)
    store.record_run(a.id, 3.0, 4.0, "error", halt_reason="boom")
    runs = store.list_runs(a.id)
    assert len(runs) == 2
    # Most recent first.
    assert runs[0]["status"] == "error"
    assert runs[1]["cycles"] == 3
    assert runs[1]["tokens_used"] == 100
    assert runs[1]["swarms_used"] == 2


def test_persistence_across_reopen(tmp_path):
    path = str(tmp_path / "s.sqlite")
    store = ScheduleStore(path)
    a = store.add(_mk("persist"))
    store.record_run(a.id, 1.0, 2.0, "ok")
    store.close()

    reopened = ScheduleStore(path)
    got = reopened.get(a.id)
    assert got is not None and got.name == "persist"
    assert len(reopened.list_runs(a.id)) == 1


def test_remove_purges_runs(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.sqlite"))
    a = store.add(_mk())
    store.record_run(a.id, 1.0, 2.0, "ok")
    assert store.remove(a.id) == REMOVE_REMOVED
    assert store.list_runs(a.id) == []


def test_active_remove_defers_and_preserves_history(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.sqlite"))
    a = store.add(_mk())
    claim = store.try_claim(a.id, fire_at=10.0, owner="live", lease_seconds=600)
    assert claim is not None
    store.record_run(a.id, 1.0, 2.0, "ok", halt_reason="prior")
    outcome = store.remove(a.id)
    assert outcome == REMOVE_CANCEL_REQUESTED
    got = store.get(a.id)
    assert got is not None
    assert got.enabled is False
    assert got.cancel_requested is True
    assert got.claim_owner == "live"
    assert got.claim_run_id == claim["run_id"]
    # Prior history + active running row preserved.
    runs = store.list_runs(a.id)
    assert len(runs) >= 2
    assert any(r["status"] == "ok" for r in runs)
    assert any(r["id"] == claim["run_id"] for r in runs)


def test_inactive_remove_atomically_purges(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.sqlite"))
    a = store.add(_mk(enabled=False))
    store.record_run(a.id, 1.0, 2.0, "ok")
    assert store.remove(a.id) == REMOVE_REMOVED
    assert store.get(a.id) is None
    assert store.list_runs(a.id) == []


def test_stale_claim_remove_recovers_then_purge_without_try_claim(tmp_path):
    """Production-shaped: stale remove recovers in-place; no try_claim needed."""
    store = ScheduleStore(str(tmp_path / "s.sqlite"))
    a = store.add(_mk())
    t0 = 1_000_000.0  # far in the past so lease is stale vs time.time()
    old = store.try_claim(
        a.id, fire_at=100.0, owner="old", lease_seconds=10, now=t0,
    )
    assert old is not None
    store.record_run(a.id, 1.0, 2.0, "ok", halt_reason="prior")
    # Stale claim: remove must clear claim fields + disable (not leave owner).
    assert store.remove(a.id) == REMOVE_STALE_RECOVERED
    got = store.get(a.id)
    assert got is not None
    assert got.enabled is False
    assert got.claim_owner == ""
    assert got.claim_run_id == ""
    assert got.cancel_requested is False
    assert got.last_status == "interrupted"
    runs = store.list_runs(a.id)
    interrupted = [r for r in runs if r["id"] == old["run_id"]]
    assert interrupted and interrupted[0]["status"] == "interrupted"
    assert interrupted[0]["halt_reason"] == "stale claim recovered"
    assert any(r["status"] == "ok" for r in runs)  # history preserved
    # Next remove atomically purges (no manual try_claim recovery).
    assert store.remove(a.id) == REMOVE_REMOVED
    assert store.get(a.id) is None
    assert store.list_runs(a.id) == []


def test_complete_claim_fenced_against_stale_owner(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.sqlite"))
    s = store.add(_mk())
    t0 = 2_000_000.0
    old = store.try_claim(
        s.id, fire_at=100.0, owner="old", lease_seconds=10, now=t0,
    )
    assert old is not None
    old_run = old["run_id"]
    # Expiry -> successor claim.
    nxt = store.try_claim(
        s.id, fire_at=200.0, owner="new", lease_seconds=60, now=t0 + 11,
    )
    assert nxt is not None
    assert nxt["run_id"] != old_run
    before = store.get(s.id)
    assert before.claim_run_id == nxt["run_id"]
    assert before.claim_owner == "new"
    successor_lease = before.claim_lease_until
    # Late completion by the old owner must be a no-op.
    assert store.complete_claim(
        s.id, old_run,
        status="ok", halt_reason="stale late win",
        cycles=99, tokens_used=99, swarms_used=9,
        ended_at=t0 + 20, fire_at=100.0,
    ) is False
    after = store.get(s.id)
    assert after.claim_run_id == nxt["run_id"]
    assert after.claim_owner == "new"
    assert after.claim_lease_until == successor_lease
    assert after.last_status == "running"
    assert after.last_fire_at == 0.0
    old_row = [r for r in store.list_runs(s.id) if r["id"] == old_run][0]
    assert old_row["status"] == "interrupted"
    assert old_row["cycles"] == 0
    live = [r for r in store.list_runs(s.id) if r["id"] == nxt["run_id"]][0]
    assert live["status"] == "running"


def test_short_max_seconds_lease_remains_fenced(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.sqlite"))
    s = store.add(Schedule(
        id="", name="short", objective="o", cron="* * * * *", max_seconds=5,
    ))
    lease = claim_lease_seconds(5)
    assert lease >= DEFAULT_LEASE_SECONDS
    assert lease > 5
    t0 = 3_000_000.0
    claim = store.try_claim(
        s.id, fire_at=50.0, owner="short-run",
        lease_seconds=lease, now=t0,
    )
    assert claim is not None
    assert claim["lease_until"] >= t0 + DEFAULT_LEASE_SECONDS
    # Still fenced well after a bare max_seconds=5 window.
    assert store.try_claim(
        s.id, fire_at=60.0, owner="intruder",
        lease_seconds=lease, now=t0 + 30,
    ) is None
    assert store.renew_claim(s.id, claim["run_id"], lease, now=t0 + 40)
    got = store.get(s.id)
    assert got.claim_lease_until >= t0 + 40 + DEFAULT_LEASE_SECONDS
    assert store.renew_claim(s.id, "not-the-owner", lease, now=t0 + 50) is False


def test_claim_lease_seconds_never_equals_short_ceiling():
    assert claim_lease_seconds(0) == DEFAULT_LEASE_SECONDS
    assert claim_lease_seconds(5) >= DEFAULT_LEASE_SECONDS
    assert claim_lease_seconds(5) != 5
    assert claim_lease_seconds(10_000) >= 10_000


def test_concurrent_two_store_claim_one_winner(tmp_path):
    path = str(tmp_path / "s.sqlite")
    store_a = ScheduleStore(path)
    s = store_a.add(_mk())
    store_a.close()

    results = []
    barrier = threading.Barrier(2)

    def worker(owner):
        st = ScheduleStore(path)
        barrier.wait()
        claim = st.try_claim(s.id, fire_at=1000.0, owner=owner, lease_seconds=60)
        results.append((owner, claim))
        st.close()

    t1 = threading.Thread(target=worker, args=("owner-a",))
    t2 = threading.Thread(target=worker, args=("owner-b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    winners = [c for _, c in results if c is not None]
    losers = [c for _, c in results if c is None]
    assert len(winners) == 1
    assert len(losers) == 1


def test_stale_claim_recovery(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.sqlite"))
    s = store.add(_mk())
    t0 = 1_000_000.0
    claim1 = store.try_claim(
        s.id, fire_at=100.0, owner="old", lease_seconds=10, now=t0,
    )
    assert claim1 is not None
    # Fresh claim blocked while lease still valid (t0+5 < t0+10).
    assert store.try_claim(
        s.id, fire_at=200.0, owner="new", lease_seconds=60, now=t0 + 5,
    ) is None
    # After lease expiry, new owner recovers and prior run is interrupted.
    claim2 = store.try_claim(
        s.id, fire_at=200.0, owner="new", lease_seconds=60, now=t0 + 11,
    )
    assert claim2 is not None
    assert claim2["owner"] == "new"
    prior = store.list_runs(s.id)
    interrupted = [r for r in prior if r["id"] == claim1["run_id"]]
    assert interrupted and interrupted[0]["status"] == "interrupted"


def test_transactional_completion(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.sqlite"))
    s = store.add(_mk())
    claim = store.try_claim(s.id, fire_at=50.0, owner="o1", lease_seconds=60)
    assert claim is not None
    assert store.get(s.id).last_status == "running"
    assert store.complete_claim(
        s.id, claim["run_id"],
        status="ok", halt_reason="objective met",
        cycles=2, tokens_used=9, swarms_used=1,
        ended_at=123.0, fire_at=50.0,
    )
    got = store.get(s.id)
    assert got.claim_owner == ""
    assert got.last_status == "ok"
    assert got.last_run_at == 123.0
    assert got.last_fire_at == 50.0
    run = store.list_runs(s.id)[0]
    assert run["status"] == "ok"
    assert run["cycles"] == 2


def test_run_history_keep_default_and_env(monkeypatch):
    assert run_history_keep() == DEFAULT_RUN_HISTORY_KEEP
    assert run_history_keep(5) == 5
    assert run_history_keep(0) == 1  # floor
    monkeypatch.setenv("HARNESS_SCHEDULE_RUN_HISTORY_KEEP", "7")
    assert run_history_keep() == 7
    monkeypatch.setenv("HARNESS_SCHEDULE_RUN_HISTORY_KEEP", "nope")
    assert run_history_keep() == DEFAULT_RUN_HISTORY_KEEP


def test_complete_claim_prunes_old_runs_keeps_recent(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.sqlite"))
    s = store.add(_mk())
    # Seed many finished runs, then complete one more via claim API.
    for i in range(12):
        store.record_run(
            s.id, started_at=float(i), ended_at=float(i) + 0.5,
            status="ok", halt_reason=f"old-{i}",
        )
    claim = store.try_claim(s.id, fire_at=100.0, owner="trim", lease_seconds=60)
    assert claim is not None
    assert store.complete_claim(
        s.id, claim["run_id"],
        status="ok", halt_reason="newest",
        ended_at=200.0, fire_at=100.0,
        history_keep=5,
    )
    runs = store.list_runs(s.id, limit=100)
    assert len(runs) == 5
    assert runs[0]["halt_reason"] == "newest"
    assert all(r["status"] != "running" for r in runs)


def test_prune_never_deletes_active_running_row(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.sqlite"))
    s = store.add(_mk())
    for i in range(8):
        store.record_run(
            s.id, started_at=float(i), ended_at=float(i) + 0.1, status="ok",
        )
    live = store.try_claim(s.id, fire_at=50.0, owner="live", lease_seconds=600)
    assert live is not None
    deleted = store.prune_runs(s.id, keep=3)
    assert deleted >= 1
    runs = store.list_runs(s.id, limit=100)
    running = [r for r in runs if r["id"] == live["run_id"]]
    assert running and running[0]["status"] == "running"
    # Terminal rows capped; running retained beyond the keep budget.
    terminal = [r for r in runs if r["status"] != "running"]
    assert len(terminal) == 3
    assert store.get(s.id).claim_run_id == live["run_id"]


def test_concurrent_complete_claim_retention(tmp_path):
    """Two completions racing prune must not drop the active/newer rows wrongly."""
    path = str(tmp_path / "s.sqlite")
    store = ScheduleStore(path)
    s = store.add(_mk())
    for i in range(20):
        store.record_run(
            s.id, started_at=float(i), ended_at=float(i) + 0.1, status="ok",
        )
    store.close()

    results = []
    barrier = threading.Barrier(2)

    def worker(owner, fire):
        st = ScheduleStore(path)
        barrier.wait()
        # Force-claim path: expire any prior lease then claim.
        now = time.time()
        with st._lock:
            st._conn.execute(
                "UPDATE schedules SET claim_lease_until = 0 WHERE id = ?",
                (s.id,),
            )
            st._conn.commit()
        claim = st.try_claim(
            s.id, fire_at=fire, owner=owner, lease_seconds=30, now=now,
        )
        if claim is None:
            results.append((owner, None, 0))
            st.close()
            return
        ok = st.complete_claim(
            s.id, claim["run_id"],
            status="ok", halt_reason=owner,
            ended_at=now + 1, fire_at=fire,
            history_keep=10,
        )
        n = len(st.list_runs(s.id, limit=200))
        results.append((owner, ok, n))
        st.close()

    t1 = threading.Thread(target=worker, args=("a", 1000.0))
    t2 = threading.Thread(target=worker, args=("b", 2000.0))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # At least one completion succeeded; history never exceeds keep by much
    # (running rows may briefly exist, but both workers complete).
    assert any(ok for _, ok, _ in results if ok)
    final = ScheduleStore(path)
    runs = final.list_runs(s.id, limit=200)
    assert len(runs) <= 10
    assert all(r["status"] != "running" for r in runs)
    final.close()


def test_request_cancel_on_disable(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.sqlite"))
    s = store.add(_mk())
    claim = store.try_claim(s.id, fire_at=10.0, owner="o", lease_seconds=600)
    assert claim is not None
    store.set_enabled(s.id, False)
    assert store.cancel_requested(s.id) is True


def test_update_fields(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.sqlite"))
    s = store.add(_mk(name="old"))
    updated = store.update_fields(
        s.id, name="new", cron="0 3 * * *", max_tokens=100,
    )
    assert updated is not None
    assert updated.name == "new"
    assert updated.cron == "0 3 * * *"
    assert updated.max_tokens == 100


def test_store_rejects_negative_ceilings(tmp_path):
    store = ScheduleStore(str(tmp_path / "s.sqlite"))
    bad = _mk("bad")
    bad.max_seconds = -1
    with pytest.raises(ValueError, match="max_seconds"):
        store.add(bad)
    assert store.list() == []

    good = store.add(_mk("good"))
    with pytest.raises(ValueError, match="max_tokens"):
        store.update_fields(good.id, max_tokens=-5)
    assert store.get(good.id).max_tokens == 0


def test_default_db_path_honors_harness_state_dir(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    assert default_db_path() == state / "schedules.sqlite"
    # Explicit path stays exact and does not use Home.
    store = ScheduleStore(str(tmp_path / "exact.sqlite"))
    assert store.path == tmp_path / "exact.sqlite"
    store.close()


def test_default_db_path_pytest_uses_temp_not_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delenv("HARNESS_STATE_DIR", raising=False)
    monkeypatch.setenv(
        "PYTEST_CURRENT_TEST",
        "tests/test_schedule_store.py::test_default_db_path_pytest_uses_temp_not_home (call)",
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    path = default_db_path()
    assert path.is_absolute()
    assert str(tempfile.gettempdir()) in str(path)
    assert "pmharness-schedule-tests" in str(path)
    assert home not in path.parents and path != home / ".pmharness" / "state" / "schedules.sqlite"
    assert not str(path).startswith(str(home))
    # Phase suffix normalized: setup vs call share the same digest path.
    monkeypatch.setenv(
        "PYTEST_CURRENT_TEST",
        "tests/test_schedule_store.py::test_default_db_path_pytest_uses_temp_not_home (setup)",
    )
    assert default_db_path() == path


def test_pytest_test_db_path_rsplit_handles_spaces_in_nodeid():
    """Parametrized / spaced node ids must not collide via split-on-first-space."""
    call = _pytest_test_db_path(
        "tests/my suite/test_x.py::test_it[a b] (call)"
    )
    setup = _pytest_test_db_path(
        "tests/my suite/test_x.py::test_it[a b] (setup)"
    )
    assert call == setup
    # Old split(" ")[0] would have keyed only on "tests/my".
    broken_prefix = _pytest_test_db_path("tests/my (call)")
    assert call != broken_prefix


def test_legacy_migrate_path_with_spaces(tmp_path, monkeypatch):
    """Read-only URI must encode spaces so SQLite can open the legacy file."""
    home = tmp_path / "home dir"
    legacy_dir = home / ".harness"
    legacy_dir.mkdir(parents=True)
    legacy_db = legacy_dir / "schedules.sqlite"
    seed = ScheduleStore(str(legacy_db))
    seed.add(_mk("spaced-legacy"))
    seed.close()

    monkeypatch.delenv("HARNESS_STATE_DIR", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    new_path = default_db_path()
    assert new_path.is_file()
    store = ScheduleStore(str(new_path))
    assert "spaced-legacy" in {s.name for s in store.list()}
    store.close()


def test_legacy_harness_db_migrated_once(tmp_path, monkeypatch):
    home = tmp_path / "home"
    legacy_dir = home / ".harness"
    legacy_dir.mkdir(parents=True)
    legacy_db = legacy_dir / "schedules.sqlite"
    # Seed a real legacy DB with one schedule.
    seed = ScheduleStore(str(legacy_db))
    seed.add(_mk("legacy"))
    seed.close()

    monkeypatch.delenv("HARNESS_STATE_DIR", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    new_path = default_db_path()
    assert new_path == home / ".pmharness" / "state" / "schedules.sqlite"
    assert new_path.is_file()
    store = ScheduleStore(str(new_path))
    names = {s.name for s in store.list()}
    assert "legacy" in names
    store.close()
    # Existing new DB is never overwritten by a second migration attempt.
    seed2 = ScheduleStore(str(legacy_db))
    seed2.add(_mk("legacy-only-new"))
    seed2.close()
    again = default_db_path()
    assert again == new_path
    store2 = ScheduleStore(str(new_path))
    names2 = {s.name for s in store2.list()}
    assert "legacy" in names2
    assert "legacy-only-new" not in names2
    store2.close()


def test_legacy_wal_row_migrated_via_backup(tmp_path, monkeypatch):
    home = tmp_path / "home"
    legacy_dir = home / ".harness"
    legacy_dir.mkdir(parents=True)
    legacy_db = legacy_dir / "schedules.sqlite"

    conn = sqlite3.connect(str(legacy_db))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE schedules (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            objective TEXT NOT NULL,
            cron TEXT NOT NULL,
            repo TEXT NOT NULL DEFAULT '',
            swarm_adapter TEXT NOT NULL DEFAULT 'demo',
            driver TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1,
            max_tokens INTEGER NOT NULL DEFAULT 0,
            max_seconds INTEGER NOT NULL DEFAULT 0,
            max_swarms INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL DEFAULT 0,
            enabled_at REAL NOT NULL DEFAULT 0,
            last_run_at REAL NOT NULL DEFAULT 0,
            last_fire_at REAL NOT NULL DEFAULT 0,
            last_status TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        """
        INSERT INTO schedules
            (id, name, objective, cron, created_at, enabled_at, enabled)
        VALUES ('walrow01', 'wal-legacy', 'obj', '* * * * *', 1, 1, 1)
        """
    )
    conn.commit()
    # Keep the writer open so the WAL sidecar remains; backup must include it.
    assert (legacy_dir / "schedules.sqlite-wal").exists()

    monkeypatch.delenv("HARNESS_STATE_DIR", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    new_path = default_db_path()
    conn.close()
    assert new_path.is_file()
    # Legacy source untouched (never deleted/mutated by migration).
    assert legacy_db.is_file()
    store = ScheduleStore(str(new_path))
    names = {s.name for s in store.list()}
    assert "wal-legacy" in names
    store.close()
