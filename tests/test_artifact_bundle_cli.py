from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from harness.artifact_bundle_cli import main


@pytest.fixture
def stored(tmp_path):
    from puppetmaster.store_factory import create_store
    from puppetmaster.models import Artifact, ArtifactType, Task
    state = tmp_path / 'state'
    store = create_store('sqlite', state, mode='ensure')
    job = store.create_job('bundle fixture', label=json.dumps({'origin': 'marionette', 'session_id': 'local-session'}))
    task = Task(job_id=job.id, role='test', instruction='local fixture')
    store.save_task(task)
    artifact = Artifact(job_id=job.id, task_id=task.id, type=ArtifactType.FINDING,
                        created_by='fixture', payload={'claim': 'local fixture'},
                        confidence=1.0, evidence=['temporary file'])
    store.save_artifact(artifact)
    return state, store, job, artifact


def test_real_cli_export_import_no_store_mutation(stored, tmp_path):
    state, store, job, artifact = stored
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'selected.txt').write_bytes(b'portable local bytes')
    bundle = tmp_path / 'bundle'
    common = ['--state-dir', str(state), '--job', job.id, '--session', 'local-session', '--bundle', str(bundle)]
    before = asdict(store.get_artifacts_by_ids(job.id, [artifact.id])[artifact.id])
    command = [sys.executable, '-I', '-c',
               'import sys; sys.path.insert(0, sys.argv.pop(1)); from harness.cli import main; raise SystemExit(main())',
               str(Path(__file__).resolve().parents[1]), 'artifact-bundle']
    result = subprocess.run(command + ['export'] + common + ['--artifact', artifact.id, '--root', str(source), '--file', 'selected.txt'],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['entries'] == 2
    output = tmp_path / 'restored'
    result = subprocess.run(command + ['import'] + common + ['--destination', str(output)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    restored = json.loads((output / 'artifacts' / (artifact.id + '.json')).read_text())
    assert restored == before
    assert (output / 'files/selected.txt').read_bytes() == b'portable local bytes'
    assert asdict(store.get_artifacts_by_ids(job.id, [artifact.id])[artifact.id]) == before
    manifest = json.loads((output / 'manifest.json').read_text())
    for entry in manifest['entries']:
        assert hashlib.sha256((output / entry['path']).read_bytes()).hexdigest() == entry['sha256']


def test_foreign_session_and_missing_artifact_rejected(stored, tmp_path):
    state, store, job, artifact = stored
    common = ['--state-dir', str(state), '--job', job.id, '--bundle', str(tmp_path / 'bundle')]
    assert main(['export'] + common + ['--session', 'foreign', '--artifact', artifact.id]) == 1
    assert main(['export'] + common + ['--session', 'local-session', '--artifact', 'missing']) == 1
    assert not (tmp_path / 'bundle').exists()


def test_selected_artifact_change_blocks_manifest(stored, tmp_path, monkeypatch):
    import harness.artifact_bundle_cli as cli
    state, store, job, artifact = stored
    original = cli._selected
    reads = 0
    def change_after_first(*args):
        nonlocal reads
        reads += 1
        if reads == 2:
            saved = store.get_artifacts_by_ids(job.id, [artifact.id])[artifact.id]
            store.save_artifact(replace(saved, payload={'claim': 'changed'}, sha256=None))
        return original(*args)
    monkeypatch.setattr(cli, '_selected', change_after_first)
    bundle = tmp_path / 'bundle'
    assert main(['export', '--state-dir', str(state), '--job', job.id, '--session', 'local-session',
                 '--bundle', str(bundle), '--artifact', artifact.id]) == 1
    assert not (bundle / 'manifest.json').exists()


def _run_bundle(*args):
    return subprocess.run(
        [sys.executable, '-I', '-c',
         'import sys; sys.path.insert(0, sys.argv.pop(1)); from harness.cli import main; raise SystemExit(main())',
         str(Path(__file__).resolve().parents[1]), 'artifact-bundle', *map(str, args)],
        capture_output=True, text=True, timeout=30)


def test_same_job_and_session_in_distinct_stores_reject_crossover(tmp_path, monkeypatch):
    import puppetmaster.models as models
    from puppetmaster.state import state_identity
    from puppetmaster.store_factory import create_store

    original = models.new_id
    monkeypatch.setattr(models, 'new_id', lambda prefix: 'job-collision' if prefix == 'job' else original(prefix))
    states = [tmp_path / 'origin', tmp_path / 'unrelated']
    for state in states:
        store = create_store('sqlite', state, mode='ensure')
        job = store.create_job('collision fixture', label=json.dumps(
            {'origin': 'marionette', 'session_id': 'same-session'}))
        assert job.id == 'job-collision'
    assert state_identity(states[0]) != state_identity(states[1])
    source = tmp_path / 'payload.txt'
    source.write_bytes(b'origin bytes')
    bundle = tmp_path / 'bundle'
    common = ['--job', 'job-collision', '--session', 'same-session', '--bundle', bundle]
    result = _run_bundle('export', '--state-dir', states[0], *common,
                         '--root', tmp_path, '--file', source.name)
    assert result.returncode == 0, result.stderr
    output = tmp_path / 'wrong-store-output'
    result = _run_bundle('import', '--state-dir', states[1], *common, '--destination', output)
    assert result.returncode == 1, result.stdout
    assert 'JobRef mismatch' in result.stderr
    assert not output.exists()
    before = (bundle / 'manifest.json').read_bytes()
    result = _run_bundle('export', '--state-dir', states[1], *common,
                         '--root', tmp_path, '--file', source.name, '--resume')
    assert result.returncode == 1
    assert 'resume selection differs' in result.stderr
    assert (bundle / 'manifest.json').read_bytes() == before


def test_relocated_bundle_repeated_offline_import_same_origin(stored, tmp_path):
    from puppetmaster.models import JobRef
    from puppetmaster.state import state_identity

    state, store, job, artifact = stored
    before_job = asdict(store.get_job(job.id))
    before_tasks = [asdict(task) for task in store.list_tasks(job.id)]
    before_artifact = asdict(store.get_artifacts_by_ids(job.id, [artifact.id])[artifact.id])
    source = tmp_path / 'original-files'
    source.mkdir()
    (source / 'selected.txt').write_bytes(b'offline bytes')
    origin = state_identity(state)
    assert state_identity(state / '..' / state.name) == origin
    for attempt in range(1):
        bundle = tmp_path / ('bundle-' + str(attempt))
        result = _run_bundle('export', '--state-dir', state, '--job', job.id,
                             '--session', 'local-session', '--bundle', bundle,
                             '--artifact', artifact.id, '--root', source, '--file', 'selected.txt')
        assert result.returncode == 0, result.stderr
        response = json.loads(result.stdout)
        assert response['job_ref'] == JobRef(job_id=job.id, state_id=origin).as_dict()
        assert response['session_id'] == 'local-session'
        relocated = tmp_path / ('relocated-' + str(attempt))
        bundle.rename(relocated)
        hidden_source = tmp_path / 'unavailable-source'
        source.rename(hidden_source)
        for copy in range(2):
            output = tmp_path / ('restored-' + str(attempt) + '-' + str(copy))
            result = _run_bundle('import', '--state-dir', state / '..' / state.name,
                                 '--job', job.id, '--session', 'local-session',
                                 '--bundle', relocated, '--destination', output)
            assert result.returncode == 0, result.stderr
            assert (output / 'files/selected.txt').read_bytes() == b'offline bytes'
            assert json.loads((output / 'artifacts' / (artifact.id + '.json')).read_text()) == before_artifact
        hidden_source.rename(source)
    assert asdict(store.get_job(job.id)) == before_job
    assert [asdict(task) for task in store.list_tasks(job.id)] == before_tasks
    assert asdict(store.get_artifacts_by_ids(job.id, [artifact.id])[artifact.id]) == before_artifact
