"""Real registry/Session lifetimes and public PM stores; no provider work."""
import threading

import pytest

from harness.job_metadata_capability import bounded_metadata_available

if not bounded_metadata_available():
    pytest.skip("public PM lacks bounded metadata APIs", allow_module_level=True)

from harness.config import HarnessConfig
from harness.session import Session
from harness.session_runners import SessionRunnerRegistry
from harness.job_readmodel import ReadContext, ViewChanged
from harness.job_metadata_view import refresh_view
from harness.api.job_readmodel import get_job_metadata


@pytest.fixture(autouse=True)
def isolate_discovery(tmp_path, monkeypatch):
    monkeypatch.setenv('PUPPETMASTER_STATE_DIR', str(tmp_path / 'absent-cli'))
    monkeypatch.setenv('HARNESS_CLI_CROSS_PROJECT', '0')


def runner(tmp_path, sid='A'):
    root = tmp_path / sid
    root.mkdir(exist_ok=True)
    return Session(HarnessConfig(driver='stub-oracle-v2', repo=str(tmp_path),
                                 state_dir=str(root), swarm_adapter='demo'))


def seeded(tmp_path):
    reg = SessionRunnerRegistry()
    a = runner(tmp_path)
    # Session.state() really constructs a new DurableState on each invocation.
    state = a.state()
    assert state is not a.state()
    job = state.store.create_job('owned', origin='marionette', session_id='A')
    reg.get_or_create('A', lambda: a)
    reg.set_active_view('A')
    view = reg.metadata_view
    assert view.describe()['availability'] == 'unavailable'
    assert refresh_view({'view_generation': view.capture().generation}, view)[0] == 200
    ctx = view.capture()
    read = ReadContext(ctx.session_id, ctx.repo, ctx.generation, 'session')
    selection = next(s.selection for s in view.reader().sources.stores if s.selection.source == 'harness')
    return reg, a, read, selection, job


def test_session_state_not_stable_and_polls_never_construct(tmp_path, monkeypatch):
    reg, a, ctx, selection, _ = seeded(tmp_path)
    reader = reg.metadata_view.reader()
    def forbidden(*a, **kw):
        pytest.fail('poll performed construction/discovery/body work')
    monkeypatch.setattr(Session, 'state', forbidden)
    monkeypatch.setattr('harness.job_metadata_view.discover_sources', forbidden)
    monkeypatch.setattr('harness.job_readmodel.create_store', forbidden)
    monkeypatch.setattr('harness.session.DurableState', forbidden)
    from puppetmaster.store import SwarmStore
    for name in ('get_job', 'list_jobs', 'list_tasks', 'list_artifacts'):
        monkeypatch.setattr(SwarmStore, name, forbidden)
    for _ in range(30):
        assert reg.metadata_view.reader() is reader
        page = reader.read_job_page(ctx, selection)
        assert len(page['rows']) == 1
        assert page['page']['outcome'] == 'complete'


def test_registry_aba_detach_drop_replace_invalidate(tmp_path):
    reg, a, ctx, selection, _ = seeded(tmp_path)
    view = reg.metadata_view
    first = view.reader()
    b = runner(tmp_path, 'B')
    reg.get_or_create('B', lambda: b)
    reg.set_active_view('B')
    reg.set_active_view('A')
    with pytest.raises(ViewChanged):
        first.read_job_page(ctx, selection)
    epoch = view.capture().generation
    reg.set_active_view('A')
    assert view.capture().generation == epoch
    assert not reg.detach_view('B')
    assert view.capture().generation == epoch
    reg.replace('A', runner(tmp_path))
    assert view.capture().generation != epoch
    epoch = view.capture().generation
    reg.detach_view('A')
    assert view.capture().session_id == ''
    assert view.capture().generation != epoch
    reg.set_active_view('A')
    epoch = view.capture().generation
    reg.drop('A')
    assert view.capture().session_id == ''
    assert view.capture().generation != epoch


def test_input_admission_gates_still_precede_epoch_change(tmp_path):
    reg, a, _, _, _ = seeded(tmp_path)
    epoch = reg.metadata_view.capture()
    a._input_admissions = 1
    with pytest.raises(RuntimeError):
        reg.drop('A')
    with pytest.raises(RuntimeError):
        reg.replace('A', runner(tmp_path, 'replacement'))
    assert reg.metadata_view.capture() == epoch
    assert reg.get('A') is a


def test_held_discovery_cannot_publish_after_switch(tmp_path, monkeypatch):
    reg, a, _, _, _ = seeded(tmp_path)
    view = reg.metadata_view
    entered, release = threading.Event(), threading.Event()
    from harness.job_metadata_view import discover_sources
    def held(*args):
        entered.set()
        assert release.wait(5)
        return discover_sources(*args)
    monkeypatch.setattr('harness.job_metadata_view.discover_sources', held)
    result = []
    t = threading.Thread(target=lambda: result.append(refresh_view(
        {'view_generation': view.capture().generation}, view)))
    t.start()
    assert entered.wait(5)
    reg.set_active_view('B', repo=str(tmp_path / 'B'))
    new = view.capture()
    assert view.refresh(new.generation)[0] == 409
    release.set()
    t.join(5)
    assert not t.is_alive()
    assert result == [(409, {'code': 'view_changed'})]
    assert view.capture() == new
    assert view.describe()['availability'] == 'unavailable'
    assert view._discovery is None


def test_held_query_rejects_aba_without_blocking_transition(tmp_path, monkeypatch):
    reg, _, ctx, selection, _ = seeded(tmp_path)
    reader = reg.metadata_view.reader()
    store = reader.sources.resolve(selection).handle
    original = store.list_job_summaries
    entered, release = threading.Event(), threading.Event()
    def held(**kwargs):
        entered.set()
        assert release.wait(5)
        return original(**kwargs)
    monkeypatch.setattr(store, 'list_job_summaries', held)
    result = []
    qs = dict(session_id=['A'], repo=[ctx.repo], view_generation=[ctx.view_generation],
              scope=['session'], source=[selection.source], state_id=[selection.state_id], mode=['snapshot'])
    t = threading.Thread(target=lambda: result.append(get_job_metadata(qs, reader)))
    t.start()
    assert entered.wait(5)
    reg.set_active_view('B', repo=ctx.repo)
    reg.set_active_view('A')
    release.set()
    t.join(5)
    assert not t.is_alive()
    assert result == [(409, {'code': 'view_changed'})]


def test_refresh_failure_and_missing_roots_are_unavailable(tmp_path, monkeypatch):
    reg = SessionRunnerRegistry()
    reg.get_or_create('A', lambda: runner(tmp_path))
    reg.set_active_view('A')
    view = reg.metadata_view
    missing_root = tmp_path / 'A'
    assert list(missing_root.iterdir()) == []
    assert view.refresh(view.capture().generation)[0] == 200
    assert list(missing_root.iterdir()) == []
    assert all(not item['available'] for item in view.describe()['sources'])
    def fail(*a):
        raise OSError('fixture')
    monkeypatch.setattr('harness.job_metadata_view.discover_sources', fail)
    assert view.refresh(view.capture().generation)[0] == 200
    assert view.describe()['availability'] == 'unavailable'
    assert view.describe()['missing'] == ['source_discovery_unavailable']


def test_real_host_attach_rebuild_swap_relocate_workspace_and_clear(owned_server, tmp_path, monkeypatch):
    from harness.api.sessions import handle_session_relocate, post_sessions_clear, post_sessions_switch
    from harness.api.workspace import post_workspace_forget, post_workspace_open
    srv = owned_server
    monkeypatch.setenv('HARNESS_DEFER_COLD_ATTACH', '0')
    monkeypatch.setattr(srv, '_puppetmaster_available', lambda: False)
    monkeypatch.setattr(srv, '_index_codegraph_bg', lambda *a: None)
    monkeypatch.setattr(srv, '_maybe_refresh_codegraph', lambda *a: None)
    a, b = tmp_path / 'repo-a', tmp_path / 'repo-b'
    a.mkdir(); b.mkdir()
    srv._cfg.repo = str(a)
    sid = srv._sessions.active
    srv._sessions.relocate(sid, str(a), repo=str(a), make_active=True)
    srv._attach_view(sid)
    view = srv._runners.metadata_view
    assert view.capture().session_id == sid and view.capture().repo == str(a)
    original = view.capture()
    srv._rebuild_pilot_and_session()
    assert view.capture().generation != original.generation
    original = view.capture()
    srv._perform_pilot_swap(srv._cfg.driver)
    assert view.capture().generation != original.generation
    original = view.capture()
    assert handle_session_relocate({'session_id': sid, 'path': str(b)}, srv._session_services())[0] == 200
    assert view.capture().repo == str(b) and view.capture().session_id == sid
    assert view.capture().generation != original.generation
    original = view.capture()
    assert post_workspace_open({'path': str(a)}, srv._workspace_services())[0] == 200
    assert view.capture().repo == str(a) and view.capture().generation != original.generation
    original = view.capture()
    assert post_sessions_switch({'id': sid}, srv._session_services())[0] == 200
    assert view.capture().repo == str(b) and view.capture().session_id == sid
    assert view.capture().generation != original.generation
    original = view.capture()
    assert post_workspace_forget({'path': str(b)}, srv._workspace_services())[0] == 200
    assert view.capture().session_id == '' and view.capture().generation != original.generation
    srv._cfg.repo = str(b)
    srv._attach_view(sid)
    original = view.capture()
    assert post_sessions_clear(srv._session_services())[0] == 200
    assert view.capture().generation != original.generation
    assert view.capture().session_id != sid


def test_deferred_real_host_publication_rotates_epoch(owned_server, monkeypatch):
    srv = owned_server
    pending = []
    monkeypatch.setenv('HARNESS_DEFER_COLD_ATTACH', '1')
    monkeypatch.setattr('harness.api.attach.schedule_deferred_build', lambda fn, **kw: pending.append((fn, kw)))
    row = srv._sessions.create('deferred', repo=srv._cfg.repo)
    srv._attach_view(row['id'], defer_cold_build=True)
    view = srv._runners.metadata_view
    before = view.capture()
    build, callbacks = pending.pop()
    callbacks['on_done'](build())
    assert view.capture().session_id == row['id']
    assert view.capture().generation != before.generation
    assert srv._pilot is srv._runners.get(row['id'])


def test_rejected_transition_restores_context_but_never_old_epoch(tmp_path):
    reg, _, ctx, _, _ = seeded(tmp_path)
    view = reg.metadata_view
    ticket = view.invalidate()
    view.restore(ticket)
    assert view.capture().session_id == ctx.session_id
    assert view.capture().repo == ctx.repo
    assert view.capture().generation != ctx.view_generation
    stale = view.invalidate()
    reg.set_active_view('B', repo='B')
    current = view.capture()
    view.restore(stale)
    assert view.capture() == current


def test_rejected_host_switch_and_lease_restore_refreshable_view(owned_server, tmp_path):
    from harness.api.sessions import post_sessions_switch
    srv = owned_server
    srv._cfg.repo = str(tmp_path)
    srv._attach_view(srv._sessions.active)
    view = srv._runners.metadata_view
    before = view.capture()
    status, result = post_sessions_switch({'id': 'not-found'}, srv._session_services())
    assert result['ok'] is False
    assert view.capture().session_id == before.session_id and view.capture().repo == before.repo
    assert view.capture().generation != before.generation
    # A lease failure occurs after SessionStore switches, before view attach.
    srv._runners._max = 1
    srv._pilot._busy.acquire()
    other = srv._sessions.create('other', repo=str(tmp_path))
    srv._sessions.switch(before.session_id)
    try:
        status, _ = post_sessions_switch({'id': other['id']}, srv._session_services())
    finally:
        srv._pilot._busy.release()
    assert status == 409
    assert view.capture().session_id == before.session_id and view.capture().repo == before.repo


def test_empty_discovery_is_unavailable_not_complete_empty(tmp_path, monkeypatch):
    from harness.job_readmodel import KnownSources
    reg = SessionRunnerRegistry()
    reg.get_or_create('A', lambda: runner(tmp_path))
    reg.set_active_view('A')
    monkeypatch.setattr('harness.job_metadata_view.discover_sources', lambda *a: KnownSources(()))
    view = reg.metadata_view
    assert view.refresh(view.capture().generation)[0] == 200
    assert view.describe()['availability'] == 'unavailable'
    assert view.describe()['missing'] == ['no_known_sources']


def test_source_refresh_replaces_reader_and_revokes_old_cursor(tmp_path):
    reg, _, ctx, selection, _ = seeded(tmp_path)
    view = reg.metadata_view
    old = view.reader()
    generation = view.capture().generation
    assert view.refresh(generation)[0] == 200
    assert view.reader() is not old
    assert view.capture().generation != generation
    with pytest.raises(ViewChanged):
        old.read_job_page(ctx, selection)


def test_attach_captures_repo_before_factory_work(owned_server, tmp_path):
    srv = owned_server
    a, b = str(tmp_path / 'A'), str(tmp_path / 'B')
    row = srv._sessions.create('capture', repo=a)
    srv._cfg.repo = a
    def build():
        pilot = srv._build_conversational_pilot()
        # A second workspace request changed global config during this build.
        srv._cfg.repo = b
        return pilot
    srv._attach_view(row['id'], factory=build, load_transcript_on_create=False)
    capture = srv._runners.metadata_view.capture()
    assert capture.session_id == row['id'] and capture.repo == a
    assert srv._cfg.repo == b
