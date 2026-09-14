"""A captured, replaced runner cannot admit new input into the shared live store."""
from types import SimpleNamespace
import threading

import pytest

from harness.api import session_control
from harness.api.session_control import SessionControlServices
from harness.api.streams import _admit_stream_input
from harness.input_receipts import InputReceiptError
from tests.test_input_receipts_integration import session


def test_retired_stream_capture_cannot_create_receipt(session):
    session._replacement_retired = True
    with pytest.raises(InputReceiptError):
        _admit_stream_input(session, 'stale capture', [], str(session.state_dir))
    assert session.input_receipts() == []


@pytest.mark.parametrize('route', ['post_session_queue', 'post_session_steer'])
def test_replacement_during_validation_cannot_admit_on_old_capture(session, monkeypatch, route):
    box = SimpleNamespace(pilot=session)
    replacement = SimpleNamespace(harness_session_id=session.harness_session_id)
    original = session_control._validate_upload_images

    def validate(*args, **kwargs):
        result = original(*args, **kwargs)
        session._replacement_retired = True
        box.pilot = replacement
        return result

    monkeypatch.setattr(session_control, '_validate_upload_images', validate)
    svc = SessionControlServices(
        cfg=session.config, get_pilot=lambda: box.pilot,
        get_runners=lambda: {session.harness_session_id: box.pilot},
        gate_active_pilot_ready=lambda: None, stash_put=lambda *_: '',
        save_active_transcript=lambda: None, upload_dir=str(session.state_dir),
        diag=lambda *_args, **_kwargs: None, pilot_swap_lock=threading.RLock(),
        get_sessions=lambda: SimpleNamespace(active=session.harness_session_id),
    )
    status, payload = getattr(session_control, route)(
        {'text': 'stale capture', 'session_id': session.harness_session_id}, svc)
    assert status == 409, payload
    assert payload['code'] == 'input_session_changed'
    assert session.input_receipts() == []

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from dataclasses import replace

from harness.pilot_replacement import LivePilotReplacement, prepare_replacement
from harness.input_receipts import session_input_store
from tests.test_input_receipts_integration import services


@pytest.mark.parametrize('route,body', [
    ('post_session_queue', {'text': 'background'}),
    ('post_session_steer', {'text': 'background'}),
    ('post_session_queue_reorder', {'ids': []}),
])
def test_explicit_queue_owner_without_active_view(session, route, body):
    svc = services(session)
    svc.get_pilot = lambda: None
    status, _ = getattr(session_control, route)({**body, 'session_id': session.harness_session_id}, svc)
    assert status == 200


def owned_services(session):
    svc = services(session)
    box = SimpleNamespace(pilot=session)
    svc.get_pilot = lambda: box.pilot
    svc.get_runners = lambda: {session.harness_session_id: box.pilot}
    svc.get_sessions = lambda: SimpleNamespace(active=box.pilot.harness_session_id)
    svc.pilot_swap_lock = threading.RLock()
    return svc, box


@pytest.mark.parametrize('route', ['post_session_queue', 'post_session_steer'])
@pytest.mark.parametrize('text', ['', ' \n\t ', 123])
def test_empty_or_invalid_text_without_attachments_has_no_receipt(session, route, text):
    svc, _ = owned_services(session)
    status, _ = getattr(session_control, route)({'text': text}, svc)
    assert status == 400
    assert session.input_receipts() == []
    assert session.list_prompts() == []


@pytest.mark.parametrize('route', ['post_session_queue', 'post_session_steer'])
def test_real_replacement_refuses_validation_reservation(session, monkeypatch, route):
    svc, box = owned_services(session)
    entered, release = threading.Event(), threading.Event()
    validate = session_control._validate_upload_images
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return validate(*args, **kwargs)
    monkeypatch.setattr(session_control, '_validate_upload_images', blocked)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(getattr(session_control, route), {'text': 'retained'}, svc)
        assert entered.wait(5)
        try:
            with svc.pilot_swap_lock, pytest.raises(RuntimeError, match='admission'):
                with LivePilotReplacement(session):
                    pytest.fail('replacement entered during input validation')
            assert session.input_receipts() == []
        finally:
            release.set()
        assert future.result(5)[0] == 200
    new = type(session)(session.config)
    with svc.pilot_swap_lock, LivePilotReplacement(session) as replacement:
        prepare_replacement(session, new, session.harness_session_id, session.state_dir,
                            session._history, actions_snapshot=replacement.actions_snapshot)
        box.pilot = new
        replacement.commit()
    rows = new.input_receipts()
    assert [r['original_text'] for r in rows] == ['retained']
    projections = new.list_prompts() if route.endswith('queue') else list(new._session_actions)
    assert len(projections) == 1
    assert (projections[0]['id'] if isinstance(projections[0], dict) else projections[0].id) == rows[0]['id']
    calls = []
    session.pilot = SimpleNamespace(chat=lambda *_a, **_k: calls.append('provider'))
    list(session.send('stale'))
    assert calls == []


@pytest.mark.parametrize('ending', ['stop', 'switch', 'continue'])
def test_slow_vision_does_not_hold_lifecycle_and_revalidates(session, monkeypatch, ending):
    svc, box = owned_services(session)
    image = Path(svc.upload_dir) / 'image.png'
    image.write_bytes(b'original image bytes')
    entered, release = threading.Event(), threading.Event()
    calls = []
    monkeypatch.setattr('harness.vision.session_supports_native_images', lambda *_: False)
    def transcribe(paths):
        calls.append('vision')
        entered.set()
        assert release.wait(5)
        return [SimpleNamespace(text='converted', error=None)]
    monkeypatch.setattr('harness.vision.transcribe_images', transcribe)
    other = type(session)(replace(session.config, state_dir=str(Path(session.state_dir) / 'other')))
    other.harness_session_id = 'other'
    other.bind_prompt_queue(other.state_dir, 'other')
    Path(other.state_dir).mkdir(parents=True, exist_ok=True)
    other_svc = services(other)
    other_svc.pilot_swap_lock = svc.pilot_swap_lock
    with ThreadPoolExecutor() as pool:
        future = pool.submit(session_control.post_session_steer, {'images': [str(image)]}, svc)
        assert entered.wait(5)
        try:
            with svc.pilot_swap_lock, pytest.raises(RuntimeError, match='admission'):
                with LivePilotReplacement(session):
                    pytest.fail('replacement entered during vision')
            assert pool.submit(session_control.post_session_queue, {'text': 'unrelated'}, other_svc).result(2)[0] == 200
            if ending == 'stop':
                pool.submit(session.interrupt).result(2)
            elif ending == 'switch':
                with svc.pilot_swap_lock:
                    box.pilot = other
        finally:
            release.set()
        code, payload = future.result(5)
    assert calls == ['vision']
    rows = session.input_receipts()
    assert len(rows) == 1
    assert session_input_store(session).attachment(rows[0]['attachments'][0]['ref']) == b'original image bytes'
    if ending == 'continue':
        assert code == 200, payload
        assert [a.id for a in session._session_actions] == [rows[0]['id']]
    else:
        assert code >= 400, payload
        assert list(session._session_actions) == []
        assert session.list_prompts() == []
        assert len(other.input_receipts()) == 1


@pytest.mark.parametrize('route,mode', [
    ('post_session_queue', None), ('post_session_queue', 'follow_up'),
    ('post_session_queue', 'interrupt'), ('post_session_queue', 'steer'),
    ('post_session_steer', None), ('post_session_steer', 'follow_up'),
    ('post_session_steer', 'interrupt'), ('post_session_steer', 'steer'),
])
@pytest.mark.parametrize('kind', ['documents', 'images'])
def test_attachment_only_api_has_retained_bytes_and_projection(session, monkeypatch, route, mode, kind):
    svc = services(session)
    path = Path(svc.upload_dir) / 'attachment.txt'
    content = b'  exact attachment bytes\n'
    path.write_bytes(content)
    monkeypatch.setattr('harness.vision.session_supports_native_images', lambda *_: True)
    body = {kind: [{'path': str(path)}] if kind == 'documents' else [str(path)]}
    if mode:
        body['delivery_mode'] = mode
    code, payload = getattr(session_control, route)(body, svc)
    assert code == 200, payload
    row, = session.input_receipts()
    assert row['original_text'] == ''
    assert session_input_store(session).attachment(row['attachments'][0]['ref']) == content
    ids = {r['id'] for r in session.list_prompts()} | {a.id for a in session._session_actions}
    assert row['id'] in ids


def test_retained_attachment_queue_lookup_precedes_empty_check(session):
    store = session_input_store(session)
    path = Path(session.state_dir) / 'a.txt'
    path.write_text('document body')
    row = store.admit('', documents=[{'path': str(path)}], upload_root=session.state_dir)
    item = session.enqueue_prompt('', input_id=row['id'])
    assert item['input_id'] == row['id']
    assert [r['id'] for r in session.list_prompts()] == [row['id']]
    with pytest.raises(InputReceiptError):
        session.enqueue_prompt('', input_id='missing')


def test_pending_and_retired_direct_projections_cannot_admit(session):
    for flag in ('_replacement_pending', '_replacement_retired'):
        setattr(session, flag, True)
        for operation in (lambda: session.enqueue_prompt('stale'), lambda: session.enqueue_steer('stale'),
                          lambda: session.steer_with_images('stale', ['missing.png'])):
            with pytest.raises(InputReceiptError):
                operation()
        setattr(session, flag, False)
    assert session.input_receipts() == []
    assert list(session._session_actions) == []


@pytest.mark.parametrize('route,mode', [
    ('queue', None), ('queue', 'follow_up'), ('queue', 'interrupt'), ('queue', 'steer'),
    ('steer', None), ('steer', 'follow_up'), ('steer', 'interrupt'), ('steer', 'steer'),
])
@pytest.mark.parametrize('kind', ['documents', 'images'])
def test_attachment_only_real_http_handler(session, monkeypatch, owned_server, route, mode, kind):
    """Exercise HTTP parsing/auth/routing over a socketpair without binding a port."""
    import socket
    import json
    svc = services(session)
    monkeypatch.setattr(owned_server, '_session_control_services', lambda: svc)
    monkeypatch.setattr(owned_server, '_POST_JSON_ROUTES', None)
    monkeypatch.setattr('harness.vision.session_supports_native_images', lambda *_: True)
    path = Path(svc.upload_dir) / 'attachment.txt'
    content = b'exact HTTP attachment\n'
    path.write_bytes(content)
    body = {kind: [{'path': str(path)}] if kind == 'documents' else [str(path)]}
    if mode:
        body['delivery_mode'] = mode
    data = json.dumps(body).encode()
    client, server = socket.socketpair()
    client.settimeout(5)
    request = (f'POST /api/session/{route} HTTP/1.1\r\nHost: localhost\r\n'
               f'X-Harness-Token: {owned_server._TOKEN}\r\nContent-Type: application/json\r\n'
               f'Content-Length: {len(data)}\r\nConnection: close\r\n\r\n').encode() + data
    def serve():
        with server:
            owned_server.Handler(server, ('127.0.0.1', 1), SimpleNamespace(server_name='localhost', server_port=0))
    try:
        with ThreadPoolExecutor() as pool:
            future = pool.submit(serve)
            client.sendall(request)
            chunks = []
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            future.result(5)
    finally:
        client.close()
    headers, raw = b''.join(chunks).split(b'\r\n\r\n', 1)
    assert b' 200 ' in headers.split(b'\r\n', 1)[0], raw
    payload = json.loads(raw)
    assert payload['ok'], payload
    row, = session.input_receipts()
    assert row['original_text'] == ''
    assert session_input_store(session).attachment(row['attachments'][0]['ref']) == content
    ids = {r['id'] for r in session.list_prompts()} | {a.id for a in session._session_actions}
    assert row['id'] in ids


def test_registry_removal_and_delete_refuse_inflight_admission(session, monkeypatch):
    from harness.pilot_replacement import input_admission
    from harness.session_runners import SessionRunnerRegistry
    from harness.api.sessions import handle_session_delete
    registry = SessionRunnerRegistry()
    registry.get_or_create(session.harness_session_id, lambda: session)
    deleted = []
    svc = SimpleNamespace(runners=registry, pilot_swap_lock=threading.RLock(),
                          sessions=SimpleNamespace(active=session.harness_session_id,
                                                   delete=lambda sid: deleted.append(sid)))
    with input_admission(session, svc.pilot_swap_lock):
        assert handle_session_delete(session.harness_session_id, svc)[0] == 409
        with pytest.raises(RuntimeError, match='admission'):
            registry.drop(session.harness_session_id)
        assert not deleted
    registry.drop(session.harness_session_id)
    with pytest.raises(InputReceiptError):
        session.enqueue_prompt('late after deletion')
    assert session.input_receipts() == []


def test_stream_capture_barrier_rejects_registry_replacement_before_receipt(session, monkeypatch):
    from harness.api.streams import _admit_owned_stream_input
    svc, box = owned_services(session)
    svc.sessions = SimpleNamespace(active=session.harness_session_id)
    entered, release = threading.Event(), threading.Event()
    def request():
        captured = box.pilot
        entered.set()
        assert release.wait(5)
        return _admit_owned_stream_input(svc, captured, session.harness_session_id,
                                         'stale stream', [], svc.upload_dir)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(request)
        assert entered.wait(5)
        try:
            with svc.pilot_swap_lock, LivePilotReplacement(session) as replacement:
                box.pilot = SimpleNamespace(harness_session_id=session.harness_session_id)
                replacement.commit()
        finally:
            release.set()
        with pytest.raises(InputReceiptError) as exc:
            future.result(5)
        assert exc.value.code == 'input_session_changed'
    assert session.input_receipts() == []


def test_stop_during_validation_cannot_create_late_receipt(session, monkeypatch):
    svc, _ = owned_services(session)
    entered, release = threading.Event(), threading.Event()
    original = session_control._validate_upload_images
    def validate(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(session_control, '_validate_upload_images', validate)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(session_control.post_session_steer, {'text': 'cancel this pending input'}, svc)
        assert entered.wait(5)
        try:
            pool.submit(session.interrupt).result(2)
        finally:
            release.set()
        code, result = future.result(5)
    assert code >= 400 and result['code'] == 'input_stopped'
    assert session.input_receipts() == []
    assert list(session._session_actions) == []


@pytest.mark.parametrize('route', ['post_session_queue', 'post_session_steer'])
@pytest.mark.parametrize('kind', ['documents', 'images'])
def test_busy_interrupt_preserves_attachment_followup(session, monkeypatch, route, kind):
    svc = services(session)
    path = Path(svc.upload_dir) / 'attachment.txt'
    path.write_bytes(b'preserve across interrupt')
    monkeypatch.setattr('harness.vision.session_supports_native_images', lambda *_: True)
    body = {'delivery_mode': 'interrupt',
            kind: [{'path': str(path)}] if kind == 'documents' else [str(path)]}
    session._busy.acquire()
    try:
        code, payload = getattr(session_control, route)(body, svc)
        assert code == 200, payload
        row, = session.input_receipts()
        assert row['status'] == 'accepted'
        assert [r['id'] for r in session.list_prompts()] == [row['id']]
        assert session_input_store(session).attachment(row['attachments'][0]['ref']) == b'preserve across interrupt'
    finally:
        session._busy.release()


@pytest.mark.parametrize('mode', ['auto', 'steer', 'follow_up', 'interrupt'])
def test_delivery_mode_rejects_unknown_retained_identity(session, mode):
    from harness.delivery_mode import apply_delivery
    result = apply_delivery(session, '', session_busy=False, requested=mode, input_id='missing')
    assert result['ok'] is False
    assert session.input_receipts() == []
    assert session.list_prompts() == []
    assert list(session._session_actions) == []


def test_late_stop_and_checkpoint_cannot_recreate_deleted_owner(session, monkeypatch, owned_server):
    from harness.api.sessions import handle_session_delete
    from harness.session_runners import SessionRunnerRegistry
    registry = SessionRunnerRegistry()
    sid = session.harness_session_id
    registry.get_or_create(sid, lambda: session)
    writes = []
    svc = SimpleNamespace(runners=registry, pilot_swap_lock=threading.RLock(),
        sessions=SimpleNamespace(active=sid, delete=lambda _: None),
        sessions_state_dir=lambda: session.state_dir, diag=lambda *_: None,
        clear_active_pilot=lambda: None)
    monkeypatch.setattr('harness.hooks.run_hooks', lambda *_: None)
    monkeypatch.setattr('harness.api.sessions.remove_session_transcript', lambda *_a, **_k: None)
    assert handle_session_delete(sid, svc)[0] == 200
    monkeypatch.setattr(owned_server, '_runners', registry)
    monkeypatch.setattr(owned_server, 'save_transcript', lambda *_a, **_k: writes.append(True))
    owned_server._persist_turn_transcript({'session_id': sid, 'pilot': session})
    with pytest.raises(InputReceiptError):
        session.retire_input_receipts_after_stop()
    assert not writes
    assert session.input_receipts() == []
