"""Compatibility refusal and exact native selection at the public boundary."""
from dataclasses import dataclass
from types import SimpleNamespace
from urllib.parse import urlencode, urlparse

import pytest
from harness.api import jobs, scoped_cancellation


def test_old_public_job_ref_refuses_without_store_reads(monkeypatch):
    @dataclass
    class OldRef:
        job_id: str
        state_id: str
    real = scoped_cancellation.import_module
    monkeypatch.setattr(scoped_cancellation, 'import_module', lambda name:
        SimpleNamespace(JobRef=OldRef) if name == 'puppetmaster.contracts' else real(name))
    assert not scoped_cancellation.runtime_available()
    svc = SimpleNamespace(get_session=lambda: pytest.fail('opened unsupported store'))
    body = {'selection': {'version': 2, 'source': 'harness', 'session_id': 'A', 'repo': '/repo',
        'job_ref': {'job_id': 'job_a', 'state_id': 'state_a', 'version': 2, 'incarnation': 'a'}}}
    assert jobs.post_swarm_cancel(body, svc) == (503, {'ok': False, 'code': 'scoped_cancellation_unsupported'})


def test_missing_candidate_module_is_explicitly_unavailable(monkeypatch):
    def missing(_):
        raise ModuleNotFoundError('unsupported contracts')
    monkeypatch.setattr(scoped_cancellation, 'import_module', missing)
    assert not scoped_cancellation.runtime_available()
    assert scoped_cancellation.cancellation_view(None, None, [])['reason'] == 'scoped_cancellation_unsupported'


def test_id_only_cancel_never_discovers_or_reads_a_store():
    assert jobs.post_swarm_cancel({'job_id': 'job_unselected'}, object())[0] == 409
    assert jobs.post_swarm_cancel({'job_id': 'local-unselected'}, object())[0] == 409


def test_receipt_route_preserves_blank_duplicates():
    import json
    from harness.http_routes import build_get_routes
    class Services:
        def __getattr__(self, _):
            return lambda: None
    sent = []
    handler = SimpleNamespace(_send=lambda status, body: sent.append((status, json.loads(body))))
    values = dict(job_id='job_a', state_id='state_a', source='harness', repo='/repo',
                  session_id='A', request_id='r', version='2', incarnation='a')
    raw = '/api/swarm/cancellation-receipt?' + urlencode(values) + '&job_id='
    build_get_routes(Services())['/api/swarm/cancellation-receipt'](
        handler, urlparse(raw), {k: [v] for k, v in values.items()})
    assert sent[-1][0] == 400


def test_local_incarnation_required_and_checked_at_effect_boundary(tmp_path):
    import threading
    from harness.local_jobs import LocalJobsMixin
    pilot = LocalJobsMixin()
    pilot.harness_session_id = 'A'
    pilot.config = SimpleNamespace(repo=str(tmp_path), driver='stub')
    pilot._local_jobs = {}
    pilot._local_job_cancels = {}
    pilot._local_jobs_lock = threading.Lock()
    pilot._local_jobs_path = str(tmp_path / 'jobs.json')
    pilot._load_local_jobs()
    pilot._register_local_job('local-one', 'work', skip_routing_preview=True)
    svc = jobs.make_job_services(cfg=pilot.config, sessions=SimpleNamespace(active='A'),
                                get_pilot=lambda: pilot)
    selection = dict(version=1, source='local', session_id='A', repo=str(tmp_path),
                     job_ref=dict(job_id='local-one', state_id=None))
    assert jobs.post_swarm_cancel({'selection': selection}, svc)[0] == 409
    selection['local_incarnation'] = 'stale'
    assert jobs.post_swarm_cancel({'selection': selection}, svc)[0] == 409
    assert not pilot._local_job_cancels['local-one'].is_set()
    selection['local_incarnation'] = pilot.local_metadata_handle().incarnation
    original = pilot.cancel_local_job
    def replaced(jid, *, incarnation):
        pilot._local_metadata.incarnation = 'replacement'
        return original(jid, incarnation=incarnation)
    pilot.cancel_local_job = replaced
    assert jobs.post_swarm_cancel({'selection': selection}, svc)[0] == 409
    assert not pilot._local_job_cancels['local-one'].is_set()
    pilot.cancel_local_job = original
    selection['local_incarnation'] = 'replacement'
    assert jobs.post_swarm_cancel({'selection': selection}, svc)[0] == 200
    assert pilot._local_job_cancels['local-one'].is_set()
