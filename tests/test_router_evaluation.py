import copy
from dataclasses import replace
import json
from pathlib import Path

import pytest
from puppetmaster.cost import PRICING_SOURCE_TERMINAL
from puppetmaster.models import Artifact, ArtifactType, JobStatus, Task, TaskStatus
from puppetmaster.store_factory import create_store

from bench.router_evaluation import QUESTIONS, compare, ingest, main, manifest, run_record, seal

REPO = Path(__file__).resolve().parents[1]


def reseal(value):
    return seal({k: v for k, v in value.items() if k != 'sha256'})


def _build_runs(root):
    snapshot = manifest(REPO)
    store = create_store('sqlite', root / 'state')
    records = []
    for row in snapshot['tasks']:
        if row['split'] != 'heldout':
            continue
        q = QUESTIONS[row['id']]
        job = store.create_job(q.task_prompt, label='router-evaluation-contract-fixture')
        task = Task(job_id=job.id, role='test', instruction=q.task_prompt, status=TaskStatus.COMPLETE)
        store.save_task(task)
        claim = json.dumps({'question_id': q.id, 'answer': {f.name: f.expected for f in q.facts}})
        store.save_artifact(Artifact(job_id=job.id, task_id=task.id, type=ArtifactType.FINDING,
            created_by='fixture', payload={'claim': claim}, confidence=1, evidence=['deterministic fixture']))
        receipt = {'job_id': job.id, 'pricing_source': PRICING_SOURCE_TERMINAL,
                   'token_usage': {}, 'tasks': [], 'actual_cost': {
                       'total_marginal_cost_usd': 0, 'priced_tasks': 1, 'unpriced_tasks': 0}}
        store.save_job(replace(job, status=JobStatus.COMPLETE, cost_receipt=receipt))
        records.append(ingest(store, job.id, q.id, snapshot['sha256']))
    left = run_record('baseline', {'routing': 'baseline'}, snapshot, records, origin='fixture')
    candidate_records = []
    for record in records:
        job = store.create_job('candidate fixture')
        task = Task(job_id=job.id, role='test', instruction='fixture', status=TaskStatus.COMPLETE)
        store.save_task(task)
        store.save_artifact(Artifact(job_id=job.id, task_id=task.id, type=ArtifactType.FINDING,
            created_by='fixture', payload=record['artifacts'][0]['payload'], confidence=1,
            evidence=['candidate fixture']))
        receipt = copy.deepcopy(record['cost_receipt'])
        receipt['job_id'] = job.id
        store.save_job(replace(job, status=JobStatus.COMPLETE, cost_receipt=receipt))
        candidate_records.append(ingest(store, job.id, record['task_id'], snapshot['sha256']))
    right = run_record('candidate', {'routing': 'candidate'}, snapshot, candidate_records, origin='fixture')
    return snapshot, left, right, store


@pytest.fixture(scope='module')
def _shared_runs(tmp_path_factory):
    return _build_runs(tmp_path_factory.mktemp('router_evaluation'))


@pytest.fixture
def runs(_shared_runs):
    snapshot, left, right, store = _shared_runs
    return copy.deepcopy(snapshot), copy.deepcopy(left), copy.deepcopy(right), store


def test_public_store_and_cli_contract(tmp_path, capsys):
    snapshot, left, right, store = _build_runs(tmp_path)
    for name, value in [('manifest', snapshot), ('left', left), ('right', right)]:
        (tmp_path / name).write_text(json.dumps(value))
    assert main(['ingest', str(tmp_path / 'manifest'), str(store.root),
                 left['records'][0]['job_ref']['job_id'], left['records'][0]['task_id'], '--repo', str(REPO)]) == 0
    captured = json.loads(capsys.readouterr().out)
    assert captured == left['records'][0]
    assert captured['job_ref']['state_id'].startswith('state_')
    assert captured['cost_receipt']['pricing_source'] == PRICING_SOURCE_TERMINAL
    assert main(['compare', str(tmp_path / 'manifest'), str(tmp_path / 'left'),
                 str(tmp_path / 'right'), '--repo', str(REPO)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['verdict'] == 'INCONCLUSIVE'
    assert report['paired_n'] == 6
    assert report['policies'][0]['successes'] == 6
    assert report['policies'][0]['selected_marginal_usd'] == 0
    assert report['policies'][0]['all_attempt_marginal_usd'] is None
    assert report['savings_fraction'] is None


@pytest.mark.parametrize('claim', ['Everything passed, excellent result',
    json.dumps({'question_id': 'repair_fn', 'answer': {'function': 'drive_with_repair'}})])
def test_prose_and_partial_facts_fail(runs, claim):
    snapshot, left, right, _ = runs
    row = left['records'][0]
    row['artifacts'][0]['payload']['claim'] = claim
    left['records'][0] = reseal(row)
    report = compare(snapshot, reseal(left), right, REPO)
    assert report['policies'][0]['successes'] == 5
    assert report['policies'][0]['failures'] == 1
    assert report['policies'][0]['n'] == 6


@pytest.mark.parametrize('amount', [None, -1, True])
def test_unpriced_stays_in_denominator(runs, amount):
    snapshot, left, right, _ = runs
    row = left['records'][0]
    row['cost_receipt']['actual_cost']['total_marginal_cost_usd'] = amount
    left['records'][0] = reseal(row)
    result = compare(snapshot, reseal(left), right, REPO)['policies'][0]
    assert result['n'] == 6
    assert result['unknown_selected_cost'] == 1
    assert result['selected_marginal_usd'] is None


def test_unequal_coverage_refuses_pairs(runs):
    snapshot, left, right, _ = runs
    left['records'].pop()
    report = compare(snapshot, reseal(left), right, REPO)
    assert report['paired_n'] == 0
    assert report['policies'][0]['n'] == 6
    assert report['policies'][0]['missing'] == 1


def test_snapshot_and_content_mismatch(runs):
    snapshot, left, right, _ = runs
    left['records'][0]['snapshot_sha256'] = 'foreign'
    with pytest.raises(ValueError, match='hash mismatch'):
        compare(snapshot, reseal(left), right, REPO)
    left['records'][0] = reseal(left['records'][0])
    with pytest.raises(ValueError, match='snapshot mismatch'):
        compare(snapshot, reseal(left), right, REPO)


def test_total_failure_and_missing_attempt_cost(runs):
    snapshot, left, right, _ = runs
    for i, row in enumerate(left['records']):
        row['job_status'] = 'failed'
        left['records'][i] = reseal(row)
    report = compare(snapshot, reseal(left), right, REPO)
    result = report['policies'][0]
    assert result['successes'] == 0
    assert result['failures'] == 6
    assert result['cost_per_verified_completion_usd'] is None
    assert result['unknown_attempt_cost'] == 6


def test_duplicate_and_training_task_refused(runs):
    snapshot, left, right, _ = runs
    left['records'].append(left['records'][0])
    with pytest.raises(ValueError, match='duplicate'):
        compare(snapshot, reseal(left), right, REPO)


def test_complete_latency_tail_and_unpriced_receipt(runs):
    snapshot, left, right, _ = runs
    for i, row in enumerate(left['records']):
        row['latency_ms'] = i + 1
        row['cost_receipt']['actual_cost']['unpriced_tasks'] = 1
        left['records'][i] = reseal(row)
    result = compare(snapshot, reseal(left), right, REPO)['policies'][0]
    assert result['latency_p95_ms'] == 6
    assert result['latency_samples'] == 6
    assert result['unknown_selected_cost'] == 6


def test_pm_generated_receipt_roundtrip(tmp_path):
    from puppetmaster.cost import build_current_registry_cost_report
    snapshot = manifest(REPO)
    store = create_store('sqlite', tmp_path / 'real-public-pm')
    job = store.create_job('offline receipt contract probe')
    task = Task(job_id=job.id, role='test', instruction='offline', status=TaskStatus.COMPLETE)
    store.save_task(task)
    artifact = Artifact(job_id=job.id, task_id=task.id, type=ArtifactType.FINDING,
                        created_by='contract-fixture', payload={'claim': 'forged good prose',
                        'tokens_in': 10, 'tokens_out': 5, 'model': 'fixture-unpriced'},
                        confidence=1, evidence=['fixture, not model quality evidence'])
    store.save_artifact(artifact)
    receipt = build_current_registry_cost_report(job.id, store.list_artifacts(job.id), registry=[])
    receipt['pricing_source'] = PRICING_SOURCE_TERMINAL
    store.save_job(replace(job, status=JobStatus.COMPLETE, cost_receipt=receipt))
    captured = ingest(store, job.id, 'repair_fn', snapshot['sha256'])
    assert captured['cost_receipt'] == receipt
    assert captured['artifacts'][0]['payload']['tokens_in'] == 10
    assert captured['cost_receipt']['actual_cost']['unpriced_tasks'] == 1
    from bench.router_evaluation import outcome
    result = outcome(captured)
    assert result['success'] is False
    assert result['selected_marginal_usd'] is None
    assert result['all_attempt_marginal_usd'] is None


def test_manifest_split_cannot_be_resealed_to_change_heldout(runs):
    snapshot, left, right, _ = runs
    snapshot['tasks'][0]['split'] = 'train'
    with pytest.raises(ValueError, match='fixed task'):
        compare(reseal(snapshot), left, right, REPO)


def test_nonfinite_and_negative_latency_is_unknown(runs):
    snapshot, left, right, _ = runs
    row = left['records'][0]
    row['latency_ms'] = -1
    left['records'][0] = reseal(row)
    result = compare(snapshot, reseal(left), right, REPO)['policies'][0]
    assert result['latency_p95_ms'] is None
    assert result['unknown_latency'] == 6


def test_same_job_cannot_stand_for_two_policies(runs):
    snapshot, left, right, _ = runs
    right['records'] = left['records']
    with pytest.raises(ValueError, match='reused job'):
        compare(snapshot, left, reseal(right), REPO)
