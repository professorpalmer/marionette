"""Allowlisted projections of explicitly selected public Puppetmaster records."""
from __future__ import annotations

import math
from datetime import datetime

from puppetmaster.cost import is_cost_final_job_status, valid_terminal_cost_receipt
from puppetmaster.usage import select_usage_records

from .api.redaction import redact_secret_text
from .api.jobs import canonical_job_outcome


def text(value, limit=512):
    if not isinstance(value, str):
        return None
    return redact_secret_text(value[:limit])


def number(value):
    if type(value) not in (int, float) or value < 0:
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def timestamp(value):
    """Wire timestamps must end in Z or ±HH:MM; normalize colon-less offsets."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    candidate = value.replace('Z', '+00:00')
    if len(candidate) >= 5 and candidate[-5] in '+-' and candidate[-4:].isdigit():
        candidate = candidate[:-2] + ':' + candidate[-2:]
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if not parsed.tzinfo:
        return None
    if value.endswith('Z') or (len(value) >= 6 and value[-6] in '+-' and value[-3] == ':'):
        return value
    return parsed.isoformat()


def confidence_value(value):
    amount = number(value)
    return None if amount is None or amount > 1 else amount


def unavailable(reason):
    return dict(kind='unavailable', reason=reason, header=None, tasks=[], artifacts=[],
                coverage=dict(tasks='unknown', artifacts='unknown'), quality='unverified')


def job_header(job):
    cost = dict(selected_usd=None, source='unavailable', basis='unknown',
                measured_cost_usd=None, estimated_cost_usd=None)
    receipt = job.cost_receipt
    if is_cost_final_job_status(job.status) and valid_terminal_cost_receipt(receipt, job.id):
        actual = receipt['actual_cost']
        cost['source'] = 'terminal_cost_receipt'
        if (type(actual.get('priced_tasks')) is int and actual['priced_tasks'] > 0
                and type(actual.get('unpriced_tasks')) is int and actual['unpriced_tasks'] == 0):
            cost['selected_usd'] = number(actual.get('total_marginal_cost_usd'))
            # Measured token counts priced against a catalog are still estimates.
            metrics = receipt.get('bounded_economics', {}).get('totals', {})
            api = metrics.get('api_cost_usd', {})
            plan = metrics.get('plan_marginal_cost_usd', {})
            if (api.get('state') == 'measured' and api.get('total') == cost['selected_usd']
                    and (plan.get('total') is None or plan.get('total') == 0)):
                cost['basis'] = 'measured'
                cost['measured_cost_usd'] = cost['selected_usd']
                cost['estimated_cost_usd'] = 0
            elif cost['selected_usd'] is not None:
                cost['basis'] = 'estimated'
                cost['estimated_cost_usd'] = cost['selected_usd']
    return dict(created_at=timestamp(job.created_at), completed_at=timestamp(job.completed_at), cost=cost)


def summary_header(job, expert):
    if expert['kind'] == 'unavailable':
        return job_header(job)
    header = dict(expert['economics']['header'], quality=expert['quality'],
                  model=None, model_provenance='unknown')
    if not all(value == 'complete' for value in expert['coverage'].values()):
        return header
    tasks = expert['tasks']
    routes = [a for a in expert['artifacts'] if a['type'] == 'routing']
    ranks = {'router-escalation': 3, 'router-fallback': 2, 'router': 1}

    def chosen(candidates):
        return max(enumerate(candidates), key=lambda pair: (
            ranks.get(pair[1]['created_by'], 0),
            datetime.fromisoformat(pair[1]['created_at'].replace('Z', '+00:00')).timestamp()
            if pair[1]['created_at'] else 0, pair[0]), default=(0, None))[1]

    def model(*candidates):
        engines = {'codex', 'cursor', 'claude', 'claude-code', 'default', 'auto'}
        return next((value for value in candidates if value and value.rsplit('/', 1)[-1].lower() not in engines), None)

    if not tasks:
        route = chosen([a for a in routes if not a['task_id'] and not a['role']])
        if route and model(route['model']):
            header.update(model=route['model'], model_provenance='job_routing')
        return header
    assigned = []
    for task in tasks:
        candidates = [a for a in routes if a['task_id'] == task['id'] or (
            not a['task_id'] and a['role'] and a['role'] == task['role']
            and sum(t['role'] == task['role'] for t in tasks) == 1)]
        route = chosen(candidates)
        assigned.append(model(route['model'] if route else None, task['model'], task['adapter']))
    if assigned and assigned[0] and all(value == assigned[0] for value in assigned):
        header.update(model=assigned[0], model_provenance='task_assignment' if len(tasks) == 1 else 'uniform_task_assignments')
    return header


def task_matches(task, ref, job_id):
    binding = ref.binding
    return (task.id == ref.id and task.job_id == job_id and str(task.status) == ref.status
            and binding is not None and binding.task_id == task.id
            and binding.generation == task.generation and binding.lease_id == task.lease_id
            and binding.owner == task.lease_owner)


def current_artifact(artifact, task):
    """Reset output needs an explicit matching epoch, never just the same task id."""
    payload = artifact.payload
    if any(str(getattr(artifact, k, '')).lower() in ('stale', 'superseded')
           for k in ('execution_status', 'grounding_status', 'claim_support_status', 'criterion_status')):
        return False
    generation = payload.get('task_generation', payload.get('generation'))
    lease = payload.get('lease_id')
    if generation is not None:
        return type(generation) is int and generation == task.generation and (lease is None or lease == task.lease_id)
    if not (task.generation == 0 and task.attempts == 0
            or task.generation == 1 and task.attempts == 1):
        return False
    created, start = timestamp(artifact.created_at), timestamp(task.created_at)
    return bool(created and start and datetime.fromisoformat(created.replace('Z', '+00:00'))
                >= datetime.fromisoformat(start.replace('Z', '+00:00')))


def model_name(value):
    name = text(value)
    if name is None or not name.strip() or len(name) > 256:
        return None
    return name


def task_projection(task, usage):
    payload = task.payload
    instruction = text(task.instruction, 2048) or ''
    facts = usage.get('selected_facts', {})
    cost = number(facts.get('real_cost_usd'))
    return dict(id=task.id, role=text(task.role) or '', instruction=instruction,
                instruction_truncated=len(task.instruction) > 2048,
                adapter=text(task.adapter) or '', model=model_name(payload.get('model')),
                created_at=timestamp(task.created_at), updated_at=timestamp(task.updated_at),
                usage=dict(tokens_in=number(facts.get('tokens_in')), tokens_out=number(facts.get('tokens_out')),
                           est_cost_usd=cost, estimated=False if cost is not None else None,
                           cost_provenance='provider' if cost is not None else None))


def artifact_projection(artifact):
    p = artifact.payload
    kind = str(artifact.type)
    result = text(p.get('result'))
    passed = p.get('passed')
    check = 'unavailable'
    if kind in ('verification', 'gate'):
        if passed is False or (result or '').lower() in ('failed', 'blocked', 'error', 'degraded'):
            check = 'failed'
        elif passed is True or (result or '').lower() in ('passed', 'ok', 'success'):
            check = 'passed'
    headline = next((text(p.get(key), 1024) for key in ('claim', 'decision', 'risk', 'check', 'change', 'summary', 'gate')
                     if isinstance(p.get(key), str)), '')
    # A worker's verification carries the task instruction as its check, so it
    # read as the instruction. The verdict, or the run itself, is the evidence.
    if kind == 'verification' and p.get('kind') == 'worker_verdict' and isinstance(p.get('verdict'), str):
        headline = text('Worker verdict ' + p['verdict'], 1024)
    elif kind == 'verification' and isinstance(p.get('turns'), int) and isinstance(p.get('total_tool_calls'), int):
        headline = f"Worker run: {p['turns']} turns, {p['total_tool_calls']} tool calls"
    rejected = p.get('rejected', [])
    return dict(id=artifact.id, task_id=artifact.task_id or None, type=kind,
        created_by=text(artifact.created_by) or '', created_at=timestamp(artifact.created_at),
        headline=headline, detail=text(p.get('detail', p.get('reason', p.get('why', p.get('mitigation')))), 2048),
        result=result, failure=text(p.get('failure', p.get('failure_class'))),
        confidence=confidence_value(artifact.confidence),
        model=model_name(p.get('adapter_model_name') or p.get('model') or p.get('model_id')),
        adapter=text(p.get('adapter')), policy=text(p.get('policy')), provider=text(p.get('provider')),
        role=text(p.get('role')), est_cost_usd=number(p.get('estimated_cost_usd', p.get('est_cost_usd'))),
        rejected=[dict(model=model_name(r.get('model', r.get('model_id'))) or '', reason=text(r.get('reason')) or '')
                  for r in rejected[:8] if isinstance(r, dict)] if isinstance(rejected, list) else [],
        check_result=check)


def _evidence_rank(artifact):
    p, kind = artifact.payload, str(artifact.type)
    if kind == 'routing':
        return 0
    if kind == 'verification' and p.get('kind') == 'worker_verdict':
        return 3
    if kind == 'verification' and isinstance(p.get('turns'), int):
        return 1
    return 2


def chronological(artifacts):
    """Oldest first. Pages arrive in random-id order and a worker saves its run
    record, findings and verdict within one second, so ties follow the order a
    worker writes them: route, run record, findings, verdict last."""
    return sorted(artifacts, key=lambda a: (timestamp(a.created_at) or '', _evidence_rank(a)))


def project(job, tasks, artifacts, coverage, *, registry=(), compaction=None):
    from .job_expert_economics import project_economics
    usage = select_usage_records(artifacts)
    projected = [artifact_projection(a) for a in chronological(artifacts)]
    task_ids = {task.id for task in tasks}
    failed = any((a['check_result'] == 'failed' or a['failure'])
                 and (a['task_id'] is None or a['task_id'] in task_ids) for a in projected)
    complete = all(v == 'complete' for v in coverage.values())
    checks = [a for a in projected if a['type'] in ('verification', 'gate')]
    checked_tasks = {a['task_id'] for a in checks if a['check_result'] == 'passed'}
    covered_tasks = all(task.id in checked_tasks for task in tasks)
    verdict = canonical_job_outcome(artifacts) if complete else None
    quality = 'unverified'
    if failed or (complete and str(job.status) == 'complete' and not verdict['trustworthy']):
        quality = 'degraded'
    elif complete and checks and covered_tasks and verdict['trustworthy'] and all(a['check_result'] == 'passed' for a in checks):
        quality = 'ok'
    compaction = compaction or dict(coverage='unavailable', reason='source_unavailable', records=[])
    economics = project_economics(job, tasks, artifacts, coverage, registry=registry,
        compaction_records=compaction['records'] if compaction['coverage'] == 'complete' else None)
    economics['compaction'] = dict(coverage=compaction['coverage'], reason=compaction['reason'])
    return dict(kind='available' if complete else 'partial', reason=None if complete else 'incomplete_coverage',
                header=job_header(job), tasks=[task_projection(t, usage.get(t.id, {})) for t in tasks],
                artifacts=projected, coverage=coverage, quality=quality,
                economics=economics)
