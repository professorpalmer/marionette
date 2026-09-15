from dataclasses import asdict

import pytest
from puppetmaster.models import Artifact, ArtifactType
from puppetmaster.store_factory import create_store

from harness.api.usage import get_usage
from test_api_usage_peel import _svc


@pytest.mark.parametrize('selected', [False, True])
def test_usage_reads_only_boot_and_session_artifacts(tmp_path, monkeypatch, selected):
    store = create_store('sqlite', tmp_path / 'jobs')
    rows = []
    expected = []
    for name, current, session, owned in [
        ('irrelevant', False, 'other', True),
        ('unowned', True, 'active', False),
        *([('boot', True, 'other', True), ('session', False, 'active', True)] if selected else []),
    ]:
        job = store.create_job(name, origin='marionette', session_id=session)
        store.save_artifact(Artifact(
            id='art-' + job.id, job_id=job.id, task_id='',
            type=ArtifactType.FINDING, payload={'claim': name},
            created_by='test', confidence=1, evidence=['test'],
        ))
        rows.append({**asdict(job), 'created_at': 'new' if current else 'old',
                     'source': 'harness', 'accounting_owned': owned})
        if name in ('boot', 'session'):
            expected.append(job.id)

    reads = []
    original = store.list_artifacts_for_jobs

    def load(ids):
        reads.extend(ids)
        return original(ids)

    monkeypatch.setattr(store, 'list_artifacts_for_jobs', load)
    svc, _ = _svc()
    svc.active_session_id = lambda: 'active'
    svc.job_in_cost_window = lambda created: created == 'new'
    svc.scoped_jobs_with_stores = lambda **_: (rows, store, None)
    seen_session = []

    def total(keys, artifacts, registry, reports):
        for key in keys:
            seen_session.extend(a.payload['claim'] for a in artifacts(key))
        return {'session_id': 'active'}

    svc.active_session_total = total
    status, payload = get_usage('', svc)
    assert status == 200
    assert [row['job_id'] for row in payload['jobs']] == expected[:1]
    assert seen_session == (['session'] if selected else [])
    assert sorted(reads) == sorted(expected)
    assert payload['session_total']['job_coverage'] == {
        'expected': int(selected), 'read': int(selected),
    }


@pytest.mark.parametrize('override', ['', 'third'])
def test_usage_reuses_same_repo_snapshot_only_within_request(override):
    svc, _ = _svc(repo='workspace')
    svc.boot_repos = lambda: {'workspace', 'other'}
    svc.active_session_total = lambda *args: {'session_id': 'active'}
    reads = []

    def scoped(repo_root=None):
        reads.append(repo_root or svc.cfg.repo)
        return [], None, None

    svc.scoped_jobs_with_stores = scoped
    expected = ['other', 'workspace'] + (['third'] if override else [])
    for _ in range(2):
        reads.clear()
        assert get_usage(override, svc)[0] == 200
        assert sorted(reads) == sorted(expected)
