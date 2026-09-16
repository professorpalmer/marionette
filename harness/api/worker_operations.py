"""Versioned selected-job operations HTTP boundary."""
from __future__ import annotations

import math

from .job_readmodel import CONTEXT, STORE, _context, _query, _respond, _selection, _text
from .scoped_cancellation import parse_bindings
from ..job_readmodel import InvalidReadRequest
from ..worker_operations import kernel_version, read_operations, validate_action_scope


def get_worker_operations(qs, reader):
    def read():
        values = _query(qs, CONTEXT | STORE | {'job_id', 'version', 'incarnation'}, {'cursor'})
        return read_operations(reader, _selection(_context(values), values), cursor=values.get('cursor'))
    return _respond(read)


def parse_command(value):
    if not isinstance(value, dict):
        raise InvalidReadRequest()
    kind = value.get('kind')
    fields = {
        'quality_loop': {'kind', 'mode', 'max_iterations', 'cost_cap_usd', 'cleanup'},
        'stop_loop': {'kind'}, 'steer': {'kind', 'task_id', 'message'},
        'broadcast': {'kind', 'message'},
    }
    if not isinstance(kind, str) or kind not in fields or value.keys() != fields[kind]:
        raise InvalidReadRequest()
    if kind == 'quality_loop':
        cap = value['cost_cap_usd']
        if (value['mode'] not in ('goal', 'review_pass')
                or type(value['max_iterations']) is not int or not 1 <= value['max_iterations'] <= 10
                or type(cap) not in (int, float) or not math.isfinite(cap) or cap <= 0 or cap > 1000
                or type(value['cleanup']) is not bool):
            raise InvalidReadRequest()
    if kind in ('steer', 'broadcast'):
        _text(value['message'], 4096)
    if kind == 'steer':
        _text(value['task_id'])
    return value


def post_worker_operation(body, reader):
    def act():
        if (not isinstance(body, dict) or body.keys() != {
                'version', 'context', 'selection', 'request_id', 'bindings', 'command'}
                or type(body['version']) is not int or body['version'] != 1):
            raise InvalidReadRequest()
        context = body['context']
        if not isinstance(context, dict) or context.keys() != CONTEXT:
            raise InvalidReadRequest()
        ctx = _context(context)
        raw = body['selection']
        if not isinstance(raw, dict) or raw.keys() != {'job_ref', 'source', 'session_id', 'repo'}:
            raise InvalidReadRequest()
        if raw['session_id'] != ctx.session_id or raw['repo'] != ctx.repo:
            raise InvalidReadRequest()
        ref = raw['job_ref']
        if (not isinstance(ref, dict) or ref.keys() != {'job_id', 'state_id', 'version', 'incarnation'}
                or type(ref['version']) is not int or ref['version'] != 2):
            raise InvalidReadRequest()
        selection = _selection(ctx, dict(ref, source=raw['source'], version='2'))
        request_id = _text(body['request_id'], 128)
        command = parse_command(body['command'])
        try:
            bindings = parse_bindings(body['bindings'])
        except (TypeError, ValueError) as exc:
            raise InvalidReadRequest() from exc
        validate_action_scope(reader, selection, bindings, command)
        # No current public PM operation can atomically stop future iterations,
        # deliver mail to an exact lease, or provide per-recipient receipts.
        # In particular, do not substitute _scoped_cancel for stop_loop.
        return dict(version=1, context=context, selection=selection.wire(), request_id=request_id,
                    outcome='unsupported', code='public_operations_contract_required',
                    kernel_version=kernel_version())
    status, result = _respond(act)
    return (409 if status == 200 else status), result
