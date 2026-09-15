"""Mid-flight producer/parser parity: RUNNING jobs must not trip invalid_metadata.

Set MARIONETTE_REGENERATE_MIDFLIGHT_FIXTURE=1 to regenerate the committed
frontend fixture. Ordinary test runs write only inside pytest's temporary directory.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from harness.job_metadata_capability import bounded_metadata_available

if not bounded_metadata_available():
    pytest.skip("public PM lacks bounded metadata APIs", allow_module_level=True)

from puppetmaster.attempts import ExecutionAttempt, UsageObservation
from puppetmaster.models import AgentRun, Artifact, ArtifactType, Task, TaskStatus
from puppetmaster.store_factory import create_store

from harness import job_expert
from harness.job_readmodel import ActiveContext, KnownSources, MetadataReader, PMSelection, ReadContext

FIXTURE = Path(__file__).resolve().parents[1] / 'webapp' / 'src' / '__tests__' / 'fixtures' / 'midflight-running.json'


def _midflight_store(tmp_path):
    store = create_store('sqlite', tmp_path / 'store', mode='ensure')
    store.init()
    job = store.create_job('midflight parity swarm', origin='marionette', session_id='session-A')
    store.update_job_status(job.id, 'running')
    job = store.get_job(job.id)
    specs = [
        ('explore', TaskStatus.QUEUED, {'model': 'deepseek-v4-flash'}),
        ('review', TaskStatus.RUNNING, {'model': 'deepseek-v4-flash'}),
        ('test', TaskStatus.COMPLETE, {'model': 'deepseek-v4-flash'}),
        ('security', TaskStatus.QUEUED, {'model': ''}),
    ]
    tasks = []
    for index, (role, status, payload) in enumerate(specs):
        task = Task(job.id, role, f'instruction for {role}', adapter='agentic', status=status,
                    payload={'session_id': 'session-A', **payload})
        if index == 3:
            task = replace(task, created_at=None, updated_at=None)
        store.save_task(task)
        tasks.append(store.get_task_by_id(task.id))
    claimed = store.claim_next_task(job.id, 'worker-1')
    assert claimed is not None
    tasks = [store.get_task_by_id(task.id) for task in tasks]
    nostart = Task(job.id, 'nostart', 'not started yet', adapter='agentic', status=TaskStatus.QUEUED,
                   payload={'session_id': 'session-A'}, created_at=None, updated_at=None)
    store.save_task(nostart)
    tasks.append(store.get_task_by_id(nostart.id))
    for task in tasks[:3]:
        store.save_artifact(Artifact(job.id, task.id, ArtifactType.ROUTING, 'router', {
            'model_id': 'deepseek-v4-flash', 'adapter': 'agentic', 'policy': 'balanced',
            'estimated_cost_usd': 0.02, 'baseline_cost_usd': 0.1, 'role': task.role,
            'task_generation': task.generation, 'lease_id': task.lease_id,
        }, 0.9, ['route']))
    store.save_artifact(Artifact(job.id, tasks[2].id, ArtifactType.FINDING, 'worker', {
        'claim': 'tests passed', 'tokens_in': 1000, 'tokens_out': 200, 'real_cost_usd': 0.05,
        'task_generation': tasks[2].generation, 'lease_id': tasks[2].lease_id,
    }, 0.8, ['usage']))
    store.save_artifact(Artifact(job.id, tasks[1].id, ArtifactType.FINDING, 'worker', {
        'claim': 'still looking', 'tokens_in': 500, 'tokens_out': 10,
        'task_generation': tasks[1].generation, 'lease_id': tasks[1].lease_id,
    }, 0.4, ['usage']))
    run = AgentRun(job.id, tasks[1].id, tasks[1].role, 'worker-1')
    store.save_run(run)
    store.record_attempt(ExecutionAttempt.from_run(
        run, adapter='agentic', model='deepseek-v4-flash', provider='deepseek'))
    store.record_usage_observation(UsageObservation(
        job.id, run.id, 'obs-1', 'sdk', run.started_at, usage_state='measured',
        tokens_in=10, tokens_out=1, cost_state='measured', cost_usd=0.01, cost_basis='api'))
    return store, job, tasks


def test_timestamp_normalizes_offset_without_colon():
    assert job_expert.timestamp('2026-09-11T12:00:00+0000').endswith('+00:00')
    assert job_expert.timestamp('2026-09-11T12:00:00') is None
    assert job_expert.model_name('') is None
    assert job_expert.model_name('  ') is None
    assert job_expert.confidence_value(1.5) is None


def test_midflight_selected_pins_and_running_page_dump_fixture(tmp_path):
    committed_fixture = FIXTURE.read_bytes()
    regenerate = os.environ.get('MARIONETTE_REGENERATE_MIDFLIGHT_FIXTURE') == '1'
    fixture = FIXTURE if regenerate else tmp_path / FIXTURE.name
    store, job, tasks = _midflight_store(tmp_path)
    sources = KnownSources.from_roots([('harness', store.root, 'sqlite', False)])
    ctx = ReadContext('session-A', '/repo', 'generation-1', 'session')
    reader = MetadataReader(lambda: ActiveContext(ctx.session_id, ctx.repo, ctx.view_generation), sources)
    selection = PMSelection(ctx, sources.stores[0].selection, store.job_ref(job.id))
    detail = reader.read_selected_metadata(selection)
    pins = reader.read_pins(ctx, [selection])
    page = reader.read_job_page(ctx, sources.stores[0].selection, status='running')

    assert detail['lifecycle'] == 'running'
    assert detail['tasks']['page']['revision'] == detail['artifacts']['page']['revision']
    assert detail['expert']['kind'] in ('available', 'partial', 'unavailable')
    assert {task['role'] for task in detail['expert']['tasks']} <= {task.role for task in tasks}
    assert all(task['model'] in (None, 'deepseek-v4-flash') for task in detail['expert']['tasks'])
    nostart = next(task for task in detail['expert']['tasks'] if task['role'] == 'nostart')
    assert nostart['created_at'] is None and nostart['updated_at'] is None
    assert detail['cost']['kind'] == 'unavailable'
    assert detail['history']['kind'] == 'available'
    assert detail['history']['attempts']['rows']
    assert pins['results'][0]['result']['kind'] == 'present'
    assert page['rows'] and page['rows'][0]['lifecycle'] == 'running'

    payload = {
        'detail': detail,
        'pins': pins,
        'page': page,
        'context': asdict(ctx),
        'selection': selection.wire(),
    }
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + '\n')
    assert fixture.stat().st_size > 1000
    if not regenerate:
        assert FIXTURE.read_bytes() == committed_fixture


def test_lane_revision_skew_degrades_expert_not_lanes(tmp_path):
    from harness.job_readmodel import PAGE_BUDGET

    store, job, _ = _midflight_store(tmp_path)
    sources = KnownSources.from_roots([('harness', store.root, 'sqlite', False)])
    ctx = ReadContext('session-A', str(tmp_path / 'repo'), 'generation-1', 'session')
    reader = MetadataReader(lambda: ActiveContext(ctx.session_id, ctx.repo, ctx.view_generation), sources)
    selection = PMSelection(ctx, sources.stores[0].selection, store.job_ref(job.id))
    attached, row, reason = reader._selected(selection)
    assert reason is None and attached is not None
    pages = {
        'tasks': attached.list_task_refs(selection.job_ref, cursor=None, **PAGE_BUDGET),
        'artifacts': attached.list_artifact_refs(selection.job_ref, cursor=None, **PAGE_BUDGET),
    }
    assert pages['tasks'].revision == pages['artifacts'].revision
    skewed = {
        'tasks': pages['tasks'],
        'artifacts': replace(pages['artifacts'], revision=pages['artifacts'].revision + 1),
    }
    expert = reader._expert(attached, row, selection, skewed, dict(tasks=None, artifacts=None))
    assert expert == job_expert.unavailable('lane_revision_skew')


def test_read_page_retries_snapshot_unavailable_then_returns_live_page(monkeypatch):
    from types import SimpleNamespace

    from harness import job_readmodel

    naps = []
    monkeypatch.setattr(job_readmodel.time, 'sleep', naps.append)
    pages = iter([
        SimpleNamespace(outcome='unavailable', reason='read_snapshot_unavailable', retry_after_ms=40),
        SimpleNamespace(outcome='unavailable', reason='read_snapshot_unavailable', retry_after_ms=None),
        SimpleNamespace(outcome='complete', reason=None, retry_after_ms=None),
    ])
    calls = []

    def method(*args, **kwargs):
        calls.append((args, kwargs))
        return next(pages)

    page = job_readmodel._read_page(method, 'ref', cursor=None, limit=1)
    assert page.outcome == 'complete'
    assert len(calls) == 3 and calls[0] == (('ref',), dict(cursor=None, limit=1))
    assert naps == [0.04, 0.1]


def test_read_page_gives_up_after_bounded_retries(monkeypatch):
    from types import SimpleNamespace

    from harness import job_readmodel

    monkeypatch.setattr(job_readmodel.time, 'sleep', lambda _s: None)
    locked = SimpleNamespace(outcome='unavailable', reason='read_snapshot_unavailable', retry_after_ms=100)
    calls = []
    page = job_readmodel._read_page(lambda: calls.append(1) or locked)
    assert page is locked and len(calls) == job_readmodel.SNAPSHOT_RETRIES


def test_read_page_does_not_retry_other_outcomes(monkeypatch):
    from types import SimpleNamespace

    from harness import job_readmodel

    monkeypatch.setattr(job_readmodel.time, 'sleep', lambda _s: pytest.fail('unexpected sleep'))
    missing = SimpleNamespace(outcome='unavailable', reason='missing', retry_after_ms=None)
    calls = []
    assert job_readmodel._read_page(lambda: calls.append(1) or missing) is missing
    assert calls == [1]
