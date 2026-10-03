"""Extract the facts the Pages site shows for one recorded swarm job.

Usage: python scripts/site_run_facts.py <state.sqlite3> <job_id> > facts.json

Reads a Puppetmaster SQLite store (copy it first if a backend has it open) and
prints JSON: the event timeline for the kernel replay, per-role routing and
usage, artifact counts and the frozen cost receipt. Every number on the site's
run sections should come from this output, not from hand edits.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime


# Bookkeeping rows that echo a heartbeat or an artifact already in the replay.
REPLAY_SKIP = frozenset(('run.saved', 'edge.upserted'))


def _ts(value):
    return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()


def _event_detail(event, payload, artifact_payloads):
    if event == 'task.saved':
        return payload.get('status', '')
    if event == 'task.claimed':
        return str(payload.get('lease_expires_at') or payload.get('claimed_at') or '')[11:19]
    if event == 'worker.failed_task':
        return payload.get('failure') or payload.get('reason') or ''
    if event == 'job.status':
        return payload.get('status', '')
    if event == 'router.auto_fallback':
        return {'reason': payload.get('reason'), 'to': payload.get('to_model'), 'attempt': payload.get('attempt')}
    if event == 'artifact.saved':
        # The event carries the artifact id; its text lives on the artifact.
        body = artifact_payloads.get(payload.get('artifact_id'), {})
        detail = {'type': payload.get('type'), 'conf': payload.get('confidence'),
                  'sha': (payload.get('sha256') or '')[:10], 'claim': str(body.get('claim') or '')[:100]}
        if payload.get('type') == 'routing' and body.get('adapter_model_name'):
            detail['model'] = body['adapter_model_name']
        return detail
    return ''


def main(db_path, job_id):
    db = sqlite3.connect(db_path)
    job = json.loads(db.execute('select data from jobs where id=?', (job_id,)).fetchone()[0])
    tasks = {tid: (role, status, json.loads(data)) for tid, role, status, data in
             db.execute('select id, role, status, data from tasks where job_id=?', (job_id,))}
    roles_by_task = {tid: role for tid, (role, _, _) in tasks.items()}
    artifacts = [(aid, task_id, kind, json.loads(data)) for aid, task_id, kind, data in
                 db.execute('select id, task_id, type, data from artifacts where job_id=? order by rowid', (job_id,))]
    artifact_payloads = {aid: a.get('payload') or {} for aid, _, _, a in artifacts}

    events, t0 = [], None
    for at, event, payload in db.execute('select at, event, payload from events where job_id=? order by id', (job_id,)):
        if event in REPLAY_SKIP:
            continue
        payload = json.loads(payload or '{}')
        t0 = _ts(at) if t0 is None else t0
        role = roles_by_task.get(payload.get('task_id'), '')
        events.append([round(_ts(at) - t0), event, role, _event_detail(event, payload, artifact_payloads)])

    receipt = job.get('cost_receipt') or {}
    usage = {t['task_id']: t for t in (receipt.get('actual_cost') or {}).get('tasks', [])}

    roles = []
    for tid, (role, status, data) in tasks.items():
        routes = [a['payload'] for _, t, kind, a in artifacts if t == tid and kind == 'routing']
        final = routes[-1] if routes else {}
        first = routes[0] if routes else {}
        candidates = []
        for item in first.get('rejected') or []:
            reason = item.get('reason') or ''
            if 'allowed_model_ids' in reason or 'no usable credentials' in reason:
                continue
            candidates.append({'id': item.get('id'), 'why': reason})
        used = usage.get(tid, {})
        roles.append({
            'id': role, 'task': tid, 'status': status,
            'need': first.get('capability_needed'), 'model': final.get('model_id'),
            'capability': final.get('capability_score'), 'billing': final.get('billing'),
            'reason': first.get('reason'), 'estimated_cost_usd': first.get('estimated_cost_usd'),
            'baseline_model': first.get('baseline_model_id'), 'baseline_cost_usd': first.get('baseline_cost_usd'),
            'fallbacks': len(routes) - 1, 'candidates': candidates,
            'considered': len(first.get('rejected') or []) + 1,
            'tokens_in': used.get('tokens_in', 0), 'tokens_out': used.get('tokens_out', 0),
            'cache_read_tokens': used.get('cache_read_tokens', 0),
            'cost_usd': used.get('marginal_cost_usd'), 'api_equivalent_usd': used.get('api_equivalent_cost_usd'),
        })

    print(json.dumps({
        'job_id': job_id, 'status': job.get('status'),
        'created_at': job.get('created_at'), 'completed_at': job.get('completed_at'),
        'events': events, 'roles': roles,
        'artifacts': dict(Counter(kind for _, _, kind, _ in artifacts)), 'artifact_total': len(artifacts),
        'receipt': {
            'token_usage': receipt.get('token_usage'),
            'actual_cost_usd': (receipt.get('counterfactual') or {}).get('actual_cost_usd'),
            'counterfactual': receipt.get('counterfactual'),
            'by_model': (receipt.get('actual_cost') or {}).get('by_model'),
        },
    }, indent=1))


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
