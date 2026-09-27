#!/usr/bin/env python3
"""Compare usage polling on identical, disposable SQLite history.

Run with an environment containing Puppetmaster, for example:
  .venv/bin/python tests/fixtures/usage_store_benchmark.py --candidate .
  .venv/bin/python tests/fixtures/usage_store_benchmark.py \
      --baseline ../before --candidate . --output usage-benchmark.json

This measures handler/store reads and child CPU/peak RSS, not the desktop app.
Receipt/pricing services are deterministic. No app or live state is accessed.
Larger histories are opt-in through --jobs and --artifacts-per-job.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace


def isolate(root: Path) -> None:
    os.environ["HARNESS_STATE_DIR"] = str(root / "harness")
    os.environ["PUPPETMASTER_STATE_DIR"] = str(root / "unused-pm")
    os.environ["PUPPETMASTER_MODELS_PATH"] = str(root / "models.json")
    os.environ["HARNESS_CLI_CROSS_PROJECT"] = "1"
    os.environ.pop("PYTEST_CURRENT_TEST", None)

    def no_network(*args, **kwargs):
        raise AssertionError("Network access is forbidden in this benchmark")

    socket.create_connection = no_network
    socket.socket.connect = no_network
    socket.socket.connect_ex = no_network


def make_fixture(root: Path, jobs: int, artifacts: int) -> None:
    from puppetmaster.models import Artifact, ArtifactType, JobStatus
    from puppetmaster.store_factory import create_store

    store = create_store("sqlite", root)
    rows = [store.create_job("Historical work", origin="cli", session_id="history")
            for _ in range(jobs)]
    for row in rows:
        store.update_job_status(row.id, JobStatus.COMPLETE)
    store.save_artifacts([
        Artifact(job_id=row.id, task_id="fixture-task", type=ArtifactType.FINDING,
                 created_by="fixture", payload={"claim": "fixture", "text": "data " * 200},
                 confidence=1, evidence=["fixture.py:1"], id=f"fixture-{index}-{number}")
        for index, row in enumerate(rows) for number in range(artifacts)
    ])
    with closing(sqlite3.connect(str(root / "state.sqlite3"))) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def process_resources() -> dict:
    peak_rss = None
    try:
        import resource

        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # macOS reports bytes; Linux/BSD report KiB. Windows lacks resource.
        peak_rss = int(value * (1 if sys.platform == "darwin" else 1024))
    except ImportError:
        pass
    return {"cpu_s": time.process_time(), "peak_rss_bytes": peak_rss, "platform": sys.platform,
            "scope": "Child process lifetime/high-water; excludes desktop and helper children"}


def measure_repo(repo: Path, fixture: Path, root: Path, args) -> dict:
    isolate(root)
    sys.path.insert(0, str(repo))
    from harness import cli_job_merge
    from harness.api import cost, usage, usage_meters
    from harness.state import DurableState
    import pmharness.registry as registry
    import puppetmaster.state as pm_state

    assert Path(usage.__file__).resolve() == repo / "harness/api/usage.py", "Wrong source imported"
    state = root / "harness"
    shutil.copytree(fixture, state)
    projects = root / "projects"
    projects.mkdir()
    for index in range(args.foreign_stores):
        shutil.copytree(fixture, projects / f"project-{index}")
    pm_state.projects_root = lambda: projects
    cli_job_merge.resolve_cli_state_dir = lambda _: None
    registry.resolve_price = lambda _: (1.0, 2.0)
    registry.price_with_source = lambda _: (1.0, 2.0, "catalog")
    cfg = SimpleNamespace(driver="fixture-model", repo=str(root / "repo"), state_dir=str(state))
    Path(cfg.repo).mkdir()
    cost._cfg = lambda: cfg
    usage_meters._usage_cache_clear_for_tests()
    durable = DurableState(str(state))
    durable.store.list_jobs()  # Bootstrap schema before measuring a running backend.
    counts = Counter()
    errors = []

    def instrument(store):
        for name in ("list_jobs", "list_tasks_for_jobs", "list_artifacts_for_jobs",
                     "list_job_summaries", "count_artifacts"):
            original = getattr(store, name, None)
            if original is None:
                continue

            def wrapped(*pos, _read=original, _name=name, **kw):
                counts[_name] += 1
                result = _read(*pos, **kw)
                if _name == "list_artifacts_for_jobs":
                    counts["artifact_bodies"] += len(result)
                return result

            setattr(store, name, wrapped)

    instrument(durable.store)
    original_open = cli_job_merge.open_cli_durable_at

    def open_store(*pos, **kw):
        counts["cli_opens"] += 1
        opened = original_open(*pos, **kw)
        if opened is not None:
            instrument(opened.store)
        return opened

    cli_job_merge.open_cli_durable_at = open_store
    meters = {"_tokens_used": 100, "_tokens_in": 100, "_tokens_out": 0, "_tokens_cached": 10}
    active = {"session_id": "session-a", "input_tokens": 100, "output_tokens": 0, "est_cost_usd": 0.01}
    pilot = SimpleNamespace(harness_session_id="session-a", config=cfg)

    def scoped(repo_root=None):
        counts["scoped_scans"] += 1
        rows = durable.list_jobs()
        if len(rows) != args.jobs or any(row["artifacts"] != args.artifacts_per_job for row in rows):
            errors.append("Historical job/artifact counts differ from the fixture")
        foreign = cli_job_merge.merge_running_cli_jobs_all_projects(seen_ids=set(), tasks_by_job={})
        if foreign:
            errors.append("Completed foreign jobs appeared as running")
        return [], durable.store, None

    services = dict(
        cfg=cfg, boot_repos=lambda: set(), boot_usage_meters=lambda: dict(meters),
        usage_cache_get=usage_meters._usage_cache_get, usage_cache_put=usage_meters._usage_cache_put,
        boot_session_cost=lambda *a: meters["_tokens_used"] / 1e6,
        scoped_jobs_with_stores=scoped, job_in_cost_window=lambda _: True,
        swarm_registry=lambda: [], job_swarm_accounting=lambda *a: (0, 0.0),
        tokens_cached_swarm=lambda _: 0, job_savings_fields=lambda _: {},
        active_session_total=lambda *a: dict(active), sum_job_set_savings=lambda *a, **kw: (0.0, 0.0),
        sum_job_set_savings_detail=lambda *a, **kw: {
            "routing_saved_usd": 0.0, "cache_saved_usd_swarm": 0.0,
            "routing_savings_basis": "unknown", "routing_tokens_compared": 0},
        cache_savings=lambda *a: 0.0, cache_savings_gross=lambda *a: 0.0,
        boot_cost_source=lambda: "estimated", tool_output_savings_fields=lambda *a, **kw: {},
        persist_boot_usage=lambda **kw: None, retry_on_locked=lambda fn: fn(),
        diag=lambda *a, **kw: None, get_pilot=lambda: pilot,
        active_session_id=lambda: active["session_id"],
    )
    # Older baseline revisions predate these production cache hooks.
    fields = {field.name for field in dataclasses.fields(usage.UsageServices)}
    optional = {
        "active_session_fingerprint": lambda: tuple(sorted(active.items())),
        "usage_store_fingerprint": getattr(cost, "_usage_store_fingerprint", None),
        "usage_request_lock": getattr(usage_meters, "_usage_request_lock", None),
    }
    services.update({key: value for key, value in optional.items() if key in fields})
    svc = usage.UsageServices(**services)
    writer = sqlite3.connect(str(state / "state.sqlite3"))
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE benchmark_heartbeat (id INTEGER PRIMARY KEY, tick INTEGER)")
    writer.commit()
    results = {}

    def measure(name, repetitions, change=None):
        before = counts.copy()
        walls, cpus = [], []
        for tick in range(repetitions):
            if change:
                change(tick)
            start, cpu = time.perf_counter(), time.process_time()
            status, payload = usage.get_usage("", svc)
            cpus.append(time.process_time() - cpu)
            walls.append(time.perf_counter() - start)
            assert not errors, errors
            assert status == 200 and payload["jobs"] == []
            assert payload["session"].get("read_status") != "unavailable", payload
            assert payload["session"]["tokens_used"] == meters["_tokens_used"], "Stale live tokens"
            assert payload["session_total"]["input_tokens"] == active["input_tokens"], "Stale receipt"
            assert payload["session_total"]["session_id"] == active["session_id"], "Wrong session"
        results[name] = {"calls": repetitions, "median_ms": statistics.median(walls) * 1000,
                         "wall_s": sum(walls), "cpu_s": sum(cpus), "counts": dict(counts - before)}

    def heartbeat(tick):
        writer.execute("INSERT OR REPLACE INTO benchmark_heartbeat VALUES (1, ?)", (tick,))
        writer.commit()

    def token(_):
        meters["_tokens_used"] += 1
        meters["_tokens_in"] += 1
        active["input_tokens"] += 1

    try:
        measure("cold", 1)
        measure("unchanged", args.repeats)
        measure("heartbeat", args.repeats, heartbeat)
        measure("pilot_tokens", args.repeats, token)
        measure("session_switch", 1, lambda _: active.update(session_id="session-b", input_tokens=13))
        measure("after_switch", args.repeats)
    finally:
        writer.close()
    digest = hashlib.sha256()
    for name in ("harness/state.py", "harness/cli_job_merge.py", "harness/api/usage.py",
                 "harness/api/usage_meters.py", "harness/api/cost.py"):
        digest.update((repo / name).read_bytes())
    return {"repo": str(repo), "source_sha256": digest.hexdigest(), "results": results,
            "process": process_resources(),
            "assertions": "passed", "cache_entries": len(usage_meters._usage_response_cache)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--candidate", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--jobs", type=int, default=100)
    parser.add_argument("--artifacts-per-job", type=int, default=20)
    parser.add_argument("--foreign-stores", type=int, default=2)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--fixture", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if min(args.jobs, args.artifacts_per_job, args.repeats) < 1 or args.foreign_stores < 0:
        parser.error("Counts must be positive; foreign-stores may be zero")
    with tempfile.TemporaryDirectory(prefix="usage-store-benchmark-") as temporary:
        root = Path(temporary)
        if args.worker:
            print(json.dumps(measure_repo(args.worker.resolve(), args.fixture, root, args)))
            return
        repos = [(label, repo.resolve()) for label, repo in
                 (("baseline", args.baseline), ("candidate", args.candidate)) if repo is not None]
        for _, repo in repos:
            if not (repo / "harness/api/usage.py").is_file():
                parser.error(f"Not a Marionette source checkout: {repo}")
        isolate(root)
        fixture = root / "fixture"
        make_fixture(fixture, args.jobs, args.artifacts_per_job)
        report = {"fixture": {"jobs_per_store": args.jobs, "artifacts_per_job": args.artifacts_per_job,
                              "foreign_stores": args.foreign_stores, "repeats": args.repeats},
                  "scope": "Isolated handler/store reads and child resources; no live HTTP, Electron, or real pricing"}
        for label, repo in repos:
            command = [sys.executable, str(Path(__file__).resolve()), "--worker", str(repo),
                       "--fixture", str(fixture), "--jobs", str(args.jobs),
                       "--artifacts-per-job", str(args.artifacts_per_job),
                       "--foreign-stores", str(args.foreign_stores), "--repeats", str(args.repeats)]
            completed = subprocess.run(command, capture_output=True, text=True, timeout=120)
            if completed.returncode:
                raise RuntimeError(f"{label} benchmark failed:\n{completed.stderr}")
            report[label] = json.loads(completed.stdout)
        rendered = json.dumps(report, indent=2)
        if args.output:
            args.output.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)


if __name__ == "__main__":
    main()
