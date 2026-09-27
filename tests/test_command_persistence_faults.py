"""Command write barriers and real backend-process crash recovery."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from command_shell_helpers import python_shell_command

import pytest

from harness.command_batches import start_command_batch
from harness.command_jobs import launch_registered_command_job
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession


def session_at(path):
    return ConversationalSession(HarnessConfig(driver='stub-oracle-v2', state_dir=str(path), repo=str(path)))


EFFECT_CODE = "from pathlib import Path; Path('effect').open('a').write('x') # private-command-marker"


def command():
    return python_shell_command(EFFECT_CODE)


@pytest.mark.parametrize('boundary', ['registration', 'checkpoint'])
@pytest.mark.parametrize('fault', ['write', 'replace', 'fsync'])
def test_write_failure_prevents_launch(tmp_path, boundary, fault):
    session = session_at(tmp_path)
    cmd = command()
    if boundary == 'checkpoint':
        session._register_command_job('local-cmd-fault', command=cmd, action_id='fault')
    target = {'write': 'harness.local_jobs_store.json.dumps', 'replace': 'harness.local_jobs_store.os.replace', 'fsync': 'harness.local_jobs_store.os.fsync'}[fault]
    with patch(target, side_effect=OSError('injected disk failure')):
        with pytest.raises(OSError):
            if boundary == 'registration':
                start_command_batch(session, [cmd], 'fault')
            else:
                launch_registered_command_job(session, 'local-cmd-fault', cmd, str(tmp_path))
    assert not (tmp_path / 'effect').exists()
    if boundary == 'checkpoint':
        assert session.get_local_job('local-cmd-fault').get('launch_checkpoint') is None


def test_terminal_write_failure_is_unknown_and_cannot_leak_into_next_write(tmp_path):
    session = session_at(tmp_path)
    cmd = command()
    session._register_command_job('local-cmd-fault', command=cmd, action_id='fault')
    session._checkpoint_command_job_launch('local-cmd-fault')
    subprocess.run(cmd, shell=True, cwd=tmp_path, check=True)
    with patch('harness.local_jobs_store.os.replace', side_effect=OSError('injected disk failure')):
        assert not session._finish_command_job('local-cmd-fault', status='completed', exit_code=0)
    row = session.get_local_job('local-cmd-fault')
    assert row['status'] == 'unknown'
    assert row['terminal_receipt'] is None
    session._persist_local_jobs()
    restarted = session_at(tmp_path)
    row = restarted.get_local_job('local-cmd-fault')
    assert row['status'] == 'unknown'
    assert row['terminal_receipt'] is None
    assert not launch_registered_command_job(restarted, row['id'], cmd, str(tmp_path))
    assert (tmp_path / 'effect').read_text() == 'x'


def test_command_evidence_survives_provider_history_cap(tmp_path):
    session = session_at(tmp_path)
    for index, state in enumerate(['registered', 'unknown', 'completed']):
        jid = 'local-cmd-' + state
        session._register_command_job(jid, command=command(), action_id=state)
        session._local_jobs[jid]['created_at'] = index
        if state == 'unknown':
            session._checkpoint_command_job_launch(jid)
        elif state == 'completed':
            session._finish_command_job(jid, status=state, exit_code=0)
    for index in range(205):
        session._local_jobs[str(index)] = {'id': str(index), 'role': 'implement', 'status': 'completed', 'created_at': 100 + index}
    session._persist_local_jobs()
    rows = json.loads(Path(session._local_jobs_path).read_text())['jobs']
    assert {r['id'] for r in rows if r.get('role') == 'command'} == {'local-cmd-registered', 'local-cmd-unknown', 'local-cmd-completed'}
    assert len([r for r in rows if r.get('role') == 'implement']) == 200
    assert 'private-command-marker' not in Path(session._local_jobs_path).read_text()
    assert base64.b64encode(EFFECT_CODE.encode()).decode() not in Path(session._local_jobs_path).read_text()


# The child dies in the real command worker; no provider is contacted.
CRASH_CHILD = '''
import sys
sys.path.insert(0, sys.argv[1])
import os, time
from pathlib import Path
from harness.config import HarnessConfig
from harness.conversation import ConversationalSession
from harness.command_batches import start_command_batch
root, boundary, cmd = sys.argv[2:]
s = ConversationalSession(HarnessConfig(driver='stub-oracle-v2', state_dir=root, repo=root))
checkpoint = s._checkpoint_command_job_launch
finish = s._finish_command_job
def checkpoint_crash(jid):
    result = checkpoint(jid)
    os._exit(71)
def finish_crash(*args, **kwargs):
    if boundary == 'terminal_write_failure':
        from unittest.mock import patch
        with patch('harness.local_jobs_store.os.replace', side_effect=OSError('disk fault')):
            assert finish(*args, **kwargs) is False
    if boundary == 'after_terminal':
        finish(*args, **kwargs)
    os._exit(71)
if boundary == 'before_launch':
    s._checkpoint_command_job_launch = checkpoint_crash
else:
    s._finish_command_job = finish_crash
start_command_batch(s, [cmd], 'crash-action')
time.sleep(15)
raise SystemExit(72)
'''


@pytest.mark.parametrize('boundary', ['before_launch', 'after_effect', 'after_terminal', 'terminal_write_failure'])
def test_real_backend_crash_same_action_never_repeats(tmp_path, boundary):
    cmd = command()
    result = subprocess.run([sys.executable, '-I', '-c', CRASH_CHILD, str(Path.cwd()), str(tmp_path), boundary, cmd], timeout=20, capture_output=True, text=True)
    assert result.returncode == 71, result.stderr
    for _ in range(2):
        session = session_at(tmp_path)
        replay = start_command_batch(session, [cmd], 'crash-action')
        expected = 'completed' if boundary == 'after_terminal' else 'unknown'
        assert replay['status'] == expected
        assert replay['children'][0]['status'] == expected
        if expected == 'unknown':
            assert replay['terminal_receipt'] is None
            assert replay['children'][0].get('terminal_receipt') is None
        assert 'private-command-marker' not in Path(session._local_jobs_path).read_text()
        assert base64.b64encode(EFFECT_CODE.encode()).decode() not in Path(session._local_jobs_path).read_text()
    assert ((tmp_path / 'effect').read_text() if (tmp_path / 'effect').exists() else '') == ('' if boundary == 'before_launch' else 'x')


def test_batch_registration_failure_never_starts_supervisor(tmp_path):
    session = session_at(tmp_path)
    replace = os.replace
    def fail_parent(src, dst):
        if any(row.get('job_kind') == 'run_command_batch' for row in json.loads(Path(src).read_text())['jobs']):
            raise OSError('parent write failed')
        return replace(src, dst)
    with patch('harness.local_jobs_store.os.replace', side_effect=fail_parent):
        with pytest.raises(OSError, match='parent write failed'):
            start_command_batch(session, [command()], 'parent-fault')
    assert not (tmp_path / 'effect').exists()
    assert all(row['job_kind'] != 'run_command_batch' for row in session.live_local_jobs())


def test_registration_collision_preserves_original_row_and_cancel(tmp_path):
    session = session_at(tmp_path)
    original = session._register_command_job('local-cmd-collision', command=command(), action_id='one')
    cancel = session._local_job_cancels['local-cmd-collision']
    cancel.set()
    with pytest.raises(ValueError, match='identity conflict'):
        session._register_command_job('local-cmd-collision', command='echo changed', action_id='two')
    assert session.get_local_job('local-cmd-collision') == original
    assert session._local_job_cancels['local-cmd-collision'] is cancel
    assert cancel.is_set()


def test_provider_write_failure_stays_best_effort(tmp_path):
    session = session_at(tmp_path)
    with patch('harness.local_jobs_store.os.replace', side_effect=OSError('disk fault')):
        session._persist_local_jobs()


def test_failed_terminal_publication_releases_batch_capacity(tmp_path):
    import time
    session = session_at(tmp_path)
    replace = os.replace
    injected = False
    def fail_first_terminal(src, dst):
        nonlocal injected
        rows = json.loads(Path(src).read_text())['jobs']
        if not injected and any(r.get('role') == 'command' and r.get('terminal_receipt') for r in rows):
            injected = True
            raise OSError('terminal write failed')
        return replace(src, dst)
    with patch('harness.local_jobs_store.os.replace', side_effect=fail_first_terminal):
        first = start_command_batch(session, [command(), command()], 'capacity-fault', max_concurrency=1)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            rows = [session.get_local_job(jid) for jid in first['child_job_ids']]
            if [r['status'] for r in rows] == ['unknown', 'completed']:
                break
            time.sleep(.01)
        assert [r['status'] for r in rows] == ['unknown', 'completed']
    restarted = session_at(tmp_path)
    replay = start_command_batch(restarted, [command(), command()], 'capacity-fault')
    assert replay['status'] == 'unknown'
    assert (tmp_path / 'effect').read_text() == 'xx'
