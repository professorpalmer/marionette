"""Bounded operator facts through real native writers, without provider calls."""
import copy
import json

import pytest

pytest.importorskip('harness.job_readmodel', reason='requires companion PR2 metadata reader')

from harness.financial_receipt import apply_local_receipt, build_local_financial_receipt
from test_local_job_metadata import Runner, ctx, forbidden


def register(runner, jid='local-native'):
    runner._register_local_job(jid, 'PRIVATE FULL PROMPT', role='implement',
                               engine='native', model='test-model')
    return runner._local_jobs[jid]


def projected(runner, jid='local-native'):
    return runner.local_metadata_handle().rows[jid]


def test_real_finish_preserves_provider_spend_and_identity(tmp_path):
    runner = Runner(tmp_path)
    register(runner)
    runner._finish_local_job('local-native', ok=False, summary='PRIVATE OUTPUT',
                             tokens=123, est_cost_usd=0.125, model='test-model')
    result = projected(runner)
    assert result['economics']['kind'] == 'provider'
    assert result['economics']['spend_usd'] == 0.125
    assert result['economics']['estimated'] is False
    assert result['usage'] == dict(kind='reported', tokens=123, source='local_job_tokens')
    assert result['display'] == dict(label='Provider worker', model='native/test-model',
                                     adapter='native', truncated=False)
    assert 'PRIVATE' not in json.dumps(result)
    assert result['lifecycle'] == 'failed'  # Spend does not imply success.


def test_initial_and_finished_unknown_zero_are_not_observed_usage(tmp_path):
    runner = Runner(tmp_path)
    register(runner)
    assert projected(runner)['usage'] == {'kind': 'unknown'}
    assert projected(runner)['economics'] == {'kind': 'unavailable'}
    runner._finish_local_job('local-native', ok=True)
    assert projected(runner)['usage'] == {'kind': 'unknown'}
    assert projected(runner)['economics']['kind'] == 'unavailable'


def test_selected_existing_lane_has_bounded_header(tmp_path):
    runner = Runner(tmp_path)
    register(runner)
    index = runner.local_metadata_handle()
    result = index.read_selected(ctx(), index.ref('local-native'), lane='output')
    assert result['summary']['display']['model'] == 'native/test-model'
    assert result['summary']['local_ref'] == index.ref('local-native')
    assert result['page']['outcome'] == 'unavailable'
    assert result['cancellation_authority'] is False
    result['summary']['display']['model'] = 'mutated'
    assert projected(runner)['display']['model'] == 'native/test-model'


def test_real_finish_estimate_keeps_usage_distinct_from_pricing(tmp_path, monkeypatch):
    runner = Runner(tmp_path)
    register(runner)
    monkeypatch.setattr('pmharness.registry.resolve_price_with_source',
                        lambda model: (1.0, 2.0, 'static'))
    runner._finish_local_job('local-native', ok=True, tokens=1000)
    receipt = runner._local_jobs['local-native']['financial_receipt']
    result = projected(runner)
    assert receipt['spend_basis'] == result['economics']['kind'] == 'estimated'
    assert result['economics']['spend_usd'] == receipt['spend_usd']
    assert result['economics']['estimated'] is True
    assert result['usage']['tokens'] == 1000


@pytest.mark.parametrize('basis,provenance,estimated,spend,tokens', [
    ('provider', 'provider', False, 0.0, 10),
    ('provider', 'provider', False, 0.25, 10),
    ('measured', 'static', False, 0.25, 10),
    ('measured', 'live', False, 0.25, 10),
    ('estimated', 'static', True, 0.25, 10),
    ('unavailable', 'unknown', True, 0.0, 0),
])
def test_canonical_receipt_scalar_contract(tmp_path, basis, provenance, estimated, spend, tokens):
    runner = Runner(tmp_path)
    job = register(runner)
    # Exercise the canonical apply seam on an observed native row. In particular,
    # only a receipt with positive usage and explicit provider provenance can
    # represent provider zero; the ordinary finish signature cannot express it.
    receipt = build_local_financial_receipt('local-native', spend_usd=spend,
        estimated=estimated, cost_provenance=provenance, tokens=tokens,
        artifacts=[{'type': 'ROUTING', 'est_cost_usd': 0.8}], routing_saved_usd=0.1)
    assert receipt['spend_basis'] == basis
    job['tokens'] = tokens
    apply_local_receipt(job, receipt)
    result = projected(runner)['economics']
    assert result['kind'] == basis
    assert result['route_forecast_usd'] == 0.8
    assert result['estimated_savings_usd'] == 0.1
    if basis != 'unavailable':
        assert result['spend_usd'] == spend
    else:
        assert 'spend_usd' not in result


def test_real_finish_explicit_zero_gap_is_not_fabricated(tmp_path, monkeypatch):
    runner = Runner(tmp_path)
    register(runner)
    monkeypatch.setattr('pmharness.registry.resolve_price_with_source',
                        lambda model: (None, None, 'unknown'))
    runner._finish_local_job('local-native', ok=True, tokens=10, est_cost_usd=0.0)
    assert projected(runner)['economics']['kind'] == 'unavailable'
    assert projected(runner)['usage']['tokens'] == 10


def test_cancelled_result_and_restart_preserve_facts(tmp_path):
    runner = Runner(tmp_path)
    register(runner)
    runner._local_jobs['local-native']['status'] = 'cancelled'
    runner._update_cancelled_local_job_provenance('local-native', tokens=17, est_cost_usd=0.5)
    before = projected(runner)
    assert before['lifecycle'] == 'cancelled'
    assert before['economics']['kind'] == 'provider'
    restarted = Runner(tmp_path)
    after = projected(restarted)
    assert after['economics'] == before['economics']
    assert after['usage'] == before['usage']
    assert after['local_ref']['incarnation'] != before['local_ref']['incarnation']


@pytest.mark.parametrize('exclusion', [dict(accounting_owned=False),
                                      dict(accounting_scope='visibility_only')])
def test_accounting_exclusion_suppresses_receipt_and_usage(tmp_path, exclusion):
    from harness.job_scoping import annotate_job_accounting
    runner = Runner(tmp_path)
    job = register(runner)
    runner._finish_local_job('local-native', ok=True, tokens=10, est_cost_usd=0.25)
    # Use production accounting resolution to establish a visibility-only row.
    excluded = annotate_job_accounting(dict(job), active_session_id='another-session')
    assert excluded['accounting_owned'] is False
    job.update({key: excluded[key] for key in exclusion})
    result = projected(runner)
    assert result['accounting'] == dict(kind='excluded', aggregation_authority=False)
    assert result['economics'] == dict(kind='unavailable')
    assert result['usage'] == dict(kind='unknown')


def test_command_supplied_preview_and_provider_goal_never_become_labels(tmp_path):
    runner = Runner(tmp_path)
    runner._register_command_job('local-command', command='echo PRIVATE_SECRET',
                                 command_preview='PRIVATE_SECRET', action_id='a')
    register(runner)
    index = runner.local_metadata_handle()
    page = index.read_page(ctx())
    assert 'PRIVATE' not in json.dumps(page)
    assert projected(runner, 'local-command')['display']['label'] == 'Command'
    for lane in ('actions', 'children', 'output'):
        detail = index.read_selected(ctx(), index.ref('local-command'), lane=lane)
        assert 'PRIVATE' not in json.dumps(detail.get('summary'))
    runner._refresh_local_job_routed_model('local-native', 'selected-new', engine='agentic')
    assert projected(runner)['display']['model'] == 'agentic/selected-new'


class NoTraversal(dict):
    def __iter__(self):
        forbidden()
    items = values = keys = __iter__
    def __deepcopy__(self, memo):
        forbidden()


class SliceFirst(str):
    def encode(self, *args, **kwargs):
        forbidden()
    def strip(self, *args, **kwargs):
        forbidden()
    def __str__(self):
        forbidden()
    def __getitem__(self, key):
        assert isinstance(key, slice) and key.stop <= 160
        return super().__getitem__(key)


def test_writer_bounds_before_copy_and_poll_never_reads_history(tmp_path, monkeypatch):
    runner = Runner(tmp_path)
    register(runner)
    runner._finish_local_job('local-native', ok=True, tokens=10, est_cost_usd=0.25)
    original = dict(runner._local_jobs['local-native'])
    original.update(model=SliceFirst('\U0001f600' * 100000),
                    adapter=SliceFirst('a' * 100000),
                    goal=NoTraversal(secret='PRIVATE'),
                    output='PRIVATE' * 100000,
                    artifacts=[NoTraversal(secret='PRIVATE')],
                    worker_provenance=NoTraversal(secret='PRIVATE'))
    receipt_values = dict(original['financial_receipt'])
    receipt = NoTraversal(receipt_values)
    receipt['nested'] = NoTraversal(secret='PRIVATE')
    original['financial_receipt'] = receipt
    monkeypatch.setattr(copy, 'deepcopy', forbidden)
    for i in range(1100):
        jid = f'local-{i:04d}'
        data = dict(original, id=jid, status='running' if i % 2 else 'completed',
                    financial_receipt=NoTraversal(receipt_values, job_id=jid, nested=receipt['nested']))
        runner._local_jobs[jid] = data
    index = runner.local_metadata_handle()
    assert projected(runner, 'local-0000')['display']['truncated'] is True
    assert len(projected(runner, 'local-0000')['display']['model']) == 160
    monkeypatch.setattr(runner, 'live_local_jobs', forbidden)
    monkeypatch.setattr(runner, 'get_local_job', forbidden)
    monkeypatch.setattr('builtins.open', forbidden)
    monkeypatch.setattr('harness.financial_receipt.build_local_financial_receipt', forbidden)
    monkeypatch.setattr('harness.financial_receipt.apply_local_receipt', forbidden)
    class NoBody(NoTraversal):
        get = __getitem__ = lambda *args: forbidden()
    runner._local_jobs = NoBody(runner._local_jobs)
    ids = []
    for lane in ('active', 'history'):
        cursor = None
        while True:
            page = index.read_page(ctx(), cursor=cursor, lane=lane)
            assert page['page']['scanned'] <= 51 and len(page['rows']) <= 50
            assert len(json.dumps(page).encode()) <= 32768
            assert 'PRIVATE' not in json.dumps(page)
            if lane == 'history':
                ids.extend(row['local_ref']['job_id'] for row in page['rows'])
            cursor = page['page']['next_cursor']
            if not cursor:
                break
    assert len(ids) == len(set(ids)) == 1101


def test_malformed_receipt_never_becomes_known_cost(tmp_path):
    runner = Runner(tmp_path)
    job = register(runner)
    receipt = build_local_financial_receipt('local-native', spend_usd=0.25,
                                            provider_cost_usd=0.25)
    for change in (dict(job_id='other'), dict(owner='pm'), dict(spend_usd=float('nan')),
                   dict(spend_usd=True), dict(spend_usd=-1), dict(spend_usd=10**1000),
                   dict(spend_basis={'nested': 'provider'}), dict(estimated=True),
                   dict(cost_provenance='provider' + 'x' * 100000)):
        job['financial_receipt'] = dict(receipt, **change)
        assert projected(runner)['economics']['kind'] == 'unavailable'


def test_enriched_selected_detail_maximum_unicode_payload_stays_bounded(tmp_path):
    runner = Runner(tmp_path)
    job = register(runner)
    job.update(model='\U0001f600' * 1000, adapter='\U0001f600' * 1000,
               output='\U0001f600' * 100000,
               actions=[dict(action_id=str(i), kind='edit', goal='\U0001f600' * 10000,
                             error='\U0001f600' * 10000) for i in range(100)],
               child_job_ids=['local-' + 'x' * 240] * 100)
    index = runner.local_metadata_handle()
    for lane in ('output', 'actions', 'children'):
        result = index.read_selected(ctx(), index.ref('local-native'), lane=lane)
        assert result['page']['outcome'] == 'partial'
        assert len(json.dumps(result).encode()) <= 32768
        assert result['page']['scanned'] <= 51
        assert len(result['rows']) <= 50
        assert result['summary']['display']['truncated'] is True


def test_recorded_native_events_keep_active_revision_and_unknown_economics(tmp_path):
    runner = Runner(tmp_path)
    register(runner)
    index = runner.local_metadata_handle()
    before = index.active_revision
    runner._ingest_local_job_events('local-native', [
        {'kind': 'action_start', 'data': {'id': 'read-1', 'kind': 'read', 'goal': 'inspect file'}},
        {'kind': 'action_end', 'data': {'id': 'read-1', 'kind': 'read', 'status': 'completed'}},
    ])
    changes = index.read_page(ctx(), lane='active', mode='changes', after_revision=before)
    assert changes['page']['outcome'] == 'complete'
    assert changes['rows'][-1]['action_count'] == 1
    assert changes['rows'][-1]['economics']['kind'] == 'unavailable'
    assert changes['rows'][-1]['usage']['kind'] == 'unknown'
    ref = index.ref('local-native')
    detail = index.read_selected(ctx(), ref, lane='actions')
    assert detail['summary']['local_ref'] == ref
    assert detail['rows'][0]['action_id'] == 'read-1'
    hidden = index.read_selected(dict(ctx(), session_id='B'), ref, lane='actions')
    assert 'summary' not in hidden
    expired = index.read_selected(ctx(), dict(ref, incarnation='other'), lane='actions')
    assert 'summary' not in expired


@pytest.mark.parametrize('known,tokens,expected', [(True, 0, 'reported'), (False, 10, 'unknown'),
                                                 (None, 0, 'unknown'), (True, 10, 'reported')])
def test_native_finish_persisted_usage_known_flag(tmp_path, monkeypatch, known, tokens, expected):
    runner = Runner(tmp_path)
    register(runner)
    monkeypatch.setattr('pmharness.registry.resolve_price_with_source',
                        lambda model: (None, None, 'unknown'))
    runner._finish_local_job('local-native', ok=False, tokens=tokens,
                             worker_provenance={'usage_known': known})
    assert runner._local_jobs['local-native']['worker_provenance']['usage_known'] is known
    result = projected(runner)
    assert result['usage']['kind'] == expected
    if expected == 'reported':
        assert result['usage']['tokens'] == tokens
    else:
        assert 'tokens' not in result['usage']
    assert result['economics']['kind'] == 'unavailable'
    restarted = Runner(tmp_path)
    assert projected(restarted)['usage'] == result['usage']


def test_enriched_fields_reach_existing_api_without_parser_changes(tmp_path):
    from harness.api.job_readmodel import get_local_metadata, get_local_metadata_detail
    from harness.job_readmodel import ActiveContext, KnownSources, MetadataReader, ReadContext
    from dataclasses import asdict
    def query(context, **kwargs):
        return {k: [str(v)] for k, v in dict(asdict(context), **kwargs).items()}
    runner = Runner(tmp_path)
    register(runner)
    runner._finish_local_job('local-native', ok=True, tokens=12, est_cost_usd=0.3)
    index = runner.local_metadata_handle()
    context = ReadContext(**ctx())
    reader = MetadataReader(lambda: ActiveContext('A', '/repo', 'generation'), KnownSources(()), index)
    status, page = get_local_metadata(query(context, lane='history'), reader)
    assert status == 200 and page['rows'][0]['economics']['spend_usd'] == 0.3
    for lane in ('actions', 'children', 'output'):
        status, detail = get_local_metadata_detail(query(context, job_id='local-native',
            incarnation=index.incarnation, lane=lane), reader)
        assert status == 200
        assert detail['summary']['display']['model'] == 'native/test-model'
        assert detail['summary']['economics']['kind'] == 'provider'
        assert len(json.dumps(detail).encode()) <= 32768
