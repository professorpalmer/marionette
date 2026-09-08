"""Strict HTTP boundary for the separate bounded metadata lane.

Authentication and request-body byte limits remain the host Handler's responsibility.
Pass parse_qs(..., keep_blank_values=True) so blank duplicate selectors are rejected.
"""
from __future__ import annotations

import re

from puppetmaster.models import JobRef

from ..job_readmodel import (
    InvalidReadRequest, MetadataReader, PMSelection, ReadContext, PM_STATUSES, StoreSelection, ViewChanged,
)
from ..paths import same_workspace_path

CONTEXT = {'session_id', 'repo', 'scope', 'view_generation'}
STORE = {'source', 'state_id'}
ID = re.compile(r'[a-zA-Z0-9_-]{1,256}\Z')
JOB = re.compile(r'job_[a-zA-Z0-9_-]{1,128}\Z')


def _text(value, maximum=256):
    if not isinstance(value, str) or not value or value != value.strip() or any(ord(c) < 32 for c in value):
        raise InvalidReadRequest()
    try:
        if len(value.encode('utf-8')) > maximum:
            raise InvalidReadRequest()
    except UnicodeError as exc:
        raise InvalidReadRequest() from exc
    return value


def _query(qs, required, optional=()):
    if not isinstance(qs, dict) or not required <= qs.keys() or qs.keys() - required - set(optional):
        raise InvalidReadRequest()
    result = {}
    for key, values in qs.items():
        if not isinstance(values, list) or len(values) != 1:
            raise InvalidReadRequest()
        maximum = 8192 if key.endswith('cursor') else 1024 if key == 'repo' else 256
        result[key] = _text(values[0], maximum)
    return result


def _context(values):
    if values['scope'] not in ('session', 'repo', 'all'):
        raise InvalidReadRequest()
    session = _text(values['session_id'])
    generation = _text(values['view_generation'], 128)
    repo = _text(values['repo'], 1024)
    if not ID.fullmatch(session) or not ID.fullmatch(generation):
        raise InvalidReadRequest()
    # Repository is only a captured context match, never a store locator.
    return ReadContext(session, repo, generation, values['scope'])


def _store(values):
    if values['source'] not in ('harness', 'cli'):
        raise InvalidReadRequest()
    identity = _text(values['state_id'])
    if not ID.fullmatch(identity):
        raise InvalidReadRequest()
    return StoreSelection(values['source'], identity)


def _selection(ctx, values):
    jid = _text(values['job_id'])
    if not JOB.fullmatch(jid):
        raise InvalidReadRequest()
    store = _store(values)
    version = values.get('version', '1')
    if version not in ('1', '2') or (version == '1' and values.get('incarnation') is not None):
        raise InvalidReadRequest()
    try:
        ref = JobRef(jid, store.state_id, version=int(version), incarnation=values.get('incarnation'))
    except ValueError as exc:
        raise InvalidReadRequest() from exc
    return PMSelection(ctx, store, ref)


def _respond(call):
    try:
        return 200, call()
    except ViewChanged:
        return 409, dict(code='view_changed')
    except InvalidReadRequest:
        return 400, dict(code='invalid_read_request')
    except ValueError as exc:
        # Public PM CursorCodec uses these two errors. Malformed stored JSON or
        # invalid stored bindings are corruption, not malformed client input.
        if str(exc) in ('invalid cursor', 'cursor does not match query'):
            return 400, dict(code='invalid_read_request')
        return 503, dict(code='store_unavailable')
    except Exception:
        return 503, dict(code='store_unavailable')


def get_job_metadata(qs: dict, reader: MetadataReader):
    def read():
        values = _query(qs, CONTEXT | STORE | {'mode'}, {'cursor', 'after_revision', 'status'})
        ctx = _context(values)
        if values['mode'] not in ('snapshot', 'changes'):
            raise InvalidReadRequest()
        if 'status' in values and values['status'] not in PM_STATUSES:
            raise InvalidReadRequest()
        after = values.get('after_revision', '0')
        if not re.fullmatch(r'0|[1-9][0-9]{0,18}', after) or int(after) > 2**63 - 1:
            raise InvalidReadRequest()
        if values['mode'] == 'snapshot' and 'after_revision' in values:
            raise InvalidReadRequest()
        return reader.read_job_page(ctx, _store(values), mode=values['mode'],
                                    cursor=values.get('cursor'), after_revision=int(after), status=values.get('status'))
    return _respond(read)


def get_job_metadata_detail(qs: dict, reader: MetadataReader):
    def read():
        values = _query(qs, CONTEXT | STORE | {'job_id'}, {'task_cursor', 'artifact_cursor', 'version', 'incarnation',
                  'attempt_cursor', 'run_cursor', 'process_outcome_cursor', 'observation_cursor'})
        return reader.read_selected_metadata(_selection(_context(values), values),
                                             **{name: values.get(name) for name in ('task_cursor', 'artifact_cursor',
                                                'attempt_cursor', 'run_cursor', 'process_outcome_cursor', 'observation_cursor')})
    return _respond(read)


def post_job_metadata_pins(body: dict, reader: MetadataReader):
    def read():
        if not isinstance(body, dict) or body.keys() != CONTEXT | {'selections'}:
            raise InvalidReadRequest()
        ctx = _context(body)
        raw = body['selections']
        if not isinstance(raw, list) or len(raw) > 8:
            raise InvalidReadRequest()
        selections = []
        for item in raw:
            if not isinstance(item, dict) or item.keys() != {'job_ref', 'source', 'session_id', 'repo'}:
                raise InvalidReadRequest()
            ref = item['job_ref']
            if (not isinstance(ref, dict) or not {'job_id', 'state_id'} <= ref.keys()
                    or ref.keys() - {'job_id', 'state_id', 'version', 'incarnation'}
                    or ('version' in ref and (type(ref['version']) is not int or ref['version'] not in (1, 2)))):
                raise InvalidReadRequest()
            if (_text(item['session_id']) != ctx.session_id
                    or not same_workspace_path(_text(item['repo'], 1024), ctx.repo)):
                raise ViewChanged()
            selections.append(_selection(ctx, dict(ref, source=item['source'], version=str(ref.get('version', 1)))))
        if len(set(selections)) != len(selections):
            raise InvalidReadRequest()
        return reader.read_pins(ctx, selections)
    return _respond(read)


def get_local_metadata(qs: dict, reader: MetadataReader):
    def read():
        values = _query(qs, CONTEXT, {'mode', 'cursor', 'after_revision', 'lane'})
        lane = values.get('lane', 'history')
        if lane not in ('history', 'active'):
            raise InvalidReadRequest()
        mode = values.get('mode', 'snapshot')
        after = values.get('after_revision', '0')
        if (mode not in ('snapshot', 'changes') or not re.fullmatch(r'0|[1-9][0-9]{0,18}', after)
                or int(after) > 2**63 - 1 or (mode == 'snapshot' and 'after_revision' in values)):
            raise InvalidReadRequest()
        return reader.read_local(_context(values), mode=mode, cursor=values.get('cursor'),
                                 after_revision=int(after), lane=lane)
    return _respond(read)


def get_local_metadata_detail(qs: dict, reader: MetadataReader):
    def read():
        values = _query(qs, CONTEXT | {'job_id', 'incarnation', 'lane'}, {'cursor', 'include_context'})
        if (('include_context' in values and values['include_context'] != 'true')
                or not ID.fullmatch(values['job_id']) or not ID.fullmatch(values['incarnation'])
                or values['lane'] not in ('actions', 'output', 'children', 'tasks', 'routing')):
            raise InvalidReadRequest()
        return reader.read_local_selected(_context(values),
                    dict(job_id=values['job_id'], incarnation=values['incarnation']),
                    lane=values['lane'], cursor=values.get('cursor'),
                    include_context=values.get('include_context') == 'true')
    return _respond(read)
