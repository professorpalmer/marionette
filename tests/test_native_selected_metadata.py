"""Real native producers through the selected API and authenticated HTTP boundary."""
import copy
import json
from pathlib import Path

import pytest

pytest.importorskip('harness.job_readmodel', reason='requires companion PR2 metadata reader')

from harness.api.job_readmodel import get_local_metadata_detail
from harness.job_readmodel import ActiveContext, KnownSources, MetadataReader
from harness.local_job_routing import _routing_artifact
from test_local_job_metadata import Runner, forbidden


def producer(tmp_path, monkeypatch):
    runner = Runner(tmp_path)
    route = _routing_artifact('cheap-model', 0.01, role='implement', reason='Policy chose forecast',
                              rejected=[], policy='balanced')
    monkeypatch.setattr('harness.local_job_routing.preview_agentic_route',
                        lambda *a, **kw: {'artifact': route})
    runner._register_local_job('local-native', 'Keyboard disclosure', role='implement', engine='agentic')
    return runner


def endpoint(runner):
    index = runner.local_metadata_handle()
    ctx = dict(session_id='A', repo=runner.config.repo, view_generation='generation', scope='session')
    reader = MetadataReader(lambda: ActiveContext('A', runner.config.repo, 'generation'),
                            KnownSources(()), local_handle=index)
    def read(lane, **kwargs):
        values = dict(ctx, job_id='local-native', incarnation=index.incarnation, lane=lane)
        values.update(kwargs)
        return get_local_metadata_detail({k: [v] for k, v in values.items()}, reader)
    return read


def test_producer_instruction_forecast_fallback_and_realized(tmp_path, monkeypatch):
    runner = producer(tmp_path, monkeypatch)
    read = endpoint(runner)
    status, tasks = read('tasks')
    assert status == 200
    assert tasks['rows'] == [dict(task_id='local-native-w0', role='implement (agentic)',
        instruction='Keyboard disclosure', status='running', adapter='agentic', model='',
        model_kind='unavailable', truncated=False)]
    route = read('routing')[1]['rows'][0]
    assert (route['task_id'], route['association'], route['model_kind'], route['model'],
            route['policy'], route['created_by']) == (
                'local-native-w0', 'explicit', 'forecast', 'cheap-model', 'balanced', 'router')
    job = runner._local_jobs['local-native']
    # Retained fallback data is distinct from the initial router producer.
    job['artifacts'].append(dict(job['artifacts'][0], model='fallback-model',
        created_by='router-fallback', detail='Recorded fallback'))
    runner._persist_local_jobs()
    routes = read('routing')[1]['rows']
    assert [r['ordinal'] for r in routes] == [0, 1]
    assert routes[-1]['created_by'] == 'router-fallback'
    assert routes[-1]['detail'] == 'Recorded fallback'
    assert job['model'] == read('tasks')[1]['rows'][0]['model'] == ''
    runner._refresh_local_job_routed_model('local-native', 'actual-model', engine='agentic')
    assert read('tasks')[1]['rows'][0]['model'] == 'agentic/actual-model'
    assert read('routing')[1]['rows'][-1]['model_kind'] == 'forecast'
    runner._finish_local_job('local-native', ok=True, model='actual-model', engine='agentic')
    routes = read('routing')[1]['rows']
    assert all(r['model_kind'] == 'realized' and r['model'] == 'actual-model' for r in routes)
    assert routes[-1]['created_by'] == 'router-fallback'


@pytest.mark.parametrize('damage', ['none', 'label', 'session', 'adapter', 'multiple', 'foreign_id'])
def test_legacy_association_requires_owner_proof(tmp_path, monkeypatch, damage):
    runner = producer(tmp_path, monkeypatch)
    job = runner._local_jobs['local-native']
    art = job['artifacts'][0]
    del art['task_id']
    if damage == 'label':
        job['label'] = json.dumps(dict(origin='external', session_id='A'))
    elif damage == 'session':
        job['label'] = json.dumps(dict(origin='marionette', session_id='B'))
    elif damage == 'adapter':
        job['tasks'][0]['adapter'] = 'foreign'
    elif damage == 'multiple':
        job['tasks'].append(dict(job['tasks'][0], id='another'))
    elif damage == 'foreign_id':
        art['task_id'] = 'foreign-w0'
    runner._persist_local_jobs()
    route = endpoint(runner)('routing')[1]['rows'][0]
    assert route['association'] == ('legacy_owner_single_task' if damage == 'none' else 'unavailable')
    assert route['task_id'] == ('local-native-w0' if damage == 'none' else None)
    assert route['created_by'] == 'router'


@pytest.mark.parametrize('fact', ['instruction', 'policy'])
def test_nested_revision_only_invalidates_cursor(tmp_path, monkeypatch, fact):
    runner = producer(tmp_path, monkeypatch)
    job = runner._local_jobs['local-native']
    job['artifacts'] = [dict(job['artifacts'][0]) for _ in range(80)]
    read = endpoint(runner)
    before = read('routing')[1]
    cursor = before['page']['next_cursor']
    assert cursor
    updated_at = job['updated_at']
    if fact == 'instruction':
        job['tasks'][0]['instruction'] = 'changed only instruction'
    else:
        job['artifacts'][-1]['policy'] = 'changed only policy'
    runner._persist_local_jobs()
    assert job['updated_at'] == updated_at
    assert read('routing', cursor=cursor)[1]['page']['outcome'] == 'expired'
    assert read('tasks')[1]['page']['revision'] > before['page']['revision']


def test_caps_scan_budget_refusal_and_no_read_writes(tmp_path, monkeypatch):
    runner = producer(tmp_path, monkeypatch)
    job = runner._local_jobs['local-native']
    job['goal'] = 'g' * 10000
    job['tasks'][0].update(instruction='\U0001f600' * 10000, role='r' * 1000)
    job['tasks'] = [dict(job['tasks'][0], id=f'task-{i}') for i in range(90)]
    job['artifacts'] = [{'type': 'FINDING', 'body': object()} for _ in range(60)] + [
        dict(type='ROUTING', model='m' * 10000, detail='d' * 10000)]
    read = endpoint(runner)
    index = runner.local_metadata_handle()
    revision = index.revision
    monkeypatch.setattr(runner, '_persist_local_jobs_locked', forbidden)
    monkeypatch.setattr(runner, '_publish_local_metadata_locked', forbidden)
    monkeypatch.setattr(copy, 'deepcopy', forbidden)
    monkeypatch.setattr('builtins.open', forbidden)
    for lane in ('tasks', 'routing'):
        status, first = read(lane)
        assert status == 200 and first['page']['outcome'] == 'partial'
        assert first['page']['scanned'] <= 51 and len(first['rows']) <= 50
        assert len(json.dumps(first).encode()) <= 32768
        cursor = first['page']['next_cursor']
        assert read(lane, job_id='foreign', cursor=cursor)[0] == 400
        assert read('routing' if lane == 'tasks' else 'tasks', cursor=cursor)[0] == 400
        assert read(lane, incarnation='stale')[1]['page']['outcome'] == 'expired'
        assert read(lane, incarnation='stale', cursor=cursor)[0] == 400
        assert read(lane, job_id='foreign')[1]['rows'] == []
        assert read(lane, session_id='B')[0] == 409
        assert read(lane, cursor=cursor)[0] == 200
    task = read('tasks')[1]['rows'][0]
    assert len(task['instruction']) == 1024 and len(task['role']) == 160 and task['truncated']
    assert read('routing')[1]['rows'] == []
    summary = index.read_page(dict(session_id='A', repo=runner.config.repo,
                                  view_generation='generation', scope='session'))['rows'][0]
    assert 'goal_preview' not in summary and 'g' * 240 not in json.dumps(summary)
    assert 'instruction' not in json.dumps(summary) and 'body' not in json.dumps(summary)
    assert index.revision == revision




def test_selected_oversize_row_refuses_without_nonprogress_cursor(tmp_path, monkeypatch):
    runner = producer(tmp_path, monkeypatch)
    job = runner._local_jobs['local-native']
    job.update(goal='\U0001f600' * 10000, model='\U0001f600' * 1000, cwd='\U0001f600' * 1000)
    job['tasks'][0].update(**{key: '\U0001f600' * 10000
                             for key in ('instruction', 'role', 'model', 'adapter', 'status')})
    runner._persist_local_jobs()
    status, body = endpoint(runner)('tasks', include_context='true')
    assert status == 200 and len(json.dumps(body).encode()) <= 32768
    assert body['page']['outcome'] == 'unavailable'
    assert body['page']['next_cursor'] is None
    assert 'selected_row_budget' in body['missing']


def test_foreign_membership_and_restart_incarnation(tmp_path, monkeypatch):
    runner = producer(tmp_path, monkeypatch)
    read = endpoint(runner)
    old = runner.local_metadata_handle().incarnation
    restarted = Runner(tmp_path)
    assert endpoint(restarted)('tasks', incarnation=old)[1]['page']['outcome'] == 'expired'
    assert endpoint(restarted)('routing')[1]['rows'][0]['association'] == 'explicit'
    runner._local_jobs['local-native']['session_id'] = 'B'
    for lane in ('tasks', 'routing'):
        status, body = read(lane)
        assert status == 200 and body['rows'] == [] and 'summary' not in body
        assert 'selected_membership_unavailable' in body['missing']


@pytest.mark.parametrize('fact', ['instruction', 'policy'])
def test_persistence_preserves_unchanged_provider_cursors(tmp_path, fact):
    runner = Runner(tmp_path)
    for name in ('a', 'b'):
        jid = 'local-' + name
        runner._register_local_job(jid, 'instruction-' + name, engine='native', model='model-' + name)
        runner._local_jobs[jid]['artifacts'] = [
            dict(type='ROUTING', task_id=jid + '-w0', policy='original') for _ in range(80)]
    index = runner.local_metadata_handle()
    context = dict(session_id='A', repo=runner.config.repo, view_generation='generation', scope='session')
    pages = {jid: index.read_selected(context, index.ref(jid), lane='routing') for jid in index.rows}
    revisions = {jid: row['revision'] for jid, row in index.rows.items()}
    active_revision = index.active_revision
    runner._persist_local_jobs()
    assert {jid: row['revision'] for jid, row in index.rows.items()} == revisions
    assert index.active_revision == active_revision
    for jid, page in pages.items():
        assert page['page']['next_cursor']
        assert index.read_selected(context, index.ref(jid), lane='routing',
            cursor=page['page']['next_cursor'])['page']['outcome'] == 'complete'
    job = runner._local_jobs['local-a']
    timestamp = job['updated_at']
    if fact == 'instruction':
        job['tasks'][0]['instruction'] = 'nested change'
    else:
        job['artifacts'][-1]['policy'] = 'nested change'
    runner._persist_local_jobs()
    assert job['updated_at'] == timestamp
    assert index.rows['local-a']['revision'] > revisions['local-a']
    assert index.rows['local-b']['revision'] == revisions['local-b']
    for jid, outcome in [('local-a', 'expired'), ('local-b', 'complete')]:
        assert index.read_selected(context, index.ref(jid), lane='routing',
            cursor=pages[jid]['page']['next_cursor'])['page']['outcome'] == outcome
    changes = index.read_page(context, lane='active', mode='changes', after_revision=active_revision)
    assert [r['local_ref']['job_id'] for r in changes['rows']] == ['local-a']
    revision = index.revision
    runner._persist_local_jobs()
    assert index.revision == revision


@pytest.mark.parametrize('model', [None, 'explicit-model'])
def test_configured_task_model_is_assigned_before_execution(tmp_path, model):
    runner = Runner(tmp_path)
    runner.config.driver = 'configured-model'
    runner._register_local_job('local-native', 'pending work', engine='native', model=model)
    task = endpoint(runner)('tasks')[1]['rows'][0]
    assert task['model'] == 'native/' + (model or 'configured-model')
    assert task['model_kind'] == 'assigned'
