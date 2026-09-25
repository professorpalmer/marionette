from __future__ import annotations

"""Version 2: deterministic accuracy of typed, labeled analysis answers.

Answers are JSON objects in finding claims, not arbitrary prose. Incomplete
answers score zero; fact coverage is diagnostic only. Wrong
values or conflicting answers invalidate the response. This measures factual
slots and protocol adherence, not reasoning quality, source access, evidence
quality, or entailment of free text. Legacy substring scores are not comparable.
Labels are pinned to repository source and guarded by offline source tests.
"""

from dataclasses import dataclass
import json

BENCHMARK_VERSION = 2


@dataclass(frozen=True)
class Fact:
    name: str
    expected: object  # int, str, or tuple of exact identifiers
    aliases: tuple = ()
    minimum: int = 0  # nonzero for a requested sample rather than the full set

    @property
    def weight(self) -> int:
        return (self.minimum or len(self.expected)) if isinstance(self.expected, tuple) else 1


@dataclass(frozen=True)
class AnalysisQ:
    id: str
    prompt: str
    facts: tuple[Fact, ...]
    source: str

    @property
    def task_prompt(self) -> str:
        fields = {f.name: ('array of exact names' if isinstance(f.expected, tuple)
                           else 'integer' if type(f.expected) is int else 'string')
                  for f in self.facts}
        return (self.prompt + '\nPut the answer in a finding claim containing ONLY a JSON '
                'object with question_id=' + json.dumps(self.id) + ' and answer (an object). '
                'Required answer fields and types: ' + json.dumps(fields) + '. '
                'Use null for unknown facts. Do not include alternatives or negated names '
                'in answer fields. Put explanation and source citations in artifact evidence.')


ANALYSIS_QUESTIONS = (
    AnalysisQ('repair_fn', 'Name the public function in harness/repair.py that wraps a '
              'driver call with repair retries and its default max_repairs.',
              (Fact('function', 'drive_with_repair'), Fact('max_repairs', 1)),
              'harness/repair.py:drive_with_repair'),
    AnalysisQ('repair_default', 'What is the default max_repairs in drive_with_repair?',
              (Fact('max_repairs', 1),), 'harness/repair.py:drive_with_repair'),
    AnalysisQ('durable_methods', 'List all public methods declared on DurableState in '
              'harness/state.py, excluding underscore-prefixed methods.',
              (Fact('methods', ('list_jobs', 'format_artifacts', 'job_artifacts', 'events_since')),),
              'harness/state.py:DurableState'),
    AnalysisQ('registry_return', 'What type of object does pmharness/registry.py build return? '
              'Use the common interface type, not a particular implementation.',
              (Fact('return_type', 'Driver', ('driver instance', 'a driver object',
                                            'an object implementing the driver protocol')),),
              'pmharness/registry.py:build'),
    AnalysisQ('pilot_envelope', 'Name the two core top-level keys documented by '
              'harness/pilot.py parse_pilot_turn for a clean pilot JSON envelope; '
              'exclude optional thinking and parser aliases.',
              (Fact('keys', ('say', 'actions')),), 'harness/pilot.py:parse_pilot_turn'),
    AnalysisQ('pilot_steps_cap', 'State _HARD_PILOT_STEPS_DEFAULT in harness/conversation.py '
              '(the default safety cap, not an environment override).',
              (Fact('steps', 40),), 'harness/conversation.py:_HARD_PILOT_STEPS_DEFAULT'),
    AnalysisQ('provider_count', 'Count profiles declared in harness/providers.py PROVIDERS '
              'and give at least three distinct canonical provider names as examples.',
              (Fact('count', 17), Fact('examples', (
                  'openrouter', 'anthropic', 'openai', 'openai-codex', 'claude-code',
                  'cursor-cli', 'nous',
                  'gemini', 'deepseek', 'zai', 'minimax', 'xai', 'nvidia', 'opencode-go',
                  'opencode-zen', 'local', 'bedrock'), minimum=3)),
              'harness/providers.py:PROVIDERS'),
)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate answer field')
        result[key] = value
    return result


def _parse_answer(text: str):
    text = text.strip()
    if text.startswith('```json\n') and text.endswith('\n```'):
        text = text[8:-4]
    return json.loads(text, object_pairs_hook=_unique_object)


def score_analysis(q: AnalysisQ, finding_text: str) -> dict:
    """Score one claim, or a JSON array of full claim strings from the runner.

    Separate findings are combined only when their supplied fields agree.
    Invalid structure is unscorable; wrong typed values are incorrect. Neither
    earns credit. `fab` means a labeled contradiction, not proven fabrication.
    """
    result = {'id': q.id, 'version': BENCHMARK_VERSION, 'hit': False,
              'fab': False, 'score': 0.0, 'status': 'unscorable', 'matched': 0,
              'required': sum(f.weight for f in q.facts), 'coverage': 0.0}
    try:
        parsed = _parse_answer(finding_text or '')
        answers = [_parse_answer(t) for t in parsed] if isinstance(parsed, list) else [parsed]
        if not answers:
            return result
        supplied = {}
        for item in answers:
            if (not isinstance(item, dict) or set(item) != {'question_id', 'answer'}
                    or item['question_id'] != q.id or not isinstance(item['answer'], dict)
                    or set(item['answer']) - {f.name for f in q.facts}):
                return result
            for key, value in item['answer'].items():
                if key in supplied:
                    # Exact identifier sets may be reordered, but never enlarged by union.
                    old = supplied[key]
                    same = type(old) is type(value) and old == value
                    if isinstance(old, list) and isinstance(value, list):
                        same = sorted(map(repr, old)) == sorted(map(repr, value))
                    if not same:
                        result.update(status='conflicting', fab=True)
                        return result
                supplied[key] = value
    except (ValueError, TypeError, AttributeError):
        return result

    matched = 0
    for fact in q.facts:
        value = supplied.get(fact.name)
        if value is None:
            continue
        if isinstance(fact.expected, tuple):
            valid = (isinstance(value, list) and all(isinstance(v, str) for v in value)
                     and len(set(value)) == len(value) and set(value) <= set(fact.expected))
            count = min(len(value), fact.weight) if valid else 0
        else:
            valid = type(value) is type(fact.expected)
            if valid and isinstance(value, str):
                valid = value.strip().casefold() in {
                    v.casefold() for v in (fact.expected,) + fact.aliases}
            elif valid:
                valid = value == fact.expected
            count = int(valid)
        if not valid:
            result.update(status='incorrect', fab=True)
            return result
        matched += count
    coverage = matched / result['required']
    complete = matched == result['required']
    result.update(matched=matched, coverage=coverage, score=float(complete), hit=complete,
                  status='complete' if complete else 'incomplete')
    return result
