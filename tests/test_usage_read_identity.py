from dataclasses import asdict

import pytest

from test_swarm_live_identity import collision, live
from test_api_usage_peel import _svc
from harness.api.usage import get_usage
from harness.api.cost import _job_swarm_accounting


def usage(states, configure=lambda svc, rows: None):
    svc, _ = _svc()
    rows = [{**asdict(s.store.get_job('job_collision')), 'source': 'harness' if i == 0 else 'cli',
             'cli_state_dir': str(s.store.root) if i else '', 'accounting_owned': True}
            for i, s in enumerate(states)]
    svc.scoped_jobs_with_stores = lambda **_: (rows, states[0].store, states[1].store)
    svc.job_swarm_accounting = _job_swarm_accounting
    from types import SimpleNamespace
    svc.swarm_registry = lambda: [SimpleNamespace(id=name, adapter_model_name=name, input_per_mtok_usd=1.,
        output_per_mtok_usd=2., billing='metered',
        marginal_cost_usd=lambda tin, tout: (tin + 2*tout)/1e6,
        estimate_cost_usd=lambda tin, tout: (tin + 2*tout)/1e6) for name in ('primary','cli','foreign')]
    svc.active_session_total = lambda keys, get, reg, reports: {'est_cost_usd': sum(_job_swarm_accounting(get(k), reg)[1] for k in keys)}
    svc.sum_job_set_savings_detail = lambda keys, get, reg, **kw: {'routing_tokens_compared': sum(_job_swarm_accounting(get(k), reg)[0] for k in keys)}
    configure(svc, rows)
    return get_usage('', svc)[1]


def test_usage_colliding_sqlite_jobs_keep_all_costs_and_savings(collision):
    result = usage(collision)
    assert [r['tokens'] for r in result['jobs']] == [110, 220, 330]
    assert len({r['job_ref']['state_id'] for r in result['jobs']}) == 3
    assert result['session']['swarm_cost_usd'] == pytest.approx(.00072)
    assert result['session_total']['est_cost_usd'] == pytest.approx(.00072)
    assert result['session']['routing_tokens_compared'] == 660


def test_usage_missing_foreign_never_borrows_primary(collision, monkeypatch):
    monkeypatch.setattr('harness.cli_job_merge.open_cli_durable_at', lambda _: None)
    result = usage(collision)
    assert [r.get('tokens') for r in result['jobs']] == [110, 220, None]
    assert result['jobs'][2]['read_status'] == 'unavailable'


@pytest.mark.parametrize('field', ['artifacts', 'tasks'])
def test_double_read_failure_is_visible_and_other_stores_survive(collision, monkeypatch, field):
    def fail(_):
        raise OSError('read failed')
    monkeypatch.setattr(collision[0].store, 'list_' + field + '_for_jobs', fail)
    monkeypatch.setattr(collision[0].store, 'list_' + field, fail)
    rows = live(collision)
    assert rows[0]['read_status'] == 'unavailable'
    assert field in rows[0]['unavailable_fields']
    assert [r['tokens'] for r in rows[1:]] == [220, 330]
    assert rows[1].get('read_status') != 'unavailable'


def test_usage_boot_window_and_active_scope_are_independent(collision):
    def configure(svc, rows):
        svc.boot_repos = lambda: {'A', 'B'}
        rows[0]['created_at'] = 'old'
        rows[1]['created_at'] = rows[2]['created_at'] = 'new'
        svc.job_in_cost_window = lambda created: created == 'new'
        svc.scoped_jobs_with_stores = lambda repo_root=None: (
            rows if repo_root == 'A' else rows[1:] if repo_root == 'B' else rows[:2],
            collision[0].store, collision[1].store)
    result = usage(collision, configure)
    assert [r['tokens'] for r in result['jobs']] == [220, 330]
    assert result['session']['routing_tokens_compared'] == 550
    assert result['session_total']['est_cost_usd'] == pytest.approx(.00036)


def test_usage_ledger_savings_stay_in_owning_store(collision):
    import json
    from harness.tool_output_savings import get_ledger
    get_ledger(str(collision[0].store.root)).record(session_id='session-a',
        tool_call_id='same', original_chars=400, compact_chars=0, job_id='job_collision')
    for n, state in enumerate(collision[1:], 2):
        (state.store.root / 'tool_output_savings.jsonl').write_text(json.dumps({
            'job_id': 'job_collision', 'tool_call_id': 'same', 'tokens_saved': n * 100,
            'original_chars': n * 400, 'compact_chars': 0}) + '\n')
    assert [r['tool_output_tokens_saved'] for r in usage(collision)['jobs']] == [100, 200, 300]


@pytest.mark.parametrize('collision', ['primary'], indirect=True)
def test_successful_empty_usage_is_distinct_from_failed_read(collision, monkeypatch):
    assert usage(collision)['jobs'][0]['tokens'] == 0
    def fail(_):
        raise OSError('unavailable')
    monkeypatch.setattr(collision[0].store, 'list_artifacts_for_jobs', fail)
    assert usage(collision)['jobs'][0]['tokens'] == 0
    monkeypatch.setattr(collision[0].store, 'list_artifacts', fail)
    failed = usage(collision)
    assert failed['jobs'][0]['read_status'] == 'unavailable'
    assert 'tokens' not in failed['jobs'][0]
    assert failed['session']['read_status'] == 'unavailable'
    assert failed['session_total']['read_status'] == 'unavailable'


def test_bulk_failure_keeps_successful_rows_within_same_store(collision, monkeypatch):
    from harness.cli_job_merge import bulk_load_store_artifacts
    store = collision[0].store
    original = store.list_artifacts
    def bulk_fail(_):
        raise OSError('bulk')
    def per_job(jid):
        if jid == 'bad':
            raise OSError('bad row')
        return original(jid)
    monkeypatch.setattr(store, 'list_artifacts_for_jobs', bulk_fail)
    monkeypatch.setattr(store, 'list_artifacts', per_job)
    unavailable = set()
    rows = bulk_load_store_artifacts(store, ['job_collision', 'bad', 'empty'], unavailable=unavailable)
    assert rows['job_collision']
    assert rows['empty'] == []
    assert unavailable == {'bad'}


def test_live_snapshot_failure_is_not_successful_empty():
    from types import SimpleNamespace
    from harness.api.jobs import get_swarm_live, make_job_services
    def fail(**_):
        raise OSError('snapshot unavailable')
    svc = make_job_services(cfg=SimpleNamespace(repo='', driver='test'),
        sessions=SimpleNamespace(active=''),
        get_session=lambda: SimpleNamespace(state=lambda: None), scoped_jobs_with_stores=fail)
    status, payload = get_swarm_live(None, svc)
    assert status == 200
    assert payload['read_status'] == 'unavailable'


def test_aggregate_failure_marks_partial_and_retries():
    svc, cache = _svc()
    original = svc.scoped_jobs_with_stores
    def fail(**kw):
        raise OSError('snapshot')
    svc.scoped_jobs_with_stores = fail
    failed = get_usage('', svc)[1]
    assert failed['session']['read_status'] == 'unavailable'
    assert failed['session']['est_cost_usd'] == .01
    assert not cache
    svc.scoped_jobs_with_stores = original
    assert get_usage('', svc)[1]['session'].get('read_status') != 'unavailable'
    assert cache


def test_session_only_failure_does_not_poison_boot_or_cache():
    svc, cache = _svc()
    def fail(*args):
        raise OSError('session')
    svc.active_session_total = fail
    failed = get_usage('', svc)[1]
    assert failed['session'].get('read_status') != 'unavailable'
    assert failed['session_total']['read_status'] == 'unavailable'
    assert not cache
    svc.active_session_total = lambda *args: {'session_id': 's', 'est_cost_usd': 2}
    assert get_usage('', svc)[1]['session_total']['est_cost_usd'] == 2
    assert cache


def test_accounting_failure_preserves_failed_identity_and_known_subtotals(collision):
    def configure(svc, rows):
        original = svc.job_swarm_accounting
        def accounting(arts, reg):
            result = original(arts, reg)
            if result[0] == 110:
                raise OSError('accounting')
            return result
        svc.job_swarm_accounting = accounting
    result = usage(collision, configure)
    assert result['jobs'][0]['read_status'] == 'unavailable'
    assert result['session']['read_status'] == 'unavailable'
    assert result['session']['swarm_cost_usd'] == pytest.approx(.0006)
    assert result['session']['job_coverage'] == {'expected': 3, 'read': 2}


def test_old_session_job_failure_does_not_blank_healthy_boot(collision, monkeypatch):
    cache = {}
    def fail(_):
        raise OSError('old job')
    monkeypatch.setattr(collision[0].store, 'list_artifacts_for_jobs', fail)
    monkeypatch.setattr(collision[0].store, 'list_artifacts', fail)
    def configure(svc, rows):
        rows[0]['created_at'] = 'old'
        svc.job_in_cost_window = lambda created: created != 'old'
        svc.usage_cache_put = lambda k, v: cache.__setitem__(k, v)
    result = usage(collision, configure)
    assert result['session'].get('read_status') != 'unavailable'
    assert result['session']['swarm_cost_usd'] == pytest.approx(.0006)
    assert result['session_total']['read_status'] == 'unavailable'
    assert not cache


def test_successful_session_cache_does_not_cross_session_identity():
    svc, cache = _svc()
    svc.active_session_total = lambda *args: {'session_id': 'A', 'est_cost_usd': 1}
    assert get_usage('', svc)[1]['session_total']['session_id'] == 'A'
    svc.active_session_total = lambda *args: {'session_id': 'B', 'est_cost_usd': 2}
    assert get_usage('', svc)[1]['session_total']['session_id'] == 'B'


@pytest.mark.parametrize('field', ['swarm_registry', 'sum_job_set_savings_detail', 'boot_usage_meters'])
def test_aggregate_error_branches_are_explicit_and_uncached(field):
    svc, cache = _svc()
    original = getattr(svc, field)
    def fail(*args, **kwargs):
        raise OSError(field)
    setattr(svc, field, fail)
    failed = get_usage('', svc)[1]
    assert failed['session']['read_status'] == 'unavailable'
    assert not cache
    setattr(svc, field, original)
    assert get_usage('', svc)[1]['session'].get('read_status') != 'unavailable'
    assert cache
