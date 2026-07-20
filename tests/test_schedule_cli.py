"""CLI proofs for schedule history/edit/run-now exit status (hermetic)."""
import subprocess

from harness.schedule_cli import _run_schedule
from harness.schedule_core import Schedule
from harness.schedule_store import ScheduleStore


def _git_repo(tmp_path, name="repo"):
    root = tmp_path / name
    root.mkdir()
    subprocess.run(
        ["git", "init"], cwd=str(root), check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return str(root)


def test_cli_history_and_edit(tmp_path, capsys):
    db = str(tmp_path / "s.sqlite")
    repo = _git_repo(tmp_path)
    rc = _run_schedule([
        "--db", db, "add",
        "--name", "nightly",
        "--cron", "0 2 * * *",
        "--objective", "audit",
        "--repo", repo,
    ])
    assert rc == 0
    out_add = capsys.readouterr().out
    assert "next fires (local time)" in out_add
    assert " local" in out_add
    store = ScheduleStore(db)
    sid = store.list()[0].id

    rc = _run_schedule([
        "--db", db, "edit", sid,
        "--name", "nightly2",
        "--cron", "0 3 * * *",
        "--max-tokens", "1000",
    ])
    assert rc == 0
    out_edit = capsys.readouterr().out
    assert "next fires (local time)" in out_edit
    got = store.get(sid)
    assert got.name == "nightly2"
    assert got.cron == "0 3 * * *"
    assert got.max_tokens == 1000

    store.record_run(sid, 1.0, 2.0, "ok", halt_reason="objective met", cycles=1)
    rc = _run_schedule(["--db", db, "history", sid, "--limit", "10"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "objective met" in out
    assert "ok" in out

    rc = _run_schedule(["--db", db, "run-history", sid])
    assert rc == 0


def test_cli_help_documents_host_local_cron(capsys):
    try:
        _run_schedule(["--help"])
    except SystemExit as exc:
        assert exc.code == 0
    out = capsys.readouterr().out.lower()
    assert "host-local" in out
    assert "at-least-once" in out


def test_cli_run_now_exit_nonzero_for_non_ok(tmp_path, monkeypatch):
    db = str(tmp_path / "s.sqlite")
    repo = _git_repo(tmp_path)
    store = ScheduleStore(db)
    s = store.add(Schedule(
        id="", name="x", objective="o", cron="* * * * *", repo=repo,
    ))
    import harness.scheduler as sched_mod

    def boom_session(sched):
        class S:
            def run_auto(self, objective, budget=None, *, require_codegraph=True):
                class E:
                    kind = "auto_halt"
                    data = {
                        "reason": "token ceiling reached (1/1)",
                        "snapshot": {},
                    }
                yield E()

            def cancel(self):
                pass
        return S()

    monkeypatch.setattr(sched_mod, "_default_session_factory", boom_session)
    monkeypatch.setattr(sched_mod, "_default_budget_factory", lambda s: object())
    rc = _run_schedule(["--db", db, "run-now", s.id])
    assert rc == 1


def test_cli_run_now_ok_exit_zero(tmp_path, monkeypatch):
    db = str(tmp_path / "s.sqlite")
    repo = _git_repo(tmp_path)
    store = ScheduleStore(db)
    s = store.add(Schedule(
        id="", name="ok", objective="o", cron="* * * * *", repo=repo,
    ))
    import harness.scheduler as sched_mod

    def ok_session(sched):
        class S:
            def run_auto(self, objective, budget=None, *, require_codegraph=True):
                class E:
                    kind = "auto_halt"
                    data = {
                        "reason": "pilot reports objective met (done)",
                        "snapshot": {"tokens_used": 1, "swarms_used": 0},
                    }
                yield E()

            def cancel(self):
                pass
        return S()

    monkeypatch.setattr(sched_mod, "_default_session_factory", ok_session)
    monkeypatch.setattr(sched_mod, "_default_budget_factory", lambda s: object())
    rc = _run_schedule(["--db", db, "run-now", s.id])
    assert rc == 0


def test_cli_list_shows_status(tmp_path, capsys):
    db = str(tmp_path / "s.sqlite")
    store = ScheduleStore(db)
    store.add(Schedule(
        id="", name="n", objective="o", cron="* * * * *", last_status="ok",
    ))
    rc = _run_schedule(["--db", db, "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "status=ok" in out


def test_cli_remove_active_does_not_print_removed(tmp_path, capsys):
    db = str(tmp_path / "s.sqlite")
    store = ScheduleStore(db)
    s = store.add(Schedule(
        id="", name="live", objective="o", cron="* * * * *",
    ))
    claim = store.try_claim(s.id, fire_at=1.0, owner="daemon", lease_seconds=600)
    assert claim is not None
    rc = _run_schedule(["--db", db, "remove", s.id])
    assert rc == 0
    out = capsys.readouterr().out.lower()
    assert "removed" not in out
    assert "cancel requested" in out
    got = store.get(s.id)
    assert got is not None
    assert got.enabled is False
    assert got.cancel_requested is True
    # Idle after completion: second remove actually deletes.
    assert store.complete_claim(
        s.id, claim["run_id"], status="cancelled", halt_reason="cancelled",
    )
    rc = _run_schedule(["--db", db, "remove", s.id])
    assert rc == 0
    out2 = capsys.readouterr().out.lower()
    assert "removed" in out2
    assert store.get(s.id) is None


def test_cli_remove_stale_claim_recovered_then_purge(tmp_path, capsys):
    db = str(tmp_path / "s.sqlite")
    store = ScheduleStore(db)
    s = store.add(Schedule(
        id="", name="stale", objective="o", cron="* * * * *",
    ))
    # Past lease so remove sees a stale claim without try_claim recovery.
    claim = store.try_claim(
        s.id, fire_at=1.0, owner="dead", lease_seconds=10, now=1_000_000.0,
    )
    assert claim is not None
    rc = _run_schedule(["--db", db, "remove", s.id])
    assert rc == 0
    out = capsys.readouterr().out.lower()
    assert "stale claim recovered" in out
    assert "remove again" in out
    assert "removed" not in out
    got = store.get(s.id)
    assert got is not None
    assert got.claim_owner == ""
    assert got.enabled is False
    rc = _run_schedule(["--db", db, "remove", s.id])
    assert rc == 0
    out2 = capsys.readouterr().out.lower()
    assert "removed" in out2
    assert store.get(s.id) is None


def test_cli_negative_ceilings_rejected(tmp_path, capsys):
    db = str(tmp_path / "s.sqlite")
    repo = _git_repo(tmp_path)
    rc = _run_schedule([
        "--db", db, "add",
        "--name", "bad",
        "--cron", "0 2 * * *",
        "--objective", "x",
        "--repo", repo,
        "--max-seconds", "-1",
    ])
    assert rc == 1
    assert "non-negative" in capsys.readouterr().out.lower()
    assert ScheduleStore(db).list() == []

    store = ScheduleStore(db)
    s = store.add(Schedule(
        id="", name="ok", objective="o", cron="0 2 * * *", repo=repo,
    ))
    before = store.get(s.id).max_tokens
    rc = _run_schedule([
        "--db", db, "edit", s.id, "--max-tokens", "-5",
    ])
    assert rc == 1
    assert "non-negative" in capsys.readouterr().out.lower()
    assert store.get(s.id).max_tokens == before
