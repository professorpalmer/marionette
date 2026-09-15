"""Session lifecycle uses real durable queues without provider execution."""
from dataclasses import replace

import pytest

from harness.session_runners import SessionRunnerRegistry
from harness.sessions import SessionStore
from tests.test_session_queue_durability import factory


@pytest.fixture
def server(factory, tmp_path, monkeypatch):
    import harness.server as srv
    monkeypatch.setattr(srv, '_cfg', replace(factory().config, state_dir=str(tmp_path)))
    monkeypatch.setattr(srv, '_sessions', SessionStore(str(tmp_path / 'sessions.json')))
    monkeypatch.setattr(srv, '_runners', SessionRunnerRegistry(max_concurrent_sessions=2))
    monkeypatch.setattr(srv, '_pilot', None)
    monkeypatch.setattr(srv, '_build_conversational_pilot', lambda **kw: factory(''))
    monkeypatch.setattr(srv, '_save_workspace_driver', lambda *_: None)
    monkeypatch.setattr(srv, '_apply_model_context_window', lambda: None)
    monkeypatch.setattr('harness.hooks.run_hooks', lambda *_: None)
    return srv


def attach(srv, title):
    sid = srv._sessions.create(title)['id']
    pilot = srv._attach_view(sid, defer_cold_build=False)
    pilot.enqueue_prompt(title)
    return sid, pilot


@pytest.mark.parametrize('clear', [False, True])
def test_last_delete_clears_pilot_without_rebinding(server, clear):
    from harness.api.sessions import post_sessions_clear
    sid, old = attach(server, 'A')
    owner = old._prompt_queue_owner
    if clear:
        code, body = post_sessions_clear(server._session_services())
    else:
        code, body = server._handle_session_delete(sid)
    assert code == 200
    assert body['active'] is None
    assert server._pilot is None
    assert server._runners.active_view_id is None
    assert old._prompt_queue_owner == owner
    sid_b, new = attach(server, 'B')
    assert new is not old
    assert [p['text'] for p in new.list_prompts()] == ['B']


def test_delete_current_does_not_attach_survivor(server):
    b, survivor = attach(server, 'B')
    a, deleted = attach(server, 'A')
    code, body = server._handle_session_delete(a)
    assert code == 200 and body['active'] is None
    assert server._pilot is None
    assert server._runners.active_view_id is None
    assert server._runners.get(b) is survivor
    assert [p['text'] for p in survivor.list_prompts()] == ['B']
    assert [p['text'] for p in deleted.list_prompts()] == ['A']


def test_delete_current_does_not_spend_a_lease_on_a_sibling(server):
    a, deleted = attach(server, 'A')
    c, busy_c = attach(server, 'C')
    d, busy_d = attach(server, 'D')
    busy_c._busy.acquire()
    busy_d._busy.acquire()
    server._sessions.create('cold survivor')
    server._sessions.switch(a)
    server._pilot = deleted  # A's view outlived its evicted idle registry entry.
    server._runners.set_active_view(a)
    try:
        code, body = server._handle_session_delete(a)
        assert code == 200 and body['active'] is None
        assert server._pilot is None
        assert server._runners.active_view_id is None
        assert deleted.harness_session_id == a
        for sid, p, text in ((c, busy_c, 'C'), (d, busy_d, 'D')):
            assert server._runners.get(sid) is p and p._busy.locked()
            assert [x['text'] for x in p.list_prompts()] == [text]
    finally:
        busy_c._busy.release()
        busy_d._busy.release()


def test_model_replacement_keeps_same_session_queue(server):
    sid, old = attach(server, 'A')
    server._perform_pilot_swap('offline-model')
    new = server._pilot
    assert new is not old
    assert server._runners.get(sid) is new
    assert new._prompt_queue_owner == old._prompt_queue_owner
    assert [p['text'] for p in new.list_prompts()] == ['A']


def test_failed_attach_restores_store_active(server):
    from harness.api.sessions import post_sessions_attach
    a, busy = attach(server, 'A')
    server._runners._max = 1
    b = server._sessions.create('B')['id']
    server._sessions.switch(a)
    busy._busy.acquire()
    try:
        code, body = post_sessions_attach({'id': b}, server._session_services())
        assert code == 409
        assert server._sessions.active == a
        assert server._runners.active_view_id == a
        assert server._pilot is busy
    finally:
        busy._busy.release()


def test_ready_rejects_stale_global_pilot(server):
    a, old = attach(server, 'A')
    b, current = attach(server, 'B')
    server._pilot = old
    with pytest.raises(RuntimeError, match='active session'):
        server._ensure_active_pilot_ready()
    assert old.harness_session_id == a
    assert current.harness_session_id == b


def test_model_swap_rejects_stale_global_pilot(server):
    a, old = attach(server, 'A')
    b, current = attach(server, 'B')
    server._pilot = old
    with pytest.raises(RuntimeError, match='active session'):
        server._perform_pilot_swap('offline-model')
    assert server._runners.get(b) is current
    assert [p['text'] for p in current.list_prompts()] == ['B']
    assert [p['text'] for p in old.list_prompts()] == ['A']


def test_cold_attach_rejects_foreign_factory_without_registering(server):
    a, old = attach(server, 'A')
    b = server._sessions.create('B')['id']
    with pytest.raises(RuntimeError):
        server._attach_view(b, factory=lambda: old)
    assert server._runners.get(b) is None
    assert server._pilot is old
    assert server._runners.active_view_id == a


def test_warm_attach_rejects_foreign_runner_before_publishing(server):
    a, old = attach(server, 'A')
    b, current = attach(server, 'B')
    server._runners.replace(a, current)
    with pytest.raises(RuntimeError):
        server._attach_view(a)
    assert server._pilot is current
    assert server._runners.active_view_id == b


def test_rebuild_keeps_same_session_queue(server):
    sid, old = attach(server, 'A')
    server._rebuild_pilot_and_session()
    new = server._pilot
    assert new is not old
    assert server._runners.get(sid) is new
    assert new._prompt_queue_owner == old._prompt_queue_owner
    assert [p['text'] for p in new.list_prompts()] == ['A']


def test_detached_stale_pilot_cannot_swap_into_store_session(server):
    a, old = attach(server, 'A')
    server._runners.detach_view(a)
    b = server._sessions.create('B')['id']
    with pytest.raises(RuntimeError):
        server._perform_pilot_swap('offline-model')
    assert old.harness_session_id == a
    assert server._runners.get(b) is None


def test_rebuild_rejects_view_change_during_construction(server, monkeypatch):
    from types import SimpleNamespace
    b, other = attach(server, 'B')
    a, old = attach(server, 'A')
    def switch_during_build(cfg):
        server._sessions.switch(b)
        server._attach_view(b)
        return SimpleNamespace(state_dir=cfg.state_dir)
    monkeypatch.setattr('harness.api.attach.Session', switch_during_build)
    with pytest.raises(RuntimeError, match='active session'):
        server._rebuild_pilot_and_session()
    assert server._pilot is other
    assert server._runners.get(a) is old
    assert server._runners.get(b) is other
    assert [p['text'] for p in other.list_prompts()] == ['B']


def test_last_delete_state_poll_is_idle_without_rearming_resume(server):
    from harness.api.session_control import get_session_state
    sid, old = attach(server, 'A')
    assert server._handle_session_delete(sid)[0] == 200
    code, body = get_session_state({'rearm_resume': ['1'], 'session_id': [sid]}, server._session_control_services())
    assert code == 200
    assert body == {'state': 'idle', 'pending_swarms': False, 'resume_pending': False,
                    'runners': {}, 'active_view_id': None, 'goal': {}, 'todos': {}}
    assert old.harness_session_id == sid


def test_rebuild_refuses_turn_started_during_construction(server, monkeypatch):
    import harness.api.attach as attach_api
    sid, old = attach(server, 'A')
    constructor = attach_api.Session

    def turn_starts_during_build(config):
        replacement = constructor(config)
        assert old._busy.acquire(blocking=False)
        return replacement

    monkeypatch.setattr(attach_api, 'Session', turn_starts_during_build)
    try:
        with pytest.raises(RuntimeError, match='busy'):
            server._rebuild_pilot_and_session()
        assert server._pilot is old
        assert server._runners.get(sid) is old
        assert [item['text'] for item in old.list_prompts()] == ['A']
    finally:
        old._busy.release()


@pytest.mark.parametrize('operation', ['swap', 'rebuild'])
def test_live_replacement_preserves_independent_transcript_and_receipts(server, operation):
    from copy import deepcopy
    from harness.input_receipts import session_input_store
    sid, old = attach(server, 'A')
    store = session_input_store(old)
    receipt = old.list_prompts()[0]
    old._history.append({'role': 'user', 'content': [{'type': 'text', 'text': 'native'}],
                         'input_id': receipt['id'], 'opaque': {'nested': [1]}})
    old._display_transcript = [{'type': 'tool', 'id': 'card', 'opaque': {'nested': [2]}}]
    old._session_job_ids = ['job-1']
    old._auto_distill = True
    old._stop_holds_idle = True
    old._cold_input_hold = True
    history, display = deepcopy(old._history), deepcopy(old._display_transcript)
    if operation == 'swap':
        server._perform_pilot_swap('offline-model')
    else:
        server._rebuild_pilot_and_session()
    new = server._pilot
    assert session_input_store(new) is store
    assert new._history == history and new._display_transcript == display
    assert new._session_job_ids == ['job-1']
    assert new._auto_distill and new._stop_holds_idle and new._cold_input_hold
    old._history[-1]['opaque']['nested'].append(3)
    old._display_transcript[0]['opaque']['nested'].append(4)
    old._session_job_ids.append('late-job')
    assert new._history == history and new._display_transcript == display
    assert new._session_job_ids == ['job-1']
    assert new.list_prompts()[0]['id'] == receipt['id']


@pytest.mark.parametrize('operation', ['swap', 'rebuild'])
def test_old_send_rejected_during_publication_and_after_retirement(server, monkeypatch, operation):
    from harness.input_receipts import session_input_store
    sid, old = attach(server, 'A')
    store = session_input_store(old)
    before = store.path.read_bytes()
    stale_send = old.send('stale captured request')
    calls = []
    monkeypatch.setattr(old, '_send_locked', lambda *a, **k: calls.append(a) or iter(()))
    bind = server._bind_pilot_services
    observed = []
    def bind_and_race(new):
        observed.extend(old.send('during publication'))
        bind(new)
    monkeypatch.setattr(server, '_bind_pilot_services', bind_and_race)
    if operation == 'swap':
        server._perform_pilot_swap('offline-model')
    else:
        server._rebuild_pilot_and_session()
    observed.extend(stale_send)
    assert len(observed) == 2 and all(e.kind == 'error' for e in observed)
    assert calls == []
    assert store.path.read_bytes() == before
    assert not server._pilot._busy.locked()


@pytest.mark.parametrize('operation', ['swap', 'rebuild'])
@pytest.mark.parametrize('failure', ['construct', 'bind'])
def test_failed_replacement_keeps_old_runner_usable(server, monkeypatch, operation, failure):
    from harness.input_receipts import session_input_store
    sid, old = attach(server, 'A')
    store = session_input_store(old)
    driver = old.config.driver
    action = old.enqueue_steer('keep this admitted input on failure')
    folded, closed = [], []
    monkeypatch.setattr(server, '_freeze_pilot_meters_into_boot_carry', lambda p: folded.append(p))
    monkeypatch.setattr(old, 'release_warm_acp', lambda **k: closed.append(k))
    def fail(*a, **k):
        raise RuntimeError('injected failure')
    if failure == 'bind':
        monkeypatch.setattr(server, '_bind_pilot_services', fail)
    else:
        monkeypatch.setattr('harness.conversation.ConversationalSession.__init__', fail)
    server._cfg.driver = 'offline-model'
    with pytest.raises(RuntimeError, match='injected failure'):
        if operation == 'swap':
            server._perform_pilot_swap('offline-model')
        else:
            server._cfg.driver = 'offline-model'
            server._rebuild_pilot_and_session()
    assert server._pilot is old and server._runners.get(sid) is old
    assert server._cfg.driver == driver
    assert session_input_store(old) is store
    assert [p['text'] for p in old.list_prompts()] == ['A']
    assert folded == [] and closed == []
    assert [a.id for a in old._session_actions] == [action.id]
    assert store.get(action.id)['status'] == 'accepted'
    assert not old._session_actions.closed
    assert not old._busy.locked()
    assert not getattr(old, '_replacement_retired', False)


@pytest.mark.parametrize('operation', ['swap', 'rebuild'])
def test_live_attachment_ids_survive_but_cold_attach_holds(server, tmp_path, operation):
    from harness.input_receipts import session_input_store
    sid, old = attach(server, 'A')
    uploads = tmp_path / 'uploads'
    uploads.mkdir()
    image = uploads / 'image.png'
    document = uploads / 'document.txt'
    image.write_bytes(b'native pixels')
    document.write_text('native document')
    queued = old.enqueue_prompt('  exact input\n', images=[str(image)],
                                documents=[str(document)], upload_root=str(uploads))
    old_store = session_input_store(old)
    if operation == 'swap':
        server._perform_pilot_swap('offline-model')
    else:
        server._rebuild_pilot_and_session()
    new = server._pilot
    assert new.list_prompts()[-1] == queued
    assert session_input_store(new) is old_store
    receipt = old_store.get(queued['id'])
    assert receipt['original_text'] == '  exact input\n'
    assert {a['kind'] for a in receipt['attachments']} == {'image', 'document'}
    assert {a['ref'] for a in receipt['attachments']} == set(queued['images'] + queued['documents'])
    server._runners.drop(sid, notify=False)
    cold = server._attach_view(sid, defer_cold_build=False)
    assert session_input_store(cold) is not old_store
    assert session_input_store(cold).instance != old_store.instance
    assert cold.list_prompts() == []
    assert [p['id'] for p in cold.held_prompts()] == [p['id'] for p in new.list_prompts()]
    assert cold._pop_next_prompt() == {}


@pytest.mark.parametrize('operation', ['swap', 'rebuild'])
def test_pending_steer_survives_without_sharing_action_store(server, operation):
    from harness.input_receipts import session_input_store
    sid, old = attach(server, 'A')
    action = old.enqueue_steer('pending human input')
    if operation == 'swap':
        server._perform_pilot_swap('offline-model')
    else:
        server._rebuild_pilot_and_session()
    new = server._pilot
    assert new._session_actions is not old._session_actions
    assert [a.id for a in new._session_actions] == [action.id]
    assert session_input_store(new).get(action.id)['status'] == 'accepted'
    assert old._session_actions.closed
    assert old.drop_queued_steers() == []
    assert session_input_store(new).get(action.id)['status'] == 'accepted'
    assert [a.id for a in new._session_actions] == [action.id]


@pytest.mark.parametrize('operation', ['swap', 'rebuild'])
def test_reservation_survives_old_cleanup_and_reaper(server, monkeypatch, operation):
    sid, old = attach(server, 'A')
    old._busy.acquire()
    abandoned_generation = old._mark_busy_acquired()
    old._release_busy(abandoned_generation)
    bind = server._bind_pilot_services
    checked = []
    def late_cleanup_during_bind(new):
        assert old._busy.locked()
        assert old._busy_gen != abandoned_generation
        old._release_busy(abandoned_generation)
        assert old._busy.locked()
        assert old._busy_since == 0
        assert not old._reap_stuck_turn()
        checked.append(True)
        bind(new)
    monkeypatch.setattr(server, '_bind_pilot_services', late_cleanup_during_bind)
    if operation == 'swap':
        server._perform_pilot_swap('offline-model')
    else:
        server._rebuild_pilot_and_session()
    new = server._pilot
    new._busy.acquire()
    generation = new._mark_busy_acquired()
    try:
        old._release_busy(abandoned_generation)
        assert new._busy.locked()
        assert checked == [True]
    finally:
        new._release_busy(generation)


@pytest.mark.parametrize('operation', ['swap', 'rebuild'])
def test_publication_barrier_refuses_concurrent_old_send(server, monkeypatch, operation):
    import threading
    from harness.input_receipts import session_input_store
    sid, old = attach(server, 'A')
    before = session_input_store(old).path.read_bytes()
    reached, finish = threading.Event(), threading.Event()
    replace = server._runners.replace
    errors = []
    def paused_replace(*a, **k):
        reached.set()
        assert finish.wait(5)
        return replace(*a, **k)
    monkeypatch.setattr(server._runners, 'replace', paused_replace)
    def swap():
        try:
            if operation == 'swap':
                server._perform_pilot_swap('offline-model')
            else:
                server._rebuild_pilot_and_session()
        except BaseException as e:
            errors.append(e)
    worker = threading.Thread(target=swap)
    worker.start()
    try:
        assert reached.wait(5)
        assert server._pilot is old
        events = list(old.send('must not run'))
        assert len(events) == 1 and events[0].kind == 'error'
        assert session_input_store(old).path.read_bytes() == before
    finally:
        finish.set()
        worker.join(5)
    assert not worker.is_alive() and not errors
    assert server._pilot is not old


def test_model_swap_keeps_existing_runtime_directory(server, tmp_path):
    sid, old = attach(server, 'A')
    runtime = str(tmp_path / 'fork-runtime')
    old.state_dir = runtime
    server._perform_pilot_swap('offline-model')
    assert server._pilot.state_dir == runtime
    assert server._pilot.config.state_dir == runtime


@pytest.mark.parametrize('operation', ['swap', 'rebuild'])
def test_repeated_replacement_folds_meters_once_at_old_rates(server, monkeypatch, operation):
    sid, old = attach(server, 'A')
    carry = dict(server._BOOT_METER_CARRY)
    cost = server._BOOT_CARRY_COST_USD
    model_rows = dict(server._BOOT_PILOT_BY_MODEL)
    try:
        server._BOOT_METER_CARRY.clear()
        server._BOOT_PILOT_BY_MODEL.clear()
        server._BOOT_CARRY_COST_USD = 0.0
        old._tokens_in = 1_000_000
        old._tokens_used = 1_000_000
        old._tokens_out = 0
        old._tokens_cached = 0
        monkeypatch.setattr(server, '_resolve_prices_for_runner',
                            lambda p: (5.0, 25.0) if p is old else (0.1, 0.3))
        closed = []
        monkeypatch.setattr(old, 'release_warm_acp', lambda **k: closed.append(k))
        expected = server._session_cost_split(old, 5.0, 25.0)
        for _ in range(3):
            if operation == 'swap':
                server._perform_pilot_swap('offline-model')
            else:
                server._cfg.driver = 'offline-model'
                server._rebuild_pilot_and_session()
            assert server._BOOT_CARRY_COST_USD == pytest.approx(expected)
            assert server._pilot._tokens_in == 0
            assert server._boot_usage_meters()['_tokens_in'] == 1_000_000
        assert len(closed) == 1
        assert old._tokens_in == 0
    finally:
        server._BOOT_METER_CARRY.clear()
        server._BOOT_METER_CARRY.update(carry)
        server._BOOT_PILOT_BY_MODEL.clear()
        server._BOOT_PILOT_BY_MODEL.update(model_rows)
        server._BOOT_CARRY_COST_USD = cost


@pytest.mark.parametrize('operation', ['swap', 'rebuild'])
def test_constructor_barrier_turn_admission_is_consistent(server, monkeypatch, operation):
    import threading
    from harness.conversation import ConvEvent, ConversationalSession
    from harness.input_receipts import session_input_store
    sid, old = attach(server, 'A')
    before = session_input_store(old).path.read_bytes()
    reached, finish = threading.Event(), threading.Event()
    construct = ConversationalSession.__init__
    errors, calls = [], []
    def paused_constructor(new, *a, **k):
        reached.set()
        assert finish.wait(5)
        construct(new, *a, **k)
    monkeypatch.setattr(ConversationalSession, '__init__', paused_constructor)
    def synthetic_send(*a, **k):
        calls.append(a)
        yield ConvEvent('notice', {'message': 'turn holds busy until closed'})
    monkeypatch.setattr(old, '_send_locked', synthetic_send)
    def replace():
        try:
            if operation == 'swap':
                server._perform_pilot_swap('offline-model')
            else:
                server._rebuild_pilot_and_session()
        except BaseException as e:
            errors.append(e)
    worker = threading.Thread(target=replace)
    worker.start()
    stream = None
    try:
        assert reached.wait(5)
        stream = old.send('construction race')
        first = next(stream)
        if operation == 'swap':
            assert first.kind == 'error' and calls == []
        else:
            assert first.kind == 'notice' and len(calls) == 1
            assert old._busy.locked()
        finish.set()
        worker.join(5)
        assert not worker.is_alive()
        if operation == 'swap':
            assert errors == [] and server._pilot is not old
        else:
            assert len(errors) == 1 and 'busy' in str(errors[0])
            assert server._pilot is old and server._runners.get(sid) is old
        assert session_input_store(old).path.read_bytes() == before
    finally:
        finish.set()
        worker.join(5)
        if stream is not None:
            stream.close()


@pytest.mark.parametrize('operation', ['swap', 'rebuild'])
def test_foreign_candidate_is_not_rebound_or_published(server, monkeypatch, operation):
    from harness.conversation import ConversationalSession
    b, foreign = attach(server, 'B')
    a, old = attach(server, 'A')
    owner = foreign._prompt_queue_owner
    construct = ConversationalSession.__init__
    candidates = []
    def foreign_constructor(new, *a, **k):
        construct(new, *a, **k)
        new.harness_session_id = b
        new.bind_prompt_queue(*owner)
        candidates.append(new)
    monkeypatch.setattr(ConversationalSession, '__init__', foreign_constructor)
    with pytest.raises(RuntimeError, match='own'):
        if operation == 'swap':
            server._perform_pilot_swap('offline-model')
        else:
            server._rebuild_pilot_and_session()
    assert server._pilot is old and server._runners.get(a) is old
    assert server._runners.get(b) is foreign
    assert foreign._prompt_queue_owner == owner
    assert foreign.harness_session_id == b
    assert candidates[0]._prompt_queue_owner == owner
    assert candidates[0].harness_session_id == b
    assert not old._busy.locked()


@pytest.mark.parametrize('operation', ['swap', 'rebuild'])
def test_reaped_old_generator_cleanup_cannot_release_replacement(server, monkeypatch, operation):
    import time
    from copy import deepcopy
    from harness.conversation import ConvEvent
    sid, old = attach(server, 'A')
    def paused_turn(*a, **k):
        yield ConvEvent('notice', {'message': 'abandoned turn'})
    monkeypatch.setattr(old, '_send_locked', paused_turn)
    stream = old.send('old turn')
    assert next(stream).kind == 'notice'
    monkeypatch.setattr(old, '_turn_deadline_seconds', lambda: 1)
    old._busy_since = time.monotonic() - 10
    old._busy_last_progress = old._busy_since
    assert old._reap_stuck_turn()
    if operation == 'swap':
        server._perform_pilot_swap('offline-model')
    else:
        server._rebuild_pilot_and_session()
    new = server._pilot
    new._history[0]['content'] = 'replacement owns this system prompt'
    history = deepcopy(new._history)
    new._busy.acquire()
    generation = new._mark_busy_acquired()
    try:
        stream.close()
        assert new._busy.locked()
        assert new._history == history
    finally:
        new._release_busy(generation)
        stream.close()


@pytest.mark.parametrize('callback', ['_checkpoint_transcript', '_finalize_turn'])
def test_retired_stream_callback_cannot_overwrite_replacement_transcript(server, callback):
    from harness.sessions import load_transcript
    sid, old = attach(server, 'A')
    old._history.append({'role': 'user', 'content': 'before swap'})
    server._perform_pilot_swap('offline-model')
    new = server._pilot
    new._history.append({'role': 'assistant', 'content': 'after swap'})
    server._checkpoint_transcript({'session_id': sid, 'pilot': new})
    before = load_transcript(server._cfg.state_dir, sid)
    getattr(server, callback)({'session_id': sid, 'pilot': old})
    assert load_transcript(server._cfg.state_dir, sid) == before
