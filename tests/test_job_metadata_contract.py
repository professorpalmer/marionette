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


def test_real_pm_five_role_swarm_is_visible_through_current_metadata(tmp_path):
    from dataclasses import replace
    from puppetmaster.orchestrator import Orchestrator
    from puppetmaster.workers import specs_for_roles

    store = create_store('sqlite', tmp_path / 'store')
    ctx = ReadContext('session-live', str(tmp_path / 'repo'), 'view-live', 'session')
    roles = ['explore', 'review', 'test', 'security-review', 'conflict-auditor']
    created = []
    specs = [replace(spec, adapter='local', payload={**spec.payload, 'auto_route': False})
             for spec in specs_for_roles(roles)]
    result = Orchestrator(store).run(
        'current metadata canonical swarm', specs=specs, worker_mode='inline',
        origin='marionette', session_id=ctx.session_id,
        on_job_created=lambda job: created.append(store.job_ref(job.id)),
    )
    assert len(created) == 1 and created[0].version == 2
    sources = KnownSources.from_roots([('harness', store.root, 'sqlite', False)])
    reader = MetadataReader(lambda: ActiveContext(ctx.session_id, ctx.repo, ctx.view_generation), sources)
    page = reader.read_job_page(ctx, sources.stores[0].selection)
    assert len(page['rows']) == 1
    summary = page['rows'][0]
    assert summary['selection']['job_ref'] == created[0].as_dict()
    assert summary['ownership'] == dict(origin='marionette', session_id=ctx.session_id, project_id=None)
    assert summary['task_count'] == 5
    selection = PMSelection(ctx, sources.stores[0].selection, created[0])
    detail = reader.read_selected_metadata(selection)
    assert {row['id'] for row in detail['tasks']['rows']} == {task.id for task in store.list_tasks(result.job.id)}
    assert {task.role for task in store.list_tasks(result.job.id)} == set(roles)


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


def test_selected_reads_use_exact_public_bodies_without_scans_or_writes(case, monkeypatch):
    store, job, reader, selection = case
    populate_history(store, job, 2)
    from puppetmaster.store import SwarmStore
    for name in ('list_jobs', 'list_tasks', 'list_artifacts', 'ensure_schema', 'init'):
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


def test_live_revision_advance_during_pin_read_keeps_header(case, monkeypatch):
    """A worker write landing mid-pin moves the job revision; the pin stays present."""
    store, job, reader, selection = case
    store.update_job_status(job.id, 'running')
    original = reader._header

    def advanced(store_handle, row, sel):
        store.save_task(Task(job.id, 'worker', 'landed mid-pin'))
        return original(store_handle, row, sel)

    monkeypatch.setattr(reader, '_header', advanced)
    pins = reader.read_pins(selection.context, [selection])
    result = pins['results'][0]['result']
    assert result['kind'] == 'present'
    assert result['row']['lifecycle'] == 'running'


def test_ownership_change_during_pin_read_is_unavailable(case, monkeypatch):
    store, job, reader, selection = case
    original = reader._header

    def changed(store_handle, row, sel):
        # Pins may follow a session move; losing session identity is a real change.
        store.save_job(replace(job, session_id=None, origin='other'))
        return original(store_handle, row, sel)

    monkeypatch.setattr(reader, '_header', changed)
    pins = reader.read_pins(selection.context, [selection])
    result = pins['results'][0]['result']
    assert result['kind'] == 'unavailable'
    assert result['reason'] == 'selection_changed'
    assert 'foreign' not in json.dumps(result)


def test_live_revision_advance_during_selected_read_keeps_lanes(case, monkeypatch):
    """A worker write landing mid-read moves the job revision; the selection is unchanged."""
    store, job, reader, _ = case
    handle = reader.sources.stores[0].handle
    original = handle.list_run_refs
    def advanced(*a, **kw):
        result = original(*a, **kw)
        store.save_task(Task(job.id, 'worker', 'landed mid-read'))
        return result
    monkeypatch.setattr(handle, 'list_run_refs', advanced)
    code, selected = detail(case)
    assert code == 200 and 'selection_changed' not in selected['missing']
    assert selected['lifecycle'] is not None
    assert selected['tasks']['page']['outcome'] == 'complete'
    assert selected['expert']['kind'] != 'unavailable'
    assert selected['expert']['coverage']['tasks'] == 'partial'


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


def expert_fixture(case, *, passed=False):
    store, job, _, _ = case
    task = Task(job.id, 'Reviewer', 'Inspect the real diff; token=private-value',
                adapter='codex', payload={'model': 'gpt-6-astra', 'api_key': 'never serialize this'})
    store.save_task(task)
    route = Artifact(job.id, task.id, ArtifactType.ROUTING, 'router-escalation',
        {'model_id': 'codex/gpt-6-astra', 'adapter_model_name': 'gpt-6-astra', 'adapter': 'codex',
         'policy': 'quality', 'provider': 'openai', 'role': 'Reviewer',
         'rejected': [{'model': 'smaller-model', 'reason': 'insufficient context'}]}, 1, ['router'])
    store.save_artifact(route)
    payload = dict(check='Regression suite', result='passed' if passed else 'failed',
                   passed=passed, detail='All green' if passed else 'Expected two rows; got one',
                   model='gpt-6-astra', real_cost_usd=0)
    payload.update(token_usage(sdk_usage={'inputTokens': 120, 'outputTokens': 0}))
    check = Artifact(job.id, task.id, ArtifactType.VERIFICATION, 'worker', payload, 1, ['pytest'])
    store.save_artifact(check)
    store.save_artifact(Artifact(job.id, task.id, ArtifactType.FINDING, 'worker',
                                {'claim': 'Exact routing matched the selected worker'}, 1, ['test']))
    return task, route, check


def test_expert_projects_current_identity_routing_failures_and_attested_zero(case):
    task, route, check = expert_fixture(case)
    code, selected = detail(case)
    assert code == 200
    expert = selected['expert']
    assert expert['kind'] == 'available'
    assert expert['coverage'] == {'tasks': 'complete', 'artifacts': 'complete'}
    assert expert['quality'] == 'degraded'
    worker = expert['tasks'][0]
    assert worker['id'] == task.id and worker['role'] == 'Reviewer'
    assert worker['model'] == 'gpt-6-astra' and worker['adapter'] == 'codex'
    assert worker['instruction'] == 'Inspect the real diff; token=REDACTED'
    assert worker['usage'] == dict(tokens_in=120, tokens_out=0, est_cost_usd=0,
                                  estimated=False, cost_provenance='provider')
    artifacts = {a['id']: a for a in expert['artifacts']}
    assert artifacts[route.id]['model'] == 'gpt-6-astra'
    assert artifacts[route.id]['created_by'] == 'router-escalation'
    assert artifacts[route.id]['rejected'] == [{'model': 'smaller-model', 'reason': 'insufficient context'}]
    assert artifacts[check.id]['headline'] == 'Regression suite'
    assert artifacts[check.id]['detail'] == 'Expected two rows; got one'
    assert artifacts[check.id]['check_result'] == 'failed'
    assert 'never serialize this' not in json.dumps(selected)


def test_expert_success_requires_complete_check_coverage_and_deduplicates_usage(case):
    task, _, check = expert_fixture(case, passed=True)
    store = case[0]
    store.save_artifact(replace(check, id='artifact_duplicate'))
    code, selected = detail(case)
    assert code == 200 and selected['expert']['quality'] == 'ok'
    assert selected['expert']['tasks'][0]['usage']['tokens_in'] == 120
    assert selected['expert']['header']['created_at'] == case[1].created_at


def test_expert_does_not_promote_old_generation_or_invent_usage(case):
    task, _, _ = expert_fixture(case)
    case[0].save_task(replace(task, generation=1, payload={'model': 'current-model'}))
    code, selected = detail(case)
    expert = selected['expert']
    assert code == 200 and expert['kind'] == 'partial'
    assert expert['quality'] == 'unverified' and expert['artifacts'] == []
    assert expert['tasks'][0]['model'] == 'current-model'
    assert all(value is None for value in expert['tasks'][0]['usage'].values())


def test_expert_rechecks_task_epoch_after_exact_body_reads(case, monkeypatch):
    task, _, _ = expert_fixture(case)
    handle = case[2].sources.stores[0].handle
    original = handle.get_task_by_id
    def raced(task_id):
        value = original(task_id)
        case[0].save_task(replace(task, generation=1, payload={'model': 'new-model'}))
        return value
    monkeypatch.setattr(handle, 'get_task_by_id', raced)
    code, selected = detail(case)
    assert code == 200 and selected['expert']['kind'] == 'unavailable'
    assert selected['expert']['tasks'] == [] and selected['expert']['artifacts'] == []


def test_explicit_header_is_cached_and_lists_stay_body_free(case, monkeypatch):
    store, job, reader, selection = case
    handle = reader.sources.stores[0].handle
    original = handle.get_job
    reads = []
    def exact(job_id):
        reads.append(job_id)
        assert job_id == job.id
        return original(job_id)
    monkeypatch.setattr(handle, 'get_job', exact)
    list_qs = {k: [str(v)] for k, v in dict(asdict(selection.context), **asdict(selection.store), mode='snapshot').items()}
    assert get_job_metadata(list_qs, reader)[0] == 200
    assert reads == []
    for _ in range(2):
        code, response = post_job_metadata_pins(dict(asdict(selection.context), selections=[selection.wire()]), reader)
        assert code == 200
        assert response['results'][0]['result']['row']['header']['created_at'] == job.created_at
    assert reads == [job.id]


def test_expert_caps_projection_and_marks_coverage(case):
    store, job, _, _ = case
    for n in range(50):
        store.save_task(Task(job.id, 'worker', 'x' * 20000, id=f'task_{n:03d}'))
    code, selected = detail(case)
    assert code == 200
    expert = selected['expert']
    assert expert['kind'] == 'partial' and expert['coverage']['tasks'] == 'partial'
    assert expert['quality'] == 'unverified'
    assert 0 < len(expert['tasks']) < 50
    assert all(t['instruction_truncated'] for t in expert['tasks'])
    assert len(json.dumps(selected).encode()) <= 98304


def test_expert_normal_claimed_worker_keeps_current_routing_and_usage(case):
    store, job, _, _ = case
    task, _, _ = expert_fixture(case, passed=True)
    claimed = store.claim_next_task(job.id, 'worker')
    assert claimed.id == task.id and claimed.generation == claimed.attempts == 1
    code, selected = detail(case)
    assert code == 200 and selected['expert']['kind'] == 'available'
    assert selected['expert']['tasks'][0]['usage']['est_cost_usd'] == 0
    assert len(selected['expert']['artifacts']) == 3


def test_expert_header_receipt_keeps_selected_zero_and_basis(case, monkeypatch):
    monkeypatch.setattr('puppetmaster.cost.load_registry', lambda *_args, **_kwargs: [])
    store, job, _, _ = case
    _, route, _ = expert_fixture(case, passed=True)
    store.save_artifact(replace(route, payload={**route.payload, 'billing': 'plan'}))
    store.update_job_status(job.id, JobStatus.COMPLETE)
    code, selected = detail(case)
    assert code == 200
    cost = selected['expert']['header']['cost']
    assert cost['source'] == 'terminal_cost_receipt'
    assert cost['selected_usd'] == 0
    assert cost['basis'] == 'estimated'  # Plan marginal pricing is not provider billing.
    assert cost['measured_cost_usd'] is None and cost['estimated_cost_usd'] == 0


def test_expert_one_passed_check_does_not_verify_unchecked_workers(case):
    expert_fixture(case, passed=True)
    store, job, _, _ = case
    store.save_task(Task(job.id, 'unchecked', 'Do another task'))
    code, selected = detail(case)
    assert code == 200 and selected['expert']['coverage']['tasks'] == 'complete'
    assert selected['expert']['quality'] == 'unverified'


def test_expert_unmatched_artifact_details_do_not_degrade_current_workers(case):
    store, job, _, _ = case
    expert_fixture(case, passed=True)
    artifact = Artifact(job.id, 'absent-task', ArtifactType.VERIFICATION, 'worker',
                        {'check': 'Unmatched check', 'result': 'failed', 'detail': 'Other task failed'}, 1, ['test'])
    store.save_artifact(artifact)
    code, selected = detail(case)
    assert code == 200
    expert = selected['expert']
    assert expert['quality'] == 'unverified' and expert['coverage']['artifacts'] == 'partial'
    unmatched = next(a for a in expert['artifacts'] if a['id'] == artifact.id)
    assert unmatched['task_id'] == 'absent-task' and unmatched['check_result'] == 'failed'
    assert unmatched['detail'] == 'Other task failed'


def test_expert_rereads_artifact_revisions_before_publishing_details(case, monkeypatch):
    _, _, check = expert_fixture(case)
    handle = case[2].sources.stores[0].handle
    original = handle.get_artifacts_by_ids
    def changed(job_id, artifact_ids):
        captured = original(job_id, artifact_ids)
        case[0].save_artifact(replace(check, payload={**check.payload, 'detail': 'Revised failure reason'}))
        return captured
    monkeypatch.setattr(handle, 'get_artifacts_by_ids', changed)
    code, selected = detail(case)
    assert code == 200 and selected['expert']['kind'] == 'unavailable'
    assert 'Expected two rows; got one' not in json.dumps(selected)
    assert selected['expert']['tasks'] == selected['expert']['artifacts'] == []


def test_expert_stops_hydrating_when_deadline_expires(case, monkeypatch):
    from types import SimpleNamespace
    import harness.job_readmodel as readmodel
    expert_fixture(case)
    ticks = iter([0, 3, 3, 3])
    monkeypatch.setattr(readmodel, 'time', SimpleNamespace(monotonic=lambda: next(ticks, 3)))
    handle = case[2].sources.stores[0].handle
    monkeypatch.setattr(handle, 'get_task_by_id', lambda *a: pytest.fail('task read after deadline'))
    monkeypatch.setattr(handle, 'get_artifacts_by_ids', lambda *a: pytest.fail('artifact read after deadline'))
    code, selected = detail(case)
    assert code == 200 and selected['expert']['reason'] == 'deadline'
    assert selected['tasks']['rows'] and selected['artifacts']['rows']


def test_expert_never_opens_foreign_or_wrong_incarnation_bodies(case, monkeypatch):
    store, job, reader, selection = case
    expert_fixture(case)
    handle = reader.sources.stores[0].handle
    monkeypatch.setattr(handle, 'get_job', lambda *a: pytest.fail('unauthorized job body'))
    foreign = replace(selection, job_ref=replace(selection.job_ref, incarnation=str(uuid4())))
    code, selected = get_job_metadata_detail(query(foreign), reader)
    assert code == 200 and selected['tasks']['rows'] == []
    store.save_job(replace(job, origin='other', session_id='foreign'))
    code, selected = detail(case)
    assert code == 200 and selected['tasks']['rows'] == []
    assert selected['display']['kind'] == 'unavailable'


def test_selected_economics_reads_exact_marionette_compaction_records(case):
    from harness.tool_output_savings import ToolOutputSavingsLedger
    store, job, _, _ = case
    expert_fixture(case, passed=True)
    ledger = ToolOutputSavingsLedger(str(store.root))
    assert ledger.record(session_id=job.session_id, job_id=job.id, tool_call_id='selected-call',
                         original_chars=800, compact_chars=400)
    assert ledger.record(session_id='foreign-session', job_id=job.id, tool_call_id='foreign-call',
                         original_chars=8000, compact_chars=400)
    code, selected = detail(case)
    assert code == 200
    economics = selected['expert']['economics']
    assert economics['compaction']['coverage'] == 'complete'
    assert economics['header']['savings']['compact_tokens'] == 100
    assert economics['header']['savings']['compaction_usd'] is None
    assert 'foreign-call' not in json.dumps(selected)
    assert economics['tasks'][selected['expert']['tasks'][0]['id']]['tokens'] == 120


def test_pin_headers_include_current_model_and_live_usage_without_disclosure(case):
    task, _, _ = expert_fixture(case)
    store, job, reader, selection = case
    code, response = post_job_metadata_pins(dict(asdict(selection.context), selections=[selection.wire()]), reader)
    assert code == 200
    header = response['results'][0]['result']['row']['header']
    assert header['model'] == 'gpt-6-astra'
    assert header['model_provenance'] == 'task_assignment'
    assert header['selected_workers'] == 1 and header['completed_workers'] == 0
    assert header['workers_complete'] is True
    assert header['usage']['tokens'] == 120 and header['usage']['complete'] is True
    assert header['cost']['selected_usd'] == 0 and header['cost']['basis'] == 'measured'
    assert header['quality'] == 'degraded'
    assert 'instruction' not in json.dumps(header) and 'rejected' not in json.dumps(header)
    assert task.id not in json.dumps(header)


def test_pin_headers_count_complete_workers_without_inheriting_one_model(case):
    from puppetmaster.models import TaskStatus
    expert_fixture(case)
    store, job, reader, selection = case
    store.save_task(Task(job.id, 'Second worker', 'Private second task', status=TaskStatus.COMPLETE,
                         adapter='codex', payload={'model': 'different-model'}))
    code, response = post_job_metadata_pins(dict(asdict(selection.context), selections=[selection.wire()]), reader)
    header = response['results'][0]['result']['row']['header']
    assert code == 200 and header['completed_workers'] == 1 and header['selected_workers'] == 2
    assert header['workers_complete'] is True
    assert header['model'] is None and header['model_provenance'] == 'unknown'
    assert header['usage']['complete'] is False and header['cost']['complete'] is False
    assert header['usage']['tokens_known_workers'] == 1


def test_pin_headers_refresh_usage_when_lifecycle_and_task_count_stay_fixed(case):
    task, _, check = expert_fixture(case)
    store, _, reader, selection = case
    body = dict(asdict(selection.context), selections=[selection.wire()])
    first = post_job_metadata_pins(body, reader)[1]['results'][0]['result']['row']
    payload = {**check.payload, 'real_cost_usd': 0.25}
    payload.update(token_usage(sdk_usage={'inputTokens': 240, 'outputTokens': 1}))
    store.save_artifact(replace(check, id='artifact_new_usage', payload=payload))
    second = post_job_metadata_pins(body, reader)[1]['results'][0]['result']['row']
    assert first['lifecycle'] == second['lifecycle'] and first['task_count'] == second['task_count']
    assert second['header']['usage']['tokens'] == 241
    assert second['header']['cost']['selected_usd'] == 0.25
    assert second['revision'] > first['revision']
