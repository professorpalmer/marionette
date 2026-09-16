import io
import json
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from harness.api import sessions
from harness.api.streams import StreamServices
from tests.test_input_receipts_integration import session


@pytest.mark.parametrize('mode', ['chat', 'auto'])
def test_registered_background_stream_uses_owner_repo(session, monkeypatch, tmp_path, mode):
    from harness.api.streams import stream_chat, stream_auto
    from harness.conversation import ConvEvent
    owner_repo = tmp_path / 'owner'
    owner_repo.mkdir()
    session.config.repo = str(owner_repo)
    viewed = SimpleNamespace(harness_session_id='other')
    refreshed, finalized, sent, ensured = [], [], [], []
    def send(text, *args, **kwargs):
        sent.append(text)
        yield ConvEvent('assistant_done', {})
    monkeypatch.setattr(session, 'send', send)
    monkeypatch.setattr(session, 'run_auto', send)
    svc = StreamServices(
        cfg=SimpleNamespace(repo=str(tmp_path / 'other')),
        sessions=SimpleNamespace(active='other', set_title_if_default=lambda *_: None),
        get_pilot=lambda: viewed, get_session=lambda: viewed,
        get_runners=lambda: {session.harness_session_id: session},
        ensure_pilot_matches_driver=lambda: pytest.fail('must not rebuild the viewed pilot'),
        ensure_session_driver=ensured.append,
        maybe_refresh_codegraph=refreshed.append,
        pilot_preflight=lambda: 'unrelated viewed driver is unavailable',
        checkpoint_transcript=lambda *_: None, finalize_turn=finalized.append,
        upload_dir=str(session.state_dir), auto_budget_from_env=lambda: None,
    )
    handler = SimpleNamespace(wfile=io.BytesIO(), send_response=lambda *_: None,
                              send_header=lambda *_: None, _cors=lambda: None,
                              end_headers=lambda: None)
    if mode == 'chat':
        stream_chat(handler, 'owner input', [], svc, session_id=session.harness_session_id)
    else:
        stream_auto(handler, 'owner input', svc, session_id=session.harness_session_id)
    assert sent == ['owner input']
    assert ensured == [session.harness_session_id]
    assert refreshed == [str(owner_repo)]
    assert finalized[0]['pilot'] is session
    assert finalized[0]['config'] is session.config


@pytest.mark.parametrize('mode', ['chat', 'auto'])
@pytest.mark.parametrize('switch_during_ensure', [False, True])
def test_unregistered_request_cannot_admit_into_active_session(session, monkeypatch, mode, switch_during_ensure):
    import harness.server as srv
    from harness.http_routes import build_get_routes

    calls = []

    class Pilot:
        name = 'synthetic'

        def chat(self, *_args, **_kwargs):
            calls.append('provider')
            return SimpleNamespace(text='{"say":"done","actions":[]}', error=None,
                                   tokens_in=0, tokens_out=0, meta={})

    session.pilot = Pilot()
    svc = StreamServices(
        cfg=session.config,
        sessions=SimpleNamespace(active=session.harness_session_id, set_title_if_default=lambda *_: None),
        get_pilot=lambda: session, get_session=lambda: session,
        get_runners=lambda: {},
        ensure_pilot_matches_driver=lambda: calls.append('ensure'),
        maybe_refresh_codegraph=lambda *_: None, pilot_preflight=lambda: None,
        checkpoint_transcript=lambda *_: None, finalize_turn=lambda *_: None,
        upload_dir=str(session.state_dir), auto_budget_from_env=lambda: None,
    )
    def ensure():
        calls.append('ensure')
        svc.sessions.active = 'different-session'
    if switch_during_ensure:
        svc.ensure_pilot_matches_driver = ensure
    monkeypatch.setattr(srv, '_stream_services', lambda: svc)

    class Handler:
        _stream_chat = srv.Handler._stream_chat
        _stream_auto = srv.Handler._stream_auto

        def __init__(self):
            self.wfile = io.BytesIO()
            self.status = None
            self.body = None

        def _send(self, status, body):
            self.status = status
            self.body = json.loads(body)

        def send_response(self, status): self.status = status
        def send_header(self, *_args): pass
        def _cors(self): pass
        def end_headers(self): pass

    handler = Handler()
    path = '/api/' + mode
    query = {'message' if mode == 'chat' else 'objective': ['A-only original'],
             'session_id': [session.harness_session_id if switch_during_ensure else 'previous-session-A']}
    build_get_routes(srv._route_services())[path](handler, urlparse(path), query)
    assert handler.status == 409
    assert handler.body['code'] == 'input_session_changed'
    assert calls == []
    assert session.input_receipts() == []


@pytest.mark.parametrize('mode', ['chat', 'auto'])
def test_stashed_submission_carries_all_fields_to_stream(monkeypatch, mode):
    import harness.server as srv
    from harness.http_routes import build_get_routes

    monkeypatch.setattr(sessions, '_CHAT_STASH', {})
    original = '  long original\n' * 1000
    metadata = {'documents': [{'ref': 'input:A:document', 'name': 'reference.txt'}],
                'retry_key': 'stable-key', 'input_id': 'input-id',
                'handoff_token': 'one-use', 'session_id': 'A'}
    mid = sessions.stash_put(original, [], **metadata)
    captured = []

    class Handler:
        def _stream_chat(self, text, images, **fields): captured.append((text, images, fields))
        def _stream_auto(self, text, images, **fields): captured.append((text, images, fields))
        def _send(self, status, body): raise AssertionError((status, body))

    path = '/api/' + mode
    build_get_routes(srv._route_services())[path](Handler(), urlparse(path), {'mid': [mid]})
    assert len(captured) == 1
    text, images, fields = captured[0]
    assert text == original and images == []
    for key, value in metadata.items():
        assert fields[key] == value
    assert sessions.stash_pop(mid) is None


@pytest.mark.parametrize('mode', ['chat', 'auto'])
def test_expired_stash_never_falls_back_to_query(monkeypatch, mode):
    import harness.server as srv
    from harness.http_routes import build_get_routes

    monkeypatch.setattr(sessions, '_CHAT_STASH', {})
    replies = []

    class Handler:
        def _stream_chat(self, *_args, **_kwargs): pytest.fail('expired input dispatched')
        def _stream_auto(self, *_args, **_kwargs): pytest.fail('expired input dispatched')
        def _send(self, status, body): replies.append((status, json.loads(body)))

    path = '/api/' + mode
    query = {'mid': ['expired'], 'message' if mode == 'chat' else 'objective': ['fallback must not run']}
    build_get_routes(srv._route_services())[path](Handler(), urlparse(path), query)
    assert replies[0][0] == 409
    assert replies[0][1]['code'] == 'input_stash_expired'
