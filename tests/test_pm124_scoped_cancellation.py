"""Candidate PM stores are the cancellation oracle; no mocked receipts."""
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from harness.api.scoped_cancellation import runtime_available
if not runtime_available():
    pytest.skip("requires exact PM cancellation contracts", allow_module_level=True)
from puppetmaster.contracts import JobRef
from puppetmaster.models import Task, TaskStatus
from puppetmaster.state import state_identity
from puppetmaster.store_contracts import task_binding
from puppetmaster.store_factory import create_store

from harness.api.jobs import make_job_services, post_swarm_cancel, get_cancellation_receipt
from harness.api.scoped_cancellation import cancellation_view
from harness.job_scoping import job_label_for_session, stamp_task_payload


@pytest.fixture
def case(tmp_path):
    store = create_store('sqlite', tmp_path / 'primary')
    job = store.create_job('cancel test', label=job_label_for_session('session-a'),
                           origin='marionette', session_id='session-a')
    repo = str(tmp_path / 'repo')
    store.save_task(Task(job_id=job.id, role='worker', instruction='test',
                        payload=stamp_task_payload({}, session_id='session-a', cwd=repo)))
    task = store.claim_next_task(job.id, 'owner-a')
    assert task is not None
    state = SimpleNamespace(store=store)
    svc = make_job_services(cfg=SimpleNamespace(repo=repo), sessions=SimpleNamespace(active='session-a'),
                            get_session=lambda: SimpleNamespace(state=lambda: state),
                            get_pilot=lambda: None)
    selection = {'version': 2, 'source': 'harness', 'session_id': 'session-a', 'repo': repo,
                 'job_ref': store.job_ref(job.id).as_dict(),
                 'bindings': [asdict(task_binding(task))]}
    return store, task, svc, {'selection': selection, 'request_id': 'request-a'}


def query(body):
    s = body['selection']
    return {k: [str(v)] for k, v in {**s['job_ref'], 'source': s['source'], 'repo': s['repo'],
                                'session_id': s['session_id'], 'request_id': body['request_id']}.items()}


def receipt(store, body):
    return store.get_cancellation_receipt(JobRef(**body['selection']['job_ref']), body['request_id'])


def test_request_is_pending_exact_replay_and_read_do_not_claim_stop(case):
    store, task, svc, body = case
    before = store.get_task_by_id(task.id)
    code, result = post_swarm_cancel(body, svc)
    assert code == 200 and result['receipt']['outcome'] == 'requested'
    assert result['receipt']['cleanup'] == 'unknown'
    assert store.get_task_by_id(task.id) == before
    assert post_swarm_cancel(body, svc) == (code, result)
    assert get_cancellation_receipt(query(body), svc) == (code, result)
    assert receipt(store, body).revision == 1
    assert store.cancellation_pending(JobRef(**body['selection']['job_ref']), task_binding(task))


def test_duplicate_request_different_body_conflicts(case):
    store, task, svc, body = case
    assert post_swarm_cancel(body, svc)[0] == 200
    changed = {**body, 'selection': {**body['selection'], 'bindings': [asdict(replace(task_binding(task), generation=99))]}}
    code, result = post_swarm_cancel(changed, svc)
    assert code == 200 and result['receipt']['outcome'] == 'conflict'
    assert receipt(store, body).bindings == (task_binding(task),)
    assert receipt(store, body).revision == 1


@pytest.mark.parametrize('change', [{'generation': 99}, {'lease_id': 'other'}, {'lease_owner': 'other'}])
def test_changed_generation_or_lease_never_cancels_successor(case, change):
    store, task, svc, body = case
    successor = replace(task, **change)
    store.save_task(successor)
    code, result = post_swarm_cancel(body, svc)
    assert code == 200 and result['receipt']['outcome'] == 'stale_binding'
    ref = JobRef(**body['selection']['job_ref'])
    assert not store.cancellation_pending(ref, task_binding(successor))
    assert not store.cancellation_pending(ref, task_binding(task))


def test_same_job_id_other_store_unchanged(case, tmp_path, monkeypatch):
    store, task, svc, body = case
    other = create_store('sqlite', tmp_path / 'other')
    monkeypatch.setattr('puppetmaster.models.new_id', lambda _: task.job_id)
    other.create_job('collision', label=job_label_for_session('session-a'))
    other.save_task(task)
    assert post_swarm_cancel(body, svc)[0] == 200
    other_ref = other.job_ref(task.job_id)
    assert other.get_cancellation_receipt(other_ref, body['request_id']) is None
    assert not other.cancellation_pending(other_ref, task_binding(task))
    wrong = {**body, 'selection': {**body['selection'], 'job_ref': other_ref.as_dict()}}
    assert post_swarm_cancel(wrong, svc)[0] == 409


@pytest.mark.parametrize('field', ['session', 'repo', 'store'])
def test_context_changes_before_write_have_no_receipt(case, field, tmp_path):
    store, _, svc, body = case
    original = store.list_task_refs
    def switch(*args, **kwargs):
        page = original(*args, **kwargs)
        if field == 'session': svc.sessions.active = 'other'
        elif field == 'repo': svc.cfg.repo = str(tmp_path / 'other')
        else: svc.get_session = lambda: SimpleNamespace(state=lambda: SimpleNamespace(store=None))
        return page
    store.list_task_refs = switch
    assert post_swarm_cancel(body, svc)[0] == 409
    assert receipt(store, body) is None


@pytest.mark.parametrize('field', ['session', 'repo'])
def test_context_changes_inside_request_reject_acknowledgement(case, field):
    store, _, svc, body = case
    original = store.request_cancellation
    def switch(*args):
        result = original(*args)
        if field == 'session': svc.sessions.active = 'other'
        else: svc.cfg.repo = '/other'
        return result
    store.request_cancellation = switch
    code, result = post_swarm_cancel(body, svc)
    assert code == 409 and result['code'] == 'cancellation_context_changed'
    assert receipt(store, body).outcome == 'requested'


@pytest.mark.parametrize('count', [200, 201])
def test_capacity_never_silently_selects_first_200(case, count):
    store, task, svc, body = case
    tasks = [task]
    for i in range(count - 1):
        new = replace(task, id=f'extra-{i}', status=TaskStatus.COMPLETE)
        store.save_task(new)
        tasks.append(new)
    body['selection']['bindings'] = [asdict(task_binding(t)) for t in tasks[:200]]
    code, result = post_swarm_cancel(body, svc)
    if count == 200:
        assert code == 200 and result['receipt']['outcome'] == 'requested'
    else:
        assert code == 409 and receipt(store, body) is None
    rendered = [{'id': t.id, 'binding': asdict(task_binding(t))} for t in tasks]
    view = cancellation_view(store, JobRef(**body['selection']['job_ref']), rendered)
    assert view['status'] == ('complete' if count == 200 else 'partial')


def test_mixed_terminal_workers_need_only_live_worker_observation(case):
    store, task, svc, body = case
    finished = replace(task, id='finished', status=TaskStatus.COMPLETE)
    store.save_task(finished)
    body['selection']['bindings'].append(asdict(task_binding(finished)))
    assert post_swarm_cancel(body, svc)[1]['receipt']['outcome'] == 'requested'
    # This checks the store transition, not proof of a process exit.
    ref = JobRef(**body['selection']['job_ref'])
    store.observe_cancellation(ref, task_binding(task), cleanup='partial')
    code, result = get_cancellation_receipt(query(body), svc)
    assert code == 200 and result['receipt']['outcome'] == 'observed_stop'
    assert result['receipt']['cleanup'] == 'partial'


def test_terminal_receipt_and_missing_receipt_are_honest(case):
    store, task, svc, body = case
    assert get_cancellation_receipt(query(body), svc)[0] == 404
    store.save_task(replace(task, status=TaskStatus.COMPLETE))
    assert post_swarm_cancel(body, svc)[1]['receipt']['outcome'] == 'already_terminal'


@pytest.mark.parametrize('field,value', [('session_id', 'foreign'), ('cwd', '/foreign')])
def test_task_ownership_before_request_and_receipt(case, field, value):
    store, task, svc, body = case
    store.save_task(replace(task, payload={**task.payload, field: value}))
    assert post_swarm_cancel(body, svc)[0] == 409
    assert receipt(store, body) is None


def test_bounded_metadata_no_list_tasks_and_mismatched_render_rejected(case):
    store, task, svc, body = case
    store.list_tasks = lambda *_: pytest.fail('unbounded cancellation task read')
    assert post_swarm_cancel(body, svc)[0] == 200
    rendered = [{'id': task.id, 'binding': asdict(replace(task_binding(task), generation=200))}]
    assert cancellation_view(store, JobRef(**body['selection']['job_ref']), rendered)['status'] == 'unavailable'


def test_real_supervised_command_stops(case, tmp_path, monkeypatch):
    """PM supervises a real subprocess; its cancellation scope records observation."""
    import os
    import sys
    import threading
    import time
    from puppetmaster.cancellation import cancellation_scope, JobCancelled
    from puppetmaster.adapters._streaming import run_streamed_subprocess
    store, task, svc, body = case
    monkeypatch.setenv('PUPPETMASTER_STATE_DIR', str(store.root))
    pid_file = tmp_path / 'command.pid'
    command = [sys.executable, '-c',
               'import os,time,pathlib; pathlib.Path(' + repr(str(pid_file)) + ').write_text(str(os.getpid())); time.sleep(30)']
    errors = []
    stopped = []
    def worker():
        try:
            with cancellation_scope(store, task):
                run_streamed_subprocess(command=command, env=None, task=task,
                                        sidecar_name='cancel-proof', timeout_seconds=15,
                                        cwd=str(tmp_path), start_new_session=True)
        except JobCancelled:
            stopped.append(True)
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not pid_file.exists() and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pid_file.exists(), errors
        pid = int(pid_file.read_text())
        os.kill(pid, 0)
        assert post_swarm_cancel(body, svc)[0] == 200
        thread.join(8)
        assert not thread.is_alive() and not errors and stopped
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        code, result = get_cancellation_receipt(query(body), svc)
        assert code == 200 and result['receipt']['outcome'] == 'observed_stop'
        assert result['receipt']['cleanup'] == 'unknown'
    finally:
        thread.join(20)


def test_reconcile_original_receipt_after_new_tasks_appear(case):
    store, task, svc, body = case
    assert post_swarm_cancel(body, svc)[0] == 200
    extra = replace(task, id='new-task', generation=20)
    store.save_task(extra)
    assert get_cancellation_receipt(query(body), svc)[0] == 200
    assert post_swarm_cancel(body, svc)[0] == 200
    assert not store.cancellation_pending(JobRef(**body['selection']['job_ref']), task_binding(extra))


def test_receipt_read_revalidates_owner_and_store_at_response(case):
    store, task, svc, body = case
    assert post_swarm_cancel(body, svc)[0] == 200
    original = store.get_cancellation_receipt
    def switch(*args):
        result = original(*args)
        svc.sessions.active = 'other'
        return result
    store.get_cancellation_receipt = switch
    assert get_cancellation_receipt(query(body), svc)[0] == 409


@pytest.mark.parametrize('generation', [True, -1, '1'])
def test_invalid_generation_is_not_coerced(case, generation):
    store, _, svc, body = case
    body['selection']['bindings'][0]['generation'] = generation
    assert post_swarm_cancel(body, svc)[0] == 409
    assert receipt(store, body) is None


def test_changed_job_ownership_during_request_rejects_response(case):
    store, task, svc, body = case
    original = store.request_cancellation
    def mutate(*args):
        value = original(*args)
        store.save_job(replace(store.get_job(task.job_id), project_id='changed-project'))
        return value
    store.request_cancellation = mutate
    assert post_swarm_cancel(body, svc)[0] == 409


def test_cli_binding_uses_only_explicit_cli_store(case, monkeypatch):
    store, _, svc, body = case
    body['selection']['source'] = 'cli'
    monkeypatch.setattr('harness.cli_job_merge.resolve_cli_state_dir', lambda _: str(store.root))
    svc.get_session = lambda: pytest.fail('CLI cancellation must not fall back to harness')
    assert post_swarm_cancel(body, svc)[0] == 200
    assert get_cancellation_receipt(query(body), svc)[0] == 200


@pytest.mark.parametrize('case_fixture', ['case', 'real_session_case'])
def test_http_route_wiring_for_request_and_receipt(request, case_fixture):
    import json
    from harness.http_routes import build_get_routes, build_post_json_routes
    _, _, svc, body = request.getfixturevalue(case_fixture)
    class Services:
        def __getattr__(self, _):
            return lambda: svc
    sent = []
    handler = SimpleNamespace(_send=lambda code, value: sent.append((code, json.loads(value))))
    build_post_json_routes(Services())['/api/swarm/cancel'](handler, body)
    assert sent[-1][0] == 200 and sent[-1][1]['receipt']['outcome'] == 'requested'
    build_get_routes(Services())['/api/swarm/cancellation-receipt'](handler, None, query(body))
    assert sent[-1] == sent[-2]


@pytest.fixture
def real_session_case(case):
    from harness.session import Session
    from harness.state import DurableState
    store, task, svc, body = case
    session = Session.__new__(Session)
    session.state_dir = str(store.root)
    first, second = session.state(), session.state()
    assert isinstance(first, DurableState) and isinstance(second, DurableState)
    assert first.store is not second.store
    assert state_identity(first.store.root) == state_identity(second.store.root)
    svc.get_session = lambda: session
    return store, task, svc, body


@pytest.mark.parametrize('field', ['state_dir', 'session', 'repo'])
@pytest.mark.parametrize('phase', ['before_write', 'inside_request', 'receipt_read'])
def test_real_session_context_changes_refuse(real_session_case, tmp_path, monkeypatch, field, phase):
    store, _, svc, body = real_session_case
    if phase == 'receipt_read':
        assert post_swarm_cancel(body, svc)[0] == 200
    method = {'before_write': 'list_task_refs', 'inside_request': 'request_cancellation',
              'receipt_read': 'get_cancellation_receipt'}[phase]
    original = getattr(type(store), method)
    def switch(current_store, *args, **kwargs):
        result = original(current_store, *args, **kwargs)
        if field == 'state_dir': svc.get_session().state_dir = str(tmp_path / 'other')
        elif field == 'session': svc.sessions.active = 'other'
        else: svc.cfg.repo = str(tmp_path / 'other-repo')
        return result
    monkeypatch.setattr(type(store), method, switch)
    code, _ = (get_cancellation_receipt(query(body), svc) if phase == 'receipt_read'
               else post_swarm_cancel(body, svc))
    assert code == 409
    assert (receipt(store, body) is None) == (phase == 'before_write')


def test_real_session_authority_does_not_recreate_state(real_session_case, monkeypatch):
    store, _, svc, body = real_session_case
    session = svc.get_session()
    original = session.state
    calls = []
    def state():
        calls.append(True)
        return original()
    monkeypatch.setattr(session, 'state', state)
    assert post_swarm_cancel(body, svc)[0] == 200
    assert len(calls) == 1
    assert get_cancellation_receipt(query(body), svc)[0] == 200
    assert len(calls) == 2
    assert receipt(store, body).outcome == 'requested'


@pytest.mark.parametrize('field', ['lease_owner', 'lease_id'])
@pytest.mark.parametrize('length', [256, 257])
def test_published_bindings_match_request_boundary(case, field, length):
    store, task, svc, body = case
    task = replace(task, **{field: 'a' * length})
    store.save_task(task)
    binding = asdict(task_binding(task))
    body['selection']['bindings'] = [binding]
    view = cancellation_view(store, JobRef(**body['selection']['job_ref']),
                             [{'id': task.id, 'binding': binding}])
    assert view['status'] == ('complete' if length == 256 else 'unavailable')
    assert post_swarm_cancel(body, svc)[0] == (200 if length == 256 else 409)


def test_recreated_store_path_cannot_cancel_same_id_successor(case, tmp_path, monkeypatch):
    store, task, svc, body = case
    root = store.root
    root.rename(tmp_path / 'retired-primary')
    replacement = create_store('sqlite', root)
    monkeypatch.setattr('puppetmaster.models.new_id', lambda prefix: task.job_id if prefix == 'job' else 'task_successor')
    successor = replacement.create_job('successor', origin='marionette', session_id='session-a')
    replacement.save_task(Task(successor.id, 'worker', 'new work',
        payload=stamp_task_payload({}, session_id='session-a', cwd=svc.cfg.repo)))
    new_ref = replacement.job_ref(successor.id)
    assert new_ref.state_id == body['selection']['job_ref']['state_id']
    assert new_ref.incarnation != body['selection']['job_ref']['incarnation']
    svc.get_session = lambda: SimpleNamespace(state=lambda: SimpleNamespace(store=replacement))
    assert post_swarm_cancel(body, svc)[0] == 409
    assert get_cancellation_receipt(query(body), svc)[0] == 409
    assert replacement.get_cancellation_receipt(new_ref, body['request_id']) is None
