"""Native foreground lane regressions extracted from the combined suite."""
import copy
import json
import pytest

pytest.importorskip('harness.job_readmodel', reason='requires companion PR2 metadata reader')
from pathlib import Path
from dataclasses import asdict
from harness.api.job_readmodel import get_local_metadata
from harness.job_readmodel import InvalidReadRequest, ActiveContext, KnownSources, MetadataReader, ReadContext
from harness.local_job_metadata import ACTIVE, JOURNAL_SIZE, LIFECYCLES
from test_local_job_metadata import Runner, ctx, forbidden, row, seed

def query(context, **kwargs):
    return {k: [str(v)] for k, v in dict(asdict(context), **kwargs).items()}


def native_fixture(path, terminal_count=3200, active_count=1):
    """Callable integration fixture: real LocalJobsMixin; foreground IDs sort last."""
    runner = Runner(path)
    seed(runner, terminal_count)
    with runner._local_jobs_lock:
        for i in range(active_count):
            jid = f'zz-active-{i:04d}'
            runner._local_jobs[jid] = dict(row(jid), status='running', terminal_receipt=None)
    return runner


def test_native_first_foreground_and_hotpath_guards(tmp_path, monkeypatch):
    runner = native_fixture(tmp_path)
    index = runner.local_metadata_handle()
    assert index.read_page(ctx())['rows'][0]['local_ref']['job_id'] == 'local-0000'
    monkeypatch.setattr(copy, 'deepcopy', forbidden)
    monkeypatch.setattr(runner, 'live_local_jobs', forbidden)
    monkeypatch.setattr(runner, 'get_local_job', forbidden)
    monkeypatch.setattr(runner, '_publish_local_metadata_locked', forbidden)
    monkeypatch.setattr(runner, '_load_local_jobs', forbidden)
    monkeypatch.setattr('builtins.open', forbidden)
    monkeypatch.setattr(Path, 'read_bytes', forbidden)
    monkeypatch.setattr(Path, 'read_text', forbidden)
    class NoScan(dict):
        values = items = __iter__ = forbidden
    class NoWalk(list):
        __iter__ = __reversed__ = forbidden
        def __getitem__(self, key):
            assert not isinstance(key, slice)
            return super().__getitem__(key)
    runner._local_jobs = NoScan(runner._local_jobs)
    index.rows = NoScan(index.rows)
    index.keys = NoWalk(index.keys)
    index.active_keys = NoWalk(index.active_keys)
    for _ in range(30):
        page = index.read_page(ctx(), lane='active')
        assert page['page']['outcome'] == 'complete'
        assert page['page']['scanned'] == 1
        assert page['rows'][0]['local_ref']['job_id'] == 'zz-active-0000'
        assert page['rows'][0]['activity'] == 'active'
        assert len(json.dumps(page).encode()) <= 32768


def test_native_active_continuation_survives_busy_and_terminal_writes(tmp_path):
    runner = native_fixture(tmp_path, active_count=121)
    index = runner.local_metadata_handle()
    first = index.read_page(ctx(), lane='active')
    checkpoint = first['page']['revision']
    assert first['page']['checkpoint'] == 0
    ids = [r['local_ref']['job_id'] for r in first['rows']]
    cursor = first['page']['next_cursor']
    while cursor:
        # Saturating HISTORY's journal must not erase the active journal.
        for i in range(JOURNAL_SIZE + 1):
            runner._local_jobs['local-0000']['updated_at'] = i
        runner._local_jobs['zz-active-0120']['updated_at'] += 1
        page = index.read_page(ctx(), lane='active', cursor=cursor)
        assert page['page']['outcome'] in ('partial', 'complete')
        assert page['page']['revision'] == checkpoint
        assert page['page']['scanned'] <= 51 and len(page['rows']) <= 50
        assert len(json.dumps(page).encode()) <= 32768
        ids.extend(r['local_ref']['job_id'] for r in page['rows'])
        cursor = page['page']['next_cursor']
    assert len(ids) == len(set(ids)) == 121
    assert page['page']['checkpoint'] == checkpoint
    changes = index.read_page(ctx(), lane='active', mode='changes', after_revision=checkpoint)
    assert changes['page']['outcome'] == 'complete'
    assert changes['page']['checkpoint'] == index.active_revision
    assert {r['local_ref']['job_id'] for r in changes['rows']} == {'zz-active-0120'}
    assert index.read_page(ctx(), mode='changes', after_revision=0)['page']['outcome'] == 'expired'


@pytest.mark.parametrize('status', sorted(LIFECYCLES))
def test_every_native_lifecycle_moves_and_exact_removals(tmp_path, status):
    runner = native_fixture(tmp_path, terminal_count=0)
    index = runner.local_metadata_handle()
    jid = 'zz-active-0000'
    start = index.active_revision
    runner._local_jobs[jid]['status'] = status
    active = index.read_page(ctx(), lane='active', mode='changes', after_revision=start)
    assert active['page']['outcome'] == 'complete'
    changed = active['rows'][0]
    assert changed['local_ref'] == index.ref(jid)
    assert changed['revision'] == index.revision
    if status in ACTIVE:
        assert changed['activity'] == 'active' and not changed['deleted']
    else:
        assert changed.keys() == {'local_ref', 'session_id', 'revision', 'deleted'}
        assert changed['deleted']
    history = index.read_page(ctx())['rows'][0]
    assert not history['deleted'] and history['lifecycle'] == status
    if status == 'stalled':
        assert history['activity'] == 'attention'
    assert bool(index.active_keys) == (status in ACTIVE)
    before = index.active_revision
    runner._local_jobs[jid]['status'] = 'queued'
    admitted = index.read_page(ctx(), lane='active', mode='changes', after_revision=before)
    assert admitted['rows'][0]['activity'] == 'active'
    before = index.active_revision
    runner._local_jobs[jid]['session_id'] = 'B'
    lost = index.read_page(ctx(), lane='active', mode='changes', after_revision=before)
    gained = index.read_page(dict(ctx(), session_id='B'), lane='active', mode='changes', after_revision=before)
    assert lost['rows'][0]['deleted'] and not gained['rows'][0]['deleted']
    assert 'B' not in json.dumps(lost)
    before = index.active_revision
    del runner._local_jobs[jid]
    deleted = index.read_page(dict(ctx(), session_id='B'), lane='active', mode='changes', after_revision=before)
    assert deleted['rows'][0]['deleted'] and not index.active_keys


@pytest.mark.parametrize('mutation', ['admit', 'depart', 'session'])
def test_native_active_membership_change_expires_snapshot(tmp_path, mutation):
    runner = native_fixture(tmp_path, terminal_count=1, active_count=80)
    index = runner.local_metadata_handle()
    first = index.read_page(ctx(), lane='active')
    if mutation == 'admit':
        runner._local_jobs['local-0000']['status'] = 'queued'
    elif mutation == 'depart':
        runner._local_jobs['zz-active-0000']['status'] = 'complete'
    else:
        runner._local_jobs['zz-active-0000']['session_id'] = 'B'
    page = index.read_page(ctx(), lane='active', cursor=first['page']['next_cursor'])
    assert page['page']['outcome'] == 'expired' and page['page']['checkpoint'] == 0
    assert page['rows'] == []


def test_native_active_query_binding_and_separate_overflow(tmp_path):
    runner = native_fixture(tmp_path, active_count=80)
    index = runner.local_metadata_handle()
    first = index.read_page(ctx(), lane='active')
    cursor = first['page']['next_cursor']
    with pytest.raises(InvalidReadRequest):
        index.read_page(ctx(), lane='history', cursor=cursor)
    context = ReadContext(**ctx())
    reader = MetadataReader(lambda: ActiveContext('A', '/repo', 'generation'), KnownSources(()), index)
    assert get_local_metadata(query(context, lane='active'), reader)[0] == 200
    assert get_local_metadata(query(context, lane='history'), reader)[0] == 200
    for value in ('', 'actions', 'ACTIVE'):
        assert get_local_metadata(query(context, lane=value), reader)[0] == 400
    qs = query(context, lane='active')
    qs['lane'].append('history')
    assert get_local_metadata(qs, reader)[0] == 400
    first_changes = index.read_page(ctx(), lane='active', mode='changes')
    assert first_changes['page']['outcome'] == 'partial'
    for i in range(JOURNAL_SIZE + 1):
        runner._local_jobs['zz-active-0000']['updated_at'] = i
    for continuation in (None, first_changes['page']['next_cursor']):
        page = index.read_page(ctx(), lane='active', mode='changes', cursor=continuation)
        assert page['page']['outcome'] == 'expired' and page['page']['checkpoint'] == 0
    # Payload-only updates do not invalidate membership traversal, even if changes expired.
    assert index.read_page(ctx(), lane='active', cursor=cursor)['page']['outcome'] == 'complete'
    assert len(index.active_journal) == len(index.journal) == JOURNAL_SIZE


def test_native_active_changes_freeze_upper_and_preserve_revision_order(tmp_path):
    runner = native_fixture(tmp_path, terminal_count=10, active_count=100)
    index = runner.local_metadata_handle()
    first = index.read_page(ctx(), lane='active', mode='changes')
    upper = first['page']['revision']
    cursor = first['page']['next_cursor']
    assert cursor and first['page']['checkpoint'] == 0
    runner._local_jobs['zz-active-0000']['updated_at'] = 20
    revisions = [r['revision'] for r in first['rows']]
    while cursor:
        page = index.read_page(ctx(), lane='active', mode='changes', cursor=cursor)
        assert page['page']['revision'] == upper
        revisions.extend(r['revision'] for r in page['rows'])
        cursor = page['page']['next_cursor']
    assert revisions == sorted(revisions) and len(revisions) == 100
    assert page['page']['checkpoint'] == upper < index.active_revision
    later = index.read_page(ctx(), lane='active', mode='changes', after_revision=upper)
    assert later['page']['outcome'] == 'complete'
    assert later['rows'][0]['revision'] > max(revisions)
    # Later payload revisions never move the earlier coverage checkpoint forward.
    assert later['page']['checkpoint'] == index.active_revision

