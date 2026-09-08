from types import SimpleNamespace
from contextlib import closing

import pytest
from harness.api.scoped_cancellation import runtime_available
from puppetmaster.cancellation import is_cancelled
from puppetmaster.state import state_identity
from tests.test_scoped_job_artifacts import stores
from harness.api import jobs


def store_dump(store):
    import sqlite3
    with closing(sqlite3.connect(store.root / 'state.sqlite3')) as connection:
        return list(connection.iterdump())


def selection(qs, **changes):
    values = {key: value[0] for key, value in qs.items()}
    values.update(changes)
    return {'version': 1, 'job_ref': {'job_id': values.pop('job_id'),
            'state_id': values.pop('state_id')}, **values}


@pytest.fixture
def v2_stores(stores, monkeypatch):
    if not runtime_available():
        pytest.skip("requires exact PM cancellation contracts")
    from puppetmaster.models import Task
    from harness.job_scoping import job_label_for_session, stamp_task_payload

    primary, cli, svc, qs = stores
    # Keep the cross-store collision, using PM's generated-ID shape and ownership.
    monkeypatch.setattr('puppetmaster.models.new_id', lambda prefix: prefix + '_0123456789ab')
    for state in (primary, cli):
        job = state.store.create_job('cancel test', label=job_label_for_session('session-a'),
                                    origin='marionette', session_id='session-a')
        state.store.save_task(Task(job_id=job.id, role='test', instruction='test',
            payload=stamp_task_payload({}, session_id='session-a', cwd=svc.cfg.repo)))
    return primary, cli, svc, {**qs, 'job_id': [job.id]}


def v2_selection(qs, store, source='harness'):
    from dataclasses import asdict
    from puppetmaster.store_contracts import task_binding

    jid = qs['job_id'][0]
    return {**selection(qs, source=source), 'version': 2,
            'job_ref': store.job_ref(jid).as_dict(),
            'bindings': [asdict(task_binding(task)) for task in store.list_tasks(jid)]}


@pytest.mark.parametrize('source', ['harness', 'cli'])
def test_running_durable_cancel_refuses_without_changing_either_store(stores, source):
    primary, cli, svc, qs = stores
    chosen, other = (primary, cli) if source == 'harness' else (cli, primary)
    jid = qs['job_id'][0]
    chosen.store.update_job_status(jid, "running")
    before = [store_dump(s.store) for s in (chosen, other)]
    flag_before = is_cancelled(jid)
    code, result = jobs.post_swarm_cancel({'job_id': jid, 'selection': selection(qs,
        source=source, state_id=state_identity(chosen.store.root))}, svc)
    assert code == 409
    assert result['code'] == 'scoped_kernel_cancellation_required'
    assert result['ok'] is False
    assert [store_dump(s.store) for s in (chosen, other)] == before
    assert is_cancelled(jid) == flag_before


@pytest.mark.parametrize('field,value', [('state_id', 'wrong'), ('source', 'other'),
    ('session_id', 'foreign'), ('repo', '/foreign'), ('job_id', 'missing')])
def test_mismatch_has_no_effect(stores, field, value):
    primary, cli, svc, qs = stores
    before = [s.store.get_job(qs['job_id'][0]).status for s in (primary, cli)]
    assert jobs.post_swarm_cancel({'selection': selection(qs, **{field: value})}, svc)[0] == 409
    assert [s.store.get_job(qs['job_id'][0]).status for s in (primary, cli)] == before
    assert not is_cancelled(qs['job_id'][0])


@pytest.mark.parametrize('body', [{}, {'job_id': 'job_collision'}, {'selection': None},
    {'selection': {}}, {'selection': {'version': 2}}, {'selection': []}])
def test_missing_or_malformed_selection(stores, body):
    primary, _, svc, qs = stores
    before = primary.store.get_job(qs['job_id'][0]).status
    assert jobs.post_swarm_cancel(body, svc)[0] in (400, 409)
    assert primary.store.get_job(qs['job_id'][0]).status == before


@pytest.mark.parametrize('method', ['list_job_summaries'])
def test_real_failure_is_503(v2_stores, monkeypatch, method):
    primary, cli, svc, qs = v2_stores
    ref = v2_selection(qs, primary.store)
    before = [store_dump(s.store) for s in (primary, cli)]
    calls = []
    def fail(*args, **kwargs):
        calls.append(kwargs)
        raise OSError('private store failure')
    # V2 ownership is read through bounded summaries, replacing legacy get_job.
    monkeypatch.setattr(primary.store, method, fail)
    code, result = jobs.post_swarm_cancel({'selection': ref, 'request_id': 'io-failure'}, svc)
    assert len(calls) == 1
    assert calls[0]['job_ref'].as_dict() == ref['job_ref']
    assert code == 503
    assert 'private' not in str(result)
    assert [store_dump(s.store) for s in (primary, cli)] == before
    assert not is_cancelled(qs['job_id'][0])


def test_context_switch_before_mutation_refuses(stores):
    primary, _, svc, qs = stores
    original = primary.store.list_tasks
    def switch(jid):
        tasks = original(jid)
        svc.sessions.active = 'other'
        return tasks
    primary.store.list_tasks = switch
    before = primary.store.get_job(qs['job_id'][0]).status
    assert jobs.post_swarm_cancel({'selection': selection(qs)}, svc)[0] == 409
    assert primary.store.get_job(qs['job_id'][0]).status == before


def test_local_cancel_uses_only_captured_pilot(stores):
    _, _, svc, qs = stores
    cancelled = []
    pilot = SimpleNamespace(harness_session_id='session-a',
        get_local_job=lambda jid: {'id': jid, 'session_id': 'session-a', 'cwd': svc.cfg.repo},
        cancel_local_job=lambda jid, **kwargs: cancelled.append(jid) or True,
        _local_metadata=SimpleNamespace(incarnation="local-incarnation"))
    svc.get_pilot = lambda: pilot
    svc.get_session = lambda: pytest.fail('local selection must not open a durable session')
    ref = {**selection(qs, source='local', job_id='local-test', state_id=None),
           'local_incarnation': 'local-incarnation'}
    assert jobs.post_swarm_cancel({'selection': ref}, svc)[0] == 200
    assert cancelled == ['local-test']
    assert not is_cancelled('local-test')
    ref['session_id'] = 'other'
    assert jobs.post_swarm_cancel({'selection': ref}, svc)[0] == 409
    assert cancelled == ['local-test']


def test_live_rows_expose_actual_store_refs(stores):
    from dataclasses import asdict
    primary, cli, svc, qs = stores
    svc.cfg.driver = 'test'
    svc.job_swarm_accounting = lambda *args: (0, 0)
    errors = []
    svc.diag = lambda *args: errors.append(str(args))
    jid = qs['job_id'][0]
    svc.scoped_jobs_with_stores = lambda **_: ([
        {**asdict(primary.store.get_job(jid)), 'source': 'harness'},
        {**asdict(cli.store.get_job(jid)), 'source': 'cli'},
    ], primary.store, cli.store)
    code, result = jobs.get_swarm_live(None, svc)
    assert code == 200
    assert result['jobs'], errors
    assert [(j['source'], j['job_ref']) for j in result['jobs']] == [
        ('harness', {'job_id': jid, 'state_id': state_identity(primary.store.root)}),
        ('cli', {'job_id': jid, 'state_id': state_identity(cli.store.root)}),
    ]


@pytest.mark.parametrize('source', ['harness', 'cli'])
def test_v2_store_refs_expose_complete_bindings_and_cancel_only_selected(v2_stores, source):
    from harness.api.scoped_cancellation import cancellation_view

    primary, cli, svc, qs = v2_stores
    chosen, other = (primary, cli) if source == 'harness' else (cli, primary)
    jid = qs['job_id'][0]
    ref = chosen.store.job_ref(jid)
    selection_v2 = v2_selection(qs, chosen.store, source)
    assert ref.as_dict() == {'job_id': jid, 'state_id': state_identity(chosen.store.root),
                             'version': 2, 'incarnation': chosen.store.incarnation}
    assert ref != other.store.job_ref(jid)
    tasks = [{'id': b['task_id'], 'binding': b} for b in selection_v2['bindings']]
    assert tasks
    before = [store_dump(s.store) for s in (chosen, other)]
    view = cancellation_view(chosen.store, ref, tasks)
    assert view['status'] == 'complete', view
    assert view['bindings'] == [t['binding'] for t in tasks]
    assert [store_dump(s.store) for s in (chosen, other)] == before
    wrong = {**selection_v2, 'job_ref': other.store.job_ref(jid).as_dict()}
    assert jobs.post_swarm_cancel({'selection': wrong, 'request_id': 'wrong-store'}, svc)[0] == 409
    assert [store_dump(s.store) for s in (chosen, other)] == before
    code, result = jobs.post_swarm_cancel({'selection': selection_v2, 'request_id': 'v2-cancel'}, svc)
    assert code == 200
    assert result['receipt']['outcome'] == 'requested'
    assert result['receipt']['job_ref'] == ref.as_dict()
    assert result['receipt']['bindings'] == tuple(view['bindings'])
    assert chosen.store.get_cancellation_receipt(ref, 'v2-cancel') is not None
    assert store_dump(other.store) == before[1]
    assert not is_cancelled(jid)


def test_legacy_unique_primary_refuses_running_worker(stores, monkeypatch):
    primary, _, svc, qs = stores
    monkeypatch.setattr('harness.cli_job_merge.resolve_cli_state_dir', lambda _: None)
    jid = qs['job_id'][0]
    before = (primary.store.get_job(jid), primary.store.list_tasks(jid))
    assert jobs.post_swarm_cancel({'job_id': jid}, svc)[0] == 409
    assert (primary.store.get_job(jid), primary.store.list_tasks(jid)) == before
    assert not is_cancelled(jid)


def test_legacy_local_refuses_without_touching_global_flag(stores):
    _, _, svc, qs = stores
    cancelled = []
    pilot = SimpleNamespace(harness_session_id='session-a',
        get_local_job=lambda jid: {'id': jid, 'session_id': 'session-a', 'cwd': svc.cfg.repo},
        cancel_local_job=lambda jid, **kwargs: cancelled.append(jid) or True,
        _local_metadata=SimpleNamespace(incarnation="local-incarnation"))
    svc.get_pilot = lambda: pilot
    assert jobs.post_swarm_cancel({'job_id': 'local-test'}, svc)[0] == 409
    assert cancelled == []
    assert not is_cancelled('local-test')


@pytest.mark.parametrize('status', ['complete', 'failed', 'cancelled'])
def test_terminal_row_is_not_rewritten(stores, status):
    primary, _, svc, qs = stores
    jid = qs['job_id'][0]
    primary.store.update_job_status(jid, status)
    before = primary.store.get_job(jid)
    code, result = jobs.post_swarm_cancel({'selection': selection(qs)}, svc)
    assert code == 409
    assert result['code'] == 'scoped_kernel_cancellation_required'
    assert primary.store.get_job(jid) == before


@pytest.mark.parametrize('field,value', [('job_id', 'missing'), ('state_id', 'wrong'),
    ('session_id', 'foreign'), ('repo', '/foreign')])
def test_refusal_does_not_write_cli_records(stores, field, value):
    import sqlite3
    _, cli, svc, qs = stores
    def dump():
        with closing(sqlite3.connect(cli.store.root / 'state.sqlite3')) as connection:
            return list(connection.iterdump())
    before = dump()
    ref = selection(qs, source='cli', state_id=state_identity(cli.store.root))
    if field in ('job_id', 'state_id'):
        ref['job_ref'][field] = value
    else:
        ref[field] = value
    assert jobs.post_swarm_cancel({'selection': ref}, svc)[0] == 409
    assert dump() == before


def test_cli_attach_failure_is_503(v2_stores, monkeypatch):
    primary, cli, svc, qs = v2_stores
    ref = v2_selection(qs, cli.store, 'cli')
    before = [store_dump(s.store) for s in (primary, cli)]
    calls = []
    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise OSError('private attach detail')
    monkeypatch.setattr('puppetmaster.store_factory.create_store', fail)
    code, result = jobs.post_swarm_cancel({'selection': ref, 'request_id': 'attach-failure'}, svc)
    assert calls == [(('sqlite', str(cli.store.root)), {'mode': 'attach'})]
    assert code == 503
    assert 'private' not in str(result)
    assert [store_dump(s.store) for s in (primary, cli)] == before
    assert not is_cancelled(qs['job_id'][0])


def test_no_sibling_discovery_during_legacy_cancel(stores, monkeypatch):
    _, _, svc, _ = stores
    monkeypatch.setattr('puppetmaster.state.find_state_dir_for_job',
                        lambda *_: pytest.fail('sibling discovery'))
    assert jobs.post_swarm_cancel({'job_id': 'missing'}, svc)[0] == 409


def test_local_real_event_and_record_are_scoped(stores, tmp_path):
    import threading
    from harness.local_jobs import LocalJobsMixin
    _, _, svc, qs = stores
    pilot = LocalJobsMixin()
    pilot.harness_session_id = 'session-a'
    pilot.config = svc.cfg
    pilot._local_jobs_lock = threading.RLock()
    pilot._local_jobs_path = str(tmp_path / 'local-jobs.json')
    pilot._local_jobs = {jid: {'id': jid, 'session_id': 'session-a', 'cwd': svc.cfg.repo,
                              'status': 'running'} for jid in ('local-selected', 'local-other')}
    pilot._local_job_cancels = {jid: threading.Event() for jid in pilot._local_jobs}
    svc.get_pilot = lambda: pilot
    pilot._initialize_local_metadata_locked()
    ref = {**selection(qs, source='local', job_id='local-selected', state_id=None),
           'local_incarnation': pilot.local_metadata_handle().incarnation}
    assert jobs.post_swarm_cancel({'selection': ref}, svc)[0] == 200
    assert pilot._local_job_cancels['local-selected'].is_set()
    assert not pilot._local_job_cancels['local-other'].is_set()
    assert pilot.get_local_job('local-selected')['status'] == 'cancelled'
    assert pilot.get_local_job('local-other')['status'] == 'running'
    assert not is_cancelled('local-selected')


def test_refusal_on_fresh_harness_reader_does_not_initialize_store(stores):
    import sqlite3
    from puppetmaster.store_factory import create_store
    primary, _, svc, qs = stores
    root = primary.store.root
    def dump():
        with closing(sqlite3.connect(root / 'state.sqlite3')) as connection:
            return list(connection.iterdump())
    before = dump()
    primary.store = create_store('sqlite', root)
    assert jobs.post_swarm_cancel({'selection': selection(qs, job_id='missing')}, svc)[0] == 409
    assert dump() == before


@pytest.mark.parametrize('source', ['harness', 'cli'])
@pytest.mark.parametrize('field,value', [('session_id', 'foreign'), ('cwd', '/foreign'), ('cwd', '')])
def test_task_authority_refusal_preserves_exact_stores(stores, source, field, value):
    primary, cli, svc, qs = stores
    chosen = primary if source == 'harness' else cli
    jid = qs['job_id'][0]
    task = chosen.store.list_tasks(jid)[0]
    task.payload[field] = value
    chosen.store.save_task(task)
    before = [store_dump(s.store) for s in (primary, cli)]
    flag_before = is_cancelled(jid)
    ref = selection(qs, source=source, state_id=state_identity(chosen.store.root))
    code, body = jobs.post_swarm_cancel({'selection': ref}, svc)
    assert code == 409
    assert body['code'] == 'scoped_kernel_cancellation_required'
    assert [store_dump(s.store) for s in (primary, cli)] == before
    assert is_cancelled(jid) == flag_before
