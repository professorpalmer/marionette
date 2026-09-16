import json
from pathlib import Path

import pytest

from harness import backend_lifetime as lifetime


def test_receipt_is_exclusive_and_drift_is_reported(tmp_path, monkeypatch):
    env = {'source_sha': 'abc', 'dirty': True, 'source_digest': 'one'}
    monkeypatch.setattr(lifetime, 'environment_descriptor', lambda root: dict(env))
    receipt = tmp_path / 'receipt.json'
    identity = {'endpoint_id': 'endpoint', 'boot_id': 'boot'}
    host = lifetime.BackendLifetime(tmp_path, receipt)
    host.publish(12345, identity, tmp_path / 'token')
    data = json.loads(receipt.read_text())
    assert data['owner'] == 'external'
    assert data['launch_id'] and data['pid'] > 0
    assert host.status()[0] == 200
    with pytest.raises(FileExistsError):
        lifetime.BackendLifetime(tmp_path, receipt).publish(12345, identity, tmp_path / 'token')
    env['source_digest'] = 'two'
    assert host.status()[0] == 409
    assert host.status()[1]['code'] == 'backend_source_drift'
    assert json.loads(receipt.read_text()) == data


def test_cleanup_never_removes_replacement_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(lifetime, 'environment_descriptor', lambda root: {})
    p = tmp_path / 'receipt.json'
    host = lifetime.BackendLifetime(tmp_path, p)
    host.publish(12345, {'endpoint_id': 'e', 'boot_id': 'b'}, tmp_path / 'token')
    p.write_text('{"launch_id":"replacement"}')
    host.close()
    assert p.exists()


def test_source_digest_detects_same_dirty_state_content_change(tmp_path):
    import subprocess
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / 'harness').mkdir()
    source = tmp_path / 'harness' / 'x.py'
    source.write_text('a=1')
    first = lifetime.source_snapshot(tmp_path)
    source.write_text('a=2')
    second = lifetime.source_snapshot(tmp_path)
    assert first['dirty'] == second['dirty'] == True
    assert first['source_digest'] != second['source_digest']

from test_endpoint_identity import endpoint, request


def test_real_handler_checks_auth_boot_and_drift(endpoint, tmp_path, monkeypatch):
    env = {'source_sha': 'test', 'dirty': True, 'source_digest': 'first'}
    monkeypatch.setattr(lifetime, 'environment_descriptor', lambda root: dict(env))
    host = lifetime.BackendLifetime(tmp_path, tmp_path / 'receipt')
    identity = endpoint._endpoint_identity().describe()
    host.publish(12345, identity, tmp_path / 'token')
    monkeypatch.setattr(lifetime, '_active', host)
    headers = {'X-Harness-Protocol':'1', 'X-Harness-Endpoint':identity['endpoint_id'], 'X-Harness-Boot':identity['boot_id']}
    assert request(endpoint, '/api/backend/lifetime', headers)[0] == 200
    assert request(endpoint, '/api/backend/lifetime', {**headers, 'X-Harness-Token':'wrong'})[0] == 403
    assert request(endpoint, '/api/backend/lifetime', {**headers, 'X-Harness-Boot':'old'})[0] == 409
    env['source_digest'] = 'second'
    status, body = request(endpoint, '/api/backend/lifetime', headers)
    assert status == 409
    assert body['code'] == 'backend_source_drift'


def test_cli_passes_explicit_lifetime_to_real_serve_entrypoint(monkeypatch, tmp_path):
    from harness import cli, server
    calls = []
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(server, 'serve', lambda **kw: calls.append(kw))
    assert cli._run_gui(['--port','12345','--lifetime-receipt',str(tmp_path / 'receipt')]) == 0
    assert calls == [dict(host='127.0.0.1', port=12345, force=False, lifetime_receipt=str(tmp_path / 'receipt'))]
    for args in (['--host','0.0.0.0'], ['--force']):
        with pytest.raises(SystemExit):
            cli._run_gui(['--lifetime-receipt',str(tmp_path / 'receipt'), *args])


def test_cli_refuses_shared_or_existing_state_before_server_start(tmp_path, monkeypatch):
    from harness import cli
    monkeypatch.delenv('HARNESS_STATE_DIR', raising=False)
    with pytest.raises(SystemExit):
        cli._run_gui(['--lifetime-receipt', str(tmp_path / 'receipt')])
    monkeypatch.setenv('HARNESS_STATE_DIR', str(tmp_path))
    (tmp_path / 'backend.json').write_text('{}')
    with pytest.raises(SystemExit):
        cli._run_gui(['--lifetime-receipt', str(tmp_path / 'receipt')])


def test_real_serve_publishes_bound_identity_and_cleans_its_receipt(endpoint, tmp_path, monkeypatch):
    monkeypatch.setattr(lifetime, '_active', None)
    import atexit
    import signal
    from types import SimpleNamespace
    from harness import auto_registry
    monkeypatch.setenv('HARNESS_STATE_DIR', str(tmp_path))
    monkeypatch.setattr(lifetime, 'environment_descriptor', lambda root: {'source_sha':'test'})
    monkeypatch.setattr(endpoint, '_mcp', SimpleNamespace(stop_all=lambda: None))
    monkeypatch.setattr(endpoint, '_maybe_auto_index_codegraph', lambda: None)
    monkeypatch.setattr(auto_registry, 'ensure_keyed_provider_registry_health', lambda: None)
    monkeypatch.setattr(auto_registry, 'start_registry_auto_refresh', lambda: None)
    monkeypatch.setattr(
        endpoint.threading,
        'Thread',
        lambda **kw: SimpleNamespace(
            start=lambda: None,
            join=lambda timeout=None: None,
            is_alive=lambda: False,
        ),
    )
    monkeypatch.setattr(atexit, 'register', lambda *args: None)
    monkeypatch.setattr(signal, 'signal', lambda *args: None)
    receipt = tmp_path / 'receipt'
    seen = []
    class Server:
        def __init__(self, address, handler):
            assert handler is endpoint.Handler
            self.server_address = ('127.0.0.1', 23456)
        def serve_forever(self):
            seen.append(json.loads(receipt.read_text()))
        def server_close(self):
            seen.append('closed')
    monkeypatch.setattr(endpoint, 'ThreadingHTTPServer', Server)
    endpoint.serve(port=0, lifetime_receipt=str(receipt))
    assert seen[0]['port'] == 23456
    assert seen[0]['boot_id'] == endpoint._endpoint_identity().boot_id
    assert seen[1] == 'closed'
    assert not receipt.exists()


@pytest.mark.parametrize('receipt_mode', [True, False])
def test_two_cli_launches_hold_state_before_server_import(tmp_path, receipt_mode):
    import os
    import subprocess
    import sys
    from concurrent.futures import ThreadPoolExecutor
    script = r'''
import builtins, json, os, sys
from pathlib import Path
from types import SimpleNamespace
from harness import cli
state = Path(os.environ['HARNESS_STATE_DIR'])
original = builtins.__import__
def intercept(name, *args, **kwargs):
    if name == 'server':
        # Stand in for server.py's import-time credential write, without providers.
        (state / 'token').write_text(str(os.getpid()))
        def serve(**kw):
            (state / 'backend.json').write_text(str(os.getpid()))
            (state / 'receipt').write_text(str(os.getpid()))
            print('OWNED', flush=True)
            sys.stdin.readline()
        return SimpleNamespace(serve=serve)
    return original(name, *args, **kwargs)
builtins.__import__ = intercept
print('READY', flush=True)
sys.stdin.readline()
cli._run_gui(['--lifetime-receipt', str(state / 'receipt')] if os.environ['RECEIPT_MODE'] == '1' else [])
'''
    (tmp_path / 'token').write_text('before')
    procs = [subprocess.Popen([sys.executable, '-c', script], env={**os.environ, 'HARNESS_STATE_DIR':str(tmp_path), 'RECEIPT_MODE':str(int(receipt_mode))},
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for _ in range(2)]
    try:
        assert [p.stdout.readline().strip() for p in procs] == ['READY', 'READY']
        for p in procs:
            p.stdin.write('go\n'); p.stdin.flush()
        with ThreadPoolExecutor(2) as pool:
            replies = list(pool.map(lambda p: p.stdout.readline().strip(), procs))
        assert sorted(replies) == ['', 'OWNED']
        winner = procs[replies.index('OWNED')]
        loser = procs[replies.index('')]
        assert loser.wait(timeout=10) == 2
        assert 'owned by another launcher' in loser.stderr.read()
        expected = str(winner.pid).encode()
        assert all((tmp_path / name).read_bytes() == expected for name in ['token','backend.json','receipt'])
        winner.stdin.write('exit\n'); winner.stdin.flush()
        assert winner.wait(timeout=10) == 0
        # Kernel release permits acquisition, but stale evidence still fails closed.
        with pytest.raises(RuntimeError, match='already exists'):
            with lifetime.exclusive_startup(tmp_path, tmp_path / 'receipt'):
                pytest.fail('stale evidence accepted')
    finally:
        for p in procs:
            if p.poll() is None:
                p.stdin.write('exit\n'); p.stdin.flush()
            p.wait(timeout=10)


def test_lease_releases_on_error(tmp_path):
    with pytest.raises(ValueError):
        with lifetime.exclusive_startup(tmp_path, tmp_path / 'receipt'):
            raise ValueError('import failed')
    with lifetime.exclusive_startup(tmp_path, tmp_path / 'receipt'):
        pass


def test_lease_failure_prevents_import(tmp_path, monkeypatch):
    def unsupported(fd):
        raise RuntimeError('unsupported lease')
    monkeypatch.setattr(lifetime, '_lock_state', unsupported)
    with pytest.raises(RuntimeError, match='unsupported lease'):
        with lifetime.exclusive_startup(tmp_path):
            pytest.fail('unsupported ownership accepted')


@pytest.mark.skipif(__import__('os').name != 'posix', reason='ESRCH recovery is POSIX-only')
def test_normal_cli_recovers_exited_owner_without_losing_history(tmp_path, monkeypatch):
    import builtins
    import errno
    import os
    import socket
    import subprocess
    import sys
    from types import SimpleNamespace
    from harness import cli
    exited = subprocess.Popen([sys.executable, '-c', 'pass'])
    assert exited.wait(timeout=10) == 0
    marker = json.dumps({'pid':exited.pid, 'port':12345})
    (tmp_path / 'backend.json').write_text(marker)
    (tmp_path / 'token').write_text('old token')
    (tmp_path / 'history.json').write_text('existing history')
    monkeypatch.setenv('HARNESS_STATE_DIR', str(tmp_path))
    def refused(*args, **kwargs):
        raise ConnectionRefusedError(errno.ECONNREFUSED, 'fixture refused')
    monkeypatch.setattr(socket, 'create_connection', refused)
    original_import = builtins.__import__
    calls = []
    def intercept(name, *args, **kwargs):
        if name == 'server':
            # This is the import boundary that initializes real server credentials.
            with pytest.raises(RuntimeError, match='owned by another launcher'):
                with lifetime.exclusive_startup(tmp_path):
                    pytest.fail('lease released before import')
            assert (tmp_path / 'history.json').read_text() == 'existing history'
            (tmp_path / 'token').write_text('new token')
            return SimpleNamespace(serve=lambda **kw: calls.append(kw))
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', intercept)
    assert cli._run_gui([]) == 0
    assert len(calls) == 1
    assert (tmp_path / 'history.json').read_text() == 'existing history'
    assert (tmp_path / 'backend.json').read_text() == marker


@pytest.mark.skipif(__import__('os').name != 'posix', reason='POSIX existence probe')
@pytest.mark.parametrize('pid_result', ['exists', 'unknown', 'absent'])
@pytest.mark.parametrize('endpoint_result', ['refused', 'timeout', 'alive'])
def test_recovery_requires_both_absent_pid_and_refused_endpoint(tmp_path, monkeypatch, pid_result, endpoint_result):
    import errno
    import socket
    from contextlib import nullcontext
    probes = []
    def probe(pid, signal):
        probes.append((pid, signal))
        if pid_result != 'exists':
            raise OSError(errno.ESRCH if pid_result == 'absent' else errno.EPERM, 'fixture')
    def connect(*args, **kwargs):
        if endpoint_result == 'alive':
            return nullcontext()
        raise OSError(errno.ECONNREFUSED if endpoint_result == 'refused' else errno.ETIMEDOUT, 'fixture')
    monkeypatch.setattr(lifetime.os, 'kill', probe)
    monkeypatch.setattr(socket, 'create_connection', connect)
    (tmp_path / 'backend.json').write_text(json.dumps({'pid':42, 'port':12345}))
    (tmp_path / 'token').write_text('original')
    if pid_result == 'absent' and endpoint_result == 'refused':
        with lifetime.exclusive_startup(tmp_path):
            pass
    else:
        with pytest.raises(RuntimeError, match='State retained'):
            with lifetime.exclusive_startup(tmp_path):
                pytest.fail('uncertain ownership accepted')
    assert probes == [(42, 0)]
    assert (tmp_path / 'token').read_text() == 'original'


@pytest.mark.parametrize('configured', [False, True])
@pytest.mark.parametrize('durable_exists', [False, True])
@pytest.mark.parametrize('explicit', [False, True])
@pytest.mark.parametrize('live_owner', [False, True])
def test_gui_leases_server_state_before_import(
    tmp_path, monkeypatch, configured, durable_exists, explicit, live_owner,
):
    import builtins
    import os
    from types import SimpleNamespace
    from harness import cli

    home = tmp_path / 'home'
    root = home / '.pmharness'
    root.mkdir(parents=True)
    durable = root / 'state'
    if durable_exists:
        durable.mkdir()
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'state_dir': str(tmp_path / 'custom')} if configured else {}))
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('USERPROFILE', str(home))
    monkeypatch.setenv('HARNESS_CONFIG', str(config))
    monkeypatch.delenv('HARNESS_STATE_DIR', raising=False)
    state = durable if durable_exists or not configured else root
    if explicit:
        state = tmp_path / 'explicit'
        monkeypatch.setenv('HARNESS_STATE_DIR', str(state))
    # Seed only evidence for existing owners; an empty default must be anchored.
    if live_owner:
        state.mkdir(parents=True, exist_ok=True)
        (state / 'backend.json').write_text(json.dumps({'pid': os.getpid(), 'port': 12345}))
        (state / 'token').write_text('original token')
    (root / 'workspace.json').write_text('legacy workspace sentinel')
    before = {p: p.read_bytes() for p in (root / 'workspace.json', state / 'token', state / 'backend.json') if p.exists()}
    imports = []
    original_import = builtins.__import__

    def intercept(name, *args, **kwargs):
        if name == 'server':
            imports.append(name)
            assert not live_owner, 'live owner must be refused before token initialization'
            assert state.is_dir()
            # Import-time token initialization and serve both require ownership.
            def assert_held(**kwargs):
                with pytest.raises(RuntimeError, match='owned by another launcher'):
                    with lifetime.exclusive_startup(state):
                        pytest.fail('actual server state was not leased')
            assert_held()
            return SimpleNamespace(serve=assert_held)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', intercept)
    if live_owner:
        with pytest.raises(SystemExit) as exc:
            cli._run_gui([])
        assert exc.value.code == 2
        assert imports == []
    else:
        assert cli._run_gui([]) == 0
        assert imports == ['server']
    assert all(path.read_bytes() == contents for path, contents in before.items())
    if not durable_exists and (explicit or configured):
        assert not durable.exists()
    assert not (tmp_path / 'custom').exists()
