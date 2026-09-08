"""Real public PM stores, fixed metadata budgets, and strict HTTP boundary regressions."""
from contextlib import closing
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from harness.job_metadata_capability import bounded_metadata_available

if not bounded_metadata_available():
    pytest.skip("public PM lacks bounded metadata APIs", allow_module_level=True)
from puppetmaster.models import Artifact, ArtifactType, Job, JobRef, JobStatus, Task
from puppetmaster.state import state_identity
from puppetmaster.store_factory import create_store

from harness.job_readmodel import ActiveContext, KnownSources, MetadataReader, PMSelection, ReadContext
from harness.api.job_readmodel import (
    get_job_metadata, get_job_metadata_detail, get_local_metadata, post_job_metadata_pins,
)


@pytest.fixture(params=['sqlite', 'file'])
def env(tmp_path, request):
    backend = request.param
    root = tmp_path / 'store'
    store = create_store(backend, root, mode='ensure')
    store.init()
    sources = KnownSources.from_roots([('harness', root, backend, False)])
    ctx = ReadContext('session-A', str(tmp_path), 'generation-1', 'session')
    active = [ActiveContext(ctx.session_id, ctx.repo, ctx.view_generation)]
    reader = MetadataReader(lambda: active[0], sources)
    return store, reader, ctx, sources.stores[0].selection, active


def job(store, n=0, **kw):
    value = store.create_job(f'goal-{n}', origin='marionette', session_id='session-A', **kw)
    return value


def query(ctx, selection=None, **kw):
    values = asdict(ctx)
    if selection:
        values.update(asdict(selection))
    values.update(kw)
    return {k: [str(v)] for k, v in values.items()}


def page(env, **kw):
    _, reader, ctx, selection, _ = env
    return get_job_metadata(query(ctx, selection, mode='snapshot', **kw), reader)


def hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file() and not p.name.endswith(('-shm', '-wal'))}


def trace_metadata(monkeypatch, *, measure=False):
    """Observe the real isolated reader through its forwarded SQLite callbacks."""
    from puppetmaster.readonly import ReadConnection
    metrics = dict(reads=[], queries=[], operations=0)
    initialize, execute = ReadConnection.__init__, ReadConnection.execute
    def instrument(connection, *args, **kwargs):
        initialize(connection, *args, **kwargs)
        def authorize(action, table, column, *_):
            if action == sqlite3.SQLITE_READ:
                metrics['reads'].append((table, column))
                assert table not in ('jobs', 'tasks', 'artifacts', 'runs',
                                     'execution_attempts', 'usage_observations', 'completions')
            assert action not in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                                  sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_DROP_TABLE)
            return sqlite3.SQLITE_OK
        connection.set_authorizer(authorize)
        if measure:
            def progress():
                metrics['operations'] += 100
                return 0
            connection.set_progress_handler(progress, 100)
    def checked(connection, sql, parameters=()):
        metrics['queries'].append((sql, parameters))
        return execute(connection, sql, parameters)
    monkeypatch.setattr(ReadConnection, '__init__', instrument)
    monkeypatch.setattr(ReadConnection, 'execute', checked)
    return metrics


def assert_bounded_page(result, *, checkpoint=0):
    assert result['page']['outcome'] in ('partial', 'complete')
    assert len(result['rows']) <= 50 and result['page']['scanned'] <= 51
    assert len(json.dumps(result).encode()) <= 65536
    assert result['page']['checkpoint'] == (result['page']['revision']
        if result['page']['outcome'] == 'complete' else checkpoint)
    assert bool(result['page']['next_cursor']) == (result['page']['outcome'] == 'partial')


def test_fixed_page_and_no_body_or_writes(env, monkeypatch):
    store, reader, ctx, selection, _ = env
    expected = set()
    for i in range(70):
        j = job(store, i)
        expected.add(j.id)
        t = Task(j.id, 'test', 'x' * 16384, payload={'blob': 'x' * 16384})
        store.save_task(t)
        store.save_artifact(Artifact(j.id, t.id, ArtifactType.FINDING, 'test', {'claim': 'x' * 16384}, 1.0, ['fixture']))
    before = hashes(store.root)
    metrics = trace_metadata(monkeypatch)
    calls = []
    from puppetmaster.store import SwarmStore
    original_list = SwarmStore.list_job_summaries
    def summary(self, *args, **kwargs):
        calls.append(kwargs)
        return original_list(self, *args, **kwargs)
    monkeypatch.setattr(SwarmStore, 'list_job_summaries', summary)
    for name in ('get_job', 'list_jobs', 'list_tasks', 'list_artifacts', 'get_completion_receipt'):
        monkeypatch.setattr(SwarmStore, name, lambda *a, **k: pytest.fail('body read'))
    code, result = page(env)
    assert code == 200
    assert result['page']['outcome'] == 'partial'
    assert_bounded_page(result)
    assert 0 < len(result['rows']) < 50
    assert result['page']['checkpoint'] == 0
    assert len(calls) == 1
    assert {k: calls[0][k] for k in ('limit', 'max_scan', 'max_bytes')} == dict(limit=50, max_scan=51, max_bytes=32768)
    ids = [r['selection']['job_ref']['job_id'] for r in result['rows']]
    while result['page']['next_cursor']:
        before_calls = len(calls)
        code, result = page(env, cursor=result['page']['next_cursor'])
        assert code == 200 and len(calls) == before_calls + 1
        assert_bounded_page(result)
        ids.extend(r['selection']['job_ref']['job_id'] for r in result['rows'])
        assert len(ids) == len(set(ids))
    assert set(ids) == expected
    assert all({k: call[k] for k in ('limit', 'max_scan', 'max_bytes')}
               == dict(limit=50, max_scan=51, max_bytes=32768) for call in calls)
    assert hashes(store.root) == before
    assert metrics['reads'] and any(table == 'projection_versions' for table, _ in metrics['reads'])


def test_partial_empty_is_not_complete(env):
    store, _, _, _, _ = env
    for i in range(70):
        store.create_job('foreign', origin='marionette', session_id='other')
    code, result = page(env)
    assert code == 200 and result['rows'] == []
    assert result['page']['outcome'] == 'partial' and result['page']['next_cursor']


def test_session_removal_does_not_expose_foreign_ownership(env):
    store, reader, ctx, selection, _ = env
    j = job(store)
    _, first = page(env)
    store.save_job(replace(j, session_id='foreign-secret', project_id='foreign-project'))
    code, changed = get_job_metadata(query(ctx, selection, mode='changes', after_revision=first['page']['checkpoint']), reader)
    assert code == 200 and len(changed['rows']) == 1
    row = changed['rows'][0]
    assert row.keys() == {'selection', 'revision', 'deleted'} and row['deleted'] is True
    assert row['selection']['session_id'] == ctx.session_id
    assert 'foreign-secret' not in json.dumps(changed) and 'foreign-project' not in json.dumps(changed)


@pytest.mark.parametrize('scope', ['repo', 'all'])
def test_null_ownership_removal_is_source_proven_without_host_cache(env, scope):
    store, reader, original, selection, _ = env
    ctx = replace(original, scope=scope)
    j = job(store)
    _, first = get_job_metadata(query(ctx, selection, mode='snapshot'), reader)
    store.save_job(replace(j, session_id=None, origin=None))
    code, result = get_job_metadata(query(ctx, selection, mode='changes', after_revision=first['page']['checkpoint']), reader)
    assert code == 200 and result['page']['outcome'] == 'complete'
    assert result['rows'][0]['deleted'] and len(result['rows'][0]) == 3
    fresh = MetadataReader(reader.active_context, reader.sources)
    _, uncertain = get_job_metadata(query(ctx, selection, mode='changes', after_revision=first['page']['checkpoint']), fresh)
    assert uncertain == result
    assert uncertain['rows'][0]['selection']['job_ref'] == store.job_ref(j.id).as_dict()
    assert uncertain['page']['checkpoint'] > first['page']['checkpoint']
    foreign = store.create_job('foreign-secret', origin='other', session_id=None)
    store.save_job(replace(foreign, origin=None))
    _, isolated = get_job_metadata(query(ctx, selection, mode='changes',
        after_revision=result['page']['checkpoint']), fresh)
    assert isolated['page']['outcome'] == 'complete' and isolated['rows'] == []
    assert 'foreign-secret' not in json.dumps(isolated)


def test_unknown_legacy_is_never_hydrated(env):
    store, _, _, _, _ = env
    store.create_job('legacy', label=json.dumps({'origin': 'marionette', 'session_id': 'session-A'}))
    _, result = page(env)
    assert not result['rows'] and 'legacy_ownership' in result['missing']
    assert result['coverage']['legacy'] == 'unavailable'


def test_cursor_tamper_binding_and_captured_snapshot(env):
    store, reader, ctx, selection, active = env
    jobs = [job(store, i) for i in range(70)]
    _, first = page(env)
    cursor = first['page']['next_cursor']
    assert page(env, cursor='!' + cursor)[0] == 400
    assert get_job_metadata(query(replace(ctx, scope='all'), selection, mode='snapshot', cursor=cursor), reader)[0] == 400
    active[0] = replace(active[0], session_id='session-B', generation='generation-2')
    assert page(env, cursor=cursor)[0] == 409
    new_ctx = replace(ctx, session_id='session-B', view_generation='generation-2')
    assert get_job_metadata(query(new_ctx, selection, mode='snapshot', cursor=cursor), reader)[0] == 400
    active[0] = ActiveContext(ctx.session_id, ctx.repo, ctx.view_generation)
    later = job(store, 100)
    unseen = next(j for j in jobs if j.id not in {r['selection']['job_ref']['job_id'] for r in first['rows']})
    removed = next(j for j in jobs if j != unseen and j.id not in {
        r['selection']['job_ref']['job_id'] for r in first['rows']})
    store.save_job(replace(unseen, goal='mutated-preview', status=JobStatus.COMPLETE))
    store.delete_job(removed.id)
    rows = list(first['rows'])
    while cursor:
        code, continued = page(env, cursor=cursor)
        assert code == 200
        assert_bounded_page(continued)
        assert continued['page']['revision'] == first['page']['revision']
        rows.extend(continued['rows'])
        cursor = continued['page']['next_cursor']
    ids = [r['selection']['job_ref']['job_id'] for r in rows]
    assert len(ids) == len(set(ids)) == 70 and set(ids) == {j.id for j in jobs}
    assert later.id not in ids
    old = next(r for r in rows if r['selection']['job_ref']['job_id'] == unseen.id)
    assert old['display']['goal_preview'] == unseen.goal and old['lifecycle'] == 'queued'
    code, delta = get_job_metadata(query(ctx, selection, mode='changes',
        after_revision=continued['page']['checkpoint']), reader)
    assert code == 200 and delta['page']['outcome'] == 'complete'
    assert {r['selection']['job_ref']['job_id'] for r in delta['rows']} == {later.id, unseen.id, removed.id}
    assert next(r for r in delta['rows'] if r['selection']['job_ref']['job_id'] == unseen.id)['lifecycle'] == 'complete'
    assert next(r for r in delta['rows'] if r['selection']['job_ref']['job_id'] == removed.id)['deleted'] is True


def test_frozen_change_continuation(env):
    store, reader, ctx, selection, _ = env
    for i in range(70):
        job(store, i)
    _, first = get_job_metadata(query(ctx, selection, mode='changes', after_revision=0), reader)
    assert first['page']['outcome'] == 'partial'
    upper = first['page']['revision']
    later = job(store, 100)
    _, second = get_job_metadata(query(ctx, selection, mode='changes', after_revision=0, cursor=first['page']['next_cursor']), reader)
    assert second['page']['outcome'] == 'complete'
    assert second['page']['checkpoint'] == upper
    assert all(r['selection']['job_ref']['job_id'] != later.id for r in second['rows'])
    assert get_job_metadata(query(ctx, selection, mode='changes', after_revision=upper, cursor=first['page']['next_cursor']), reader)[0] == 400


def test_detail_exact_scope_and_budget(env):
    store, reader, ctx, selection, _ = env
    j = job(store)
    expected = dict(tasks=set(), artifacts=set())
    for i in range(206):
        t = Task(j.id, 'test', 'body', id=f'task_{i:04d}')
        a = Artifact(j.id, t.id, ArtifactType.FINDING, 'test', {'claim': 'recorded', 'passed': True}, 1.0, ['fixture'])
        store.save_task(t)
        store.save_artifact(a)
        expected['tasks'].add(t.id)
        expected['artifacts'].add(a.id)
    qs = query(ctx, selection, **store.job_ref(j.id).as_dict())
    code, result = get_job_metadata_detail(qs, reader)
    assert code == 200
    assert result['cancellation_authority'] is False
    assert result['cost']['kind'] == 'unavailable' and result['cost']['reason'] == 'no_terminal_receipt'
    assert result['history']['kind'] == 'available'
    assert result['history']['counts']['captured_runs'] == 0
    assert result['artifacts']['rows'][0]['check_result'] == 'unavailable'
    # The task and artifact byte budgets can exhaust at different row counts.
    for lane, parameter in (('tasks', 'task_cursor'), ('artifacts', 'artifact_cursor')):
        current = result
        ids = []
        tokens = set()
        assert current[lane]['page']['outcome'] == 'partial'
        while True:
            assert len(json.dumps(current).encode()) <= 98304
            assert current['cancellation_authority'] is False
            chunk = current[lane]
            assert len(chunk['rows']) <= 50 and chunk['page']['scanned'] <= 51
            ids.extend(r['id'] for r in chunk['rows'])
            assert len(ids) == len(set(ids))
            token = chunk['page']['next_cursor']
            if token is None:
                assert chunk['page']['outcome'] == 'complete'
                break
            assert chunk['page']['outcome'] == 'partial' and token not in tokens
            tokens.add(token)
            code, current = get_job_metadata_detail(dict(qs, **{parameter: [token]}), reader)
            assert code == 200
        assert set(ids) == expected[lane]
    store.save_job(replace(j, session_id='other'))
    _, denied = get_job_metadata_detail(qs, reader)
    assert denied['tasks']['rows'] == denied['artifacts']['rows'] == []
    assert denied['lifecycle'] is None


def test_missing_old_store_no_creation(tmp_path):
    for name in ('missing', 'old'):
        root = tmp_path / name
        if name == 'old':
            root.mkdir()
            with closing(sqlite3.connect(root / 'state.sqlite3')) as c, c:
                c.execute('CREATE TABLE metadata (key TEXT, value TEXT)')
                c.execute("INSERT INTO metadata VALUES ('schema_version','1')")
        sources = KnownSources.from_roots([('harness', root, 'sqlite', False)])
        ctx = ReadContext('session-A', str(tmp_path), 'g', 'session')
        reader = MetadataReader(lambda: ActiveContext(ctx.session_id, ctx.repo, 'g'), sources)
        before = hashes(tmp_path)
        code, result = get_job_metadata(query(ctx, sources.stores[0].selection, mode='snapshot'), reader)
        assert code == 200
        assert result['page']['outcome'] == 'unavailable' and result['rows'] == []
        assert hashes(tmp_path) == before
        assert root.exists() == (name == 'old')


def test_large_binding_unavailable_without_truncation(env):
    store, reader, ctx, selection, _ = env
    j = job(store)
    store.save_task(Task(j.id, 'test', 'body', lease_owner='x' * 300000))
    _, result = get_job_metadata_detail(query(ctx, selection, job_id=j.id), reader)
    assert result['tasks']['page']['outcome'] == 'unavailable'
    assert result['tasks']['rows'] == [] and result['cancellation_authority'] is False


def test_stale_context_during_public_read(env, monkeypatch):
    store, reader, ctx, selection, active = env
    job(store)
    from puppetmaster.store import SwarmStore
    original = SwarmStore.list_job_summaries
    def read(self, *a, **kw):
        result = original(self, *a, **kw)
        active[0] = replace(active[0], generation='changed')
        return result
    monkeypatch.setattr(SwarmStore, 'list_job_summaries', read)
    code, result = page(env)
    assert code == 409 and result == {'code': 'view_changed'}


def test_strict_boundary_and_pins(env):
    store, reader, ctx, selection, _ = env
    refs = [PMSelection(ctx, selection, JobRef(job(store, i).id, selection.state_id)) for i in range(9)]
    body = dict(asdict(ctx), selections=[s.wire() for s in refs[:8]])
    code, result = post_job_metadata_pins(body, reader)
    assert code == 200 and len(result['results']) == 8
    assert all(r['result']['kind'] == 'present' for r in result['results'])
    assert post_job_metadata_pins(dict(body, selections=[s.wire() for s in refs]), reader)[0] == 400
    for extra in ({'limit': ['1']}, {'repo': ['', ctx.repo]}, {'source': ['cli', 'harness']}, {'mode': ['']}, {'cursor': [True]}):
        qs = query(ctx, selection, mode='snapshot')
        qs.update(extra)
        assert get_job_metadata(qs, reader)[0] == 400
    assert get_local_metadata(query(ctx), reader)[1]['page']['outcome'] == 'unavailable'


def test_cross_store_collision_and_alias_preference(env, tmp_path, monkeypatch):
    store, _, ctx, selection, active = env
    j = job(store)
    foreign = create_store('sqlite', tmp_path / 'foreign', mode='ensure')
    monkeypatch.setattr('puppetmaster.models.new_id', lambda prefix: j.id if prefix == 'job' else prefix + '_fixture')
    foreign.create_job('foreign', origin='foreign', session_id='session-A')
    sources = KnownSources.from_roots([('cli', store.root, store.backend_name, False),
                                     ('harness', store.root, store.backend_name, False),
                                     ('cli', foreign.root, 'sqlite', False)])
    assert len(sources.stores) == 2 and sources.stores[0].selection.source == 'harness'
    reader = MetadataReader(lambda: active[0], sources)
    selected = PMSelection(ctx, sources.stores[1].selection, JobRef(j.id, state_identity(foreign.root)))
    _, pins = post_job_metadata_pins(dict(asdict(ctx), selections=[selected.wire()]), reader)
    assert pins['results'][0]['result']['kind'] == 'unavailable'
    assert get_job_metadata_detail(query(ctx, selected.store, job_id=j.id), reader)[1]['tasks']['rows'] == []


def test_same_path_replacement_requires_explicit_source_refresh(env, tmp_path, monkeypatch):
    store, reader, ctx, selection, _ = env
    j = job(store)
    old_ref = store.job_ref(j.id)
    assert page(env)[0] == 200
    replacement = create_store(store.backend_name, tmp_path / 'replacement', mode='ensure')
    replacement.init()
    monkeypatch.setattr('puppetmaster.models.new_id', lambda prefix: j.id if prefix == 'job' else prefix + '_fixture')
    replacement.create_job('replacement body', origin='marionette', session_id='session-A')
    old_id = state_identity(store.root)
    shutil.rmtree(store.root)
    shutil.move(str(replacement.root), str(store.root))
    assert state_identity(store.root) == old_id
    code, result = page(env)
    assert code == 200 and result['page']['outcome'] == 'unavailable'
    assert result['rows'] == [] and 'selection_changed' in result['missing']
    assert 'replacement body' not in json.dumps(result)
    fresh_sources = KnownSources.from_roots([('harness', store.root, store.backend_name, False)])
    fresh = MetadataReader(reader.active_context, fresh_sources)
    code, refreshed = get_job_metadata(query(ctx, selection, mode='snapshot'), fresh)
    assert code == 200 and refreshed['page']['outcome'] == 'complete'
    ref = refreshed['rows'][0]['selection']['job_ref']
    assert ref['job_id'] == j.id and ref['state_id'] == old_id
    assert ref['version'] == 2 and ref['incarnation'] != old_ref.incarnation


def test_repo_visibility_and_sibling_status_removal(env, tmp_path, monkeypatch):
    store, _, ctx, _, active = env
    monkeypatch.setenv('HARNESS_CLI_CROSS_PROJECT', '1')
    sibling = create_store('sqlite', tmp_path / 'sibling', mode='ensure')
    j = sibling.create_job('sibling', origin='marionette', session_id=ctx.session_id, project_id='unrelated-project')
    sibling.save_job(replace(j, status=JobStatus.RUNNING))
    sources = KnownSources.from_roots([('harness', store.root, store.backend_name, False),
                                     ('cli', sibling.root, 'sqlite', True)])
    reader = MetadataReader(lambda: active[0], sources)
    selection = sources.stores[1].selection
    _, denied = get_job_metadata(query(replace(ctx, scope='repo'), selection, mode='snapshot', status='running'), reader)
    assert denied['page']['outcome'] == 'unavailable'
    assert get_job_metadata(query(ctx, selection, mode='snapshot'), reader)[0] == 400
    _, first = get_job_metadata(query(ctx, selection, mode='snapshot', status='running'), reader)
    assert len(first['rows']) == 1
    sibling.save_job(replace(j, status=JobStatus.COMPLETE, session_id='foreign'))
    _, change = get_job_metadata(query(ctx, selection, mode='changes', status='running', after_revision=first['page']['checkpoint']), reader)
    assert change['rows'][0]['deleted'] and len(change['rows'][0]) == 3
    assert 'foreign' not in json.dumps(change)
    monkeypatch.setenv('HARNESS_CLI_CROSS_PROJECT', '0')
    assert get_job_metadata(query(ctx, selection, mode='snapshot', status='running'), reader)[1]['page']['outcome'] == 'unavailable'


def test_repo_ignores_project_identity_and_pin_retains_owned_other_session(env):
    store, reader, ctx, selection, _ = env
    j = store.create_job('owned', session_id='other', origin='marionette', project_id='unrelated')
    _, result = get_job_metadata(query(replace(ctx, scope='repo'), selection, mode='snapshot'), reader)
    assert len(result['rows']) == 1
    ref = PMSelection(ctx, selection, JobRef(j.id, selection.state_id))
    _, pins = post_job_metadata_pins(dict(asdict(ctx), selections=[ref.wire()]), reader)
    assert pins['results'][0]['result']['kind'] == 'present'
    assert get_job_metadata_detail(query(ctx, selection, job_id=j.id), reader)[1]['tasks']['page']['outcome'] == 'unavailable'


def test_wrapper_overflow_keeps_checkpoint_and_no_rows(env):
    store, reader, original, selection, active = env
    # Long ASCII repo repeated in each exact selection exceeds the host wrapper
    # budget although the public PM page still fits. It is never row-clipped.
    ctx = replace(original, repo='/' + 'r' * 1000)
    active[0] = ActiveContext(ctx.session_id, ctx.repo, ctx.view_generation)
    for i in range(70):
        job(store, i)
    _, result = get_job_metadata(query(ctx, selection, mode='changes', after_revision=0), reader)
    assert result['page']['outcome'] == 'unavailable'
    assert result['page']['checkpoint'] == 0 and result['page']['next_cursor'] is None
    assert result['rows'] == [] and 'response_budget' in result['missing']
    assert len(json.dumps(result).encode()) <= 65536


def test_sustained_writes_do_not_trigger_refill(env, monkeypatch):
    store, reader, ctx, selection, _ = env
    expected = {job(store, i).id for i in range(70)}
    handle = reader.sources.stores[0].handle
    original = handle.list_job_summaries
    calls = []
    def counted(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(handle, 'list_job_summaries', counted)
    for i in range(5):
        code, current = page(env)
        assert code == 200 and current['page']['outcome'] == 'partial'
        revision = current['page']['revision']
        captured = set(expected)
        ids = [r['selection']['job_ref']['job_id'] for r in current['rows']]
        while current['page']['next_cursor']:
            expected.add(job(store, 100 + len(expected)).id)
            before = len(calls)
            code, current = page(env, cursor=current['page']['next_cursor'])
            assert code == 200 and len(calls) == before + 1
            assert_bounded_page(current)
            assert current['page']['revision'] == revision
            ids.extend(r['selection']['job_ref']['job_id'] for r in current['rows'])
            assert len(ids) == len(set(ids))
        assert set(ids) == captured
        assert current['page']['checkpoint'] == revision
    assert all({k: call[k] for k in ('limit', 'max_scan', 'max_bytes')}
               == dict(limit=50, max_scan=51, max_bytes=32768) for call in calls)


def test_projection_epoch_and_pending(env):
    store, reader, ctx, selection, _ = env
    for i in range(60):
        job(store, i)
    _, first = page(env)
    database = store.root / ('state.sqlite3' if store.backend_name == 'sqlite' else 'metadata.sqlite3')
    # Fixture corruption/epoch rotation only: production never accesses tables.
    with closing(sqlite3.connect(database)) as c, c:
        c.execute("UPDATE projection_meta SET value=CAST(value AS INTEGER)+1 WHERE key='epoch'")
    assert page(env, cursor=first['page']['next_cursor'])[1]['page']['outcome'] == 'cursor_expired'
    with closing(sqlite3.connect(database)) as c, c:
        fields = c.execute('PRAGMA table_info(projection_pending)').fetchall()
        assert fields
        c.execute("INSERT INTO projection_pending VALUES ('fixture')")
    assert page(env)[1]['page']['outcome'] == 'unavailable'


def test_unknown_source_does_not_attach_arbitrary_path(env, monkeypatch):
    _, reader, ctx, selection, _ = env
    monkeypatch.setattr('harness.job_readmodel.create_store', lambda *a, **kw: pytest.fail('unknown store opened'))
    _, result = get_job_metadata(query(ctx, replace(selection, state_id='unknown'), mode='snapshot'), reader)
    assert result['page']['outcome'] == 'unavailable'
    assert get_job_metadata(query(ctx, selection, mode='snapshot', state_dir='/tmp/arbitrary'), reader)[0] == 400


def test_corrupt_store_is_503(env):
    store, reader, ctx, selection, _ = env
    database = store.root / ('state.sqlite3' if store.backend_name == 'sqlite' else 'metadata.sqlite3')
    database.write_bytes(b'not a database')
    assert page(env)[0] == 503


def test_poll_never_constructs_store_or_recreates_deleted_root(env, monkeypatch):
    store, reader, ctx, selection, _ = env
    job(store)
    monkeypatch.setattr('harness.job_readmodel.create_store', lambda *a, **kw: pytest.fail('constructor in poll'))
    assert page(env)[0] == 200
    shutil.rmtree(store.root)
    assert page(env)[1]['page']['outcome'] == 'unavailable'
    assert not store.root.exists()


def test_invalid_scalar_and_corrupt_metadata_classification(env):
    store, reader, ctx, selection, _ = env
    assert get_job_metadata(query(ctx, selection, mode='snapshot', status='invalid'), reader)[0] == 400
    qs = query(ctx, selection, mode='snapshot')
    qs['repo'] = ['\ud800']
    assert get_job_metadata(qs, reader)[0] == 400
    job(store)
    database = store.root / ('state.sqlite3' if store.backend_name == 'sqlite' else 'metadata.sqlite3')
    with closing(sqlite3.connect(database)) as c, c:
        c.execute("UPDATE projection_current SET scope='not-json'")
    code, corrupt = page(env)
    assert code == 200 and corrupt['page']['outcome'] == 'unavailable'
    assert corrupt['page']['reason'] == 'membership_invalid' and corrupt['rows'] == []
    assert corrupt['page']['checkpoint'] == 0


def test_null_counts_and_independent_detail_cursors(env):
    store, reader, ctx, selection, _ = env
    j = job(store)
    for i in range(60):
        store.save_task(Task(j.id, 'test', 'body', id=f'task_{i:04d}'))
    database = store.root / ('state.sqlite3' if store.backend_name == 'sqlite' else 'metadata.sqlite3')
    with closing(sqlite3.connect(database)) as c, c:
        c.execute("UPDATE projection_current SET task_count=NULL, artifact_count=NULL WHERE kind='job'")
    _, listed = page(env)
    assert listed['rows'][0]['task_count'] is listed['rows'][0]['artifact_count'] is None
    _, detail = get_job_metadata_detail(query(ctx, selection, job_id=j.id), reader)
    cursor = detail['tasks']['page']['next_cursor']
    assert cursor
    assert get_job_metadata_detail(query(ctx, selection, job_id=j.id, artifact_cursor=cursor), reader)[0] == 400
    other = job(store, 100)
    assert get_job_metadata_detail(query(ctx, selection, job_id=other.id, task_cursor=cursor), reader)[0] == 400


def test_source_discovery_is_once_bounded_and_scratch_excluded(tmp_path, monkeypatch):
    from harness.job_readmodel import discover_sources
    primary = tmp_path / 'primary'
    create_store('sqlite', primary, mode='ensure')
    called = []
    monkeypatch.setattr('harness.job_readmodel.resolve_cli_state_dir', lambda workspace: str(primary))
    monkeypatch.setattr('harness.job_readmodel.cross_project_scan_enabled', lambda: True)
    def candidates(primary_resolved, *, max_opens):
        called.append((primary_resolved, max_opens))
        return [str(tmp_path / 'pmh-edit-scratch'), str(tmp_path / 'sibling')]
    monkeypatch.setattr('harness.job_readmodel._foreign_state_dir_candidates', candidates)
    sources = discover_sources(primary, tmp_path)
    assert len(called) == 1 and called[0][1] == 32
    assert len(sources.stores) == 2
    assert sources.stores[0].selection.source == 'harness'
    assert all(s.root.name != 'pmh-edit-scratch' for s in sources.stores)


def test_old_file_store_without_projection_stays_unavailable(tmp_path, monkeypatch):
    root = tmp_path / 'old-file'
    root.mkdir()
    (root / 'legacy-job.json').write_text('{"body":"never read"}')
    before = hashes(root)
    sources = KnownSources.from_roots([('harness', root, 'file', False)])
    ctx = ReadContext('session-A', str(tmp_path), 'g', 'session')
    reader = MetadataReader(lambda: ActiveContext(ctx.session_id, ctx.repo, 'g'), sources)
    monkeypatch.setattr('harness.job_readmodel.create_store', lambda *a, **k: pytest.fail('constructor in poll'))
    _, result = get_job_metadata(query(ctx, sources.stores[0].selection, mode='snapshot'), reader)
    assert result['page']['outcome'] == 'unavailable' and result['rows'] == []
    assert hashes(root) == before and not (root / 'metadata.sqlite3').exists()


def test_current_owner_retained_while_prior_owner_receives_tombstone(env):
    store, reader, ctx, selection, _ = env
    j = job(store)
    ref = store.job_ref(j.id)
    _, first = page(env)
    moved = replace(j, session_id='session-B', goal='new-owner-preview')
    store.save_job(moved)
    after = first['page']['checkpoint']
    code, departed = get_job_metadata(query(ctx, selection, mode='changes', after_revision=after), reader)
    assert code == 200 and departed['page']['outcome'] == 'complete'
    assert len(departed['rows']) == 1 and departed['rows'][0]['deleted']
    assert set(departed['rows'][0]) == {'selection', 'revision', 'deleted'}
    assert 'session-B' not in json.dumps(departed) and 'new-owner-preview' not in json.dumps(departed)
    current_ctx = replace(ctx, session_id='session-B')
    current_reader = MetadataReader(lambda: ActiveContext('session-B', ctx.repo, ctx.view_generation), reader.sources)
    for owner_ctx, owner_reader in ((current_ctx, current_reader), (replace(ctx, scope='repo'), reader),
                                    (replace(ctx, scope='all'), reader)):
        code, retained = get_job_metadata(query(owner_ctx, selection, mode='changes', after_revision=after), owner_reader)
        assert code == 200 and retained['page']['outcome'] == 'complete'
        assert len(retained['rows']) == 1 and not retained['rows'][0]['deleted']
        assert retained['rows'][0]['selection']['job_ref'] == ref.as_dict()
        assert retained['rows'][0]['ownership']['session_id'] == 'session-B'
        assert retained['rows'][0]['display']['goal_preview'] == 'new-owner-preview'
    store.save_job(replace(moved, session_id=ctx.session_id))
    code, returned = get_job_metadata(query(ctx, selection, mode='changes',
        after_revision=departed['page']['checkpoint']), reader)
    assert code == 200 and returned['page']['outcome'] == 'complete'
    assert len(returned['rows']) == 1 and not returned['rows'][0]['deleted']
    assert returned['rows'][0]['revision'] > departed['rows'][0]['revision']


def test_missing_source_membership_witness_does_not_advance_checkpoint(env):
    store, reader, ctx, selection, _ = env
    j = job(store)
    _, first = page(env)
    store.save_job(replace(j, session_id=None, origin=None))
    database = store.root / ('state.sqlite3' if store.backend_name == 'sqlite' else 'metadata.sqlite3')
    with closing(sqlite3.connect(database)) as connection, connection:
        changed = connection.execute("UPDATE projection_changes SET previous_membership=NULL "
                                     "WHERE kind='job' AND revision>?", (first['page']['checkpoint'],))
        assert changed.rowcount > 0
    code, result = get_job_metadata(query(ctx, selection, mode='changes',
        after_revision=first['page']['checkpoint']), reader)
    assert code == 200 and result['page']['outcome'] == 'unavailable'
    assert result['rows'] == [] and result['page']['checkpoint'] == first['page']['checkpoint']
    assert 'previous_membership_unavailable' in result['missing']


def test_reader_safety_instrumentation_rejects_source_reads_and_writes(env, monkeypatch):
    from puppetmaster.projections import connection
    store, reader, _, _, _ = env
    job(store)
    before = hashes(store.root)
    metrics = trace_metadata(monkeypatch)
    assert page(env)[0] == 200 and metrics['reads']
    if store.backend_name == 'sqlite':
        with pytest.raises(AssertionError):
            with connection(reader.sources.stores[0].handle, metadata_only=True) as c:
                c.execute('SELECT id FROM jobs')
    else:
        with pytest.raises(sqlite3.OperationalError, match='no such table: jobs'):
            with connection(reader.sources.stores[0].handle, metadata_only=True) as c:
                c.execute('SELECT id FROM jobs')
    with pytest.raises(AssertionError):
        with connection(reader.sources.stores[0].handle, metadata_only=True) as c:
            c.execute('CREATE TABLE forbidden_write (id INTEGER)')
    assert hashes(store.root) == before
