"""Native writer and bounded observation regressions (no model/network calls)."""
import copy
import json
import threading
from types import SimpleNamespace

import pytest

pytest.importorskip('harness.job_readmodel', reason='requires companion PR2 metadata reader')

from harness.local_jobs import LocalJobsMixin
from harness.local_job_metadata import JOURNAL_SIZE, LocalMetadataIndex
from harness.job_readmodel import ActiveContext, InvalidReadRequest, KnownSources, MetadataReader, ReadContext


class Runner(LocalJobsMixin):
    def __init__(self, path):
        self.config = SimpleNamespace(repo=str(path), driver='stub')
        self.state_dir = str(path)
        self.harness_session_id = 'A'
        self._local_jobs = {}
        self._local_jobs_lock = threading.Lock()
        self._local_job_cancels = {}
        self._local_jobs_path = str(path / 'swarm_local_jobs.json')
        self._display_transcript = []
        self._load_local_jobs()


def row(jid, session='A'):
    return dict(id=jid, session_id=session, role='command', job_kind='run_command',
                status='completed', created_at=1, updated_at=1,
                terminal_receipt={'status': 'completed', 'exit_code': 0},
                tasks=[], artifacts=[], actions=[], output='')


def seed(runner, n=1100):
    # Real native storage and metadata publication, no fake readmodel/store.
    with runner._local_jobs_lock:
        for i in range(n):
            runner._local_jobs[f'local-{i:04d}'] = row(f'local-{i:04d}')


def ctx():
    return dict(session_id='A', repo='/repo', view_generation='generation', scope='session')


def forbidden(*args, **kwargs):
    pytest.fail('unbounded/body operation reached metadata poll')


def test_large_retained_native_page_never_reads_bodies(tmp_path, monkeypatch):
    runner = Runner(tmp_path)
    seed(runner)
    with runner._local_jobs_lock:
        for job in runner._local_jobs.values():
            job['output'] = 'private-body' * 10000
            job['actions'] = [dict(action_id=str(i), goal='private-action' * 1000) for i in range(80)]
    baseline = runner.live_local_jobs()
    assert len(baseline) == 1100 and len(baseline[-1]['actions']) == 80
    index = runner.local_metadata_handle()
    monkeypatch.setattr(copy, 'deepcopy', forbidden)
    monkeypatch.setattr(runner, 'live_local_jobs', forbidden)
    monkeypatch.setattr(runner, 'get_local_job', forbidden)
    monkeypatch.setattr('builtins.open', forbidden)
    class NoScan(dict):
        values = items = __iter__ = forbidden
    runner._local_jobs = NoScan(runner._local_jobs)
    first = index.read_page(ctx())
    for _ in range(30):
        page = index.read_page(ctx())
        assert page['rows'] == first['rows']
        assert page['page']['scanned'] <= 51
        assert 0 < len(page['rows']) <= 50
        assert len(json.dumps(page).encode()) <= 32768
        assert 'private-' not in json.dumps(page)
    cursor = None
    ids = []
    while True:
        page = index.read_page(ctx(), cursor=cursor)
        ids.extend(r['local_ref']['job_id'] for r in page['rows'])
        cursor = page['page']['next_cursor']
        if cursor is None:
            break
    assert len(ids) == len(set(ids)) == 1100


def test_changes_frozen_revision_removals_scope_and_journal(tmp_path):
    runner = Runner(tmp_path)
    seed(runner, 100)
    index = runner.local_metadata_handle()
    before = index.revision
    runner._local_jobs['local-0001']['status'] = 'failed'
    runner._local_jobs['local-0002']['session_id'] = 'B'
    del runner._local_jobs['local-0003']
    changes = index.read_page(ctx(), mode='changes', after_revision=before)
    assert changes['page']['outcome'] == 'complete'
    byid = {r['local_ref']['job_id']: r for r in changes['rows']}
    assert byid['local-0001']['lifecycle'] == 'failed'
    assert byid['local-0002']['deleted'] and byid['local-0003']['deleted']
    assert all(r['local_ref']['incarnation'] == index.incarnation for r in byid.values())
    first = index.read_page(ctx(), mode='changes', after_revision=0)
    upper = first['page']['revision']
    assert first['page']['checkpoint'] == 0
    runner._local_jobs['local-0000']['updated_at'] = 2
    cursor = first['page']['next_cursor']
    while cursor:
        page = index.read_page(ctx(), mode='changes', after_revision=0, cursor=cursor)
        assert page['page']['revision'] == upper
        cursor = page['page']['next_cursor']
    assert page['page']['checkpoint'] == upper < index.revision
    for i in range(JOURNAL_SIZE + 1):
        runner._local_jobs['local-0000']['updated_at'] = i
    assert index.read_page(ctx(), mode='changes', after_revision=0)['page']['outcome'] == 'expired'
    assert index.read_page(ctx(), mode='changes', after_revision=0, cursor=first['page']['next_cursor'])['page']['outcome'] == 'expired'


def test_cursor_context_selection_incarnation_and_expiry(tmp_path, monkeypatch):
    runner = Runner(tmp_path)
    seed(runner, 100)
    index = runner.local_metadata_handle()
    first = index.read_page(ctx())
    cursor = first['page']['next_cursor']
    for change in ({'scope': 'repo'}, {'view_generation': 'other'}, {'session_id': 'B'}, {'repo': '/other'}):
        with pytest.raises(InvalidReadRequest):
            index.read_page(dict(ctx(), **change), cursor=cursor)
    with pytest.raises(InvalidReadRequest):
        index.read_page(ctx(), mode='changes', cursor=cursor)
    with pytest.raises(InvalidReadRequest):
        LocalMetadataIndex(runner).read_page(ctx(), cursor=cursor)
    monkeypatch.setattr('harness.local_job_metadata.time.monotonic', lambda: 1e15)
    expired = index.read_page(ctx(), cursor=cursor)
    assert expired['page']['outcome'] == 'expired' and expired['rows'] == []


def test_continuous_writes_and_empty_incomplete_are_honest(tmp_path):
    runner = Runner(tmp_path)
    seed(runner, 100)
    index = runner.local_metadata_handle()
    for _ in range(5):
        page = index.read_page(ctx())
        runner._local_jobs['local-0000']['status'] = 'running'
        held = index.read_page(ctx(), cursor=page['page']['next_cursor'])
        assert held['page']['outcome'] == 'expired' and held['page']['checkpoint'] == 0
    empty = index.read_page(dict(ctx(), session_id='B'))
    assert empty['rows'] == [] and empty['page']['outcome'] == 'partial'
    assert empty['page']['scanned'] == 51 and empty['page']['checkpoint'] == 0
    assert 'unretained_history' in empty['missing']


def test_selected_actions_output_children_bound_before_copy(tmp_path, monkeypatch):
    runner = Runner(tmp_path)
    seed(runner, 2)
    job = runner._local_jobs['local-0000']
    job['actions'] = [dict(action_id=str(i), kind='edit', goal='x' * 100000,
                           error='e' * 100000, status='running') for i in range(100)]
    job['output'] = '\U0001f600' * 100000
    job['child_job_ids'] = [f'local-{i:04d}' for i in range(100)]
    index = runner.local_metadata_handle()
    ref = index.ref('local-0000')
    monkeypatch.setattr(copy, 'deepcopy', forbidden)
    monkeypatch.setattr('builtins.open', forbidden)
    for lane in ('actions', 'output', 'children'):
        first = index.read_selected(ctx(), ref, lane=lane)
        assert first['page']['outcome'] == 'partial'
        assert first['page']['scanned'] <= 51 and len(first['rows']) <= 50
        assert len(json.dumps(first).encode()) <= 32768
        assert first['cancellation_authority'] is False
        with pytest.raises(InvalidReadRequest):
            index.read_selected(ctx(), index.ref('local-0001'), lane=lane, cursor=first['page']['next_cursor'])
        with pytest.raises(InvalidReadRequest):
            index.read_selected(ctx(), ref, lane='output' if lane != 'output' else 'actions', cursor=first['page']['next_cursor'])
        second = index.read_selected(ctx(), ref, lane=lane, cursor=first['page']['next_cursor'])
        assert second['rows'] != first['rows'] or lane == 'output'
    assert index.read_selected(ctx(), dict(ref, incarnation='other'))['page']['outcome'] == 'expired'
    page = index.read_selected(ctx(), ref)
    job['actions'] = [dict(action_id='new', kind='read', status='complete')]
    assert index.read_selected(ctx(), ref, cursor=page['page']['next_cursor'])['page']['outcome'] == 'expired'
    assert index.read_selected(ctx(), ref)['rows'][0]['action_id'] == 'new'


def test_native_action_writer_unpersisted_changes_and_deepcopy(tmp_path, monkeypatch):
    runner = Runner(tmp_path)
    seed(runner, 1)
    runner._local_jobs['local-0000']['status'] = 'running'
    index = runner.local_metadata_handle()
    before = index.revision
    # Native event path still publishes when persistence is disabled by caller/tests.
    monkeypatch.setattr(runner, '_persist_local_jobs_locked', lambda **kw: None)
    runner._upsert_local_job_action('local-0000', {'kind': 'action_start',
        'data': {'id': 'a', 'kind': 'edit', 'goal': 'file'}})
    changed = index.read_page(ctx(), mode='changes', after_revision=before)
    assert changed['rows'][-1]['action_count'] == 1
    copied = runner.get_local_job('local-0000')
    copied['status'] = 'failed'
    assert index.read_page(ctx())['rows'][0]['lifecycle'] == 'running'


def test_cold_restart_unknown_receipts_and_corrupt_load(tmp_path):
    runner = Runner(tmp_path)
    runner._register_command_job('local-command', command='echo safe', action_id='action')
    runner._checkpoint_command_job_launch('local-command')
    restarted = Runner(tmp_path)
    result = restarted.local_metadata_handle().read_page(ctx())['rows'][0]
    assert result['kind'] == 'run_command' and result['lifecycle'] == 'unknown'
    assert result['receipts'] == dict(terminal=False, launch=True, recovery=True, child=False)
    assert restarted.local_metadata_handle().incarnation != runner.local_metadata_handle().incarnation
    (tmp_path / 'swarm_local_jobs.json').write_text('{broken')
    corrupt = Runner(tmp_path)
    assert corrupt.local_metadata_handle().read_page(ctx())['page']['outcome'] == 'unavailable'


def test_retained_history_and_classifications(tmp_path):
    runner = Runner(tmp_path)
    seed(runner, 1001)
    for i in range(205):
        runner._local_jobs[f'provider-{i:04d}'] = dict(row(f'provider-{i:04d}'), job_kind='', role='implement')
    runner._local_jobs['wave'] = dict(row('wave'), job_kind='parallel_wave', role='parallel_wave')
    runner._local_jobs['batch'] = dict(row('batch'), job_kind='run_command_batch', role='command_batch')
    runner._persist_local_jobs()
    assert len(runner._local_jobs) == 1208  # existing live history is not silently pruned
    stored = json.loads((tmp_path / 'swarm_local_jobs.json').read_text())['jobs']
    assert len(stored) == 1202  # all 1002 command receipts + 200 provider/wave history
    restarted = Runner(tmp_path)
    assert len(restarted._local_jobs) == 1202
    kinds = set()
    cursor = None
    while True:
        page = runner.local_metadata_handle().read_page(ctx(), cursor=cursor)
        kinds.update(r['kind'] for r in page['rows'])
        cursor = page['page']['next_cursor']
        if cursor is None:
            break
    assert kinds == {'run_command', 'run_command_batch', 'parallel_wave', 'provider'}


def test_invalid_and_missing_index_not_empty(tmp_path):
    runner = Runner(tmp_path)
    index = runner.local_metadata_handle()
    runner._local_jobs['bad'] = dict(row('bad'), session_id='')
    assert index.read_page(ctx())['page']['outcome'] == 'unavailable'
    del runner._local_jobs['bad']
    assert index.read_page(ctx())['page']['outcome'] == 'complete'
    active = ActiveContext('A', '/repo', 'generation')
    reader = MetadataReader(lambda: active, KnownSources(()))
    assert reader.read_local(ReadContext(**ctx()))['page']['outcome'] == 'unavailable'


def test_unsupported_index_and_selected_output_coverage(tmp_path):
    runner = Runner(tmp_path)
    seed(runner, 1)
    index = runner.local_metadata_handle()
    runner._local_jobs['local-0000'].update(output='retained', output_spilled=True, output_chars=100000)
    detail = index.read_selected(ctx(), index.ref('local-0000'), lane='output')
    assert detail['page']['outcome'] == 'complete'
    assert detail['output'] == dict(coverage='in_memory_only', source_chars=100000, spilled=True)
    index.version = 2
    assert not index.describe()['available']
    assert index.read_page(ctx())['page']['outcome'] == 'unavailable'
    assert index.read_selected(ctx(), index.ref('local-0000'))['page']['outcome'] == 'unavailable'


def test_snapshot_scope_transition_never_hides_missing_coverage(tmp_path):
    runner = Runner(tmp_path)
    seed(runner, 60)
    index = runner.local_metadata_handle()
    session = index.read_page(ctx())
    broad = index.read_page(dict(ctx(), scope='all'))
    assert session['missing'] == broad['missing'] == ['unretained_history']
    before = index.revision
    runner._local_jobs['local-0000']['session_id'] = 'B'
    a = index.read_page(ctx(), mode='changes', after_revision=before)
    b = index.read_page(dict(ctx(), session_id='B'), mode='changes', after_revision=before)
    assert a['rows'][0]['deleted'] is True
    assert b['rows'][0]['deleted'] is False
    assert a['rows'][0]['local_ref'] == b['rows'][0]['local_ref']
    assert a['missing'] == b['missing'] == ['unretained_history']


def test_removed_or_replaced_row_callback_cannot_resurrect(tmp_path):
    runner = Runner(tmp_path)
    seed(runner, 1)
    index = runner.local_metadata_handle()
    stale = runner._local_jobs['local-0000']
    del runner._local_jobs['local-0000']
    revision = index.revision
    stale['status'] = 'running'
    assert index.revision == revision and index.read_page(ctx())['rows'] == []
    runner._local_jobs['local-0000'] = row('local-0000')
    stale['status'] = 'failed'
    assert index.read_page(ctx())['rows'][0]['lifecycle'] == 'completed'


def test_failed_command_receipt_remains_unknown_in_projection(tmp_path, monkeypatch):
    runner = Runner(tmp_path)
    runner._register_command_job('local-command', command='echo safe', action_id='action')
    runner._checkpoint_command_job_launch('local-command')
    def fail(*args):
        raise OSError('disk failure')
    monkeypatch.setattr('harness.local_jobs.os.replace', fail)
    assert runner._finish_command_job('local-command', status='completed', summary='done', exit_code=0, output='done') is False
    row = runner.local_metadata_handle().read_page(ctx())['rows'][0]
    assert row['lifecycle'] == 'unknown' and row['receipts']['terminal'] is False
    assert row['receipts']['recovery'] is True


@pytest.mark.parametrize('contents', ['{"jobs":[null]}', '{"jobs":[{"id":""}]}', '{"jobs":{}}'])
def test_malformed_cold_history_never_claims_complete_empty(tmp_path, contents):
    (tmp_path / 'swarm_local_jobs.json').write_text(contents)
    runner = Runner(tmp_path)
    page = runner.local_metadata_handle().read_page(ctx())
    assert page['page']['outcome'] == 'unavailable'
    assert page['rows'] == []



def test_copy_observation_containers_never_copies_index_or_changes_authority(tmp_path):
    runner = Runner(tmp_path)
    seed(runner, 1)
    index = runner.local_metadata_handle()
    copied = copy.deepcopy(runner._local_jobs)
    assert type(copied) is dict and type(copied['local-0000']) is dict
    copied['local-0000']['status'] = 'failed'
    shallow = copy.copy(runner._local_jobs['local-0000'])
    shallow['status'] = 'cancelled'
    assert index.read_page(ctx())['rows'][0]['lifecycle'] == 'completed'


def test_provider_registration_cannot_replace_observed_cancellation_identity(tmp_path):
    runner = Runner(tmp_path)
    runner._register_local_job('local-once', 'first', skip_routing_preview=True)
    first = runner._local_jobs['local-once']
    event = runner._local_job_cancels['local-once']
    ref = runner.local_metadata_handle().ref('local-once')
    with pytest.raises(ValueError, match='identity conflict'):
        runner._register_local_job('local-once', 'replacement', skip_routing_preview=True)
    assert runner._local_jobs['local-once'] is first
    assert runner._local_job_cancels['local-once'] is event
    assert runner.local_metadata_handle().ref('local-once') == ref
