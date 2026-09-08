"""Selected history and economics through public Puppetmaster contracts."""
from dataclasses import asdict, replace


from uuid import uuid4


import json


import re


import pytest

from harness.job_metadata_capability import bounded_metadata_available

if not bounded_metadata_available():
    pytest.skip("public PM lacks bounded metadata APIs", allow_module_level=True)


from puppetmaster.attempts import ExecutionAttempt, UsageObservation


from puppetmaster.models import AgentRun, Artifact, ArtifactType, JobRef, JobStatus, Task


from puppetmaster.store_factory import create_store


from puppetmaster.usage import token_usage


from harness.api.job_readmodel import get_job_metadata, get_job_metadata_detail, post_job_metadata_pins


from harness.job_readmodel import ActiveContext, KnownSources, MetadataReader, PMSelection, ReadContext


@pytest.fixture(params=['sqlite', 'file'])
def case(tmp_path, request):
    store = create_store(request.param, tmp_path / 'store')
    job = store.create_job('expert goal', origin='marionette', session_id='session-a')
    ctx = ReadContext('session-a', str(tmp_path / 'repo'), 'view-a', 'session')
    sources = KnownSources.from_roots([('harness', store.root, request.param, False)])
    reader = MetadataReader(lambda: ActiveContext(ctx.session_id, ctx.repo, ctx.view_generation), sources)
    selection = PMSelection(ctx, sources.stores[0].selection, store.job_ref(job.id))
    return store, job, reader, selection


def query(selection, **extra):
    return {k: [str(v)] for k, v in dict(asdict(selection.context), source=selection.store.source,
                                      **selection.job_ref.as_dict(), **extra).items()}


def detail(case, **extra):
    return get_job_metadata_detail(query(case[3], **extra), case[2])


def test_v2_pins_display_and_explicit_legacy(case):
    store, job, reader, selection = case
    code, selected = detail(case)
    assert code == 200 and selected['selection']['job_ref'] == store.job_ref(job.id).as_dict()
    assert selected['display'] == dict(kind='available', goal_preview='expert goal',
        goal_preview_truncated=False, delivery='pending', quality='unverified')
    assert selected['cost']['kind'] == 'unavailable' and selected['cost']['reason'] == 'no_terminal_receipt'
    assert selected['history']['counts']['captured_attempts'] == 0
    assert selected['history']['counts']['complete_invocation_history'] is False
    legacy = replace(selection, job_ref=JobRef(job.id, selection.store.state_id))
    code, old = get_job_metadata_detail(query(legacy), reader)
    assert code == 200 and old['selection']['job_ref'] == legacy.job_ref.as_dict()
    assert old['cost']['reason'] == old['history']['reason'] == 'legacy_ref'
    code, pinned = post_job_metadata_pins(dict(asdict(selection.context), selections=[selection.wire(), legacy.wire()]), reader)
    assert code == 200
    assert [row['result']['row']['selection']['job_ref'] for row in pinned['results']] == [selection.job_ref.as_dict(), legacy.job_ref.as_dict()]


@pytest.mark.parametrize('fields', [dict(version='2'), dict(version='1', incarnation=str(uuid4())),
                                    dict(version='true'), dict(version='2', incarnation='bad')])
def test_invalid_nested_ref_is_rejected(case, fields):
    qs = query(case[3])
    qs.pop('version'); qs.pop('incarnation')
    qs.update({k: [v] for k, v in fields.items()})
    assert get_job_metadata_detail(qs, case[2])[0] == 400
    ref = {**case[3].job_ref.as_dict(), **fields}
    assert post_job_metadata_pins(dict(asdict(case[3].context), selections=[dict(case[3].wire(), job_ref=ref)]), case[2])[0] == 400


def populate_history(store, job, count):
    for n in range(count):
        run = AgentRun(job.id, f'task_{n}', 'worker', 'worker-a', id=f'run_{n}')
        store.save_run(run)
        store.record_attempt(ExecutionAttempt(job.id, run.task_id, run.id, f'attempt_{n}', 'now', 'codex', 'model-a', 'provider-a'))
        store.record_usage_observation(UsageObservation(job.id, f'attempt_{n}', f'observation_{n}', 'sdk', 'now',
            usage_state='measured', tokens_in=10, tokens_out=0, cost_state='measured', cost_usd=0,
            cost_basis='api', returncode=0, timed_out=False))


def test_captured_history_pagination_and_full_ref_cursor_binding(case):
    store, job, reader, selection = case
    populate_history(store, job, 35)
    code, first = detail(case)
    assert code == 200
    history = first['history']
    assert history['kind'] == 'available'
    assert history['counts'] == dict(captured_attempts=35, captured_runs=35, captured_process_outcomes=35,
        captured_observations=35, outcome='available', coverage='captured', complete_invocation_history=False)
    params = dict(attempts='attempt_cursor', runs='run_cursor', process_outcomes='process_outcome_cursor', observations='observation_cursor')
    for lane, parameter in params.items():
        token = history[lane]['page']['next_cursor']
        assert token and history[lane]['page']['outcome'] == 'partial'
        assert 0 < len(history[lane]['rows']) <= 20
        assert history[lane]['page']['scanned'] <= 21
        assert history[lane]['page']['complete_invocation_history'] is False
        ids = {row['sequence'] for row in history[lane]['rows']}
        while token:
            code, next_page = detail(case, **{parameter: token})
            assert code == 200
            rows = next_page['history'][lane]['rows']
            assert not ids.intersection(row['sequence'] for row in rows)
            ids.update(row['sequence'] for row in rows)
            token = next_page['history'][lane]['page']['next_cursor']
        assert len(ids) == 35
    observation = history['observations']['rows'][0]['facts']
    assert observation['tokens_out'] == observation['cost_usd'] == 0
    assert observation['identity_state'] == 'available' and observation['run_id'] and observation['task_id']
    assert history['runs']['rows'][0]['completion']['outcome'] == 'legacy_unknown'
    assert first['cost']['kind'] == 'unavailable'  # captured usage is not selected/live cost
    token = history['attempts']['page']['next_cursor']
    stale = replace(selection, job_ref=replace(selection.job_ref, incarnation=str(uuid4())))
    assert get_job_metadata_detail(query(stale, attempt_cursor=token), reader)[0] == 400
    assert detail(case, run_cursor=token)[0] == 400
    legacy = replace(selection, job_ref=JobRef(job.id, selection.store.state_id))
    assert get_job_metadata_detail(query(legacy, attempt_cursor=token), reader)[0] == 400
    assert len(json.dumps(first).encode()) < 98304


def test_selected_receipt_economics_preserves_metric_states(case):
    store, job, _, _ = case
    payload = dict(check='test', result='passed', model='model-a', real_cost_usd=0)
    payload.update(token_usage(sdk_usage={'inputTokens': 12, 'outputTokens': 0}))
    store.save_artifact(Artifact(job.id, 'task-a', ArtifactType.VERIFICATION, 'worker', payload, 1, ['test']))
    store.update_job_status(job.id, JobStatus.COMPLETE)
    code, selected = detail(case)
    expected = store.get_selected_economics(case[3].job_ref)
    assert expected.outcome == 'available'
    assert code == 200 and selected['cost'] == dict(kind=expected.outcome, **asdict(expected))
    assert selected['cost']['source'] == 'terminal_receipt'
    assert selected['cost']['totals']['tokens_out']['total'] == 0
    assert selected['cost']['totals']['tokens_out']['state'] == 'measured'
    assert selected['cost']['totals']['cache_read_tokens']['total'] is None
    assert selected['display']['delivery'] == selected['display']['quality'] == 'unverified'
    assert selected['artifact_count'] == 1
    assert selected['artifacts']['rows'][0]['check_result'] == 'unavailable'


def test_published_completion_receipt_from_bounded_run_page(case):
    store, job, _, selection = case
    store.save_task(Task(job.id, 'worker', 'work'))
    task = store.claim_next_task(job.id, 'worker-a')
    run = AgentRun(job.id, task.id, task.role, 'worker-a')
    expected = store.submit_completion(task, run, [], {}, job_ref=selection.job_ref)
    assert expected.outcome == 'published'
    code, selected = detail(case)
    assert code == 200
    assert selected['history']['runs']['rows'][0]['completion'] == asdict(expected)
    assert selected['task_count'] == 1
    assert selected['cancellation_authority'] is False


def test_selected_reads_never_hydrate_source_bodies_or_write(case, monkeypatch):
    store, job, reader, selection = case
    populate_history(store, job, 2)
    from puppetmaster.store import SwarmStore
    for name in ('get_job', 'list_jobs', 'list_tasks', 'list_artifacts', 'get_task_by_id', 'ensure_schema', 'init'):
        monkeypatch.setattr(SwarmStore, name, lambda *a, **kw: pytest.fail('body read or schema mutation'), raising=False)
    from puppetmaster.readonly import ReadConnection
    execute = ReadConnection.execute
    reads = []
    def checked(connection, sql, parameters=()):
        reads.append(sql)
        assert not re.search(r'\b(?:FROM|JOIN)\s+(?:jobs|tasks|artifacts|runs|execution_attempts|usage_observations|completions)\b', sql, re.I)
        assert not re.search(r'\b(?:INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b', sql, re.I)
        return execute(connection, sql, parameters)
    monkeypatch.setattr(ReadConnection, 'execute', checked)
    for _ in range(3):
        code, selected = detail(case)
        assert code == 200 and selected['history']['kind'] == 'available'
    assert any('historical_refs' in sql for sql in reads)
    assert any('selected_economics_current' in sql for sql in reads)


def test_ownership_change_during_selected_read_discards_all_lanes(case, monkeypatch):
    store, job, reader, _ = case
    handle = reader.sources.stores[0].handle
    original = handle.list_run_refs
    def changed(*a, **kw):
        result = original(*a, **kw)
        store.save_job(replace(job, session_id='foreign'))
        return result
    monkeypatch.setattr(handle, 'list_run_refs', changed)
    code, selected = detail(case)
    assert code == 200 and selected['missing'][0] == 'selection_changed'
    assert selected['lifecycle'] is None and selected['tasks']['rows'] == selected['artifacts']['rows'] == []
    assert selected['history']['kind'] == selected['cost']['kind'] == 'unavailable'
    assert 'foreign' not in json.dumps(selected)


def test_previous_membership_uses_same_policy_and_no_foreign_tombstones(case):
    store, job, reader, selection = case
    ctx = replace(selection.context, scope='all')
    filters = {k: [str(v)] for k, v in dict(asdict(ctx), **asdict(selection.store), mode='snapshot').items()}
    code, before = get_job_metadata(filters, reader)
    assert code == 200
    store.save_job(replace(job, session_id=None, origin=None))
    store.create_job('foreign', session_id=None, origin='other')
    fresh = MetadataReader(reader.active_context, reader.sources)
    filters.update(mode=['changes'], after_revision=[str(before['page']['checkpoint'])])
    code, changed = get_job_metadata(filters, fresh)
    assert code == 200 and changed['page']['outcome'] == 'complete'
    assert len(changed['rows']) == 1 and changed['rows'][0]['deleted'] is True
    assert changed['rows'][0].keys() == {'selection', 'revision', 'deleted'}
    assert 'foreign' not in json.dumps(changed)


def test_task_and_artifact_cursors_cannot_cross_incarnations_or_lanes(case):
    store, job, reader, selection = case
    for n in range(60):
        task = Task(job.id, 'worker', 'work', id=f'task_{n:04d}')
        store.save_task(task)
        store.save_artifact(Artifact(job.id, task.id, ArtifactType.FINDING, 'worker', {'claim': 'recorded'}, 1, ['test']))
    code, selected = detail(case)
    assert code == 200
    tokens = {lane: selected[lane]['page']['next_cursor'] for lane in ('tasks', 'artifacts')}
    assert all(tokens.values())
    stale = replace(selection, job_ref=replace(selection.job_ref, incarnation=str(uuid4())))
    for lane, parameter in (('tasks', 'task_cursor'), ('artifacts', 'artifact_cursor')):
        assert get_job_metadata_detail(query(stale, **{parameter: tokens[lane]}), reader)[0] == 400
    assert detail(case, artifact_cursor=tokens['tasks'])[0] == 400
    assert detail(case, task_cursor=tokens['artifacts'])[0] == 400
    assert selected['cancellation_authority'] is False


def test_cli_previous_membership_never_promotes_foreign_origin(case):
    store, job, _, selection = case
    store.save_job(replace(job, origin='foreign'))
    ctx = replace(selection.context, scope='all')
    sources = KnownSources.from_roots([('cli', store.root, store.backend_name, False)])
    reader = MetadataReader(lambda: ActiveContext(ctx.session_id, ctx.repo, ctx.view_generation), sources)
    checkpoint = store.list_job_summaries().revision
    # This prior session stamp is insufficient for CLI ownership.
    store.save_job(replace(job, origin=None, session_id=None))
    query_values = dict(asdict(ctx), **asdict(sources.stores[0].selection), mode='changes', after_revision=checkpoint)
    code, changed = get_job_metadata({k: [str(v)] for k, v in query_values.items()}, reader)
    assert code == 200 and changed['rows'] == []
    assert changed['page']['outcome'] == 'complete'
