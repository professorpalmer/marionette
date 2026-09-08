"""Discardable local observation index; execution rows remain the authority.

Writer order: execution lock -> index lock. Polls take only the index lock.
No index lock spans filesystem work. Sorted-key maintenance costs O(N) on
insert/delete; the finite revision ring costs O(1). Provider writers compare
selected task/route projections; readers never construct these snapshots.
"""
from __future__ import annotations

import base64
import bisect
import hashlib
import hmac
import json
import math
import secrets
import threading
import time
from dataclasses import dataclass

LIMIT = 50
MAX_SCAN = 51
MAX_BYTES = 32768
JOURNAL_SIZE = 2048
CURSOR_SECONDS = 120
LIFECYCLES = frozenset(('registered', 'queued', 'pending', 'running', 'in_progress',
                        'started', 'completed', 'complete', 'done', 'failed', 'cancelled',
                        'timeout', 'timed_out', 'truncated', 'partial', 'unknown', 'stitching', 'stalled'))
ACTIVE = frozenset(('registered', 'queued', 'pending', 'running', 'in_progress', 'started', 'stitching'))
ATTENTION = frozenset(('stalled', 'failed', 'timeout', 'timed_out', 'truncated', 'partial', 'unknown'))


@dataclass(frozen=True)
class LocalRef:
    job_id: str
    incarnation: str

    def wire(self):
        return dict(job_id=self.job_id, incarnation=self.incarnation)


def text(value, limit):
    # Slice before encoding, stripping, conversion or copying.
    return value[:limit] if isinstance(value, str) else ''


def identity(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and value.isascii() and all(
        c.isalnum() or c in '_-' for c in value)


def number(value):
    return value if type(value) in (int, float) and abs(value) < 1e18 and math.isfinite(value) else None


def count(value):
    return len(value) if isinstance(value, (list, tuple)) else None


def envelope(outcome='unavailable', *, revision=0, checkpoint=0, scanned=0):
    return dict(version=1, page=dict(outcome=outcome, revision=revision,
                checkpoint=checkpoint, scanned=scanned, next_cursor=None), rows=[],
                coverage=dict(membership='retained_local_history', ordering='id',
                              historical='unavailable'), missing=['unretained_history'])


def encoded(value):
    return json.dumps(value).encode('utf-8')


def selected_context(row, kind):
    """Only exact owner scalars; never materialize tasks or provider payloads."""
    def bounded(value, limit):
        if not isinstance(value, str):
            return None
        prefix = value[:limit]
        raw = prefix.encode('utf-8', errors='replace')
        clipped = raw[:limit].decode('utf-8', errors='ignore')
        return dict(text=clipped, truncated=len(value) > len(prefix) or len(raw) > limit)

    command = kind in ('run_command', 'run_command_batch')
    source = 'command_preview' if command else 'goal'
    request = bounded(row.get(source), 2048)
    return dict(source=source, request=request, cwd=bounded(row.get('cwd'), 512),
                omission='request_unavailable' if request is None else
                         'raw_command_not_retained' if command else 'none')


def capped_fields(source, caps):
    result = {key: text(source.get(key), cap) for key, cap in caps.items()}
    result['truncated'] = any(isinstance(source.get(key), str) and len(source[key]) > cap
                              for key, cap in caps.items())
    return result


def selected_task(source):
    if not isinstance(source, dict):
        return dict(unavailable=True)
    result = capped_fields(source, dict(role=160, instruction=1024, status=40, adapter=40, model=160))
    result['task_id'] = source.get('id') if identity(source.get('id')) else None
    result['model_kind'] = 'assigned' if result['model'] else 'unavailable'
    return result


def selected_route(source, row, ordinal):
    if not isinstance(source, dict) or source.get('type') != 'ROUTING':
        return None
    result = capped_fields(source, dict(model=160, role=160, policy=40, adapter=40,
                                        created_by=40, detail=512))
    result.update(ordinal=ordinal, task_id=None, association='unavailable',
                  model_kind='realized' if source.get('model_kind') == 'realized' else 'forecast',
                  est_cost_usd=number(source.get('est_cost_usd')))
    tasks = row.get('tasks')
    if not isinstance(tasks, list) or len(tasks) != 1 or not isinstance(tasks[0], dict):
        return result
    task = tasks[0]
    tid = task.get('id')
    if not identity(tid) or tid != row.get('id', '') + '-w0':
        return result
    if source.get('task_id') == tid:
        result.update(task_id=tid, association='explicit')
    elif 'task_id' not in source:
        # Legacy ownership is attested by the local producer's retained label,
        # exact sole task and adapter, never by a route creator string alone.
        label = row.get('label')
        if not isinstance(label, str) or len(label) > 2048:
            return result
        try:
            label = json.loads(label)
        except (ValueError, TypeError):
            return result
        if (isinstance(label, dict) and label.get('origin') == 'marionette'
                and label.get('session_id') == row.get('session_id')
                and row.get('adapter') in ('native', 'agentic')
                and task.get('adapter') == row.get('adapter')
                and row.get('job_kind') not in ('run_command', 'run_command_batch', 'parallel_wave')
                and row.get('role') not in ('command', 'command_batch', 'parallel_wave')):
            result.update(task_id=tid, association='legacy_owner_single_task')
    return result

def operator_facts(jid, row, kind):
    """Copy only writer-owned scalars; receipt construction is never a read path."""
    if not any(key in row for key in ('model', 'adapter', 'tokens', 'financial_receipt',
                                      'accounting_owned', 'accounting_scope')):
        return dict(economics=dict(kind='unavailable'))
    caps = dict(model=160, adapter=40)
    display = {key: text(row.get(key), cap) for key, cap in caps.items()}
    if kind != 'provider':
        display['model'] = ''
    display['label'] = {'provider': 'Provider worker', 'run_command': 'Command',
                        'run_command_batch': 'Command batch',
                        'parallel_wave': 'Parallel wave'}[kind]
    display['truncated'] = any(isinstance(row.get(key), str) and len(row[key]) > cap
                               for key, cap in caps.items())
    excluded = (row.get('accounting_owned') is False
                or row.get('accounting_scope') == 'visibility_only')
    accounting = dict(kind='excluded' if excluded else
                      'declared' if row.get('accounting_owned') is True else 'unresolved',
                      aggregation_authority=False)
    usage = dict(kind='unknown')
    economics = dict(kind='unavailable')
    if excluded:
        return dict(display=display, usage=usage, economics=economics, accounting=accounting)
    tokens = row.get('tokens')
    worker = row.get('worker_provenance')
    usage_known = worker.get('usage_known') if isinstance(worker, dict) else None
    if (type(tokens) is int and 0 <= tokens < 10**18 and usage_known is not False
            and (tokens > 0 or usage_known is True)):
        usage = dict(kind='reported', tokens=tokens, source='local_job_tokens')
    receipt = row.get('financial_receipt')
    if (isinstance(receipt, dict) and receipt.get('job_id') == jid
            and receipt.get('owner') == 'local'):
        basis = receipt.get('spend_basis')
        amount = number(receipt.get('spend_usd'))
        provenance = receipt.get('cost_provenance')
        estimated = receipt.get('estimated')
        valid_basis = (
            (basis == 'provider' and provenance == 'provider' and estimated is False)
            or (basis == 'measured' and provenance in ('live', 'static') and estimated is False)
            or (basis == 'estimated' and provenance in ('provider', 'live', 'static', 'default', 'unknown')
                and estimated is True))
        if valid_basis and amount is not None and amount >= 0:
            economics.update(kind=basis, spend_usd=amount, estimated=estimated,
                             cost_provenance=provenance, source='financial_receipt')
        for key in ('route_forecast_usd', 'estimated_savings_usd'):
            value = number(receipt.get(key))
            if value is not None and value >= 0:
                economics[key] = value
    return dict(display=display, usage=usage, economics=economics, accounting=accounting)


class LocalMetadataIndex:
    def __init__(self, owner):
        self.version = 1
        self.owner = owner
        self.incarnation = secrets.token_hex(16)
        self.lock = threading.RLock()
        self.secret = secrets.token_bytes(32)
        self.revision = 0
        self.rows = {}
        self._selected_facts = {}
        self.keys = []
        self.journal = [None] * JOURNAL_SIZE
        self.active_keys = []
        self.active_revision = 0
        self.active_epoch = 0
        self.active_journal = [None] * JOURNAL_SIZE
        self.invalid = set()
        self.available = True

    def describe(self):
        with self.lock:
            return dict(available=self.version == 1 and self.available and not self.invalid,
                        incarnation=self.incarnation, version=self.version, lanes=['active', 'history'],
                        active_statuses=sorted(ACTIVE), attention_statuses=sorted(ATTENTION))

    def ref(self, jid):
        return LocalRef(jid, self.incarnation).wire()

    def project(self, jid, row):
        if not identity(jid) or not isinstance(row, dict) or row.get('id') != jid:
            raise ValueError('invalid local identity')
        sid = row.get('session_id')
        if not identity(sid):
            raise ValueError('unknown session ownership')
        kind = row.get('job_kind')
        if kind not in ('run_command', 'run_command_batch', 'parallel_wave'):
            role = row.get('role')
            kind = {'command': 'run_command', 'command_batch': 'run_command_batch',
                    'parallel_wave': 'parallel_wave'}.get(role, 'provider')
        parent = row.get('parent_wave_id') or row.get('batch_id')
        status = row.get('status')
        lifecycle = status if isinstance(status, str) and len(status) <= 40 and status in LIFECYCLES else 'unknown'
        return dict(local_ref=self.ref(jid), lifecycle=lifecycle,
                    activity='active' if lifecycle in ACTIVE else 'attention' if lifecycle in ATTENTION else 'terminal',
                    kind=kind, session_id=sid,
                    parent_ref=self.ref(parent) if identity(parent) else None,
                    task_count=count(row.get('tasks')), action_count=count(row.get('actions')),
                    artifact_count=count(row.get('artifacts')), child_count=count(row.get('child_job_ids')),
                    created_at=number(row.get('created_at')), updated_at=number(row.get('updated_at')),
                    receipts=dict(terminal=isinstance(row.get('terminal_receipt'), dict),
                                  launch=isinstance(row.get('launch_checkpoint'), dict),
                                  recovery=isinstance(row.get('recovery_receipt'), dict),
                                  child=isinstance(row.get('child_launch_receipt'), dict)),
                    **operator_facts(jid, row, kind), deleted=False)

    def publish(self, jid, row, *, force=False):
        try:
            projected = self.project(jid, row) if row is not None else None
        except (ValueError, TypeError):
            with self.lock:
                self.invalid.add(jid)
            return
        with self.lock:
            self.invalid.discard(jid)
            previous = self.rows.get(jid)
            # Writer-only snapshots of the selected wire fields. Never traverse
            # arbitrary artifact bodies, and never put these facts in list rows.
            if projected is not None and projected['kind'] == 'provider':
                tasks, artifacts = row.get('tasks'), row.get('artifacts')
                facts = (
                    [selected_task(task) for task in tasks] if isinstance(tasks, list) else None,
                    [selected_route(art, row, i) for i, art in enumerate(artifacts)]
                    if isinstance(artifacts, list) else None,
                )
                force = force or self._selected_facts.get(jid) != facts
                self._selected_facts[jid] = facts
            else:
                self._selected_facts.pop(jid, None)
            if projected is None and previous is None:
                return
            if projected is not None and previous is not None and not force:
                if all(previous.get(k) == v for k, v in projected.items()):
                    return
            self.revision += 1
            if projected is None:
                del self.rows[jid]
                self.keys.pop(bisect.bisect_left(self.keys, jid))
                projected = dict(local_ref=self.ref(jid), session_id=previous['session_id'], deleted=True)
            else:
                if previous is None:
                    bisect.insort(self.keys, jid)
                self.rows[jid] = projected
            projected['revision'] = self.revision
            was_active = previous is not None and previous.get('lifecycle') in ACTIVE
            is_active = projected.get('lifecycle') in ACTIVE
            if was_active != is_active:
                if is_active:
                    bisect.insort(self.active_keys, jid)
                else:
                    self.active_keys.pop(bisect.bisect_left(self.active_keys, jid))
            if (was_active != is_active or (was_active and is_active
                    and previous['session_id'] != projected['session_id'])):
                self.active_epoch += 1
            if was_active or is_active:
                self.active_revision += 1
                self.active_journal[self.active_revision % JOURNAL_SIZE] = (
                    self.active_revision, projected, previous)
            # Previous membership permits exact scope-loss removals.
            self.journal[self.revision % JOURNAL_SIZE] = (self.revision, projected, previous)

    def _binding(self, context, mode, after, selection=None, lane=None):
        return hashlib.sha256(encoded([context, self.incarnation, mode, after, selection, lane])).hexdigest()

    def _token(self, binding, revision, offset, deadline, epoch=None):
        data = encoded([binding, revision, offset, deadline, epoch])
        return base64.urlsafe_b64encode(hmac.digest(self.secret, data, 'sha256') + data).decode()

    def _decode(self, token, binding):
        from .job_readmodel import InvalidReadRequest
        try:
            if not isinstance(token, str) or len(token) > 4096:
                raise ValueError()
            raw = base64.b64decode(token, altchars=b'-_', validate=True)
            signature, data = raw[:32], raw[32:]
            if not hmac.compare_digest(signature, hmac.digest(self.secret, data, 'sha256')):
                raise ValueError()
            bound, revision, offset, deadline, epoch = json.loads(data)
            if bound != binding or type(revision) is not int or type(offset) is not int or offset < 0:
                raise ValueError()
            return revision, offset, deadline, epoch
        except (ValueError, TypeError, UnicodeError) as exc:
            raise InvalidReadRequest() from exc

    @staticmethod
    def _owned(row, context):
        return row is not None and (context['scope'] != 'session' or row['session_id'] == context['session_id'])

    def read_page(self, context, *, mode='snapshot', cursor=None, after_revision=0, lane='history'):
        from .job_readmodel import InvalidReadRequest
        if (lane not in ('history', 'active') or mode not in ('snapshot', 'changes')
                or type(after_revision) is not int or after_revision < 0):
            raise InvalidReadRequest()
        binding = self._binding(context, mode, after_revision, lane=lane)
        decoded = self._decode(cursor, binding) if cursor is not None else None
        with self.lock:
            result = envelope(checkpoint=after_revision)
            result['incarnation'] = self.incarnation
            result['lane'] = lane
            if lane == 'active':
                result['coverage'].update(membership='retained_local_active', metadata='live_during_traversal')
            if self.version != 1 or not self.available or self.invalid:
                result['missing'].append('local_index_unavailable')
                return result
            revision = self.active_revision if lane == 'active' else self.revision
            keys = self.active_keys if lane == 'active' else self.keys
            journal = self.active_journal if lane == 'active' else self.journal
            epoch = self.active_epoch if lane == 'active' else self.revision
            upper, offset, deadline, captured_epoch = decoded or (
                revision, 0 if mode == 'snapshot' else after_revision,
                int(time.monotonic()) + CURSOR_SECONDS, epoch)
            if (deadline < time.monotonic() or upper > revision or after_revision > upper
                    or (mode == 'snapshot' and captured_epoch != epoch)
                    or (mode == 'changes' and offset < revision - JOURNAL_SIZE)):
                result['page']['outcome'] = 'expired'
                return result
            end = len(keys) if mode == 'snapshot' else upper
            scanned = 0
            budget = 22000  # Reserve envelope/context/cursor and ASCII escaping.
            while offset < end and scanned < MAX_SCAN and len(result['rows']) < LIMIT:
                if mode == 'snapshot':
                    row, previous = self.rows[keys[offset]], None
                else:
                    event = journal[(offset + 1) % JOURNAL_SIZE]
                    if event is None or event[0] != offset + 1:
                        result['page']['outcome'] = 'unavailable'
                        result['rows'] = []
                        result['missing'].append('journal_corrupt')
                        return result
                    _, row, previous = event
                scanned += 1
                item = None
                current_member = self._owned(row, context) and (lane == 'history' or row.get('lifecycle') in ACTIVE)
                previous_member = self._owned(previous, context) and (lane == 'history' or previous.get('lifecycle') in ACTIVE)
                if current_member:
                    item = row
                elif mode == 'changes' and previous_member:
                    item = dict(local_ref=row['local_ref'], session_id=previous['session_id'],
                                deleted=True, revision=row['revision'])
                if item is not None:
                    size = len(encoded(item))
                    if size > budget:
                        break
                    # Only small projected fields are serialized/copied, never execution bodies.
                    result['rows'].append(json.loads(encoded(item)))
                    budget -= size + 2
                offset += 1
            complete = offset == end
            result['page'].update(outcome='complete' if complete else 'partial', revision=upper,
                                  checkpoint=upper if complete else after_revision, scanned=scanned,
                                  next_cursor=None if complete else self._token(binding, upper, offset, deadline, captured_epoch))
            return result

    def read_selected(self, context, local_ref, *, lane='actions', cursor=None, include_context=False):
        from .job_readmodel import InvalidReadRequest
        if (not isinstance(local_ref, dict) or set(local_ref) != {'job_id', 'incarnation'}
                or type(include_context) is not bool
                or not identity(local_ref.get('job_id')) or lane not in ('actions', 'output', 'children', 'tasks', 'routing')):
            raise InvalidReadRequest()
        binding = self._binding(context, 'selected', 0, local_ref, [lane, include_context])
        decoded = self._decode(cursor, binding) if cursor is not None else None
        result = envelope()
        result.update(local_ref=local_ref, lane=lane, cancellation_authority=False)
        if local_ref['incarnation'] != self.incarnation:
            result['page']['outcome'] = 'expired'
            return result
        # Selected reads alone may touch the exact execution row. No I/O occurs here.
        with self.owner._local_jobs_lock:
            with self.lock:
                if self.version != 1 or not self.available or self.invalid:
                    result['missing'].append('local_index_unavailable')
                    return result
                summary = self.rows.get(local_ref['job_id'])
                if not self._owned(summary, context):
                    result['missing'].append('selected_membership_unavailable')
                    return result
                revision = summary['revision']
                upper, offset, deadline, _ = decoded or (revision, 0, int(time.monotonic()) + CURSOR_SECONDS, None)
                if upper != revision or deadline < time.monotonic():
                    result['page']['outcome'] = 'expired'
                    return result
                result['summary'] = json.loads(encoded(summary))
                result['page']['revision'] = revision
                row = self.owner._local_jobs.get(local_ref['job_id'])
                if (not isinstance(row, dict) or row.get('id') != local_ref['job_id']
                        or row.get('session_id') != summary['session_id']):
                    result.pop('summary', None)
                    return result
                if include_context:
                    result['selected_context'] = selected_context(row, summary['kind'])
                value = row.get({'actions': 'actions', 'children': 'child_job_ids', 'output': 'output', 'tasks': 'tasks', 'routing': 'artifacts'}[lane])
                expected = str if lane == 'output' else list
                if not isinstance(value, expected):
                    result['missing'].append('selected_lane_unavailable')
                    return result
                result['total'] = len(value)
                scanned = 0
                budget = 22000 - len(encoded(summary)) - len(encoded(result.get('selected_context')))
                if lane == 'output':
                    result['output'] = dict(coverage='in_memory_only',
                        source_chars=number(row.get('output_chars')),
                        spilled=row.get('output_spilled') is True or bool(row.get('spill_uri')))
                    chunk = value[offset:offset + min(2048, max(1, budget // 12))]
                    result['rows'] = [dict(offset=offset, text=chunk)] if chunk else []
                    offset += len(chunk)
                    scanned = 1 if chunk else 0
                else:
                    while offset < len(value) and scanned < MAX_SCAN and len(result['rows']) < LIMIT:
                        source = value[offset]
                        scanned += 1
                        if lane == 'tasks':
                            item = selected_task(source)
                        elif lane == 'routing':
                            item = selected_route(source, row, offset)
                            if item is None:
                                offset += 1
                                continue
                        elif lane == 'children':
                            item = dict(local_ref=self.ref(source)) if identity(source) else dict(unavailable=True)
                        elif isinstance(source, dict):
                            caps = dict(action_id=128, kind=64, goal=240, status=40, error=240, worker_id=256)
                            item = {key: text(source.get(key), cap) for key, cap in caps.items()}
                            item['duration_ms'] = number(source.get('duration_ms'))
                            item['truncated'] = any(isinstance(source.get(key), str) and len(source[key]) > cap
                                                    for key, cap in caps.items())
                        else:
                            item = dict(unavailable=True)
                        size = len(encoded(item))
                        if size > budget:
                            if not result['rows'] and lane in ('tasks', 'routing'):
                                result['missing'].append('selected_row_budget')
                                result['page']['scanned'] = scanned
                                return result
                            break
                        budget -= size + 2
                        result['rows'].append(item)
                        offset += 1
                complete = offset == len(value)
                result['page'].update(outcome='complete' if complete else 'partial', revision=upper,
                                      checkpoint=upper if complete else 0, scanned=scanned,
                                      next_cursor=None if complete else self._token(binding, upper, offset, deadline))
        return result


class ObservedRow(dict):
    """Observe native top-level mutations, including writes that do not persist.

    Nested actions are replaced by the native action writers. Persistence also
    republishes counts, covering legacy nested list changes at that seam.
    """
    def __init__(self, value, index, jid):
        super().__init__(value)
        self._index, self._jid = index, jid

    def __copy__(self):
        return dict(self)

    def __deepcopy__(self, memo):
        import copy
        return copy.deepcopy(dict(self), memo)

    def _publish(self):
        if self._index.owner._local_jobs.get(self._jid) is self:
            self._index.publish(self._jid, self, force=True)

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._publish()

    def __delitem__(self, key):
        super().__delitem__(key)
        self._publish()

    def update(self, *args, **kwargs):
        super().update(*args, **kwargs)
        self._publish()

    def pop(self, key, *default):
        value = super().pop(key, *default)
        self._publish()
        return value

    def clear(self):
        super().clear()
        self._publish()

    def popitem(self):
        value = super().popitem()
        self._publish()
        return value

    def __ior__(self, other):
        self.update(other)
        return self

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]


class ObservedJobs(dict):
    def __copy__(self):
        return dict(self)

    def __deepcopy__(self, memo):
        import copy
        return copy.deepcopy(dict(self), memo)

    def __init__(self, values, index):
        super().__init__()
        self.index = index
        for jid, row in values.items():
            self[jid] = row

    def __setitem__(self, jid, row):
        if isinstance(row, ObservedRow) and row._index is self.index and row._jid == jid:
            wrapped = row
        else:
            wrapped = ObservedRow(row, self.index, jid) if isinstance(row, dict) else row
        super().__setitem__(jid, wrapped)
        self.index.publish(jid, wrapped)

    def __delitem__(self, jid):
        super().__delitem__(jid)
        self.index.publish(jid, None)

    def pop(self, jid, *default):
        if jid in self:
            value = self[jid]
            del self[jid]
            return value
        if default:
            return default[0]
        raise KeyError(jid)

    def clear(self):
        for jid in list(self):
            del self[jid]

    def update(self, *args, **kwargs):
        for jid, row in dict(*args, **kwargs).items():
            self[jid] = row

    def setdefault(self, jid, default=None):
        if jid not in self:
            self[jid] = default
        return self[jid]

    def popitem(self):
        if not self:
            raise KeyError('popitem(): dictionary is empty')
        jid = next(reversed(self))
        return jid, self.pop(jid)

    def __ior__(self, other):
        self.update(other)
        return self
