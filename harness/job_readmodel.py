"""Body-free PM list reads and explicitly selected, revision-fenced projections."""
from __future__ import annotations

import base64
import hmac
import json
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal

from puppetmaster.identity import StoreIdentityError
from puppetmaster.models import JobRef
from puppetmaster.state import state_identity
from puppetmaster.store_factory import create_store

from .cli_job_merge import (
    _foreign_state_dir_candidates,
    cross_project_scan_enabled,
    is_marionette_host_scratch_dir,
    resolve_cli_state_dir,
)
from .job_scoping import job_owned_by_marionette
from .paths import same_workspace_path
from .job_metadata_capability import ActiveContext, ViewChanged
from . import job_expert
from .job_expert_compaction import read_selected_compaction

PAGE_BUDGET = dict(limit=50, max_scan=51, max_bytes=32768)
EXACT_BUDGET = dict(limit=1, max_scan=2, max_bytes=8192)
SNAPSHOT_RETRIES = 3


def _read_page(method, *args, **kwargs):
    """Live workers hold SQLite write locks for short bursts; the store reports that as
    ``read_snapshot_unavailable`` with a retry hint. Honor it a bounded number of times so a
    lock burst does not surface as an unavailable roster."""
    page = method(*args, **kwargs)
    for _ in range(SNAPSHOT_RETRIES - 1):
        if page.outcome != 'unavailable' or getattr(page, 'reason', None) != 'read_snapshot_unavailable':
            break
        time.sleep(min(max(getattr(page, 'retry_after_ms', None) or 100, 20), 250) / 1000)
        page = method(*args, **kwargs)
    return page
HISTORY_BUDGET = dict(limit=20, max_scan=21, max_bytes=8192)
HISTORY_LANES = {
    'attempts': 'list_attempt_refs', 'runs': 'list_run_refs',
    'process_outcomes': 'list_process_outcome_refs',
    'observations': 'list_usage_observation_refs',
}
RUNNING = frozenset(('queued', 'running', 'stitching', 'in_progress', 'pending', 'started'))
PM_STATUSES = RUNNING | frozenset(('complete', 'failed', 'stalled', 'cancelled'))
ATTENTION = frozenset(('failed', 'stalled'))
MEMBERSHIP_IDS = 200


class InvalidReadRequest(ValueError):
    pass


@dataclass(frozen=True)
class ReadContext:
    session_id: str
    repo: str
    view_generation: str
    scope: Literal['session', 'repo', 'all']


@dataclass(frozen=True)
class StoreSelection:
    source: Literal['harness', 'cli']
    state_id: str


@dataclass(frozen=True)
class PMSelection:
    context: ReadContext
    store: StoreSelection
    job_ref: JobRef

    def wire(self):
        return dict(job_ref=self.job_ref.as_dict(), source=self.store.source,
                    session_id=self.context.session_id, repo=self.context.repo)


@dataclass(frozen=True)
class KnownStore:
    selection: StoreSelection
    root: Path
    backend: Literal['sqlite', 'file']
    cross_project: bool = False
    handle: object = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class KnownSources:
    stores: tuple[KnownStore, ...]

    @classmethod
    def from_roots(cls, roots):
        """Trusted host roots only; bounded and deduplicated before serving reads."""
        entries = list(roots)
        if len(entries) > 34:
            raise ValueError('at most two primary and 32 sibling roots')
        result = {}
        for source, root, backend, cross_project in sorted(entries, key=lambda x: x[0] != 'harness'):
            if source not in ('harness', 'cli') or backend not in ('sqlite', 'file'):
                raise ValueError('invalid host source')
            path = Path(root).resolve()
            if is_marionette_host_scratch_dir(path):
                continue
            identity = state_identity(path)
            if identity not in result:
                database = path / ('state.sqlite3' if backend == 'sqlite' else 'metadata.sqlite3')
                # Store handles are captured at view creation, not in the HTTP
                # hot path. A disappearing root can never be recreated by a poll.
                handle = create_store(backend, path, mode='deferred') if path.is_dir() and database.is_file() else None
                result[identity] = KnownStore(StoreSelection(source, identity), path, backend, cross_project, handle)
        return cls(tuple(result.values()))

    def resolve(self, selection):
        return next((s for s in self.stores if s.selection == selection), None)


def discover_sources(harness_root, workspace, *, harness_backend='sqlite'):
    """Run at host view creation, never inside a metadata request."""
    primary = resolve_cli_state_dir(workspace)
    roots = [('harness', harness_root, harness_backend, False)]
    if primary:
        roots.append(('cli', primary, 'sqlite', False))
    if cross_project_scan_enabled():
        roots.extend(('cli', root, 'sqlite', True) for root in
                     _foreign_state_dir_candidates(str(Path(primary).resolve()) if primary else '', max_opens=32))
    return KnownSources.from_roots(roots)


def _attach(known):
    # Public attach() sets journal_mode=WAL even on rejected old stores.
    # Deferred construction plus public metadata reads never invokes init/ensure.
    # Reuse the startup handle: constructors can create roots and chmod them.
    database = known.root / ('state.sqlite3' if known.backend == 'sqlite' else 'metadata.sqlite3')
    if not known.root.is_dir() or not database.is_file():
        return None
    return known.handle


def _page(outcome='unavailable', revision=0, cursor=None, scanned=0, checkpoint=0):
    return dict(outcome=outcome, revision=revision, next_cursor=cursor,
                scanned=scanned, checkpoint=checkpoint)


def _size(value):
    # Exactly the encoding used by http_routes.send_json, including ASCII escaping.
    return len(json.dumps(value).encode('utf-8'))


class MetadataReader:
    """One host view lifetime; immutable sources and a bounded authorization window.

    active_context must atomically capture session, repo and an ABA-safe generation.
    No callback may discover roots, instantiate DurableState or load job bodies.
    """

    def __init__(self, active_context: Callable[[], ActiveContext], sources: KnownSources, local_handle=None):
        self.active_context = active_context
        self.sources = sources
        self.local_handle = local_handle
        self._secret = secrets.token_bytes(32)
        if len(sources.stores) > 34:
            raise ValueError('at most 34 known sources')
        # The captured view fixes sources/context; only scopes and status lanes vary.
        self.membership_stream_limit = sum(
            len(RUNNING) * 2 if s.cross_project else (len(PM_STATUSES) + 1) * 3
            for s in sources.stores)
        self._seen = OrderedDict()
        self._headers = OrderedDict()
        from .api.cost import _swarm_registry
        self._registry = tuple(_swarm_registry()[:1024])
        self._lock = threading.Lock()

    def check(self, ctx):
        if ctx.scope not in ('session', 'repo', 'all'):
            raise InvalidReadRequest()
        active = self.active_context()
        if (active.session_id != ctx.session_id or active.generation != ctx.view_generation
                or not same_workspace_path(active.repo, ctx.repo)):
            raise ViewChanged()
        return active

    def _binding(self, ctx, selection, lane, after, status, job_id):
        return [asdict(ctx), asdict(selection), lane, after, status, job_id]

    def _cursor(self, token, binding):
        if token is None:
            return None
        try:
            raw = base64.b64decode(token, altchars=b'-_', validate=True)
            signature, data = raw[:32], raw[32:]
            if not hmac.compare_digest(signature, hmac.digest(self._secret, data, 'sha256')):
                raise ValueError()
            value = json.loads(data)
            if value['binding'] != binding or not isinstance(value['cursor'], str):
                raise ValueError()
            return value['cursor']
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            raise InvalidReadRequest() from exc

    def _wrap(self, cursor, binding):
        if cursor is None:
            return None
        data = json.dumps(dict(binding=binding, cursor=cursor), separators=(',', ':')).encode()
        return base64.urlsafe_b64encode(hmac.digest(self._secret, data, 'sha256') + data).decode()

    def _wire_page(self, page, binding, after):
        result = _page(page.outcome, page.revision, self._wrap(page.next_cursor, binding),
                       page.scanned, page.revision if page.outcome == 'complete' else after)
        if page.reason is not None:
            result['reason'] = page.reason
        if page.retry_after_ms is not None:
            result['retry_after_ms'] = page.retry_after_ms
        return result

    def _known(self, ctx, selection):
        known = self.sources.resolve(selection)
        if known and (not known.cross_project or (ctx.scope != 'repo' and cross_project_scan_enabled())):
            return known
        return None

    def _owned(self, row, known, ctx, *, pin=False):
        if not row.session_id or not job_owned_by_marionette(
            session_id=row.session_id, origin=row.origin or '', source=known.selection.source,
            allow_registered_heal=False,
        ):
            return False
        if known.cross_project and row.status not in RUNNING:
            return False
        return (ctx.scope != 'session' or row.session_id == ctx.session_id
                or (pin and not known.cross_project))

    def _previous_owned(self, row, known, ctx, status):
        if row.previous_membership != 'present':
            return False
        previous = replace(row, status=row.previous_status, origin=row.previous_origin,
                           project_id=row.previous_project_id, session_id=row.previous_session_id)
        return self._owned(previous, known, ctx) and (status is None or previous.status == status)

    @staticmethod
    def _display(row):
        return dict(kind='available' if row.goal_preview is not None else 'unavailable',
                    goal_preview=row.goal_preview, goal_preview_truncated=row.goal_preview_truncated,
                    delivery=row.delivery, quality=row.quality,
                    **({} if row.goal_preview is not None else {'reason': 'goal_preview_unavailable'}))

    def _summary(self, row, ctx, selection, *, removed=False):
        result = dict(selection=PMSelection(ctx, selection, row.job_ref).wire(),
                      revision=row.revision, deleted=removed)
        if not removed:
            result.update(lifecycle=row.status,
                          activity=('active' if row.status in RUNNING else 'attention' if row.status in ATTENTION
                                    else 'terminal' if row.status in PM_STATUSES else 'unknown'),
                          ownership=dict(origin=row.origin, session_id=row.session_id, project_id=row.project_id),
                          task_count=row.task_count, artifact_count=row.artifact_count, stamp=row.stamp,
                          display=self._display(row), economics=dict(kind='unavailable', reason='selected_only'))
        return result

    @staticmethod
    def _coerce_store_job_ref(value, state_id):
        if isinstance(value, JobRef):
            return value if value.state_id == state_id else None
        if not isinstance(value, dict):
            return None
        raw = value.get('job_ref') if isinstance(value.get('job_ref'), dict) else value
        if not isinstance(raw, dict):
            return None
        try:
            version = raw.get('version', 1)
            ref = JobRef(raw['job_id'], raw['state_id'], version=int(version),
                         incarnation=raw.get('incarnation'))
        except (ValueError, KeyError, TypeError):
            return None
        return ref if ref.state_id == state_id else None

    def _session_known_job_refs(self, ctx, selection):
        """PM refs the harness already associates with this session (local + registry)."""
        found = {}

        def remember(ref):
            if ref is not None and ref.job_id not in found:
                found[ref.job_id] = ref

        handle = self.local_handle
        if handle is not None:
            rows = getattr(handle, 'rows', None)
            if isinstance(rows, dict):
                def collect_local():
                    for jid in sorted(rows):
                        row = rows[jid]
                        if not isinstance(row, dict) or row.get('deleted'):
                            continue
                        if ctx.scope != 'all' and row.get('session_id') != ctx.session_id:
                            continue
                        canonical = row.get('canonical')
                        if (not isinstance(canonical, dict) or canonical.get('source') != 'harness'
                                or (ctx.scope != 'all' and canonical.get('session_id') != ctx.session_id)):
                            continue
                        remember(self._coerce_store_job_ref(canonical.get('job_ref'), selection.state_id))
                lock = getattr(handle, 'lock', None)
                if lock is not None:
                    with lock:
                        collect_local()
                else:
                    collect_local()
        for entry in self._registry:
            if isinstance(entry, dict):
                sid = entry.get('session_id')
                payload = entry
            else:
                sid = getattr(entry, 'session_id', None)
                payload = getattr(entry, 'job_ref', None)
                if payload is None:
                    payload = entry
            if ctx.scope != 'all' and sid != ctx.session_id:
                continue
            remember(self._coerce_store_job_ref(payload, selection.state_id))
        return tuple(found[key] for key in sorted(found)[:50])

    def _exact_owned_row(self, store, job_ref, known, ctx, status):
        try:
            page = store.list_job_summaries(job_ref=job_ref, **EXACT_BUDGET)
        except Exception:
            return None
        if page.outcome != 'complete' or len(page.items) != 1 or page.items[0].deleted:
            return None
        row = page.items[0]
        if row.job_ref != job_ref or not self._owned(row, known, ctx):
            return None
        if status is not None and row.status != status:
            return None
        return row

    def read_job_page(self, ctx, selection, *, mode='snapshot', cursor=None, after_revision=0, status=None):
        if (mode not in ('snapshot', 'changes') or type(after_revision) is not int or after_revision < 0
                or (status is not None and status not in PM_STATUSES)):
            raise InvalidReadRequest()
        active = self.check(ctx)
        binding = self._binding(ctx, selection, mode, after_revision, status, None)
        pm_cursor = self._cursor(cursor, binding)
        result = dict(version=1, context=asdict(ctx), store=asdict(selection), mode=mode,
                      page=_page(checkpoint=after_revision), rows=[],
                      coverage=dict(membership='known_metadata', legacy='unavailable', ordering='id', store_set='known_sources'),
                      missing=['legacy_ownership', 'economics'])
        known = self._known(ctx, selection)
        if known is None:
            result['missing'].append('store_unavailable')
            self.check(ctx)
            return result
        result['lanes'] = dict(statuses=sorted(RUNNING if known.cross_project else PM_STATUSES),
                               active_statuses=sorted(RUNNING),
                               attention_statuses=[] if known.cross_project else sorted(ATTENTION))
        if known.cross_project and status not in RUNNING:
            raise InvalidReadRequest()
        store = _attach(known)
        if store is None:
            result['missing'].append('store_unavailable')
            self.check(ctx)
            return result
        filters = dict(session_id=ctx.session_id if ctx.scope == 'session' else None,
                       origin='marionette' if selection.source == 'cli' else None, status=status)
        # Exact reads run before the scan so every prepended row's revision is
        # <= the scan page revision (the client parser requires it).
        prepended = []
        if (mode == 'snapshot' and pm_cursor is None
                and selection.source == 'harness' and not known.cross_project):
            for ref in self._session_known_job_refs(ctx, selection):
                row = self._exact_owned_row(store, ref, known, ctx, status)
                if row is not None:
                    prepended.append(row)
        method = store.list_job_summaries if mode == 'snapshot' else store.read_job_summary_changes
        try:
            page = method(cursor=pm_cursor, after_revision=after_revision, **filters, **PAGE_BUDGET)
        except StoreIdentityError:
            result['missing'].append('selection_changed')
            self.check(ctx)
            return result
        result['page'] = self._wire_page(page, binding, after_revision)
        if page.reason:
            result['missing'].append(page.reason)
        if page.outcome not in ('complete', 'partial'):
            self.check(ctx)
            return result
        key = (active, ctx.scope, selection, status)
        with self._lock:
            seen = self._seen.get(key, {})
            updates = []
            uncertain = False
            # Every scanned row must fit: its cursor already advances past it.
            # Exact rows supplement spare capacity without consuming scan coverage.
            scanned_refs = {row.job_ref for row in page.items}
            for row in page.items:
                owned = not row.deleted and self._owned(row, known, ctx)
                previous_owned = self._previous_owned(row, known, ctx, status)
                if owned:
                    result['rows'].append(self._summary(row, ctx, selection))
                    updates.append((row.job_ref, row.revision))
                elif row.job_ref in seen or previous_owned:
                    if row.revision >= seen.get(row.job_ref, 0):
                        result['rows'].append(self._summary(row, ctx, selection, removed=True))
                elif mode == 'changes' and row.previous_membership == 'unavailable':
                    uncertain = True
            for row in prepended:
                if (len(result['rows']) >= 50 or row.job_ref in scanned_refs
                        or row.revision > page.revision):
                    continue
                scanned_refs.add(row.job_ref)
                result['rows'].append(self._summary(row, ctx, selection))
                updates.append((row.job_ref, row.revision))
            if uncertain:
                result.update(page=_page(checkpoint=after_revision), rows=[])
                result['missing'].append('previous_membership_unavailable')
            elif prepended and result['page']['outcome'] in ('complete', 'partial'):
                result['page']['scanned'] = max(result['page']['scanned'], len(result['rows']))
            if _size(result) > 65536:
                result.update(page=_page(checkpoint=after_revision), rows=[])
                result['missing'].append('response_budget')
            self.check(ctx)
            if result['page']['outcome'] in ('complete', 'partial'):
                window = self._seen.setdefault(key, OrderedDict())
                for jid, revision in updates:
                    window[jid] = max(revision, window.get(jid, 0))
                    window.move_to_end(jid)
                while len(window) > MEMBERSHIP_IDS:
                    window.popitem(last=False)
                self._seen.move_to_end(key)
                while len(self._seen) > self.membership_stream_limit:
                    self._seen.popitem(last=False)
        return result

    def _selected(self, selection, *, pin=False):
        ctx = selection.context
        known = self._known(ctx, selection.store)
        if known is None:
            return None, None, 'store_unavailable'
        store = _attach(known)
        if store is None:
            return None, None, 'store_unavailable'
        try:
            page = _read_page(store.list_job_summaries, job_ref=selection.job_ref, **EXACT_BUDGET)
        except StoreIdentityError:
            return None, None, 'selection_changed'
        if page.outcome != 'complete':
            return None, None, page.reason or page.outcome
        if len(page.items) != 1 or page.items[0].deleted:
            return None, None, 'missing'
        row = page.items[0]
        if row.job_ref != selection.job_ref or not self._owned(row, known, ctx, pin=pin):
            return None, None, 'ownership_unknown'
        return store, row, None

    def read_pins(self, ctx, selections):
        if len(selections) > 8 or any(s.context != ctx or s.job_ref.state_id != s.store.state_id for s in selections):
            raise InvalidReadRequest()
        self.check(ctx)
        results = []
        deadline = time.monotonic() + 2
        for selection in selections:
            self.check(ctx)
            store, row, reason = self._selected(selection, pin=True)
            header = self._header(store, row, selection) if not reason and time.monotonic() < deadline else None
            if time.monotonic() >= deadline:
                header = None
            _, current, changed = self._selected(selection, pin=True)
            if not reason and (changed or current.revision != row.revision):
                reason = 'selection_changed'
            result = dict(kind='unavailable', reason=reason) if reason else dict(
                kind='present', row=self._summary(row, ctx, selection.store))
            if result['kind'] == 'present' and header is not None:
                result['row']['header'] = header
            results.append(dict(selection=selection.wire(), result=result))
        response = dict(version=1, context=asdict(ctx), results=results)
        if _size(response) > 65536:
            response['results'] = [dict(selection=s.wire(), result=dict(kind='unavailable', reason='response_budget'))
                                   for s in selections]
        self.check(ctx)
        return response

    def read_selected_metadata(self, selection, *, task_cursor=None, artifact_cursor=None,
                               attempt_cursor=None, run_cursor=None, process_outcome_cursor=None,
                               observation_cursor=None):
        if selection.job_ref.state_id != selection.store.state_id:
            raise InvalidReadRequest()
        ctx = selection.context
        self.check(ctx)
        bindings = {lane: self._binding(ctx, selection.store, lane, 0, None, selection.job_ref.as_dict())
                    for lane in ('tasks', 'artifacts', *HISTORY_LANES)}
        cursors = dict(tasks=self._cursor(task_cursor, bindings['tasks']),
                       artifacts=self._cursor(artifact_cursor, bindings['artifacts']))
        history_cursors = dict(attempts=attempt_cursor, runs=run_cursor,
                               process_outcomes=process_outcome_cursor, observations=observation_cursor)
        history_cursors = {lane: self._cursor(token, bindings[lane]) for lane, token in history_cursors.items()}
        unavailable_reason = 'legacy_ref' if selection.job_ref.version == 1 else 'selection_unavailable'
        result = dict(version=1, selection=selection.wire(), context=asdict(ctx), lifecycle=None,
                      tasks=dict(page=_page(), rows=[]), artifacts=dict(page=_page(), rows=[]),
                      display=dict(kind='unavailable', reason='selection_unavailable'),
                      task_count=None, artifact_count=None,
                      history=dict(kind='unavailable', reason=unavailable_reason),
                      cost=dict(kind='unavailable', reason=unavailable_reason),
                      cancellation_authority=False, missing=['history', 'cost'])
        store, row, reason = self._selected(selection)
        if reason:
            result.update(display=dict(kind='unavailable', reason=reason),
                          history=dict(kind='unavailable', reason=reason),
                          cost=dict(kind='unavailable', reason=reason))
            result['missing'].append(reason)
            self.check(ctx)
            return result
        result.update(lifecycle=row.status, display=self._display(row),
                      task_count=row.task_count, artifact_count=row.artifact_count)
        # Task and artifact lanes are separate bounded reads; a write landing between
        # them skews their revisions. Re-read a bounded number of times for a matched pair.
        selected_pages = {}
        for _ in range(3):
            selected_pages = {}
            for lane, method in (('tasks', store.list_task_refs), ('artifacts', store.list_artifact_refs)):
                self.check(ctx)
                selected_pages[lane] = _read_page(method, selection.job_ref, cursor=cursors[lane], **PAGE_BUDGET)
            if selected_pages['tasks'].revision == selected_pages['artifacts'].revision:
                break
        for lane, page in selected_pages.items():
            rows = []
            for item in page.items:
                if item.job_ref != selection.job_ref:
                    raise RuntimeError('public metadata escaped selected ref')
                projected = dict(id=item.id, status=item.status, stamp=item.stamp, revision=item.revision)
                if lane == 'tasks':
                    projected['binding'] = asdict(item.binding) if item.binding else None
                else:
                    projected.update(task_id=item.task_id, type=item.artifact_type, sha256=item.sha256,
                                     presence='recorded', check_result='unavailable')
                rows.append(projected)
            result[lane] = dict(page=self._wire_page(page, bindings[lane], 0), rows=rows)
        if selection.job_ref.version == 2:
            self.check(ctx)
            economics = store.get_selected_economics(selection.job_ref, expected_summary_revision=row.revision)
            if economics.job_ref != selection.job_ref:
                raise RuntimeError('public economics escaped selected ref')
            result['cost'] = dict(kind=economics.outcome, **asdict(economics))
            if economics.outcome == 'available':
                result['missing'].remove('cost')
            result['history'] = self._history(store, selection, bindings, history_cursors)
            if result['history']['kind'] == 'available':
                result['missing'].remove('history')
        if selected_pages['tasks'].revision != selected_pages['artifacts'].revision:
            result['expert'] = job_expert.unavailable('lane_revision_skew')
        else:
            result['expert'] = self._expert(store, row, selection, selected_pages, cursors)
        # Separate bounded reads are not a transaction. Never return lanes from a
        # selection that lost ownership or identity while they were being read. A live
        # job's summary revision advances under every worker write; that is the same
        # selection, and the lane pages already carry their own revisions.
        _, current, reason = self._selected(selection)
        if (not reason and current.job_ref == row.job_ref and current.revision == row.revision
                and row.status in RUNNING and row.status != 'stitching'
                and not task_cursor and not artifact_cursor
                and selected_pages['tasks'].outcome == 'complete'
                and selected_pages['tasks'].revision == selected_pages['artifacts'].revision
                and selected_pages['tasks'].revision >= row.revision
                and selected_pages['artifacts'].outcome in ('complete', 'partial')
                and len(selected_pages['tasks'].items) == row.task_count):
            from .job_lifecycle import terminal_task_lifecycle

            terminal = terminal_task_lifecycle(item.status for item in selected_pages['tasks'].items)
            if terminal:
                result['lifecycle'] = terminal
        if not reason and current.revision != row.revision:
            result['lifecycle'] = current.status if current.status in RUNNING else None
        if reason or current.job_ref != row.job_ref:
            result.update(lifecycle=None, display=dict(kind='unavailable', reason='selection_changed'),
                          task_count=None, artifact_count=None,
                          tasks=dict(page=_page(), rows=[]), artifacts=dict(page=_page(), rows=[]),
                          history=dict(kind='unavailable', reason='selection_changed'),
                          cost=dict(kind='unavailable', reason='selection_changed'),
                          missing=['selection_changed', 'history', 'cost'],
                          expert=job_expert.unavailable('selection_changed'))
        if _size(result) > 98304:
            result['expert'] = job_expert.unavailable('response_budget')
        if _size(result) > 98304:
            result.update(tasks=dict(page=_page(), rows=[]), artifacts=dict(page=_page(), rows=[]),
                          history=dict(kind='unavailable', reason='response_budget'),
                          cost=dict(kind='unavailable', reason='response_budget'))
            result['missing'] = list(dict.fromkeys(result['missing'] + ['response_budget', 'history', 'cost']))
        self.check(ctx)
        return result

    def _header(self, store, row, selection):
        if selection.job_ref.version != 2:
            return None
        key = (selection, row.revision)
        with self._lock:
            cached = self._headers.get(key)
            if cached and time.monotonic() - cached[0] < 2:
                return cached[1]
        job = store.get_job(selection.job_ref.job_id)
        if job.id != row.job_ref.job_id or str(job.status) != row.status:
            return None
        cursors = dict(tasks=None, artifacts=None)
        for _ in range(3):
            pages = {lane: _read_page(method, selection.job_ref, cursor=None, **PAGE_BUDGET)
                     for lane, method in (('tasks', store.list_task_refs), ('artifacts', store.list_artifact_refs))}
            if pages['tasks'].revision == pages['artifacts'].revision:
                break
        if pages['tasks'].revision != pages['artifacts'].revision:
            header = job_expert.summary_header(job, job_expert.unavailable('lane_revision_skew'))
        else:
            expert = self._expert(store, row, selection, pages, cursors, job=job, pin=True, for_header=True)
            header = job_expert.summary_header(job, expert)
        with self._lock:
            self._headers[key] = (time.monotonic(), header)
            self._headers.move_to_end(key)
            while len(self._headers) > 64:
                self._headers.popitem(last=False)
        return header

    def _expert(self, store, row, selection, pages, cursors, *, job=None, pin=False, for_header=False):
        if selection.job_ref.version != 2:
            return job_expert.unavailable('legacy_ref')
        if pages['tasks'].revision != pages['artifacts'].revision:
            return job_expert.unavailable('lane_revision_skew')
        deadline = time.monotonic() + 2
        _, current, reason = self._selected(selection, pin=pin)
        if reason or current.job_ref != row.job_ref:
            return job_expert.unavailable('selection_changed')
        job = job or store.get_job(selection.job_ref.job_id)
        if job.id != selection.job_ref.job_id or str(job.status) != row.status:
            return job_expert.unavailable('selection_changed')
        tasks = []
        coverage = {lane: 'complete' if page.outcome == 'complete' and cursors[lane] is None else 'partial'
                    for lane, page in pages.items()}
        for ref in pages['tasks'].items:
            self.check(selection.context)
            if time.monotonic() >= deadline:
                coverage['tasks'] = 'partial'
                break
            task = store.get_task_by_id(ref.id)
            if not job_expert.task_matches(task, ref, job.id):
                return job_expert.unavailable('task_changed')
            if task.payload.get('session_id') not in (None, row.session_id):
                return job_expert.unavailable('task_scope_changed')
            tasks.append(task)
        by_id = {task.id: task for task in tasks}
        artifacts = []
        if time.monotonic() < deadline:
            refs = pages['artifacts'].items
            bodies = store.get_artifacts_by_ids(job.id, [ref.id for ref in refs])
            for ref in refs:
                artifact = bodies.get(ref.id)
                if (artifact is None or artifact.job_id != job.id or artifact.id != ref.id
                        or artifact.sha256 != ref.sha256 or artifact.task_id != ref.task_id
                        or str(artifact.type) != ref.artifact_type):
                    return job_expert.unavailable('artifact_changed')
                if artifact.task_id:
                    task = by_id.get(artifact.task_id)
                    if task is None:
                        coverage['artifacts'] = 'partial'
                    elif not job_expert.current_artifact(artifact, task):
                        coverage['artifacts'] = 'partial'
                        continue
                artifacts.append(artifact)
        else:
            coverage['artifacts'] = 'partial'
        compaction = dict(coverage='unavailable', reason='identity_unavailable', records=[])
        known = self.sources.resolve(selection.store)
        created_at = job_expert.timestamp(job.created_at)
        if not job.session_id and row.session_id:
            job = replace(job, session_id=row.session_id)
        if known is not None and job.session_id and created_at and time.monotonic() < deadline:
            compaction = read_selected_compaction(known.root, session_id=job.session_id, job_id=job.id,
                created_at=datetime.fromisoformat(created_at.replace('Z', '+00:00')).timestamp())
        # Re-read the identical finite reference pages. A row hydrated above that was revised
        # meanwhile would publish stale content, so that still invalidates the projection.
        # Rows appended by a live job (a task bound, an artifact landed) leave this projection
        # one step behind: report partial coverage rather than no roster. The page revision
        # alone advances under every store write and is not a change to the selection.
        for lane, method in (('tasks', store.list_task_refs), ('artifacts', store.list_artifact_refs)):
            current = _read_page(method, selection.job_ref, cursor=cursors[lane], **PAGE_BUDGET)
            hydrated = tuple(pages[lane].items)
            if current.outcome != pages[lane].outcome or tuple(current.items[:len(hydrated)]) != hydrated:
                return job_expert.unavailable('selection_changed')
            if len(current.items) != len(hydrated):
                coverage[lane] = 'partial'
        if pages['tasks'].revision != pages['artifacts'].revision:
            return job_expert.unavailable('lane_revision_skew')
        if time.monotonic() >= deadline:
            return job_expert.unavailable('deadline')
        projected = job_expert.project(job, tasks, artifacts, coverage, registry=self._registry, compaction=compaction)
        if for_header:
            return projected
        # Preserve useful rows while refusing to turn a capped response into a
        # complete verification verdict. No extra pages or body scans are started.
        while _size(projected) > 49152 and (projected['tasks'] or projected['artifacts']):
            lane = 'artifacts' if projected['artifacts'] else 'tasks'
            omitted = projected[lane].pop()
            if lane == 'tasks':
                projected['economics']['tasks'].pop(omitted['id'], None)
                header = projected['economics']['header']
                remaining = len(projected['tasks'])
                header['selected_workers'] = remaining
                header['completed_workers'] = min(header.get('completed_workers', 0), remaining)
                header['usage']['selected_workers'] = remaining
                header['usage']['tokens_known_workers'] = min(header['usage']['tokens_known_workers'], remaining)
                header['usage']['cost_known_workers'] = min(header['usage']['cost_known_workers'], remaining)
                if header['cost'].get('plan_workers') is not None:
                    header['cost']['plan_workers'] = min(header['cost']['plan_workers'], remaining)
            else:
                omitted_id = omitted['id']
                for usage in projected['economics']['tasks'].values():
                    if usage.get('source_artifact_id') == omitted_id:
                        usage['source_artifact_id'] = None
            projected['coverage'][lane] = 'partial'
            projected.update(kind='partial', reason='response_budget', quality='unverified')
            projected['economics']['header']['usage']['complete'] = False
            projected['economics']['header']['cost']['complete'] = False
            if lane == 'tasks':
                projected['economics']['header']['workers_complete'] = False
        self.check(selection.context)
        return projected

    def _history(self, store, selection, bindings, cursors):
        counts = store.historical_evidence_counts(selection.job_ref)
        result = dict(kind='available' if counts.outcome == 'available' else 'unavailable',
                      counts=asdict(counts), missing=[] if counts.outcome == 'available' else ['counts'])
        for lane, method in HISTORY_LANES.items():
            self.check(selection.context)
            page = getattr(store, method)(selection.job_ref, cursor=cursors[lane], **HISTORY_BUDGET)
            rows = []
            for item in page.items:
                if item.job_ref != selection.job_ref:
                    raise RuntimeError('public history escaped selected ref')
                projected = asdict(item)
                if lane == 'runs':
                    receipt = store.get_completion_receipt(selection.job_ref, item.facts['id'])
                    if receipt.job_ref != selection.job_ref or receipt.run_id != item.facts['id']:
                        raise RuntimeError('public completion escaped selected run')
                    projected['completion'] = asdict(receipt)
                rows.append(projected)
            result[lane] = dict(page=dict(outcome=page.outcome,
                next_cursor=self._wrap(page.next_cursor, bindings[lane]), scanned=page.scanned,
                captured_count=page.captured_count, coverage=page.coverage,
                complete_invocation_history=page.complete_invocation_history), rows=rows)
            if page.outcome not in ('complete', 'partial'):
                result['kind'] = 'unavailable'
                result['missing'].append(lane)
        return result

    def read_local(self, ctx, *, mode='snapshot', cursor=None, after_revision=0, lane='history'):
        if lane not in ('active', 'history'):
            raise InvalidReadRequest()
        self.check(ctx)
        context = dict(session_id=ctx.session_id, repo=ctx.repo,
                       view_generation=ctx.view_generation, scope=ctx.scope)
        if self.local_handle is None:
            result = dict(version=1, page=_page(checkpoint=after_revision), rows=[],
                          missing=['local_index_unavailable'])
        else:
            result = self.local_handle.read_page(context, mode=mode, cursor=cursor,
                                                 after_revision=after_revision, lane=lane)
        result['context'] = context
        if _size(result) > 32768:
            result = dict(version=1, context=context, page=_page(checkpoint=after_revision),
                          rows=[], missing=['response_budget'])
        result['lane'] = lane
        self.check(ctx)
        return result

    def read_local_selected(self, ctx, local_ref, *, lane='actions', cursor=None, include_context=False):
        self.check(ctx)
        context = dict(session_id=ctx.session_id, repo=ctx.repo,
                       view_generation=ctx.view_generation, scope=ctx.scope)
        if self.local_handle is None:
            result = dict(version=1, page=_page(), rows=[], missing=['local_index_unavailable'])
        else:
            result = self.local_handle.read_selected(context, local_ref, lane=lane, cursor=cursor, include_context=include_context)
        result['context'] = context
        if _size(result) > 32768:
            result = dict(version=1, context=context, page=_page(), rows=[], missing=['response_budget'])
        self.check(ctx)
        return result
