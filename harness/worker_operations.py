"""Selected worker operations, using only public Puppetmaster records.

The installed kernel has artifact reads and exact task bindings, but no public
quality-loop/mailbox/claim operation contract. Never substitute job-id controls
or infer a review verdict from lifecycle, findings, or free-form output.
"""
from __future__ import annotations

from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version

from . import job_expert
from .job_readmodel import InvalidReadRequest, PMSelection, ViewChanged, _read_page, _size
from .paths import same_workspace_path

LEDGER_BUDGET = dict(limit=20, max_scan=21, max_bytes=16384)
MAX_RESPONSE_BYTES = 65536
FEATURES = ('verdicts', 'cleanup', 'quality_loop', 'failure_routes', 'steering', 'claims')


def kernel_version():
    try:
        return version('puppetmaster-ai')
    except PackageNotFoundError:
        return 'unknown'


def capabilities(store):
    """Availability describes bindings implemented here, not guessed PM methods."""
    supported = all(callable(getattr(store, name, None)) for name in (
        'list_artifact_refs', 'get_artifacts_by_ids', 'get_task_by_id'))
    return {name: dict(state='available' if name == 'verdicts' and supported else 'unsupported',
                       reason=None if name == 'verdicts' and supported else
                       'public_operations_contract_required') for name in FEATURES}


def selected_store(reader, selection: PMSelection):
    """Use the metadata view's captured sources; the request never locates a store."""
    reader.check(selection.context)
    if selection.job_ref.state_id != selection.store.state_id:
        raise InvalidReadRequest()
    if selection.job_ref.version != 2:
        raise InvalidReadRequest()
    store, row, reason = reader._selected(selection)
    known = reader.sources.resolve(selection.store)
    if (reason or row.session_id != selection.context.session_id
            or known is None or known.cross_project):
        raise ViewChanged()
    return store, row


def check_selection(reader, selection, original):
    store, current = selected_store(reader, selection)
    if any(getattr(current, key) != getattr(original, key)
           for key in ('job_ref', 'session_id', 'origin', 'project_id')):
        raise ViewChanged()
    return store, current


def verdict_record(artifact):
    """Consume PM's worker_verdict artifact; it remains a worker advisory."""
    payload = artifact.payload
    if payload.get('kind') != 'worker_verdict':
        return None
    verdict, reason = payload.get('verdict'), payload.get('reason')
    valid = (str(artifact.type) == 'verification' and payload.get('source') == 'worker'
             and payload.get('advisory') is True and verdict in ('PASS', 'FAIL', 'PARTIAL')
             and isinstance(reason, str) and bool(reason.strip()))
    projected_reason = reason[:2048] if valid else 'Invalid structured worker verdict.'
    while _size(projected_reason) > 2048:
        projected_reason = projected_reason[:len(projected_reason) // 2]
    return dict(kind='verdict', id=artifact.id, task_id=artifact.task_id,
                created_at=artifact.created_at, verdict=verdict if valid else 'unknown',
                reason=projected_reason, reason_truncated=valid and projected_reason != reason,
                advisory=True)


def read_operations(reader, selection, *, cursor=None):
    store, row = selected_store(reader, selection)
    ctx = selection.context
    binding = reader._binding(ctx, selection.store, 'worker_operations_v1', 0, None,
                              selection.job_ref.as_dict())
    inner_cursor = reader._cursor(cursor, binding)
    result = dict(version=1, context=asdict(ctx), selection=selection.wire(),
                  kernel_version=kernel_version(), lifecycle=row.status,
                  capabilities=capabilities(store),
                  ledger=dict(outcome='unavailable', rows=[], next_cursor=None,
                              scanned=0, coverage='page', reason='public_operations_contract_required'),
                  verdict=dict(state='missing', authority='worker_advisory'),
                  accounting='references_only')
    if result['capabilities']['verdicts']['state'] != 'available':
        check_selection(reader, selection, row)
        return result
    page = _read_page(store.list_artifact_refs, selection.job_ref, cursor=inner_cursor, **LEDGER_BUDGET)
    if page.outcome not in ('complete', 'partial'):
        result['ledger'].update(outcome=page.outcome, reason=page.reason or page.outcome)
        check_selection(reader, selection, row)
        return result
    if len(page.items) > LEDGER_BUDGET['limit'] or page.scanned > LEDGER_BUDGET['max_scan']:
        raise ValueError('PM ledger budget exceeded')
    if any(ref.job_ref != selection.job_ref for ref in page.items):
        raise ViewChanged()
    reader.check(ctx)
    bodies = store.get_artifacts_by_ids(selection.job_ref.job_id, [ref.id for ref in page.items])
    records = []
    for ref in page.items:
        reader.check(ctx)
        artifact = bodies.get(ref.id)
        if (artifact is None or artifact.job_id != selection.job_ref.job_id or artifact.id != ref.id
                or artifact.sha256 != ref.sha256 or artifact.task_id != ref.task_id
                or str(artifact.type) != ref.artifact_type):
            raise ViewChanged()
        record = verdict_record(artifact)
        if record is not None:
            task = store.get_task_by_id(artifact.task_id)
            if (task.job_id != selection.job_ref.job_id
                    or task.payload.get('session_id') != ctx.session_id
                    or not same_workspace_path(task.payload.get('cwd') or '', ctx.repo)):
                raise ViewChanged()
            if not job_expert.current_artifact(artifact, task):
                record.update(verdict='unknown', reason='Verdict belongs to an earlier task attempt.', reason_truncated=False)
            records.append(record)
    # A recreated job or revised artifact must not publish hydrated old content.
    current_page = _read_page(store.list_artifact_refs, selection.job_ref,
                              cursor=inner_cursor, **LEDGER_BUDGET)
    if (current_page.outcome != page.outcome or tuple(current_page.items) != tuple(page.items)):
        raise ViewChanged()
    check_selection(reader, selection, row)
    result['ledger'] = dict(outcome=page.outcome, rows=records, scanned=page.scanned,
                            next_cursor=reader._wrap(page.next_cursor, binding), coverage='page', reason=None)
    # No aggregate PASS: a page is not a complete review and duplicate verdicts
    # are not evidence that one supersedes the other.
    result['verdict']['state'] = 'recorded' if records else 'missing'
    if _size(result) > MAX_RESPONSE_BYTES:
        raise ValueError('worker operations response budget exceeded')
    return result


def validate_action_scope(reader, selection, bindings, command):
    """Same task generation/lease fences used by scoped cancellation.

    PM must eventually compare these atomically with the action. This check is
    admission only; it does not authorize an unfenced job-id mutation.
    """
    from .api.scoped_cancellation import task_page

    store, row = selected_store(reader, selection)
    page = task_page(store, selection.job_ref)
    if page.outcome != 'complete':
        raise ViewChanged()
    current = {item.id: item.binding for item in page.items}
    supplied = {item.task_id: item for item in bindings}
    if command['kind'] == 'steer':
        if set(supplied) != {command['task_id']}:
            raise InvalidReadRequest()
    elif set(supplied) != set(current):
        raise ViewChanged()
    for task_id, binding in supplied.items():
        if binding != current.get(task_id):
            raise ViewChanged()
        task = store.get_task_by_id(task_id)
        if (task.job_id != selection.job_ref.job_id
                or task.payload.get('session_id') != selection.context.session_id
                or not same_workspace_path(task.payload.get('cwd') or '', selection.context.repo)):
            raise ViewChanged()
    check_selection(reader, selection, row)
    return store
