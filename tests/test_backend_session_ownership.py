"""Session ownership races without providers or live durable stores."""
from copy import copy
from types import SimpleNamespace
import json
import threading

import pytest

from harness.api.attach import AttachServices, attach_view, ensure_active_pilot_ready
from harness.api.session_control import SessionControlServices, get_session_goal, post_session_goal
from harness.conversation import ConversationalSession
from harness.deferred_attach import DeferredPilotPlaceholder
from harness.session_goal import SessionGoal, SessionGoalStore
from harness.session_runners import SessionRunnerRegistry


def services(tmp_path):
    cfg = SimpleNamespace(repo='A', driver='driver-A', state_dir=str(tmp_path))
    box = SimpleNamespace(pilot=None, session=SimpleNamespace())
    reg = SessionRunnerRegistry(max_concurrent_sessions=3)
    def build(*, config=None):
        return SimpleNamespace(config=config or copy(cfg), state_dir=str(tmp_path),
                               harness_session_id='', _busy=threading.Lock())
    svc = AttachServices(
        get_pilot=lambda: box.pilot, set_pilot=lambda p: setattr(box, 'pilot', p),
        get_session=lambda: box.session, set_session=lambda s: setattr(box, 'session', s),
        cfg=cfg, runners=reg, sessions=SimpleNamespace(active='A', rows=lambda: [{'id': 'A'}, {'id': 'B'}]),
        pilot_swap_lock=threading.RLock(), bind_pilot_services=lambda p: None,
        build_conversational_pilot=build, sync_pilot_session_id=lambda: None,
        sessions_state_dir=lambda: str(tmp_path), diag=lambda *a, **k: None,
        apply_model_context_window=lambda: None,
        freeze_pilot_meters_into_boot_carry=lambda p: None,
        runner_config_snapshot=lambda: copy(cfg))
    return svc, box


def test_build_captures_config_before_worker_starts(tmp_path, monkeypatch):
    svc, box = services(tmp_path)
    pending = []
    monkeypatch.setattr('harness.api.attach.schedule_deferred_build',
                        lambda fn, **kw: pending.append((fn, kw)))
    monkeypatch.setenv('HARNESS_DEFER_COLD_ATTACH', '1')
    a = attach_view('A', svc, load_transcript_on_create=False, defer_cold_build=True)
    svc.cfg.repo, svc.cfg.driver = 'B', 'driver-B'
    b = attach_view('B', svc, load_transcript_on_create=False, defer_cold_build=True)
    # Scheduling barrier: neither worker executes until both views are selected.
    for fn, callbacks in pending:
        callbacks['on_done'](fn())
    assert (a.real_pilot.config.repo, a.real_pilot.config.driver) == ('A', 'driver-A')
    assert (b.real_pilot.config.repo, b.real_pilot.config.driver) == ('B', 'driver-B')
    assert a.real_pilot.harness_session_id == 'A'
    assert box.pilot is b.real_pilot


def test_readiness_waiter_cannot_publish_over_new_view(tmp_path):
    svc, box = services(tmp_path)
    a = DeferredPilotPlaceholder(session_id='A', state_dir=str(tmp_path))
    b = DeferredPilotPlaceholder(session_id='B', state_dir=str(tmp_path))
    svc.runners.get_or_create('A', lambda: a)
    svc.runners.get_or_create('B', lambda: b)
    svc.runners.set_active_view('A')
    box.pilot = a
    entered, release = threading.Event(), threading.Event()
    original = a.ensure_ready
    def wait(**kw):
        entered.set()
        assert release.wait(3)
        return original(**kw)
    a.ensure_ready = wait
    errors = []
    def request():
        try:
            ensure_active_pilot_ready(svc, timeout=1)
        except RuntimeError as exc:
            errors.append(str(exc))
    thread = threading.Thread(target=request)
    thread.start()
    assert entered.wait(3)
    svc.runners.set_active_view('B')
    box.pilot = b
    a.mark_ready(SimpleNamespace(harness_session_id='A', state_dir=str(tmp_path)))
    release.set()
    thread.join(3)
    assert not thread.is_alive()
    assert box.pilot is b
    assert errors


def goal_session(tmp_path, sid):
    session = ConversationalSession.__new__(ConversationalSession)
    session.state_dir = str(tmp_path)
    session.harness_session_id = sid
    session._goal_store = SessionGoalStore(str(tmp_path))
    session._session_goal = session._goal_store.load()
    session.reload_session_goal()
    return session


def test_goals_survive_independent_pause_resume_eviction_reload(tmp_path):
    a = goal_session(tmp_path, 'A')
    b = goal_session(tmp_path, 'B')
    a.set_session_goal('alpha')
    b.set_session_goal('beta')
    a.pause_session_goal()
    b.complete_session_goal()
    registry = SessionRunnerRegistry(max_concurrent_sessions=3)
    registry.get_or_create('A', lambda: a)
    registry.get_or_create('B', lambda: b)
    registry.drop('A', notify=False)
    registry.drop('B', notify=False)
    del a, b
    a, b = goal_session(tmp_path, 'A'), goal_session(tmp_path, 'B')
    assert a.session_goal_dict()['text'] == 'alpha'
    assert a.session_goal_dict()['status'] == 'paused'
    a.resume_session_goal()
    a.reload_session_goal()
    assert a.session_goal_dict()['status'] == 'active'
    assert b.session_goal_dict()['text'] == 'beta'
    assert b.session_goal_dict()['status'] == 'complete'
    assert goal_session(tmp_path, 'B').session_goal_dict()['status'] == 'complete'


def test_legacy_goal_requires_explicit_owner(tmp_path):
    legacy = SessionGoalStore(str(tmp_path))
    legacy.save(SessionGoal().set('legacy'))
    assert goal_session(tmp_path, 'A').session_goal_dict()['text'] == ''
    assert goal_session(tmp_path, 'B').session_goal_dict()['text'] == ''
    assert legacy.load().text == 'legacy'
    path = tmp_path / 'session_goal.json'
    payload = json.loads(path.read_text())
    payload['session_id'] = 'A'
    path.write_text(json.dumps(payload))
    assert goal_session(tmp_path, 'A').session_goal_dict()['text'] == 'legacy'
    assert goal_session(tmp_path, 'B').session_goal_dict()['text'] == ''


@pytest.mark.parametrize('switch_during_gate', [False, True])
def test_goal_mutation_rejects_foreign_session(tmp_path, switch_during_gate):
    a, b = SimpleNamespace(harness_session_id='A'), SimpleNamespace(harness_session_id='B')
    mutations = []
    b.resume_session_goal = lambda: mutations.append('B')
    box = SimpleNamespace(pilot=a if switch_during_gate else b)
    def gate():
        box.pilot = b
        return None
    svc = SessionControlServices(
        cfg=SimpleNamespace(), get_pilot=lambda: box.pilot, get_runners=lambda: None,
        gate_active_pilot_ready=gate, stash_put=lambda *a: '',
        save_active_transcript=lambda: None, upload_dir=str(tmp_path), diag=lambda *a: None)
    code, _ = post_session_goal({'action': 'resume', 'session_id': 'A'}, svc)
    assert code == 409
    assert mutations == []
    code, _ = get_session_goal('A', svc)
    assert code == 409


def test_goal_request_resolves_registered_background_owner(tmp_path):
    a, b = goal_session(tmp_path, 'A'), goal_session(tmp_path, 'B')
    svc = SessionControlServices(
        cfg=SimpleNamespace(), get_pilot=lambda: b, get_runners=lambda: {'A': a, 'B': b},
        gate_active_pilot_ready=lambda: {'error': 'unrelated view loading'},
        stash_put=lambda *a: '', save_active_transcript=lambda: None,
        upload_dir=str(tmp_path), diag=lambda *a: None)
    code, _ = post_session_goal({'text': 'alpha', 'session_id': 'A'}, svc)
    assert code == 200
    assert get_session_goal('A', svc)[1]['goal']['text'] == 'alpha'
    assert b.session_goal_dict()['text'] == ''


def test_bound_goal_rebind_does_not_copy_previous_owner(tmp_path):
    session = goal_session(tmp_path, 'A')
    session.set_session_goal('alpha')
    session.harness_session_id = 'B'
    session.reload_session_goal()
    assert session.session_goal_dict()['text'] == ''
    session.set_session_goal('beta')
    session.harness_session_id = 'A'
    session.reload_session_goal()
    assert session.session_goal_dict()['text'] == 'alpha'


def test_goal_legacy_request_detects_switch_during_gate(tmp_path):
    a = goal_session(tmp_path, 'A')
    b = goal_session(tmp_path, 'B')
    b.set_session_goal('beta')
    b.pause_session_goal()
    box = SimpleNamespace(pilot=a)
    def gate():
        box.pilot = b
        return None
    svc = SessionControlServices(
        cfg=SimpleNamespace(), get_pilot=lambda: box.pilot, get_runners=lambda: None,
        gate_active_pilot_ready=gate, stash_put=lambda *a: '',
        save_active_transcript=lambda: None, upload_dir=str(tmp_path), diag=lambda *a: None)
    code, _ = post_session_goal({'action': 'resume'}, svc)
    assert code == 409
    assert b.session_goal_dict()['status'] == 'paused'


def test_goal_mutation_rechecks_pointer_after_gate(tmp_path):
    a = goal_session(tmp_path, 'A')
    b = goal_session(tmp_path, 'B')
    a.set_session_goal('alpha')
    b.set_session_goal('beta')
    a.pause_session_goal()
    b.pause_session_goal()
    box = SimpleNamespace(pilot=a)
    class SwitchBeforeLock:
        def __enter__(self):
            box.pilot = b
        def __exit__(self, *args):
            pass
    svc = SessionControlServices(
        cfg=SimpleNamespace(), get_pilot=lambda: box.pilot, get_runners=lambda: None,
        gate_active_pilot_ready=lambda: None, stash_put=lambda *a: '',
        save_active_transcript=lambda: None, upload_dir=str(tmp_path), diag=lambda *a: None,
        pilot_swap_lock=SwitchBeforeLock())
    code, _ = post_session_goal({'action': 'resume', 'session_id': 'A'}, svc)
    assert code == 409
    assert a.session_goal_dict()['status'] == 'paused'
    assert b.session_goal_dict()['status'] == 'paused'
