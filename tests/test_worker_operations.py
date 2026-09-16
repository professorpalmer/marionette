"""Real PM store records through the selected-job HTTP boundary."""
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from harness.api.worker_operations import get_worker_operations, post_worker_operation
from harness.job_metadata_capability import ActiveContext, worker_operation_handler
from harness.job_readmodel import KnownSources, MetadataReader, PMSelection, ReadContext
from puppetmaster.models import Artifact, ArtifactType, JobRef, Task
from puppetmaster.store_factory import create_store


@pytest.fixture(params=['sqlite', 'file'])
def env(tmp_path, request):
    root = tmp_path / 'state'
    store = create_store(request.param, root, mode='ensure')
    store.init()
    job = store.create_job('Worker operations', origin='marionette', session_id='session-A')
    sources = KnownSources.from_roots([('harness', root, request.param, False)])
    ctx = ReadContext('session-A', str(tmp_path), 'generation-1', 'session')
    active = [ActiveContext(ctx.session_id, ctx.repo, ctx.view_generation)]
    reader = MetadataReader(lambda: active[0], sources)
    job_ref = store.list_job_summaries(limit=1).items[0].job_ref
    selection = PMSelection(ctx, sources.stores[0].selection, job_ref)
    task = Task(job.id, 'review', 'Review the fixture', payload={'session_id': ctx.session_id, 'cwd': ctx.repo})
    store.save_task(task)
    return SimpleNamespace(store=store, reader=reader, ctx=ctx, active=active, selection=selection, task=task)


def query(env, **kw):
    values = {**asdict(env.ctx), **asdict(env.selection.store), **env.selection.job_ref.as_dict(), **kw}
    return {key: [str(value)] for key, value in values.items()}


def save_verdict(env, verdict='PASS', **payload):
    artifact = Artifact(env.task.job_id, env.task.id, ArtifactType.VERIFICATION, 'review-worker',
                        dict(kind='worker_verdict', source='worker', advisory=True, verdict=verdict,
                             reason='Review fixture', check='Fixture review', result='unknown', **payload), 1.0, ['worker_verdict'])
    env.store.save_artifact(artifact)
    return artifact


def action(env, kind='stop_loop', **options):
    bindings = [asdict(item.binding) for item in env.store.list_task_refs(env.selection.job_ref).items]
    return dict(version=1, context=asdict(env.ctx), selection=env.selection.wire(),
                request_id='request-1', bindings=bindings, command=dict(kind=kind, **options))


@pytest.mark.parametrize('verdict', ['PASS', 'FAIL', 'PARTIAL', 'UNKNOWN'])
def test_structured_records_are_advisory_not_lifecycle(env, verdict):
    artifact = save_verdict(env, verdict)
    status, result = get_worker_operations(query(env), env.reader)
    assert status == 200
    assert result['lifecycle'] == 'queued'
    assert result['ledger']['rows'][0]['id'] == artifact.id
    assert result['ledger']['rows'][0]['verdict'] == (verdict if verdict != 'UNKNOWN' else 'unknown')
    assert result['verdict'] == dict(state='recorded', authority='worker_advisory')
    assert result['accounting'] == 'references_only'
    assert result['capabilities']['quality_loop']['state'] == 'unsupported'


def test_prose_and_completed_lifecycle_cannot_mint_pass(env):
    from puppetmaster.models import JobStatus
    job = env.store.get_job(env.task.job_id)
    env.store.save_job(replace(job, status=JobStatus.COMPLETE))
    env.store.save_artifact(Artifact(job.id, env.task.id, ArtifactType.FINDING, 'worker',
                                    {'claim': 'VERDICT: PASS - everything is fine'}, 1.0, ['fixture']))
    status, result = get_worker_operations(query(env), env.reader)
    assert status == 200
    assert result['lifecycle'] == 'complete'
    assert result['verdict']['state'] == 'missing'
    assert result['ledger']['rows'] == []


def test_bound_ledger_cursor_cannot_cross_job_or_view(env):
    for _ in range(24):
        save_verdict(env)
    status, first = get_worker_operations(query(env), env.reader)
    assert status == 200
    assert len(first['ledger']['rows']) <= 20
    assert first['ledger']['scanned'] <= 21
    assert first['ledger']['outcome'] == 'partial'
    cursor = first['ledger']['next_cursor']
    status, second = get_worker_operations(query(env, cursor=cursor), env.reader)
    assert status == 200
    assert set(r['id'] for r in first['ledger']['rows']).isdisjoint(r['id'] for r in second['ledger']['rows'])
    assert get_worker_operations(dict(query(env, cursor=cursor), job_id=['job_other']), env.reader)[0] in (400, 409)
    env.active[0] = replace(env.active[0], generation='generation-2')
    assert get_worker_operations(query(env, cursor=cursor), env.reader)[0] == 409


def test_selection_rejects_collision_session_repo_and_incarnation(env):
    for key, value in [('session_id', 'session-B'), ('repo', env.ctx.repo + '/other'),
                       ('state_id', 'another-store'), ('incarnation', '12345678-1234-4234-8234-123456789abc')]:
        assert get_worker_operations(dict(query(env), **{key: [value]}), env.reader)[0] == 409
    other = env.store.create_job('Other session', origin='marionette', session_id='session-B')
    ref = next(item.job_ref for item in env.store.list_job_summaries().items if item.job_ref.job_id == other.id)
    foreign = dict(query(env), scope=['all'], job_id=[ref.job_id], incarnation=[ref.incarnation])
    assert get_worker_operations(foreign, env.reader)[0] == 409


def test_duplicate_blank_selector_is_rejected(env):
    assert get_worker_operations(dict(query(env), job_id=['', env.task.job_id]), env.reader)[0] == 400


def test_mid_read_aba_and_recreated_job_do_not_publish_verdict(env, monkeypatch):
    save_verdict(env)
    handle = env.reader.sources.stores[0].handle
    original = handle.get_artifacts_by_ids

    def switched(*args, **kw):
        result = original(*args, **kw)
        env.active[0] = replace(env.active[0], generation='generation-3')
        return result

    monkeypatch.setattr(handle, 'get_artifacts_by_ids', switched)
    assert get_worker_operations(query(env), env.reader) == (409, {'code': 'view_changed'})


def test_old_attempt_verdict_is_unknown(env):
    save_verdict(env)
    env.store.save_task(replace(env.task, generation=3, attempts=3))
    status, result = get_worker_operations(query(env), env.reader)
    assert status == 200
    assert result['ledger']['rows'][0]['verdict'] == 'unknown'


@pytest.mark.parametrize('kind,options', [
    ('quality_loop', dict(mode='goal', max_iterations=3, cost_cap_usd=1.0, cleanup=True)),
    ('quality_loop', dict(mode='review_pass', max_iterations=3, cost_cap_usd=1.0, cleanup=False)),
    ('stop_loop', {}), ('broadcast', dict(message='Check the regression')),
])
def test_missing_kernel_contract_never_falls_back_to_cancel(env, monkeypatch, kind, options):
    handle = env.reader.sources.stores[0].handle
    monkeypatch.setattr(handle, 'request_cancellation', lambda *a, **k: pytest.fail('must not cancel only current tasks'))
    status, result = post_worker_operation(action(env, kind, **options), env.reader)
    assert status == 409
    assert result['outcome'] == 'unsupported'
    assert result['request_id'] == 'request-1'
    assert result['selection'] == env.selection.wire()


def test_targeted_steer_and_broadcast_bind_every_exact_recipient(env):
    body = action(env, 'steer', task_id=env.task.id, message='Check the regression')
    assert post_worker_operation(body, env.reader)[1]['outcome'] == 'unsupported'
    stale = {**body, 'bindings': [{**body['bindings'][0], 'generation': 90}]}
    assert post_worker_operation(stale, env.reader)[0] == 409
    wrong = {**body, 'command': {**body['command'], 'task_id': 'task_other'}}
    assert post_worker_operation(wrong, env.reader)[0] == 400
    broadcast = action(env, 'broadcast', message='Check the regression')
    env.store.save_task(Task(env.task.job_id, 'review', 'New worker', payload=env.task.payload))
    assert post_worker_operation(broadcast, env.reader)[0] == 409


def test_actions_reject_cross_scope_before_admission(env):
    body = action(env)
    env.store.save_task(replace(env.task, payload={**env.task.payload, 'session_id': 'session-B'}))
    assert post_worker_operation(body, env.reader)[0] == 409


@pytest.mark.parametrize('field,value', [('max_iterations', 0), ('max_iterations', True),
                                       ('cost_cap_usd', float('nan')), ('cost_cap_usd', -1), ('cleanup', 'yes')])
def test_invalid_loop_bounds(env, field, value):
    body = action(env, 'quality_loop', mode='goal', max_iterations=3, cost_cap_usd=1, cleanup=False)
    body['command'][field] = value
    assert post_worker_operation(body, env.reader)[0] == 400


def test_unsupported_metadata_host_does_not_construct_reader():
    view = SimpleNamespace(supported=False, reader=lambda: pytest.fail('legacy metadata host'))
    assert worker_operation_handler('get_worker_operations')({}, view)[0] == 503
