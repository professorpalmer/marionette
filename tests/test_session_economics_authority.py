from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from puppetmaster.models import Artifact, ArtifactType, Task
from puppetmaster.store_factory import create_store

import harness.server

from harness.api import usage_meters
from harness.api.usage import get_usage
from harness.sessions import SessionStore
from test_api_usage_peel import _svc


@pytest.fixture
def economics(tmp_path, monkeypatch):
    sessions = SessionStore(str(tmp_path / 'sessions.json'))
    sid = sessions.create()['id']
    sessions.accumulate_meters(sid, input_tokens=1000, output_tokens=200,
                              cache_read_tokens=400, nominal_cost_usd=2,
                              billing='plan', value_complete=True)
    monkeypatch.setattr(usage_meters, '_sessions', lambda: sessions)
    store = create_store('sqlite', tmp_path / 'jobs')
    svc, _ = _svc(pilot=SimpleNamespace(harness_session_id=sid))
    svc.active_session_total = usage_meters._active_session_total
    svc.active_session_id = lambda: sid
    rows = []
    svc.scoped_jobs_with_stores = lambda **_: (rows, store, None)
    svc.swarm_registry = lambda: [SimpleNamespace(
        id='model', adapter_model_name='model', billing='api',
        input_per_mtok_usd=1., output_per_mtok_usd=2.,
        estimate_cost_usd=lambda tin, tout: (tin + 2 * tout) / 1e6)]

    def add(billing='plan', provider=0, model='model', replay=False):
        job = store.create_job('worker', origin='marionette', session_id=sid)
        task = Task(id='task-' + job.id, job_id=job.id, role='worker', instruction='work')
        store.save_task(task)
        for n, (kind, payload) in enumerate([
            (ArtifactType.ROUTING, {'model_id': model, 'billing': billing, 'adapter': 'codex', 'policy': 'fixture'}),
            (ArtifactType.VERIFICATION, {'check': 'usage', 'result': 'ok', 'model': model, 'tokens_in': 100,
             'tokens_out': 20, 'real_cost_usd': provider, 'tokens_estimated': False}),
        ]):
            store.save_artifact(Artifact(id=f'{job.id}-{n}', job_id=job.id,
                task_id=task.id, type=kind, payload=payload, confidence=1,
                created_by='router', evidence=['fixture usage']))
        row = {**asdict(job), 'source': 'harness', 'accounting_owned': True}
        rows.append(row)
        if replay:
            rows.append(row)
        return job

    return sessions, sid, svc, add, store


def test_plan_worker_nominal_and_marginal_are_separate(economics):
    _, _, svc, add, _ = economics
    add(replay=True)
    total = get_usage('', svc)[1]['session_total']
    assert total['nominal_cost_usd'] == pytest.approx(2.00014)
    assert total['est_cost_usd'] == 0
    assert total['list_price_complete'] is True
    assert total['cost_source'] == 'plan_estimated'
    assert total['tokens_used'] == 1200
    assert total['prompt_cache_hit_ratio'] == .4
    assert total['job_coverage'] == {'expected': 1, 'read': 1}


def test_provider_spend_is_not_catalog_nominal(economics):
    _, _, svc, add, _ = economics
    add(billing='api', provider=.75)
    total = get_usage('', svc)[1]['session_total']
    assert total['est_cost_usd'] == .75
    assert total['nominal_cost_usd'] == pytest.approx(2.00014)
    assert total['list_price_complete'] is True


def test_missing_catalog_keeps_nominal_incomplete(economics):
    _, _, svc, add, _ = economics
    add(billing='api', provider=.75, model='retired')
    total = get_usage('', svc)[1]['session_total']
    assert total['est_cost_usd'] == .75
    assert total['nominal_cost_usd'] == 2
    assert total['list_price_complete'] is False


def test_invalid_cache_ratio_is_unknown(economics):
    sessions, sid, svc, _, _ = economics
    sessions.accumulate_meters(sid, cache_read_tokens=2000,
                              nominal_cost_usd=0, value_complete=True)
    assert get_usage('', svc)[1]['session_total']['prompt_cache_hit_ratio'] is None


def test_report_failure_preserves_session_subtotal(economics, monkeypatch):
    _, _, svc, add, _ = economics
    add()
    def fail(*args, **kwargs):
        raise OSError('financial report unavailable')
    monkeypatch.setattr('harness.financial_receipt.load_pm_cost_report', fail)
    total = get_usage('', svc)[1]['session_total']
    assert total['read_status'] == 'unavailable'
    assert total['nominal_cost_usd'] == 2
    assert total['list_price_complete'] is False


def test_boot_store_failure_does_not_taint_session(economics):
    _, _, svc, add, _ = economics
    add()
    read = svc.scoped_jobs_with_stores
    svc.boot_repos = lambda: {'unrelated'}
    def scoped(repo_root=None):
        if repo_root == 'unrelated':
            raise OSError('unrelated boot store')
        return read()
    svc.scoped_jobs_with_stores = scoped
    payload = get_usage('', svc)[1]
    assert payload['session']['read_status'] == 'unavailable'
    assert payload['session_total'].get('read_status') is None
    assert payload['session_total']['list_price_complete'] is True


def test_legacy_terminal_job_does_not_reprice_at_current_catalog(economics):
    _, _, svc, add, store = economics
    job = add(billing='api')
    store.save_job(replace(store.get_job(job.id), status='complete', cost_receipt=None))
    total = get_usage('', svc)[1]['session_total']
    assert total['est_cost_usd'] == 0
    assert total['nominal_cost_usd'] == 2
    assert total['list_price_complete'] is False
    assert total['read_status'] == 'unavailable'


def test_partial_reports_keep_known_job_subtotals(economics):
    _, _, svc, add, _ = economics
    add(billing='api', provider=.75)
    add(billing='unknown')
    total = get_usage('', svc)[1]['session_total']
    assert total['est_cost_usd'] == .75
    assert total['nominal_cost_usd'] == pytest.approx(2.00028)
    assert total['list_price_complete'] is False
    assert total['read_status'] == 'unavailable'


def test_report_read_coverage_excludes_failed_report(economics, monkeypatch):
    from harness.financial_receipt import load_pm_cost_report
    _, _, svc, add, _ = economics
    add(billing='api', provider=.75)
    failed = add()
    def report(store, jid, **kwargs):
        if jid == failed.id:
            raise OSError('report unavailable')
        return load_pm_cost_report(store, jid, **kwargs)
    monkeypatch.setattr('harness.financial_receipt.load_pm_cost_report', report)
    total = get_usage('', svc)[1]['session_total']
    assert total['est_cost_usd'] == .75
    assert total['job_coverage'] == {'expected': 2, 'read': 1}
    assert total['read_status'] == 'unavailable'
