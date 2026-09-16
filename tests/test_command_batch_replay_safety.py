"""Real subprocess effects across logical-action replay and journal recovery."""
from unittest.mock import patch

from command_shell_helpers import python_shell_command

from test_command_batches import _Session, _wait_batch_terminal
from harness.command_batches import start_command_batch


def effect_command(exit_code=1, pause=False, distinct=False):
    code = "from pathlib import Path; p=Path('effect'); p.open('a').write('x'); raise SystemExit(%d)" % exit_code
    if distinct:
        code = code.replace("p=Path('effect')", "import os; p=Path('effect-%d' % os.getpid())")
    if pause:
        code = code.replace("raise SystemExit", "import time; time.sleep(2); raise SystemExit")
    return python_shell_command(code)


def test_timeout_after_effect_same_action_does_not_repeat(tmp_path):
    sess = _Session(str(tmp_path), str(tmp_path))
    command = effect_command(pause=True)
    with patch('harness.command_policy.effective_command_timeout', return_value=0.2):
        first = start_command_batch(sess, [command], 'effect-action')
        settled = _wait_batch_terminal(sess, first['batch_id'])
    assert settled['status'] == 'failed'
    assert settled['children'][0]['status'] == 'timeout'
    assert (tmp_path / 'effect').read_text() == 'x'
    second = start_command_batch(sess, [command], 'effect-action')
    _wait_batch_terminal(sess, second['batch_id'])
    assert (tmp_path / 'effect').read_text() == 'x'
    assert second['child_job_ids'] == first['child_job_ids']


import time
import pytest
from harness.command_batches import cancel_command_batch
from harness.command_jobs import launch_registered_command_job, build_pending_receipt


def test_nonzero_exit_receipt_and_explicit_new_action(tmp_path):
    sess = _Session(str(tmp_path), str(tmp_path))
    command = effect_command()
    first = start_command_batch(sess, [command], 'first')
    batch = _wait_batch_terminal(sess, first['batch_id'])
    assert batch['children'][0]['terminal_receipt']['exit_code'] == 1
    assert batch['children'][0]['status'] == 'failed'
    replay = start_command_batch(sess, [command], 'first')
    assert replay['children'][0]['terminal_receipt'] == batch['children'][0]['terminal_receipt']
    second = start_command_batch(sess, [command], 'intentional-retry')
    _wait_batch_terminal(sess, second['batch_id'])
    assert second['child_job_ids'] != first['child_job_ids']
    assert (tmp_path / 'effect').read_text() == 'xx'


def test_identical_occurrences_remain_distinct_across_restart(tmp_path):
    sess = _Session(str(tmp_path), str(tmp_path))
    command = effect_command(distinct=True)
    first = start_command_batch(sess, [command, command], 'duplicates')
    batch = _wait_batch_terminal(sess, first['batch_id'])
    effects = {path.name: path.read_text() for path in tmp_path.glob('effect-*')}
    assert len(effects) == 2
    assert set(effects.values()) == {'x'}
    assert all(child['status'] == 'failed' and child['terminal_receipt']['exit_code'] == 1 for child in batch['children'])
    assert len({c['idempotency_key'] for c in batch['children']}) == 2
    restarted = _Session(str(tmp_path), str(tmp_path))
    restarted._load_local_jobs()
    replay = start_command_batch(restarted, [command, command], 'duplicates')
    assert replay['child_job_ids'] == first['child_job_ids']
    assert [c['terminal_receipt'] for c in replay['children']] == [c['terminal_receipt'] for c in batch['children']]
    assert {path.name: path.read_text() for path in tmp_path.glob('effect-*')} == effects


@pytest.mark.parametrize('change', ['command', 'count', 'order', 'cwd'])
def test_action_payload_is_immutable(tmp_path, change):
    sess = _Session(str(tmp_path), str(tmp_path))
    commands = [effect_command(), effect_command(0)]
    with patch('harness.command_batches._start_batch_supervisor') as launch:
        first = start_command_batch(sess, commands, 'immutable')
        if change == 'command':
            commands[0] = effect_command(2)
        elif change == 'count':
            commands.pop()
        elif change == 'order':
            commands.reverse()
        else:
            sess.config.repo = str(tmp_path / 'other')
        with pytest.raises(ValueError, match='identity conflict'):
            start_command_batch(sess, commands, 'immutable')
        assert launch.call_count == 1
    assert len(sess._local_jobs) == 3
    assert not (tmp_path / 'effect').exists()


def test_recycled_batch_action_id_after_terminal_starts_new(tmp_path):
    sess = _Session(str(tmp_path), str(tmp_path))
    first = start_command_batch(sess, [effect_command(0)], 'recycle')
    _wait_batch_terminal(sess, first['batch_id'])
    second = start_command_batch(sess, [effect_command(0, distinct=True)], 'recycle')
    assert second['batch_id'] != first['batch_id']
    _wait_batch_terminal(sess, second['batch_id'])
    replay = start_command_batch(sess, [effect_command(0)], 'recycle')
    assert replay['batch_id'] == first['batch_id']


@pytest.mark.parametrize('restart', [False, True])
def test_checkpoint_with_lost_receipt_is_unknown_and_never_repeated(tmp_path, restart):
    sess = _Session(str(tmp_path), str(tmp_path))
    command = effect_command(0)
    with patch('harness.command_batches._start_batch_supervisor'):
        first = start_command_batch(sess, [command], 'lost-receipt')
    cid = first['child_job_ids'][0]
    assert sess._checkpoint_command_job_launch(cid)
    # Fault boundary: process has an effect, but no running/terminal journal write.
    import subprocess
    subprocess.run(command, shell=True, cwd=tmp_path, check=True)
    if restart:
        sess = _Session(str(tmp_path), str(tmp_path))
        sess._load_local_jobs()
    with patch('harness.command_batches._start_batch_supervisor') as launch:
        replay = start_command_batch(sess, [command], 'lost-receipt')
        launch.assert_not_called()
    assert replay['status'] == 'unknown'
    assert replay['terminal_receipt'] is None
    assert replay['children'][0]['status'] == 'unknown'
    assert replay['children'][0].get('terminal_receipt') is None
    assert build_pending_receipt(sess.get_local_job(cid))['status'] == 'unknown'
    assert not launch_registered_command_job(sess, cid, command, str(tmp_path))
    assert (tmp_path / 'effect').read_text() == 'x'


def test_registered_unlaunched_occurrences_resume_once(tmp_path):
    sess = _Session(str(tmp_path), str(tmp_path))
    command = effect_command(0, distinct=True)
    with patch('harness.command_batches._start_batch_supervisor'):
        first = start_command_batch(sess, [command, command], 'unstarted')
    replay = start_command_batch(sess, [command, command], 'unstarted')
    assert replay['child_job_ids'] == first['child_job_ids']
    assert _wait_batch_terminal(sess, first['batch_id'])['status'] == 'completed'
    effects = list(tmp_path.glob('effect-*'))
    assert len(effects) == 2
    assert all(path.read_text() == 'x' for path in effects)


def test_cancel_after_partial_effect_does_not_restart_siblings(tmp_path):
    sess = _Session(str(tmp_path), str(tmp_path))
    command = effect_command(pause=True)
    first = start_command_batch(sess, [command, effect_command(0)], 'cancel', max_concurrency=1)
    deadline = time.monotonic() + 3
    while not (tmp_path / 'effect').exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert (tmp_path / 'effect').read_text() == 'x'
    cancel_command_batch(sess, first['batch_id'])
    batch = _wait_batch_terminal(sess, first['batch_id'])
    replay = start_command_batch(sess, [command, effect_command(0)], 'cancel')
    assert replay['child_job_ids'] == first['child_job_ids']
    assert all(c['status'] == 'cancelled' for c in replay['children'])
    assert replay['terminal_receipt'] == batch['terminal_receipt']
    assert (tmp_path / 'effect').read_text() == 'x'


def test_concurrent_same_action_launches_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    sess = _Session(str(tmp_path), str(tmp_path))
    command = effect_command(0)
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(lambda _: start_command_batch(sess, [command], 'race'), range(4)))
    assert len({r['batch_id'] for r in receipts}) == 1
    _wait_batch_terminal(sess, receipts[0]['batch_id'])
    assert (tmp_path / 'effect').read_text() == 'x'


@pytest.mark.parametrize('terminal', ['completed', 'failed', 'cancelled', 'timeout', 'truncated'])
def test_each_terminal_receipt_is_retained(tmp_path, terminal):
    sess = _Session(str(tmp_path), str(tmp_path))
    command = effect_command()
    with patch('harness.command_batches._start_batch_supervisor'):
        first = start_command_batch(sess, [command, command], 'terminal')
    for cid in first['child_job_ids']:
        sess._finish_command_job(cid, status=terminal, summary='observed', exit_code=7, output='partial')
    sess._sync_command_batch_from_children(first['batch_id'])
    original = [sess.get_local_job(cid)['terminal_receipt'] for cid in first['child_job_ids']]
    with patch('harness.command_batches._start_batch_supervisor') as launch:
        replay = start_command_batch(sess, [command, command], 'terminal')
        launch.assert_not_called()
    assert [c['terminal_receipt'] for c in replay['children']] == original
    assert not (tmp_path / 'effect').exists()


def test_restart_before_launch_retains_honest_cancellation(tmp_path):
    sess = _Session(str(tmp_path), str(tmp_path))
    command = effect_command()
    with patch('harness.command_batches._start_batch_supervisor'):
        first = start_command_batch(sess, [command], 'before-launch')
    restarted = _Session(str(tmp_path), str(tmp_path))
    restarted._load_local_jobs()
    replay = start_command_batch(restarted, [command], 'before-launch')
    assert replay['child_job_ids'] == first['child_job_ids']
    child = replay['children'][0]
    assert child['status'] == 'cancelled'
    assert child['terminal_receipt']['had_launch_checkpoint'] is False
    assert not (tmp_path / 'effect').exists()


def test_checkpoint_before_process_start_exposes_uncertainty(tmp_path):
    sess = _Session(str(tmp_path), str(tmp_path))
    command = effect_command()
    with patch('harness.command_batches._start_batch_supervisor'):
        first = start_command_batch(sess, [command], 'pre-process')
    sess._checkpoint_command_job_launch(first['child_job_ids'][0])
    replay = start_command_batch(sess, [command], 'pre-process')
    assert replay['status'] == 'unknown'
    assert not (tmp_path / 'effect').exists()


def test_replay_observation_does_not_require_new_capacity(tmp_path):
    sess = _Session(str(tmp_path), str(tmp_path))
    command = effect_command(0)
    first = start_command_batch(sess, [command], 'capacity')
    _wait_batch_terminal(sess, first['batch_id'])
    sess._resource_pressure_admit.return_value = False
    replay = start_command_batch(sess, [command], 'capacity')
    assert replay['status'] == 'completed'
    assert (tmp_path / 'effect').read_text() == 'x'
