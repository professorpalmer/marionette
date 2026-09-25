import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pmharness import analysis_bench_runner as runner, bridge
from pmharness.analysis_bench import ANALYSIS_QUESTIONS, score_analysis

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('qid,text', [
    ('repair_default', 'max_repairs defaults to99'),
    ('pilot_envelope', 'say'),
    ('durable_methods', 'list_jobs'),
    ('repair_fn', 'drive_with_repair'),
    ('repair_default', ''),
])
def test_baseline_false_positives(qid, text):
    q = next(q for q in ANALYSIS_QUESTIONS if q.id == qid)
    assert score_analysis(q, text)['score'] == 0.0


def answer(q, values):
    return json.dumps({'question_id': q.id, 'answer': values})


def correct(q):
    return {f.name: list(f.expected) if isinstance(f.expected, tuple) else f.expected
            for f in q.facts}


@pytest.mark.parametrize('q', ANALYSIS_QUESTIONS, ids=lambda q: q.id)
def test_labeled_positive_negative_incomplete_negated_conflicting(q):
    values = correct(q)
    assert score_analysis(q, answer(q, values))['score'] == 1.0
    assert score_analysis(q, answer(q, {}))['score'] == 0
    for fact in q.facts:
        missing = dict(values)
        missing.pop(fact.name)
        assert score_analysis(q, answer(q, missing))['score'] == 0
        if isinstance(fact.expected, tuple):
            missing[fact.name] = [fact.expected[0]]
            assert score_analysis(q, answer(q, missing))['score'] == 0
        for wrong in (99, False, 'not ' + str(fact.expected), ['wrong'], {'not': fact.expected}):
            negative = {**values, fact.name: wrong}
            assert score_analysis(q, answer(q, negative))['score'] == 0
            conflict = json.dumps([answer(q, values), answer(q, negative)])
            assert score_analysis(q, conflict)['score'] == 0
    # Formatting, field order, and identifier-set order have no semantic effect.
    reordered = {k: list(reversed(v)) if isinstance(v, list) else v
                 for k, v in reversed(list(values.items()))}
    assert score_analysis(q, answer(q, reordered))['score'] == 1
    assert score_analysis(q, '```json\n' + answer(q, values) + '\n```')['score'] == 1


def test_paraphrased_return_type():
    q = next(q for q in ANALYSIS_QUESTIONS if q.id == 'registry_return')
    for text in ('Driver', 'driver instance', 'a driver object',
                 'an object implementing the driver protocol'):
        assert score_analysis(q, answer(q, {'return_type': text}))['score'] == 1
    for text in ('not a Driver', 'Driver or dictionary', 'a driver object is not returned'):
        assert score_analysis(q, answer(q, {'return_type': text}))['score'] == 0


def test_partial_set_coverage_without_completion_and_extra_members():
    q = next(q for q in ANALYSIS_QUESTIONS if q.id == 'durable_methods')
    result = score_analysis(q, answer(q, {'methods': ['list_jobs']}))
    assert result['score'] == 0 and result['hit'] is False
    assert result['status'] == 'incomplete'
    assert (result['matched'], result['required'], result['coverage']) == (1, 4, .25)
    for methods in (['list_jobs', 'list_jobs'], list(q.facts[0].expected) + ['save']):
        assert score_analysis(q, answer(q, {'methods': methods}))['score'] == 0


def test_strict_numbers_duplicate_keys_and_unrelated_evidence():
    q = next(q for q in ANALYSIS_QUESTIONS if q.id == 'repair_default')
    for value in (11, 10, 99, True, 1.0, '1', 'not 1', '1 or 99'):
        assert score_analysis(q, answer(q, {'max_repairs': value}))['score'] == 0
    duplicate = '{"question_id":"repair_default","answer":{"max_repairs":99,"max_repairs":1}}'
    assert score_analysis(q, duplicate)['score'] == 0
    assert score_analysis(q, json.dumps({'evidence': 'max_repairs=1'}))['score'] == 0
    assert score_analysis(q, json.dumps([answer(q, {'max_repairs': 1}), 'unknown']))['score'] == 0


def test_provider_count_requires_examples():
    q = next(q for q in ANALYSIS_QUESTIONS if q.id == 'provider_count')
    result = score_analysis(q, answer(q, {'count': 17}))
    assert result['score'] == 0 and result['hit'] is False
    assert (result['matched'], result['required'], result['coverage']) == (1, 4, .25)
    assert score_analysis(q, answer(q, {'count': 17, 'examples': ['openai', 'local', 'zai']}))['score'] == 1
    assert score_analysis(q, answer(q, {'count': 9, 'examples': ['openai', 'local', 'zai']}))['score'] == 0


def tree(path):
    return ast.parse((ROOT / path).read_text())


def test_labels_match_current_source_without_provider_calls():
    questions = {q.id: q for q in ANALYSIS_QUESTIONS}
    repair = next(n for n in tree('harness/repair.py').body if isinstance(n, ast.FunctionDef)
                  and n.name == 'drive_with_repair')
    assert questions['repair_fn'].facts[0].expected == repair.name
    index = [arg.arg for arg in repair.args.kwonlyargs].index('max_repairs')
    assert ast.literal_eval(repair.args.kw_defaults[index]) == questions['repair_default'].facts[0].expected == questions['repair_fn'].facts[1].expected
    state = next(n for n in tree('harness/state.py').body if isinstance(n, ast.ClassDef) and n.name == 'DurableState')
    methods = {n.name for n in state.body if isinstance(n, ast.FunctionDef) and not n.name.startswith('_')}
    assert set(questions['durable_methods'].facts[0].expected) == methods
    build = next(n for n in tree('pmharness/registry.py').body if isinstance(n, ast.FunctionDef) and n.name == 'build')
    assert build.returns.id == questions['registry_return'].facts[0].expected
    pilot = next(n for n in tree('harness/pilot.py').body if isinstance(n, ast.FunctionDef) and n.name == 'parse_pilot_turn')
    assert '{say, actions}' in ast.get_docstring(pilot)
    assert set(questions['pilot_envelope'].facts[0].expected) == {'say', 'actions'}
    for path, name, qid in [('harness/providers.py', 'PROVIDERS', 'provider_count'),
                            ('harness/conversation.py', '_HARD_PILOT_STEPS_DEFAULT', 'pilot_steps_cap')]:
        value = next(n.value for n in tree(path).body if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == name for t in n.targets))
        if qid == 'provider_count':
            names = [ast.literal_eval(next(k.value for k in call.keywords if k.arg == 'name')) for call in value.elts]
            assert len(names) == questions[qid].facts[0].expected
            assert set(names) == set(questions[qid].facts[1].expected)
        else:
            assert ast.literal_eval(value) == questions[qid].facts[0].expected


def test_runner_scores_full_production_compacted_claim(monkeypatch):
    q = next(q for q in ANALYSIS_QUESTIONS if q.id == 'provider_count')
    claim = json.dumps(json.loads(answer(q, correct(q))), indent=4)
    assert len(claim) > 240
    artifact = bridge._compact_artifact(SimpleNamespace(type='finding', payload={
        'claim': claim, 'evidence': 'harness/providers.py:PROVIDERS'}, confidence=1))
    assert artifact['body'] == claim
    assert len(artifact['headline']) == 240
    monkeypatch.setattr(runner, 'ANALYSIS_QUESTIONS', (q,))
    for key in ('HARNESS_REPO', 'HARNESS_SWARM_ADAPTER', 'HARNESS_ANALYSIS_MODEL'):
        monkeypatch.setenv(key, 'test')
    def execute(intent, **kwargs):
        assert 'question_id' in intent.goal and 'answer fields' in intent.goal
        return SimpleNamespace(artifacts=[artifact, {'type': 'verification', 'body': 'wrong'}], adapter='offline')
    monkeypatch.setattr(bridge, 'execute_intent', execute)
    result = runner.run_analysis_bench('offline', str(ROOT))
    assert result['mean'] == 100 and result['version'] == 2
    artifact['body'] = answer(q, {'count': 99})
    assert runner.run_analysis_bench('offline', str(ROOT))['mean'] == 0
    artifact['body'] = ''
    artifact['headline'] = answer(q, {'count': 16})
    result = runner.run_analysis_bench('offline', str(ROOT))
    assert result['mean'] == 0 and result['hits'] == 0


def test_runner_failure_and_evidence_only_do_not_earn_credit(monkeypatch):
    q = next(q for q in ANALYSIS_QUESTIONS if q.id == 'repair_default')
    monkeypatch.setattr(runner, 'ANALYSIS_QUESTIONS', (q,))
    for key in ('HARNESS_REPO', 'HARNESS_SWARM_ADAPTER', 'HARNESS_ANALYSIS_MODEL'):
        monkeypatch.setenv(key, 'test')
    def execute(intent, **kwargs):
        return SimpleNamespace(artifacts=[{
            'type': 'finding', 'headline': 'unknown', 'evidence': answer(q, correct(q)),
        }], adapter='offline')
    monkeypatch.setattr(bridge, 'execute_intent', execute)
    assert runner.run_analysis_bench('offline', str(ROOT))['mean'] == 0
    def fail(intent, **kwargs):
        raise RuntimeError('offline failure')
    monkeypatch.setattr(bridge, 'execute_intent', fail)
    result = runner.run_analysis_bench('offline', str(ROOT))
    assert result['mean'] == 0 and result['rows'][0]['status'] == 'unscorable'


def test_correct_prose_is_unscorable_just_like_wrong_prose():
    q = next(q for q in ANALYSIS_QUESTIONS if q.id == 'repair_default')
    for text in ('drive_with_repair defaults max_repairs to 1.',
                 'drive_with_repair defaults max_repairs to 99.'):
        result = score_analysis(q, text)
        assert result['score'] == 0 and result['hit'] is False
        assert result['status'] == 'unscorable' and result['fab'] is False
    assert score_analysis(q, answer(q, {'max_repairs': 1}))['status'] == 'complete'
    assert score_analysis(q, answer(q, {'max_repairs': 99}))['status'] == 'incorrect'
