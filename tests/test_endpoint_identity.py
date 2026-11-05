"""Endpoint contract exercised through the real HTTP parser and Handler."""
import io
import json
import os
import sqlite3
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from harness.api.endpoint import get_endpoint, validate_request
from harness.api.session_events import SessionEventStore


def request(server, path, headers=None, method='GET'):
    class Connection:
        output = io.BytesIO()
        def makefile(self, mode, *args):
            return io.BytesIO(raw) if mode == 'rb' else self.output
        def sendall(self, data):
            self.output.write(data)
    fields = {'Host': 'localhost', 'X-Harness-Token': server._TOKEN, **(headers or {})}
    raw = (f'{method} {path} HTTP/1.0\r\n' + ''.join(f'{k}: {v}\r\n' for k, v in fields.items()) + '\r\n').encode()
    conn = Connection()
    server.Handler(conn, ('127.0.0.1', 4321), SimpleNamespace())
    head, body = conn.output.getvalue().split(b'\r\n\r\n', 1)
    return int(head.split()[1]), json.loads(body)


@pytest.fixture
def endpoint(monkeypatch, tmp_path):
    from harness import server
    from harness.endpoint_identity import EndpointIdentity
    identity = EndpointIdentity(tmp_path / 'endpoint.json', 'boot-test')
    monkeypatch.setattr(server, '_endpoint_identity', lambda: identity)
    monkeypatch.setattr(server, '_TOKEN', 'temporary-owner')
    monkeypatch.setattr(server.Handler, 'log_message', lambda *a: None)
    return server


def test_future_cursor_is_rejected_before_sampling():
    from harness.api.session_events import read_events_since
    status, body = read_events_since('s', 999, sse_svc=None, session_control_svc=None, store=SessionEventStore())
    assert status == 409
    assert body['code'] == 'future_cursor'
    assert body['events'] == []


def test_persistent_endpoint_distinct_from_boot(tmp_path):
    from harness.endpoint_identity import EndpointIdentity
    path = tmp_path / 'endpoint.json'
    a = EndpointIdentity(path, 'boot-a')
    b = EndpointIdentity(path, 'boot-b')
    assert a.endpoint_id == b.endpoint_id
    assert a.boot_id != b.boot_id
    assert a.endpoint_id not in (a.boot_id, b.boot_id)


def test_handshake_and_owner_compatibility(endpoint):
    for headers in ({}, {'X-Harness-Protocol': '1'}):
        status, body = request(endpoint, '/api/endpoint', headers)
        assert status == 200
        assert body['protocol_version'] == 1
        assert body['device_auth'] == ('bounded_read_v1' if os.name == 'posix' else 'disabled')
        assert body['boot_id'] == 'boot-test'
    assert request(endpoint, '/api/endpoint', {'X-Harness-Token': 'wrong'})[0] == 403


@pytest.mark.parametrize('headers,code', [
    ({'X-Harness-Protocol': '999'}, 'unsupported_protocol'),
    ({'X-Harness-Protocol': ''}, 'unsupported_protocol'),
    ({'X-Harness-Endpoint': 'other'}, 'endpoint_mismatch'),
    ({'X-Harness-Boot': 'old'}, 'boot_mismatch'),
])
def test_negotiation_rejects_identity(endpoint, headers, code):
    identity = endpoint._endpoint_identity()
    fields = {'X-Harness-Protocol': '1', 'X-Harness-Endpoint': identity.endpoint_id, 'X-Harness-Boot': identity.boot_id, **headers}
    status, body = request(endpoint, '/api/session/events?session=s&since=0', fields)
    assert status in (409, 426)
    assert body['code'] == code


@pytest.mark.parametrize('method', ['GET', 'POST', 'DELETE'])
def test_device_cannot_fall_back_to_owner(endpoint, method):
    status, body = request(endpoint, '/api/session/events?session=other', {'X-Harness-Device-Token': 'revoked-or-unknown'}, method)
    assert status == 403
    assert body['code'] == 'device_denied'


def test_versioned_replay_requires_explicit_session_and_stream(endpoint, monkeypatch):
    from harness.api import session_events as events
    from test_session_events_cursor import _sse_svc, _sc_svc
    store = SessionEventStore()
    monkeypatch.setattr(events, '_default_store', store)
    monkeypatch.setattr(endpoint, '_sse_services', lambda: _sse_svc({}))
    monkeypatch.setattr(endpoint, '_session_control_services', lambda: _sc_svc({}))
    identity = endpoint._endpoint_identity()
    headers = {'X-Harness-Protocol': '1', 'X-Harness-Endpoint': identity.endpoint_id, 'X-Harness-Boot': identity.boot_id}
    assert request(endpoint, '/api/session/events', headers)[0] == 400
    status, initial = request(endpoint, '/api/session/events?session=s', headers)
    assert status == 200
    query = urlencode({'session': 's', 'since': initial['cursor'], 'stream_id': initial['stream_id']})
    assert request(endpoint, '/api/session/events?' + query, headers)[0] == 200
    assert request(endpoint, '/api/session/events?session=s&since=999999', headers)[0] == 409
    store.clear_session('s')
    status, body = request(endpoint, '/api/session/events?' + query, headers)
    assert status == 409
    assert body['code'] == 'stream_mismatch'


def test_device_cannot_use_wiki_nonce_bypass(endpoint):
    status, body = request(endpoint, '/api/wiki/connect?nonce=unused', {'X-Harness-Device-Token': ''})
    assert status == 403
    assert body['code'] == 'device_denied'


def test_legacy_stream_token_cannot_bypass_negotiation(endpoint):
    status, _ = request(endpoint, '/api/chat?token=temporary-owner', {
        'X-Harness-Token': '', 'X-Harness-Protocol': '999',
    })
    assert status == 403


def test_stream_identity_does_not_cross_sessions_or_eviction():
    store = SessionEventStore(max_sessions=1)
    store.append('a', 'message', {})
    stream_a = store.since('a')['stream_id']
    store.append('b', 'message', {})
    assert store.since('b', 1, stream_id=stream_a)['code'] == 'stream_mismatch'
    store.append('a', 'message', {})
    assert store.since('a', 1, stream_id=stream_a)['code'] == 'stream_mismatch'


def test_identity_corruption_is_not_silently_rotated(endpoint, tmp_path):
    from harness.api.endpoint import get_endpoint
    from harness.endpoint_identity import EndpointIdentity
    path = tmp_path / 'corrupt.json'
    path.write_text('broken')
    status, body = get_endpoint(lambda: EndpointIdentity(path, 'boot'))
    assert status == 503
    assert body['code'] == 'endpoint_unavailable'
    assert path.read_text() == 'broken'


def test_real_pm_host_record_is_reused(monkeypatch, tmp_path):
    from harness import server
    from puppetmaster.host_lifecycle import record_host_start
    from puppetmaster.store_factory import create_store
    monkeypatch.delenv('PUPPETMASTER_WORKER', raising=False)
    store = create_store('sqlite', str(tmp_path / 'pm'))
    first = record_host_start(store)
    assert first is not None
    monkeypatch.setattr(server, '_session', SimpleNamespace(state=lambda: SimpleNamespace(store=store)))
    monkeypatch.setattr(server, '_state_home', lambda: str(tmp_path / 'app'))
    monkeypatch.setattr(server, '_endpoint_instance', None)
    identity = server._endpoint_identity()
    assert identity.boot_id == first.boot_id
    assert record_host_start(store).boot_id == first.boot_id
    # An active-view replacement cannot change the endpoint's boot identity.
    monkeypatch.setattr(server, '_session', None)
    assert server._endpoint_identity() is identity


@pytest.mark.parametrize('method', ['GET', 'POST', 'DELETE'])
def test_missing_boot_pin_blocks_versioned_dispatch(endpoint, method):
    status, body = request(endpoint, '/api/sessions/other', {
        'X-Harness-Protocol': '1',
        'X-Harness-Endpoint': endpoint._endpoint_identity().endpoint_id,
    }, method)
    assert status == 409
    assert body['code'] == 'boot_mismatch'


def test_concurrent_identity_creation_converges(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from harness.endpoint_identity import EndpointIdentity
    with ThreadPoolExecutor(max_workers=4) as pool:
        identities = list(pool.map(lambda _: EndpointIdentity(tmp_path / 'endpoint.json', 'boot'), range(12)))
    assert len({item.endpoint_id for item in identities}) == 1


@pytest.mark.parametrize('error', [sqlite3.OperationalError('private database detail'), RuntimeError('PM store failed')])
def test_factory_failure_is_structured_unavailable(error):
    def fail():
        raise error
    for status, body in (get_endpoint(fail), validate_request({'X-Harness-Protocol':'1'}, '/api/session/events', fail)):
        assert status == 503
        assert body['code'] == 'endpoint_unavailable'
        assert str(error) not in body['error']


@pytest.mark.parametrize('method,path', [('GET','/api/endpoint'),('GET','/api/session/events'),('POST','/api/session/persist'),('DELETE','/api/sessions/s')])
@pytest.mark.parametrize('error', [sqlite3.OperationalError('private database detail'), RuntimeError('PM store failed')])
def test_http_guard_returns_503_on_factory_failure(endpoint, monkeypatch, method, path, error):
    def fail():
        raise error
    monkeypatch.setattr(endpoint, '_endpoint_identity', fail)
    status, body = request(endpoint, path, {'X-Harness-Protocol':'1'}, method)
    assert status == 503
    assert body['code'] == 'endpoint_unavailable'
    assert str(error) not in body['error']
