"""Cold-process probes of actual filesystem publication boundaries."""
import json
import os
import subprocess
import sys

import pytest

from harness import plugin_registry as registry
from tests.test_agent_plugins import plugins_home
from tests.test_plugin_install_transaction import prepared

CRASH = r'''
import os, sys
from pathlib import Path
from harness import plugin_registry as r
replace = os.replace
boundary = sys.argv[2]
def interrupted(src, dst):
    replace(src, dst)
    src, dst = Path(src), Path(dst)
    hit = ((boundary == 'old' and dst.name == 'old') or
           (boundary == 'new' and src.name == 'new') or
           (boundary in ('enabled.json', 'capabilities.json') and dst.name == boundary))
    if hit:
        os._exit(73)
os.replace = interrupted
r.install_from_source(sys.argv[1], force=True)
'''
COLD = r'''
import json, sys
from harness import plugin_registry as r
mode, source, ident = sys.argv[1:]
if mode == 'enable':
    r.enable_plugin(ident)
elif mode == 'install':
    try:
        r.install_from_source(source)
    except r.AgentPluginError as exc:
        assert 'already installed' in str(exc), str(exc)
records = r.discover_plugins()
print(json.dumps([r.plugin_record_to_dict(x) for x in records]))
'''


def run(code, *args):
    return subprocess.run([sys.executable, '-c', code, *map(str, args)],
                          env=os.environ.copy(), capture_output=True, text=True, timeout=20)


@pytest.mark.parametrize('boundary', ['old', 'new', 'enabled.json', 'capabilities.json'])
@pytest.mark.parametrize('entry', ['discover', 'install', 'enable'])
def test_cold_recovery_restores_original(plugins_home, tmp_path, boundary, entry):
    source, old = prepared(tmp_path)
    root = registry.plugins_dir()
    before = {name: (root / name).read_bytes() for name in ('enabled.json', 'capabilities.json')}
    crashed = run(CRASH, source, boundary)
    assert crashed.returncode == 73, crashed.stderr
    for _ in range(2):
        recovered = run(COLD, entry, source, old.id)
        assert recovered.returncode == 0, recovered.stderr
        record, = json.loads(recovered.stdout)
        assert (record['version'], record['enabled'], record['consented_capabilities']) == ('1.2.3', True, ['fs'])
        assert record['stamp_ok']
        assert {name: (root / name).read_bytes() for name in before} == before


@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.parametrize('boundary', ['new', 'enabled.json', 'capabilities.json'])
def test_crash_never_enables_disabled_or_fresh_install(plugins_home, tmp_path, existing, boundary):
    from tests.test_agent_plugins import _valid_package
    if existing:
        source, old = prepared(tmp_path)
        registry.disable_plugin(old.id)
    else:
        source = _valid_package(tmp_path / 'source')
    assert run(CRASH, source, boundary).returncode == 73
    for _ in range(2):
        cold = run(COLD, 'discover', source, 'portable.test')
        assert cold.returncode == 0, cold.stderr
        records = json.loads(cold.stdout)
        assert not any(x['enabled'] for x in records)
        if existing:
            assert records[0]['version'] == '1.2.3'
            assert records[0]['consented_capabilities'] == ['fs']
        else:
            assert records == []
            assert not (registry.plugins_dir() / 'enabled.json').exists()
            assert not (registry.plugins_dir() / 'capabilities.json').exists()


@pytest.mark.parametrize('corruption', ['traversal', 'absolute', 'foreign', 'symlink', 'missing_old', 'bad_json'])
def test_ambiguous_recovery_blocks_and_preserves_evidence(plugins_home, tmp_path, corruption):
    from tests.test_plugin_install_transaction import snapshot
    source, old = prepared(tmp_path)
    assert run(CRASH, source, 'new').returncode == 73
    root = registry.plugins_dir()
    transaction, = root.glob('.install-*')
    marker_path = transaction / 'transaction.json'
    marker = json.loads(marker_path.read_text())
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'keep').write_text('unchanged')
    if corruption == 'traversal':
        marker['id'] = '../outside'
    elif corruption == 'absolute':
        marker['id'] = str(outside)
    elif corruption == 'foreign':
        (root / old.id / 'foreign').write_text('do not delete')
    elif corruption == 'symlink':
        (transaction / 'old' / 'link').symlink_to(outside, target_is_directory=True)
    elif corruption == 'missing_old':
        (transaction / 'old').rename(tmp_path / 'saved-old')
    marker_path.write_text('{' if corruption == 'bad_json' else json.dumps(marker))
    before = snapshot(root)
    for _ in range(2):
        cold = run(COLD, 'discover', source, old.id)
        assert cold.returncode != 0
        assert 'recovery blocked' in cold.stderr
        assert snapshot(root) == before
    assert (outside / 'keep').read_text() == 'unchanged'


def test_interrupted_recovery_replays_snapshots(plugins_home, tmp_path):
    source, old = prepared(tmp_path)
    before = {n: (registry.plugins_dir() / n).read_bytes() for n in ('enabled.json', 'capabilities.json')}
    assert run(CRASH, source, 'capabilities.json').returncode == 73
    code = CRASH[:CRASH.index('r.install_from_source')] + 'r.discover_plugins()\n'
    assert run(code, source, 'enabled.json').returncode == 73
    for _ in range(2):
        cold = run(COLD, 'discover', source, old.id)
        assert cold.returncode == 0, cold.stderr
        record, = json.loads(cold.stdout)
        assert (record['version'], record['enabled'], record['consented_capabilities']) == ('1.2.3', True, ['fs'])
        assert {n: (registry.plugins_dir() / n).read_bytes() for n in before} == before


def test_committed_crash_keeps_validated_new_disabled(plugins_home, tmp_path):
    source, old = prepared(tmp_path)
    code = r'''
import json, os, sys
from pathlib import Path
from harness import plugin_registry as r
replace = os.replace
def interrupted(src, dst):
    replace(src, dst)
    dst = Path(dst)
    if dst.name == 'transaction.json' and json.loads(dst.read_text())['phase'] == 'committed':
        os._exit(73)
os.replace = interrupted
r.install_from_source(sys.argv[1], force=True)
'''
    assert run(code, source).returncode == 73
    for _ in range(2):
        cold = run(COLD, 'discover', source, old.id)
        assert cold.returncode == 0, cold.stderr
        record, = json.loads(cold.stdout)
        assert (record['version'], record['enabled'], record['consented_capabilities']) == ('2.0.0', False, [])
        assert record['stamp_ok']
    enable = run(COLD, 'enable', source, old.id)
    assert enable.returncode != 0
    assert 'consent required' in enable.stderr


def test_recovery_failure_retains_journal_for_retry(plugins_home, tmp_path, monkeypatch):
    from harness import plugin_recovery
    source, old = prepared(tmp_path)
    assert run(CRASH, source, 'capabilities.json').returncode == 73
    transaction, = registry.plugins_dir().glob('.install-*')
    marker = (transaction / 'transaction.json').read_bytes()
    def unavailable(*args):
        raise OSError('disk unavailable')
    with monkeypatch.context() as patch:
        patch.setattr(plugin_recovery, 'atomic_bytes', unavailable)
        with pytest.raises(registry.AgentPluginError, match='evidence retained'):
            registry.discover_plugins()
    assert (transaction / 'transaction.json').read_bytes() == marker
    cold = run(COLD, 'discover', source, old.id)
    assert cold.returncode == 0, cold.stderr
    record, = json.loads(cold.stdout)
    assert (record['version'], record['enabled'], record['consented_capabilities']) == ('1.2.3', True, ['fs'])


def test_foreign_transaction_content_is_not_deleted(plugins_home, tmp_path):
    source, old = prepared(tmp_path)
    assert run(CRASH, source, 'new').returncode == 73
    transaction, = registry.plugins_dir().glob('.install-*')
    foreign = transaction / 'foreign'
    foreign.mkdir()
    (foreign / 'keep').write_text('keep')
    cold = run(COLD, 'discover', source, old.id)
    assert cold.returncode != 0
    assert 'unexpected transaction contents' in cold.stderr
    assert (foreign / 'keep').read_text() == 'keep'


def test_other_process_waits_for_live_install(plugins_home, tmp_path):
    source, old = prepared(tmp_path)
    code = r'''
import os, sys
from pathlib import Path
from harness import plugin_registry as r
replace = os.replace
def paused(src, dst):
    replace(src, dst)
    if Path(dst).name == 'old':
        print('paused', flush=True)
        assert sys.stdin.readline().strip() == 'continue'
os.replace = paused
r.install_from_source(sys.argv[1], force=True)
'''
    writer = subprocess.Popen([sys.executable, '-c', code, str(source)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, env=os.environ.copy())
    reader = None
    try:
        assert writer.stdout.readline().strip() == 'paused'
        reader = subprocess.Popen([sys.executable, '-c', COLD, 'discover', str(source), old.id],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, env=os.environ.copy())
        with pytest.raises(subprocess.TimeoutExpired):
            reader.communicate(timeout=0.25)
        _, error = writer.communicate('continue\n', timeout=20)
        assert writer.returncode == 0, error
        output, error = reader.communicate(timeout=20)
        assert reader.returncode == 0, error
        record, = json.loads(output)
        assert (record['version'], record['enabled'], record['consented_capabilities']) == ('2.0.0', False, [])
    finally:
        for process in (writer, reader):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=5)


@pytest.mark.parametrize('target', ['state', 'marker', 'transaction'])
def test_recovery_rejects_symlink_paths(plugins_home, tmp_path, target):
    source, old = prepared(tmp_path)
    assert run(CRASH, source, 'new').returncode == 73
    root = registry.plugins_dir()
    transaction, = root.glob('.install-*')
    outside = tmp_path / 'outside'
    if target == 'transaction':
        transaction.rename(outside)
        transaction.symlink_to(outside, target_is_directory=True)
    else:
        path = root / 'enabled.json' if target == 'state' else transaction / 'transaction.json'
        path.rename(outside)
        path.symlink_to(outside)
    before = outside.read_bytes() if outside.is_file() else (outside / 'transaction.json').read_bytes()
    cold = run(COLD, 'discover', source, old.id)
    assert cold.returncode != 0
    after = outside.read_bytes() if outside.is_file() else (outside / 'transaction.json').read_bytes()
    assert before == after


def test_crash_while_preparing_commit_marker_rolls_back(plugins_home, tmp_path):
    source, old = prepared(tmp_path)
    code = r'''
import json, os, sys
from pathlib import Path
from harness import plugin_registry as r
replace = os.replace
def interrupted(src, dst):
    if Path(dst).name == 'transaction.json' and json.loads(Path(src).read_text())['phase'] == 'committed':
        os._exit(73)
    replace(src, dst)
os.replace = interrupted
r.install_from_source(sys.argv[1], force=True)
'''
    assert run(code, source).returncode == 73
    for _ in range(2):
        cold = run(COLD, 'discover', source, old.id)
        assert cold.returncode == 0, cold.stderr
        record, = json.loads(cold.stdout)
        assert (record['version'], record['enabled'], record['consented_capabilities']) == ('1.2.3', True, ['fs'])
