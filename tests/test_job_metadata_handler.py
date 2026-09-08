"""Actual authenticated Handler dispatch, with optional real loopback transport.

PM_METADATA_REQUIRE_HTTP=1 requires sockets (parent replay); default executes the
same raw HTTP bytes through BaseHTTPRequestHandler with an in-memory socket.
"""
import http.client
import io
import json
import os
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from harness.job_metadata_capability import bounded_metadata_available

if not bounded_metadata_available():
    pytest.skip("public PM lacks bounded metadata APIs", allow_module_level=True)

from harness.config import HarnessConfig
from harness.session import Session
from harness.session_runners import SessionRunnerRegistry
from puppetmaster.models import Task, Artifact, ArtifactType
from test_job_readmodel import assert_bounded_page, hashes, trace_metadata


class MemorySocket:
    def __init__(self, request):
        self.input = io.BytesIO(request)
        self.output = bytearray()

    def makefile(self, *args):
        return self.input

    def sendall(self, data):
        self.output.extend(data)


@pytest.fixture
def host(tmp_path, monkeypatch):
    import harness.server as server
    monkeypatch.setenv('PUPPETMASTER_STATE_DIR', str(tmp_path / 'absent-cli'))
    monkeypatch.setenv('HARNESS_CLI_CROSS_PROJECT', '0')
    reg = SessionRunnerRegistry()
    session = Session(HarnessConfig(driver='stub-oracle-v2', repo=str(tmp_path),
                                   state_dir=str(tmp_path / 'store'), swarm_adapter='demo'))
    store = session.state().store
    jobs = [store.create_job('fixture', origin='marionette', session_id='A') for _ in range(55)]
    expected = dict(tasks=set(), artifacts=set())
    for i in range(55):
        task = Task(jobs[0].id, 'fixture', 'body')
        store.save_task(task)
        artifact = Artifact(jobs[0].id, task.id, ArtifactType.FINDING, 'fixture', {'claim': 'body'}, 1.0, ['fixture'])
        store.save_artifact(artifact)
        expected['tasks'].add(task.id)
        expected['artifacts'].add(artifact.id)
    reg.get_or_create('A', lambda: session)
    reg.set_active_view('A')
    monkeypatch.setattr(server, '_runners', reg)
    monkeypatch.setattr(server, '_session', session)
    monkeypatch.setattr(server, '_GET_ROUTES', None)
    monkeypatch.setattr(server, '_POST_JSON_ROUTES', None)
    httpd = None
    if os.environ.get('PM_METADATA_REQUIRE_HTTP') == '1':
        # conftest permits loopback sockets. Binding can still be sandbox-denied.
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()

    def request(method, path, body=None, *, token=True, host='127.0.0.1', headers=None):
        encoded = b'' if body is None else body if isinstance(body, bytes) else json.dumps(body).encode()
        fields = [('Host', host), ('Content-Length', str(len(encoded)))]
        if token:
            fields.append(('X-Harness-Token', server._TOKEN))
        fields.extend(headers or [])
        if httpd:
            conn = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=5)
            conn.putrequest(method, path, skip_host=True)
            for key, value in fields:
                conn.putheader(key, value)
            conn.endheaders(encoded)
            response = conn.getresponse()
            status, data = response.status, response.read()
            conn.close()
        else:
            raw = f'{method} {path} HTTP/1.0\r\n' + ''.join(f'{k}: {v}\r\n' for k, v in fields) + '\r\n'
            sock = MemorySocket(raw.encode() + encoded)
            server.Handler(sock, ('127.0.0.1', 12345), SimpleNamespace(server_name='localhost', server_port=80))
            head, data = bytes(sock.output).split(b'\r\n\r\n', 1)
            status = int(head.split(b' ')[1])
        return status, json.loads(data), len(data)

    yield SimpleNamespace(server=server, reg=reg, session=session, store=store, jobs=jobs, expected=expected, request=request)
    if httpd:
        httpd.shutdown()
        httpd.server_close()
        thread.join(5)


def context(host):
    status, payload, _ = host.request('GET', '/api/jobs/metadata/view')
    assert status == 200
    status, payload, _ = host.request('POST', '/api/jobs/metadata/view/refresh',
                                    {'view_generation': payload['context']['view_generation']})
    assert status == 200
    selection = next(s for s in payload['sources'] if s['source'] == 'harness')
    return dict(payload['context'], scope='session', source=selection['source'], state_id=selection['state_id'])


def path(ctx, suffix='', **kw):
    return '/api/jobs/metadata' + suffix + '?' + urlencode(dict(ctx, **kw))


def test_actual_auth_host_parser_body_limits(host):
    request = host.request
    for method, url, body in [('GET', '/api/jobs/metadata/view', None),
                              ('GET', '/api/jobs/metadata', None),
                              ('GET', '/api/jobs/metadata/detail', None),
                              ('GET', '/api/jobs/metadata/local', None),
                              ('POST', '/api/jobs/metadata/pins', {}),
                              ('POST', '/api/jobs/metadata/view/refresh', {})]:
        assert request(method, url, body, token=False)[0] == 403
        assert request(method, url, body, host='rebound.invalid')[0] == 403
    assert request('GET', '/api/jobs/metadata/view', token=False, headers=[('X-Harness-Token', 'wrong')])[0] == 403
    assert request('GET', '/api/jobs/metadata/view?unknown=')[0] == 400
    assert request('GET', '/api/jobs/metadata/view', headers=[('Origin', 'https://evil.invalid')])[0] == 403
    ctx = context(host)
    good = path(ctx, mode='snapshot')
    for suffix in ('&scope=', '&scope=repo', '&scope=session', '&unknown=', '&cursor=', '&limit=99'):
        assert request('GET', good + suffix)[0] == 400
    assert request('GET', path(dict(ctx, scope='wrong'), mode='snapshot'))[0] == 400
    assert request('POST', '/api/jobs/metadata/pins', b' ' * 65537)[0] == 413
    assert request('POST', '/api/jobs/metadata/pins', b'{')[0] == 400
    assert request('POST', '/api/jobs/metadata/pins?scope=', {})[0] == 400
    assert request('POST', '/api/jobs/metadata/pins', {}, headers=[('Content-Length', '-1')])[0] == 400
    assert request('POST', '/api/jobs/metadata/pins', {}, headers=[('Transfer-Encoding', 'chunked')])[0] == 400


def test_actual_snapshot_continuation_detail_pins_and_aba(host):
    ctx = context(host)
    request = host.request
    status, first, size = request('GET', path(ctx, mode='snapshot'))
    assert status == 200 and size <= 65536
    assert first['page']['outcome'] == 'partial'
    cursor = first['page']['next_cursor']
    current = first
    ids = []
    tokens = set()
    while True:
        assert_bounded_page(current)
        assert current['page']['revision'] == first['page']['revision']
        ids.extend(r['selection']['job_ref']['job_id'] for r in current['rows'])
        assert len(ids) == len(set(ids))
        token = current['page']['next_cursor']
        if token is None:
            break
        assert token not in tokens
        tokens.add(token)
        status, current, size = request('GET', path(ctx, mode='snapshot', cursor=token))
        assert status == 200 and size <= 65536
    assert set(ids) == {j.id for j in host.jobs}
    selected = host.store.job_ref(host.jobs[0].id).as_dict()
    status, detail, size = request('GET', path(ctx, '/detail', **selected))
    assert status == 200 and size <= 98304
    assert detail['cancellation_authority'] is False
    for lane, parameter in (('tasks', 'task_cursor'), ('artifacts', 'artifact_cursor')):
        current = detail
        ids = []
        tokens = set()
        assert current[lane]['page']['outcome'] == 'partial'
        while True:
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
            status, current, size = request('GET', path(ctx, '/detail', **selected, **{parameter: token}))
            assert status == 200 and size <= 98304 and current['cancellation_authority'] is False
        assert set(ids) == host.expected[lane]
    pin_context = {k: ctx[k] for k in ('session_id', 'repo', 'view_generation', 'scope')}
    status, pins, size = request('POST', '/api/jobs/metadata/pins', dict(pin_context, selections=[first['rows'][0]['selection']]))
    assert status == 200 and size <= 65536 and pins['results'][0]['result']['kind'] == 'present'
    assert request('GET', path(pin_context, '/local'))[1]['page']['outcome'] == 'unavailable'
    host.reg.set_active_view('B', repo=ctx['repo'])
    host.reg.set_active_view('A')
    assert request('GET', path(ctx, mode='snapshot', cursor=cursor))[0] == 409
    assert request('GET', path(ctx, '/detail', job_id=host.jobs[0].id))[0] == 409
    assert request('POST', '/api/jobs/metadata/view/refresh', {'view_generation': ctx['view_generation']})[0] == 409


def test_actual_sustained_polls_no_body_constructor_discovery(host, monkeypatch):
    ctx = context(host)
    reader = host.reg.metadata_view.reader()
    store = reader.sources.stores[0].handle
    original = store.list_job_summaries
    calls = []
    def summary(**kw):
        calls.append(kw)
        return original(**kw)
    monkeypatch.setattr(store, 'list_job_summaries', summary)
    def forbidden(*a, **kw):
        pytest.fail('metadata poll exceeded its lane')
    monkeypatch.setattr(Session, 'state', forbidden)
    monkeypatch.setattr('harness.session.DurableState', forbidden)
    monkeypatch.setattr('harness.job_readmodel.create_store', forbidden)
    monkeypatch.setattr('harness.job_metadata_view.discover_sources', forbidden)
    from puppetmaster.store import SwarmStore
    for method in ('get_job', 'list_jobs', 'list_tasks', 'list_artifacts'):
        monkeypatch.setattr(SwarmStore, method, forbidden)
    before = hashes(host.store.root)
    selections = [dict(job_ref=host.store.job_ref(j.id).as_dict(), source='harness',
                       session_id='A', repo=ctx['repo']) for j in host.jobs[:8]]
    metrics = trace_metadata(monkeypatch)
    pin_context = {k: ctx[k] for k in ('session_id', 'repo', 'view_generation', 'scope')}
    for _ in range(30):
        assert host.request('GET', '/api/jobs/metadata/view')[0] == 200
        assert host.request('GET', path(ctx, mode='snapshot'))[0] == 200
        status, pinned, size = host.request('POST', '/api/jobs/metadata/pins', dict(pin_context, selections=selections))
        assert status == 200 and size <= 65536
        assert [r['result']['row']['selection'] for r in pinned['results']] == selections
        assert host.reg.metadata_view.reader() is reader
    assert len(calls) == 30 * 9
    assert sum(kw['limit'] == 50 and kw['max_scan'] == 51 and kw['max_bytes'] == 32768 for kw in calls) == 30
    assert sum(kw['limit'] == 1 and kw['max_scan'] == 2 and kw['max_bytes'] == 8192 for kw in calls) == 240
    assert hashes(host.store.root) == before
    assert metrics['reads'] and any(table == 'projection_versions' for table, _ in metrics['reads'])


def test_actual_held_read_and_discovery_cross_switch(host, monkeypatch):
    ctx = context(host)
    store = host.reg.metadata_view.reader().sources.stores[0].handle
    original = store.list_job_summaries
    entered, release = threading.Event(), threading.Event()
    def held(**kw):
        entered.set()
        assert release.wait(5)
        return original(**kw)
    monkeypatch.setattr(store, 'list_job_summaries', held)
    result = []
    t = threading.Thread(target=lambda: result.append(host.request('GET', path(ctx, mode='snapshot'))))
    t.start()
    assert entered.wait(5)
    host.reg.set_active_view('B', repo=ctx['repo'])
    host.reg.set_active_view('A')
    release.set()
    t.join(5)
    assert not t.is_alive() and result[0][0] == 409
    from harness.job_metadata_view import discover_sources
    entered.clear(); release.clear(); result.clear()
    def discover(*a):
        entered.set()
        assert release.wait(5)
        return discover_sources(*a)
    monkeypatch.setattr('harness.job_metadata_view.discover_sources', discover)
    generation = host.reg.metadata_view.capture().generation
    t = threading.Thread(target=lambda: result.append(host.request('POST', '/api/jobs/metadata/view/refresh',
                                                                  {'view_generation': generation})))
    t.start()
    assert entered.wait(5)
    host.reg.detach_view('A')
    release.set()
    t.join(5)
    assert not t.is_alive() and result[0][0] == 409
    current = host.request('GET', '/api/jobs/metadata/view')[1]
    assert current['availability'] == 'unavailable' and current['sources'] == []


def test_authoritative_view_generation_retires_after_session_round_trip(host):
    request = host.request
    original = request('GET', '/api/jobs/metadata/view')[1]['context']['view_generation']
    host.reg.set_active_view('B', repo=host.session.config.repo)
    intermediate = request('GET', '/api/jobs/metadata/view')[1]['context']['view_generation']
    host.reg.set_active_view('A')
    returned = request('GET', '/api/jobs/metadata/view')[1]['context']['view_generation']
    assert len({original, intermediate, returned}) == 3
    assert request('POST', '/api/jobs/metadata/view/refresh', {'view_generation': original})[0] == 409
    assert request('GET', '/api/jobs/metadata/view')[1]['context']['view_generation'] == returned
