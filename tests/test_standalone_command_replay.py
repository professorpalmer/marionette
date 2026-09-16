"""Standalone replay through the product action dispatcher and real processes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

from command_shell_helpers import python_shell_command

import pytest

from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.pilot import PilotAction
from harness.send_loop_phases import dispatch_local_action


def session_at(root):
    s = ConversationalSession(HarnessConfig(driver='stub-oracle-v2', state_dir=str(root), repo=str(root)))
    s.harness_session_id = 'standalone-session'
    return s


def command(exit_code=0):
    return python_shell_command("from pathlib import Path; Path('effect').open('a').write('x'); raise SystemExit(%d)" % exit_code)


def dispatch(s, cmd, background, aid='same-action'):
    act = PilotAction(kind='run_command', command=cmd, background=background)
    events = list(dispatch_local_action(s, act, aid, False, []))
    return [e.data for e in events if e.kind == 'action_result'][-1]


def settle(s, result):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        row = s.get_local_job(result['job_id'])
        if row['status'] not in ('registered', 'running'):
            return row
        time.sleep(.01)
    raise AssertionError(row)


@pytest.mark.parametrize('background', [False, True])
def test_replay_and_distinct_action(tmp_path, background):
    s = session_at(tmp_path)
    first = dispatch(s, command(), background)
    assert settle(s, first)['status'] == 'completed'
    for cold in (False, True, True):
        if cold:
            s = session_at(tmp_path)
        replay = dispatch(s, command(), background)
        assert replay['job_id'] == first['job_id']
        assert replay['status'] == 'completed'
    assert (tmp_path / 'effect').read_text() == 'x'
    second = dispatch(s, command(), background, 'intentional-repeat')
    assert settle(s, second)['status'] == 'completed'
    assert second['job_id'] != first['job_id']
    assert (tmp_path / 'effect').read_text() == 'xx'


@pytest.mark.parametrize('background', [False, True])
@pytest.mark.parametrize('change', ['command', 'cwd', 'session'])
def test_identity_conflict_refuses_inflight_mutation(tmp_path, background, change):
    from harness.command_jobs import standalone_command_job_id
    s = session_at(tmp_path)
    hold = python_shell_command("import time; time.sleep(8)")
    jid = standalone_command_job_id('same-action', hold)
    s._register_command_job(
        jid, command=hold, action_id='same-action', cwd=str(tmp_path)
    )
    s._checkpoint_command_job_launch(jid)
    s._mark_command_job_running(jid)
    cmd = hold
    if change == 'command':
        cmd = command()
    elif change == 'cwd':
        other = tmp_path / 'other'
        other.mkdir()
        s.config.repo = str(other)
    else:
        s.harness_session_id = 'other-session'
    result = dispatch(s, cmd, background)
    assert result.get('error')
    assert 'identity conflict' in result['error']
    assert not (tmp_path / 'effect').exists()
    assert len(s._local_jobs) == 1


@pytest.mark.parametrize('background', [False, True])
def test_recycled_action_id_after_terminal_runs_new_command(tmp_path, background):
    s = session_at(tmp_path)
    first = dispatch(s, command(), background)
    assert settle(s, first)['status'] == 'completed'
    other = python_shell_command(
        "from pathlib import Path; Path('other').write_text('y')"
    )
    second = dispatch(s, other, background)
    assert not second.get('error')
    assert second['job_id'] != first['job_id']
    assert settle(s, second)['status'] == 'completed'
    assert (tmp_path / 'effect').read_text() == 'x'
    assert (tmp_path / 'other').read_text() == 'y'


@pytest.mark.parametrize('background', [False, True])
def test_registration_failure_never_executes(tmp_path, background):
    s = session_at(tmp_path)
    with patch.object(s, '_persist_local_jobs_locked', side_effect=OSError('disk failure')):
        result = dispatch(s, command(), background)
    assert result.get('error')
    assert not (tmp_path / 'effect').exists()


CRASH_CHILD = r'''
import sys
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[1] + '/tests')
import os, time
from test_standalone_command_replay import session_at, dispatch
root, boundary, background, cmd = sys.argv[2:]
s = session_at(root)
checkpoint, finish = s._checkpoint_command_job_launch, s._finish_command_job
def checkpoint_crash(jid):
    checkpoint(jid)
    os._exit(71)
def finish_crash(*args, **kwargs):
    if boundary == 'after_terminal':
        assert finish(*args, **kwargs)
    os._exit(71)
if boundary == 'before_launch':
    s._checkpoint_command_job_launch = checkpoint_crash
else:
    s._finish_command_job = finish_crash
dispatch(s, cmd, background == 'True')
time.sleep(10)
os._exit(72)
'''


@pytest.mark.parametrize('background', [False, True])
@pytest.mark.parametrize('boundary', ['before_launch', 'after_effect', 'after_terminal'])
def test_subprocess_crash_two_cold_product_replays(tmp_path, background, boundary):
    result = subprocess.run([sys.executable, '-I', '-c', CRASH_CHILD, str(Path.cwd()), str(tmp_path), boundary, str(background), command()], capture_output=True, text=True, timeout=15)
    assert result.returncode == 71, result.stderr
    jobs = json.loads((tmp_path / 'swarm_local_jobs.json').read_text())['jobs']
    jid = jobs[0]['id']
    for _ in range(2):
        s = session_at(tmp_path)
        replay = dispatch(s, command(), background)
        assert replay['job_id'] == jid
        assert replay['status'] == ('completed' if boundary == 'after_terminal' else 'unknown')
        if boundary != 'after_terminal':
            assert replay['terminal_receipt'] is None
            assert 'exit_code' not in replay
        assert len(s._local_jobs) == 1
    effect = tmp_path / 'effect'
    assert (effect.read_text() if effect.exists() else '') == ('' if boundary == 'before_launch' else 'x')


@pytest.mark.parametrize('background', [False, True])
@pytest.mark.parametrize('boundary', ['registration', 'checkpoint', 'terminal'])
@pytest.mark.parametrize('fault', ['write', 'fsync', 'replace'])
def test_product_write_barriers(tmp_path, background, boundary, fault):
    s = session_at(tmp_path)
    target = {'write': 'json.dump', 'fsync': 'harness.local_jobs.os.fsync', 'replace': 'harness.local_jobs.os.replace'}[fault]
    method = {'registration': '_register_command_job', 'checkpoint': '_checkpoint_command_job_launch', 'terminal': '_finish_command_job'}[boundary]
    original = getattr(s, method)
    def fail(*args, **kwargs):
        with patch(target, side_effect=OSError('injected disk fault')):
            return original(*args, **kwargs)
    with patch.object(s, method, side_effect=fail):
        result = dispatch(s, command(), background)
        if boundary == 'terminal':
            assert settle(s, result)['status'] == 'unknown'
        else:
            assert result.get('error')
    if boundary == 'terminal':
        for cold in (False, True, True):
            if cold:
                s = session_at(tmp_path)
            replay = dispatch(s, command(), background)
            assert replay['status'] == 'unknown'
            assert replay['terminal_receipt'] is None
            assert 'exit_code' not in replay
        assert (tmp_path / 'effect').read_text() == 'x'
    else:
        assert not (tmp_path / 'effect').exists()


@pytest.mark.parametrize('background', [False, True])
def test_same_process_concurrent_replay(tmp_path, background):
    from concurrent.futures import ThreadPoolExecutor
    s = session_at(tmp_path)
    # Hold the real child across simultaneous dispatch calls.
    cmd = python_shell_command("from pathlib import Path; import time; Path('effect').open('a').write('x'); time.sleep(.2)")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: dispatch(s, cmd, background), range(4)))
    assert len({r['job_id'] for r in results}) == 1
    assert settle(s, results[0])['status'] == 'completed'
    assert (tmp_path / 'effect').read_text() == 'x'


@pytest.mark.parametrize('background', [False, True])
def test_legacy_random_row_reconciles(tmp_path, background):
    s = session_at(tmp_path)
    s._register_command_job('local-cmd-old-random', command=command(), action_id='same-action', cwd=str(tmp_path))
    s._checkpoint_command_job_launch('local-cmd-old-random')
    subprocess.run(command(), shell=True, cwd=tmp_path, check=True)
    s._finish_command_job('local-cmd-old-random', status='completed', exit_code=0)
    replay = dispatch(session_at(tmp_path), command(), background)
    assert replay['job_id'] == 'local-cmd-old-random'
    assert replay['status'] == 'completed'
    assert (tmp_path / 'effect').read_text() == 'x'


@pytest.mark.parametrize('background', [False, True])
def test_registration_alone_is_not_success(tmp_path, background):
    from harness.command_jobs import register_foreground_command_job
    s = session_at(tmp_path)
    act = PilotAction(kind='run_command', command=command(), background=background)
    jid = register_foreground_command_job(s, act, 'same-action')
    for _ in range(2):
        result = dispatch(session_at(tmp_path), command(), background)
        assert result['job_id'] == jid
        assert result['status'] == 'cancelled'
        assert result['terminal_receipt']['had_launch_checkpoint'] is False
    assert not (tmp_path / 'effect').exists()


@pytest.mark.parametrize('background', [False, True])
def test_native_send_receives_unknown_on_two_cold_replays(tmp_path, background):
    from pmharness.drivers.base import DriverResponse
    from harness.sessions import load_transcript
    result = subprocess.run([sys.executable, '-I', '-c', CRASH_CHILD, str(Path.cwd()), str(tmp_path), 'after_effect', str(background), command()], capture_output=True, text=True, timeout=15)
    assert result.returncode == 71, result.stderr

    class Pilot:
        model = 'stub-oracle-v2'
        supports_streaming = False
        def __init__(self):
            self.calls = 0
            self.receipts = []
        def chat(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return DriverResponse(text='', meta={'tool_calls': [{'id': 'same-action', 'type': 'function', 'function': {'name': 'run_command', 'arguments': json.dumps({'command': command(), 'background': background})}}]}, tokens_in=1, tokens_out=1, latency_ms=1)
            results = [m for m in messages if m.get('role') == 'tool']
            self.receipts.append(json.JSONDecoder().raw_decode(results[-1]['content'])[0])
            return DriverResponse(text='Observed unknown outcome.', tokens_in=1, tokens_out=1, latency_ms=1)
        def complete(self, prompt, **kwargs):
            raise AssertionError('Expected native chat')

    for _ in range(2):
        s = session_at(tmp_path)
        s.load_history(load_transcript(str(tmp_path), s.harness_session_id))
        pilot = Pilot()
        s.pilot = pilot
        s._build_visible_tools_schema = lambda: [{'type': 'function', 'function': {'name': 'run_command', 'parameters': {'type': 'object'}}}]
        s._maybe_compact_history = lambda *a, **k: iter(())
        s._submit_housekeeping = lambda *a, **k: None
        events = list(s.send('Observe same command action.'))
        results = [e.data for e in events if e.kind == 'action_result' and e.data.get('kind') == 'run_command']
        assert results, [(e.kind, e.data) for e in events]
        assert results[-1]['status'] == 'unknown'
        assert pilot.receipts[-1]['status'] == 'unknown'
        assert pilot.receipts[-1]['terminal_receipt'] is None
        assert 'exit_code' not in pilot.receipts[-1]
    assert (tmp_path / 'effect').read_text() == 'x'


@pytest.mark.parametrize('background', [False, True])
def test_failed_exit_replays_failure(tmp_path, background):
    cmd = command(exit_code=7)
    s = session_at(tmp_path)
    first = dispatch(s, cmd, background)
    assert settle(s, first)['status'] == 'failed'
    replay = dispatch(session_at(tmp_path), cmd, background)
    assert replay['status'] == 'failed'
    assert replay['exit_code'] == 7
    assert (tmp_path / 'effect').read_text() == 'x'


@pytest.mark.parametrize('background', [False, True])
def test_replay_output_stays_bounded(tmp_path, background):
    cmd = python_shell_command("print('x' * 80000)")
    s = session_at(tmp_path)
    first = dispatch(s, cmd, background)
    assert settle(s, first)['status'] == 'completed'
    replay = dispatch(session_at(tmp_path), cmd, background)
    assert replay['status'] == 'completed'
    assert len(replay['output']) <= 50 * 1024 + 100


@pytest.mark.parametrize('background', [False, True])
def test_ambiguous_legacy_actions_refuse(tmp_path, background):
    s = session_at(tmp_path)
    for jid in ('local-cmd-legacy-one', 'local-cmd-legacy-two'):
        s._register_command_job(jid, command=command(), action_id='same-action', cwd=str(tmp_path))
    result = dispatch(s, command(), background)
    assert 'multiple prior jobs' in result['error']
    assert not (tmp_path / 'effect').exists()


@pytest.mark.parametrize('same_cwd', [False, True])
@pytest.mark.parametrize('failure', ['executor', 'thread_start', 'base_exception', 'orphan'])
def test_independent_launch_ownership(tmp_path, same_cwd, failure):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from harness.command_jobs import build_pending_receipt
    roots = [tmp_path / name for name in ('held', 'broken')]
    for root in roots:
        root.mkdir()
    held, broken = map(session_at, roots)
    if same_cwd:
        broken.config.repo = held.config.repo
    if failure == 'orphan':
        from harness.command_jobs import register_foreground_command_job
        act = PilotAction(kind='run_command', command=command())
        jid = register_foreground_command_job(broken, act, 'same-action')
        assert broken._checkpoint_command_job_launch(jid)
    entered, release = threading.Event(), threading.Event()
    original = held._do_run_command
    def held_executor(act):
        result = original(act)
        entered.set()
        assert release.wait(5)
        return result
    def abrupt(act):
        broken_original(act)
        raise (KeyboardInterrupt() if failure == 'base_exception' else RuntimeError('lost executor'))
    broken_original = broken._do_run_command
    with patch.object(held, '_do_run_command', side_effect=held_executor), ThreadPoolExecutor() as pool:
        future = pool.submit(dispatch, held, command(), False)
        assert entered.wait(5)
        try:
            if failure == 'orphan':
                result = dispatch(broken, command(), False)
            elif failure == 'thread_start':
                with patch('harness.command_jobs.threading.Thread.start', side_effect=RuntimeError('no thread')):
                    result = dispatch(broken, command(), True)
            elif failure == 'base_exception':
                with patch.object(broken, '_do_run_command', side_effect=abrupt), pytest.raises(KeyboardInterrupt):
                    dispatch(broken, command(), False)
                result = dispatch(broken, command(), False)
            else:
                with patch.object(broken, '_do_run_command', side_effect=abrupt):
                    result = dispatch(broken, command(), False)
            assert result['status'] == 'unknown'
            assert result['terminal_receipt'] is None
            assert 'exit_code' not in result
            held_row = next(iter(held._local_jobs.values()))
            assert held_row['id'] == result['job_id']
            assert build_pending_receipt(held_row)['status'] == 'registered'
            for cold in (False, True, True):
                if cold:
                    broken = session_at(roots[1])
                    if same_cwd:
                        broken.config.repo = held.config.repo
                replay = dispatch(broken, command(), failure == 'thread_start')
                assert replay['status'] == 'unknown'
                assert replay['terminal_receipt'] is None
                assert 'exit_code' not in replay
        finally:
            release.set()
        assert future.result()['status'] == 'ok'
        assert held.get_local_job(held_row['id'])['status'] == 'completed'
    expected = 1 if failure in ('thread_start', 'orphan') else 2
    assert sum(len((root / 'effect').read_text()) for root in roots if (root / 'effect').exists()) == expected


@pytest.mark.parametrize('background', [False, True])
@pytest.mark.parametrize('executable_path', ['current', 'spaces'])
def test_shell_fixture_quotes_and_executable_paths(tmp_path, background, executable_path):
    import venv

    executable = sys.executable
    if executable_path == 'spaces':
        env_dir = tmp_path / 'python environment with spaces'
        venv.EnvBuilder(with_pip=False, symlinks=os.name != 'nt').create(env_dir)
        executable = str(env_dir / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python'))
    expected = 'single\' double" percent%PATH% bang! caret^ amp& pipe| less< greater> backslash\\\nnext line'
    code = (
        'from pathlib import Path\n'
        f'value = {expected!r}\n'
        'with Path("quoted effect").open("a", encoding="utf-8") as effect:\n'
        '    effect.write(value)\n'
        'raise SystemExit(7)\n'
    )
    cmd = python_shell_command(code, executable=executable)
    s = session_at(tmp_path)
    first = dispatch(s, cmd, background)
    settled = settle(s, first)
    assert settled['status'] == 'failed'
    assert settled['exit_code'] == 7, settled.get('output')
    replay = dispatch(session_at(tmp_path), cmd, background)
    assert replay['job_id'] == first['job_id']
    assert replay['status'] == 'failed'
    assert replay['exit_code'] == 7
    assert (tmp_path / 'quoted effect').read_text(encoding='utf-8') == expected
