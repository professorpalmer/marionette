"""Bounded cancellation views and strict public PM contract boundaries."""
from dataclasses import asdict

from importlib import import_module
from inspect import signature


def runtime_available(store=None):
    """No store discovery or body reads when the pinned PM lacks exact controls."""
    try:
        contracts = import_module('puppetmaster.contracts')
        identity = import_module('puppetmaster.identity')
        if not hasattr(identity, 'StoreIdentityError'):
            return False
        if not {'version', 'incarnation'} <= signature(contracts.JobRef).parameters.keys():
            return False
        if not {'task_id', 'generation', 'lease_id', 'owner'} <= signature(contracts.TaskBinding).parameters.keys():
            return False
    except (ImportError, AttributeError, TypeError, ValueError):
        return False
    return store is None or all(callable(getattr(store, name, None)) for name in (
        'list_job_summaries', 'list_task_refs', 'get_task_by_id',
        'get_cancellation_receipt', 'request_cancellation'))

MAX_BINDINGS = 200


def parse_job_ref(value):
    from puppetmaster.contracts import JobRef
    if (not isinstance(value, dict) or not {'job_id', 'state_id'} <= value.keys()
            or value.keys() - {'job_id', 'state_id', 'version', 'incarnation'}):
        raise ValueError('invalid job reference')
    return JobRef(**value)


def parse_bindings(value):
    from puppetmaster.contracts import TaskBinding
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_BINDINGS:
        raise ValueError('provide 1..200 task bindings')
    bindings = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {'task_id', 'generation', 'lease_id', 'owner'}:
            raise ValueError('invalid task binding')
        if any(v is not None and (not isinstance(v, str) or not v or len(v) > 256)
               for v in (item['lease_id'], item['owner'])):
            raise ValueError('invalid lease identity')
        if not isinstance(item['task_id'], str) or not 1 <= len(item['task_id']) <= 256:
            raise ValueError('invalid task identity')
        bindings.append(TaskBinding(**item))
    if len({b.task_id for b in bindings}) != len(bindings):
        raise ValueError('duplicate task binding')
    return tuple(sorted(bindings, key=lambda b: b.task_id))


def task_page(store, ref):
    return store.list_task_refs(ref, limit=MAX_BINDINGS, max_bytes=262144, max_scan=201)


def cancellation_view(store, ref, rendered_tasks):
    """Never pair authority for a successor with a previously rendered worker."""
    if not runtime_available(store):
        return {'status': 'unavailable', 'limit': MAX_BINDINGS, 'reason': 'scoped_cancellation_unsupported'}
    if ref.version != 2:
        return {'status': 'unavailable', 'limit': MAX_BINDINGS, 'reason': 'legacy_ref'}
    try:
        page = task_page(store, ref)
        if page.outcome != 'complete':
            return {'status': page.outcome, 'limit': MAX_BINDINGS}
        bindings = [asdict(item.binding) for item in page.items if item.binding is not None]
        parse_bindings(bindings)
        rendered = {t['id']: t.get('binding') for t in rendered_tasks}
        if (not bindings or len(bindings) != len(page.items) or len(rendered) != len(bindings)
                or any(rendered.get(b['task_id']) != b for b in bindings)):
            return {'status': 'unavailable', 'limit': MAX_BINDINGS}
        return {'status': 'complete', 'limit': MAX_BINDINGS, 'bindings': bindings}
    except (AttributeError, OSError, TypeError, ValueError):
        return {'status': 'unavailable', 'limit': MAX_BINDINGS}
