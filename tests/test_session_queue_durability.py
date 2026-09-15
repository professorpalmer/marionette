"""Offline admission and real cold attach regressions; no provider execution."""
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.api.attach import AttachServices, attach_view, ensure_active_pilot_ready
from harness.api.session_control import (SessionControlServices, post_session_queue,
    post_session_queue_reorder, post_session_steer)
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.session_runners import SessionRunnerRegistry
from harness.sessions import SessionStore, save_transcript


@pytest.fixture
def factory(tmp_path, monkeypatch):
    monkeypatch.setattr('harness.conversation.prov.build_pilot', lambda *_: SimpleNamespace())
    for name in ('SkillStore', 'RuleStore'):
        monkeypatch.setattr('harness.conversation.' + name, lambda *_a, **_k: SimpleNamespace(list=lambda *_: []))
    monkeypatch.setattr('harness.conversation.MemoryStore', lambda *_a, **_k: SimpleNamespace(render_block=lambda: ''))
    monkeypatch.setattr('harness.conversation.WikiClient', lambda *_a, **_k: None)
    monkeypatch.setattr('harness.plugin_registry.list_enabled_plugin_skills', lambda *_a, **_k: [])
    monkeypatch.setattr('harness.browser_auth.ensure_shared_browser_env', lambda: {})
    def make(sid='A'):
        s = ConversationalSession(HarnessConfig(state_dir=str(tmp_path)))
        s.harness_session_id = sid
        return s
    return make


def services(s):
    return SessionControlServices(cfg=s.config, get_pilot=lambda: s, get_runners=lambda: {s.harness_session_id: s},
        gate_active_pilot_ready=lambda: None, stash_put=lambda *_: '',
        save_active_transcript=lambda: None, upload_dir=s.state_dir, diag=lambda *_: None)


def test_enqueue_model_belongs_to_captured_session(factory):
    session = factory()
    svc = services(session)
    svc.cfg = replace(session.config, driver='another-session-model')
    code, result = post_session_queue({'session_id': 'A', 'text': 'keep my model'}, svc)
    assert code == 200
    assert result['item']['model'] == session.config.driver


@pytest.mark.parametrize("deferred", [False, True])
def test_real_cold_attach_two_sessions_and_fork(factory, tmp_path, deferred):
    store = SessionStore(str(tmp_path / 'sessions.json'))
    a, b = store.create()['id'], store.create()['id']
    save_transcript(str(tmp_path), a, {'history': [{'role': 'user', 'content': 'parent history'}]})
    fork = store.fork_at(a, 1, str(tmp_path))['id']
    def boot():
        box = SimpleNamespace(pilot=None, session=SimpleNamespace(state_dir=str(tmp_path)))
        reg = SessionRunnerRegistry(max_concurrent_sessions=3)
        svc = AttachServices(get_pilot=lambda: box.pilot, set_pilot=lambda p: setattr(box,'pilot',p),
            get_session=lambda: box.session, set_session=lambda s: setattr(box,'session',s),
            cfg=HarnessConfig(state_dir=str(tmp_path)), runners=reg, sessions=store,
            pilot_swap_lock=threading.RLock(), bind_pilot_services=lambda p: None,
            build_conversational_pilot=lambda **kw: ConversationalSession(kw['config']),
            sync_pilot_session_id=lambda: setattr(box.pilot,'harness_session_id',reg.active_view_id),
            sessions_state_dir=lambda: str(tmp_path), diag=lambda *_a, **_k: None,
            apply_model_context_window=lambda: None, freeze_pilot_meters_into_boot_carry=lambda p: None,
            runner_config_snapshot=lambda: HarnessConfig(state_dir=str(tmp_path)))
        def attach(sid):
            attach_view(sid, svc, defer_cold_build=deferred)
            return ensure_active_pilot_ready(svc, timeout=5)
        return attach
    attach = boot()
    first = attach(a)
    first.enqueue_prompt('only A', source='goal_mode')
    assert attach(b).list_prompts() == []
    attach(b).enqueue_prompt('only B')
    assert attach(fork).list_prompts() == []
    attach(fork).enqueue_prompt('only fork')
    assert attach(a) is first
    assert [x['text'] for x in first.list_prompts()] == ['only A']
    cold = boot()
    for sid, text in ((a, 'only A'), (b, 'only B'), (fork, 'only fork')):
        s = cold(sid)
        assert [x['text'] for x in s.held_prompts()] == [text]
        assert s.list_prompts() == []
        assert s._pop_next_prompt() == {}
        assert [x['text'] for x in s.held_prompts()] == [text]


@pytest.mark.parametrize('operation', ['add', 'remove', 'clear', 'reorder', 'pop', 'follow_up', 'interrupt', 'auto'])
@pytest.mark.parametrize('failure', ['replace', 'write'])
def test_write_failure_never_acknowledges_or_publishes(factory, monkeypatch, operation, failure):
    s = factory()
    a = s.enqueue_prompt('keep A')
    b = s.enqueue_prompt('keep B')
    before = s.list_prompts()
    disk = Path(s._prompt_queue_path).read_bytes()
    def fail(*_a, **_k):
        raise PermissionError('injected failure')
    monkeypatch.setattr('os.replace' if failure == 'replace' else 'json.dump', fail)
    if operation == 'pop':
        with pytest.raises(Exception):
            s._pop_next_prompt()
    else:
        svc = services(s)
        if operation == 'reorder':
            code, body = post_session_queue_reorder({'ids': [b['id'], a['id']]}, svc)
        elif operation in ('follow_up', 'interrupt', 'auto'):
            s._busy.acquire()
            code, body = post_session_steer({'text': 'draft', 'delivery_mode': operation}, svc)
        else:
            request = {'add': {'text': 'draft'}, 'remove': {'id': a['id']}, 'clear': {'clear': True}}[operation]
            code, body = post_session_queue(request, svc)
        assert code >= 400 and body['ok'] is False
        assert body['code'] == ('queue_write_failed' if operation == 'reorder' else 'input_commit_uncertain')
    assert s.list_prompts() == before
    assert Path(s._prompt_queue_path).read_bytes() == disk


def test_concurrent_snapshot_write_publish(factory, monkeypatch):
    s = factory()
    s.enqueue_prompt('base')
    entered, release = threading.Event(), threading.Event()
    original = os.replace
    def barrier(src, dst):
        if threading.current_thread().name.startswith('writer'):
            entered.set()
            assert release.wait(5)
        return original(src, dst)
    monkeypatch.setattr(os, 'replace', barrier)
    with ThreadPoolExecutor(1, thread_name_prefix='writer') as one, ThreadPoolExecutor(1) as two:
        first = one.submit(s.enqueue_prompt, 'first')
        assert entered.wait(5)
        # No uncommitted snapshot may have been published.
        assert [x['text'] for x in s._prompt_queue] == ['base']
        second = two.submit(s.enqueue_prompt, 'second')
        try:
            assert not second.done()
        finally:
            release.set()
        first.result(5)
        second.result(5)
    assert [x['text'] for x in factory().held_prompts()] == ['base', 'first', 'second']


def test_legacy_is_read_only_and_corrupt_owned_file_is_held(factory, tmp_path):
    legacy = tmp_path / 'prompt_queue.json'
    raw = '{"queue": [{"id":"old", "text":"legacy secret draft"}]}'
    legacy.write_text(raw)
    s = factory()
    assert s.list_prompts() == []
    assert s.prompt_queue_recovery()[0]['content'] == raw
    s.enqueue_prompt('new')
    assert legacy.read_text() == raw
    path = Path(s._prompt_queue_path)
    path.write_text('{broken')
    restarted = factory()
    with pytest.raises(Exception):
        restarted.enqueue_prompt('must not overwrite')
    assert path.read_text() == '{broken'
    assert any(r['content'] == '{broken' for r in restarted.prompt_queue_recovery())


def test_cli_without_id_is_isolated(factory):
    a = factory('')
    a.enqueue_prompt('invocation A')
    assert factory('').list_prompts() == []


def test_failed_start_mode_has_no_admitted_action(factory, monkeypatch):
    s = factory()
    before = s._session_actions.snapshot()
    def fail(*_a, **_k):
        raise PermissionError('cannot replace')
    monkeypatch.setattr(os, 'replace', fail)
    code, body = post_session_steer({'text': 'draft', 'turn_input_mode': 'start_if_idle'}, services(s))
    assert code == 503 and body['code'] == 'input_commit_uncertain'
    assert s._session_actions.snapshot() == before
    assert s.list_prompts() == []


def test_failed_native_image_queue_does_not_fall_back_to_steer(factory, monkeypatch):
    s = factory()
    monkeypatch.setattr('harness.vision.session_supports_native_images', lambda _s: True)
    (Path(s.state_dir) / 'image.png').write_bytes(b'original image bytes')
    def fail(*_a, **_k):
        raise PermissionError('cannot replace')
    monkeypatch.setattr(os, 'replace', fail)
    code, body = post_session_steer({'text': 'draft', 'images': [str(Path(s.state_dir) / 'image.png')]}, services(s))
    assert code == 503 and body['code'] == 'input_commit_uncertain'
    assert s.list_prompts() == []
    assert s.drain_steer() == []


@pytest.mark.parametrize('action', ['add', 'remove', 'clear', 'reorder'])
def test_mutation_rejects_foreign_renderer_session(factory, action):
    s = factory()
    item = s.enqueue_prompt('belongs to A')
    body = {'add': {'text': 'draft'}, 'remove': {'id': item['id']}, 'clear': {'clear': True}, 'reorder': {'ids': []}}[action]
    handler = post_session_queue_reorder if action == 'reorder' else post_session_queue
    code, result = handler({**body, 'session_id': 'B'}, services(s))
    assert code == 409 and result['code'] == 'session_changed'
    assert s.list_prompts() == [item]


@pytest.mark.parametrize('second_op', ['remove', 'clear', 'pop', 'reorder'])
def test_two_live_objects_share_owner_transaction(factory, monkeypatch, second_op):
    a, b = factory(), factory()
    first_item = a.enqueue_prompt('base')
    b.list_prompts()
    entered, release = threading.Event(), threading.Event()
    original = os.replace
    def barrier(src, dst):
        if threading.current_thread().name.startswith('writer'):
            entered.set()
            assert release.wait(5)
        return original(src, dst)
    monkeypatch.setattr(os, 'replace', barrier)
    calls = {'remove': lambda: b.remove_prompt(first_item['id']), 'clear': b.clear_prompts,
             'pop': b._pop_next_prompt, 'reorder': lambda: b.reorder_prompts([])}
    with ThreadPoolExecutor(1, thread_name_prefix='writer') as one, ThreadPoolExecutor(1) as two:
        write = one.submit(a.enqueue_prompt, 'added')
        assert entered.wait(5)
        mutation = two.submit(calls[second_op])
        try:
            assert not mutation.done()
            assert [x['text'] for x in a._prompt_queue] == ['base']
        finally:
            release.set()
        write.result(5)
        mutation.result(5)
    expected = {'remove': ['added'], 'clear': [], 'pop': ['base', 'added'], 'reorder': ['base', 'added']}[second_op]
    assert [x['text'] for x in a.list_prompts()] == expected
    assert b.list_prompts() == []
    assert [x['text'] for x in b.held_prompts()] == expected
    assert [x['text'] for x in factory().held_prompts()] == expected


def test_separate_processes_cannot_overwrite_a_newer_snapshot(factory, tmp_path):
    import subprocess
    import sys
    import time
    code = '''
import os, sys, threading, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from harness.prompt_queue import PromptQueueMixin
class Queue(PromptQueueMixin):
    pass
q = Queue()
q.state_dir = sys.argv[2]
q._prompt_queue = []
q._prompt_queue_lock = threading.RLock()
q.bind_prompt_queue(q.state_dir, 'A')
root = Path(q.state_dir)
role = sys.argv[3]
if role == 'first':
    original = os.replace
    def blocked_replace(src, dst):
        (root / 'first-entered').touch()
        deadline = time.monotonic() + 10
        while not (root / 'release').exists():
            if time.monotonic() > deadline:
                raise TimeoutError('release barrier timed out')
            time.sleep(0.01)
        return original(src, dst)
    os.replace = blocked_replace
(root / (role + '-attempting')).touch()
q.enqueue_prompt(role)
(root / (role + '-done')).touch()
'''
    def wait_file(name):
        deadline = time.monotonic() + 10
        while not (tmp_path / name).exists():
            assert time.monotonic() < deadline, name
            time.sleep(0.01)
    root = str(Path(__file__).resolve().parents[1])
    env = {k: v for k, v in os.environ.items() if k in ('PATH', 'TMPDIR', 'SYSTEMROOT')}
    first = subprocess.Popen([sys.executable, '-I', '-c', code, root, str(tmp_path), 'first'], env=env)
    second = None
    try:
        wait_file('first-entered')
        second = subprocess.Popen([sys.executable, '-I', '-c', code, root, str(tmp_path), 'second'], env=env)
        wait_file('second-attempting')
        with pytest.raises(subprocess.TimeoutExpired):
            second.wait(timeout=0.15)
        assert not (tmp_path / 'second-done').exists()
    finally:
        (tmp_path / 'release').touch()
        assert first.wait(timeout=10) == 0
        if second is not None:
            assert second.wait(timeout=10) == 0
    assert [x['text'] for x in factory().held_prompts()] == ['first', 'second']


def test_gui_poll_before_session_id_does_not_bind_cli_namespace(factory):
    from harness.api.session_control import get_session_queue
    s = factory('')
    code, body = get_session_queue(None, services(s))
    assert code == 409 and body['code'] == 'queue_session_unbound'
    assert getattr(s, '_prompt_queue_owner', None) is None
    s.harness_session_id = 'A'
    s.bind_prompt_queue(s.state_dir, 'A')
    assert s.enqueue_prompt('after attach')['text'] == 'after attach'


def test_reorder_waits_for_real_session_readiness(factory):
    s = factory()
    item = s.enqueue_prompt('keep')
    svc = services(s)
    svc.gate_active_pilot_ready = lambda: {'ok': False, 'code': 'pilot_not_ready'}
    code, body = post_session_queue_reorder({'ids': []}, svc)
    assert code == 409 and body['code'] == 'pilot_not_ready'
    assert s.list_prompts() == [item]
