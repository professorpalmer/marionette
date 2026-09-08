import { parseMetadataDisplay, parseSelectedEconomics, parseSelectedHistory, historyCursors } from './selectedMetadataEvidence';
import type { MetadataDisplay, SelectedEconomics, SelectedHistory, HistoryCursorName } from './selectedMetadataEvidence';
import { isPublicJobRef, jobRefQuery } from './publicJobRef';
import { localPath, parseLocalList, parseLocalDetail } from './localJobMetadata';
import type { LocalRef, LocalDetail, LocalList, LocalLane } from './localJobMetadata';
import { browserResponse, controlDeadline, ControlRequestError } from './boundedControl';
import { EndpointSessionClient, isEndpointMismatch } from './endpointSession';
import { getHarnessIpc } from './transport';
import { desktopBridgeMissing } from './operationalDiagnostic';
import type { JSONResponse } from '../../electron/json-response.mjs';
import type { SelectedJobRef } from './jobArtifacts';

export type PublicJobRef = SelectedJobRef['job_ref'];
export type MetadataTarget = { session_id: string; repo: string; scope: 'session' | 'repo' | 'all' };
export type MetadataContext = MetadataTarget & { view_generation: string };
export type MetadataSource = { source: 'harness' | 'cli'; state_id: string; cross_project: boolean; available: boolean };
export type MetadataSelection = Pick<MetadataContext, 'session_id' | 'repo'> & { source: MetadataSource['source']; job_ref: PublicJobRef };
export type MetadataView = { local?: { available: false; incarnation: null } | { available: boolean; incarnation: string; version: 1; lanes?: LocalLane[] }; version: 1; context: Omit<MetadataContext, 'scope'>; availability: 'known' | 'unavailable'; sources: MetadataSource[]; missing: string[]; refreshing: boolean };
export type MetadataPage = { revision: number; scanned: number; checkpoint: number; reason?: string; retry_after_ms?: number } & (
  | { outcome: 'complete'; next_cursor: null }
  | { outcome: 'partial'; next_cursor: string }
  | { outcome: 'unavailable' | 'cursor_expired'; next_cursor: null }
);
type UnknownField = { kind: 'unavailable'; reason?: string };
export type MetadataSummary = { selection: MetadataSelection; revision: number; deleted: false;
  lifecycle: string | null; ownership: { origin: string | null; session_id: string | null; project_id: string | null };
  task_count: number | null; artifact_count: number | null; stamp: 'known' | 'legacy_unknown';
  display: MetadataDisplay; economics: UnknownField };
export type MetadataRow = MetadataSummary | { selection: MetadataSelection; revision: number; deleted: true };
export const pmActiveStatuses = ['queued', 'running', 'stitching', 'in_progress', 'pending', 'started'] as const;
const pmStatuses = [...pmActiveStatuses, 'complete', 'failed', 'stalled', 'cancelled'];
export type MetadataStream = { store: Pick<MetadataSource, 'source' | 'state_id'>; status: typeof pmActiveStatuses[number] | 'complete' | 'failed' | 'stalled' | 'cancelled' | null };
export type Traversal = { mode: 'snapshot' | 'changes'; after_revision: number; cursor: string | null };
export type MetadataList = { version: 1; context: MetadataContext; store: MetadataStream['store']; mode: Traversal['mode']; page: MetadataPage; rows: MetadataRow[];
  coverage: { membership: 'known_metadata'; legacy: 'unavailable'; ordering: 'id'; store_set: 'known_sources' }; missing: string[] };
export type MetadataTask = { id: string; status: string | null; stamp: MetadataSummary['stamp']; revision: number;
  binding: { task_id: string; generation: number | null; lease_id: string | null; owner: string | null } | null };
export type MetadataArtifact = Omit<MetadataTask, 'binding'> & { task_id: string | null; type: string | null; sha256: string | null; presence: 'recorded'; check_result: 'unavailable' };
export type MetadataDetail = { version: 1; context: MetadataContext; selection: MetadataSelection; lifecycle: string | null;
  tasks: { page: MetadataPage; rows: MetadataTask[] }; artifacts: { page: MetadataPage; rows: MetadataArtifact[] };
  display?: MetadataDisplay; task_count?: number | null; artifact_count?: number | null;
  history: SelectedHistory; cost: SelectedEconomics | { kind: 'unavailable'; reason: string };
  cancellation_authority: false; missing: string[] };
export type MetadataPinResult = { selection: MetadataSelection; result: { kind: 'present'; row: MetadataSummary } | { kind: 'unavailable'; reason: string } };
export type MetadataPins = { version: 1; context: MetadataContext; results: MetadataPinResult[] };
export type DetailCursors = { task_cursor: string | null; artifact_cursor: string | null } & Partial<Record<HistoryCursorName, string | null>>;
export type MetadataErrorCode = 'invalid_metadata' | 'invalid_request' | 'view_changed' | 'refresh_in_progress' | 'endpoint_changed' | 'unavailable' | 'outcome_unknown' | 'bounds_exceeded' | 'busy';
export class MetadataError extends Error {
  readonly code: MetadataErrorCode;
  constructor(code: MetadataErrorCode) { super(code); this.code = code; }
}
function fail(): never { throw new MetadataError('invalid_metadata'); }
function record(v: unknown): v is Record<string, unknown> { return v !== null && typeof v === 'object' && !Array.isArray(v); }
function object(v: unknown, fields: string[]): Record<string, unknown> {
  if (!record(v) || Object.keys(v).length !== fields.length || !fields.every(k => Object.hasOwn(v, k))) return fail();
  return v;
}
function text(v: unknown, max = 256): string {
  if (typeof v !== 'string' || !v || v.trim() !== v || /[\u0000-\u001f\ud800-\udfff]/u.test(v)
    || new TextEncoder().encode(v).byteLength > max) return fail();
  return v;
}
function id(v: unknown, max = 256): string { const s = text(v, max); if (!/^[a-zA-Z0-9_-]+$/.test(s)) return fail(); return s; }
function nullableText(v: unknown, max = 98304): string | null {
  if (v === null) return null;
  if (typeof v !== 'string' || new TextEncoder().encode(v).byteLength > max) return fail();
  return v;
}
function integer(v: unknown): number { if (typeof v !== 'number' || !Number.isSafeInteger(v) || v < 0) return fail(); return v; }
function nullableCount(v: unknown): number | null { return v === null ? null : integer(v); }
function flag(v: unknown): boolean { if (typeof v !== 'boolean') return fail(); return v; }
function array(v: unknown, max: number): unknown[] { if (!Array.isArray(v) || v.length > max) return fail(); return v; }
function missing(v: unknown): string[] { return array(v, 64).map(s => text(s)); }
function stamp(v: unknown): MetadataSummary['stamp'] { if (v !== 'known' && v !== 'legacy_unknown') return fail(); return v; }
function unavailable(v: unknown): UnknownField {
  const o = object(v, ['kind', ...(record(v) && Object.hasOwn(v, 'reason') ? ['reason'] : [])]);
  if (o.kind !== 'unavailable') return fail();
  return { kind: 'unavailable', ...(o.reason === undefined ? {} : { reason: text(o.reason) }) };
}
function cursor(v: unknown): string | null {
  if (v === null) return null;
  const s = text(v, 8192);
  // Authentication belongs to the host. Only accept bounded canonical base64url wire tokens here.
  if (!/^[A-Za-z0-9_-]+={0,2}$/.test(s) || s.length % 4 !== 0 || s.length < 44) return fail();
  return s;
}
export function metadataSelectionKey(s: MetadataSelection): string { return JSON.stringify([s.source, s.job_ref.state_id, s.job_ref.job_id, s.session_id, s.repo, ...(s.job_ref.version === 2 ? [2, s.job_ref.incarnation] : [])]); }
export function metadataStreamKey(s: MetadataStream): string { return JSON.stringify([s.store.source, s.store.state_id, s.status]); }
export function sameMetadataContext(a: MetadataContext, b: MetadataContext): boolean {
  return a.session_id === b.session_id && a.repo === b.repo && a.scope === b.scope && a.view_generation === b.view_generation;
}
function context(v: unknown): MetadataContext {
  const o = object(v, ['session_id', 'repo', 'scope', 'view_generation']);
  if (o.scope !== 'session' && o.scope !== 'repo' && o.scope !== 'all') return fail();
  return { session_id: id(o.session_id), repo: text(o.repo, 1024), scope: o.scope, view_generation: id(o.view_generation, 128) };
}
export function validateMetadataTarget(v: MetadataTarget): MetadataTarget {
  const c = context({ ...v, view_generation: 'validation' });
  return { session_id: c.session_id, repo: c.repo, scope: c.scope };
}
function expectedContext(v: unknown, expected: MetadataContext): MetadataContext {
  const c = context(v); if (!sameMetadataContext(c, expected)) return fail(); return c;
}
function store(v: unknown): MetadataStream['store'] {
  const o = object(v, ['source', 'state_id']);
  if (o.source !== 'harness' && o.source !== 'cli') return fail();
  return { source: o.source, state_id: id(o.state_id) };
}
export function parseMetadataSelection(v: unknown, c: Pick<MetadataContext, 'session_id' | 'repo'>): MetadataSelection {
  const o = object(v, ['job_ref', 'source', 'session_id', 'repo']);
  const versioned = record(o.job_ref) && Object.hasOwn(o.job_ref, 'version');
  const r = object(o.job_ref, versioned ? ['job_id', 'state_id', 'version', 'incarnation'] : ['job_id', 'state_id']);
  if (versioned && r.version !== 2) return fail();
  const jobId = text(r.job_id);
  if (!/^job_[a-zA-Z0-9_-]{1,128}$/.test(jobId) || o.session_id !== c.session_id || o.repo !== c.repo) return fail();
  const s = store({ source: o.source, state_id: r.state_id });
  const job_ref: PublicJobRef = versioned ? { job_id: jobId, state_id: s.state_id, version: 2, incarnation: id(r.incarnation, 128) } : { job_id: jobId, state_id: s.state_id };
  if (!isPublicJobRef(job_ref)) return fail();
  return { job_ref, source: s.source, session_id: c.session_id, repo: c.repo };
}
export function parseMetadataView(v: unknown, target: MetadataTarget): MetadataView {
  const o = object(v, ['version', 'context', 'availability', 'sources', 'missing', 'refreshing', ...(record(v) && Object.hasOwn(v, 'local') ? ['local'] : [])]);
  let local: MetadataView['local'];
  if (o.local !== undefined) {
    if (!record(o.local)) return fail();
    if (o.local.available === false && o.local.incarnation === null) { object(o.local, ['available', 'incarnation']); local = { available: false, incarnation: null }; }
    else { const l = object(o.local, ['available', 'incarnation', 'version', ...(Object.hasOwn(o.local, 'lanes') ? ['lanes', 'active_statuses', 'attention_statuses'] : [])]);
      if (l.lanes !== undefined) {
        if (JSON.stringify(l.lanes) !== JSON.stringify(['active', 'history'])) return fail();
        array(l.active_statuses, 32).forEach(v => text(v, 40)); array(l.attention_statuses, 32).forEach(v => text(v, 40));
      } if (l.version !== 1) return fail(); local = { available: flag(l.available), incarnation: id(l.incarnation), version: 1, ...(l.lanes ? { lanes: ['active', 'history'] } : {}) }; }
  }
  const c = object(o.context, ['session_id', 'repo', 'view_generation']);
  if (o.version !== 1 || (o.availability !== 'known' && o.availability !== 'unavailable')
    || c.session_id !== target.session_id || c.repo !== target.repo) return fail();
  const parsed = context({ ...c, scope: target.scope });
  const sources = array(o.sources, 34).map(v => {
    const s = object(v, ['source', 'state_id', 'cross_project', 'available']);
    const identity = store({ source: s.source, state_id: s.state_id });
    const cross_project = flag(s.cross_project);
    if (cross_project && identity.source !== 'cli') return fail();
    return { ...identity, cross_project, available: flag(s.available) };
  });
  if (new Set(sources.map(s => s.state_id)).size !== sources.length
    || sources.filter(s => s.cross_project).length > 32 || sources.filter(s => !s.cross_project).length > 2
    || (o.availability === 'known' && (!sources.length || o.refreshing === true))) return fail();
  return { version: 1, context: { session_id: parsed.session_id, repo: parsed.repo, view_generation: parsed.view_generation },
    ...(local ? { local } : {}), availability: o.availability, sources, missing: missing(o.missing), refreshing: flag(o.refreshing) };
}
function page(v: unknown, after: number, previousCursor: string | null, maxScan = 51): MetadataPage {
  const o = object(v, ['outcome', 'revision', 'next_cursor', 'scanned', 'checkpoint',
    ...['reason', 'retry_after_ms'].filter(key => record(v) && Object.hasOwn(v, key))]);
  const hints = { ...(o.reason === undefined ? {} : { reason: text(o.reason) }), ...(o.retry_after_ms === undefined ? {} : { retry_after_ms: integer(o.retry_after_ms) }) };
  const revision = integer(o.revision), scanned = integer(o.scanned), checkpoint = integer(o.checkpoint), next_cursor = cursor(o.next_cursor);
  if (scanned > maxScan) return fail();
  switch (o.outcome) {
    case 'complete':
      if (next_cursor !== null || checkpoint !== revision || revision < after) return fail();
      return { outcome: o.outcome, revision, scanned, checkpoint, next_cursor, ...hints };
    case 'partial':
      if (next_cursor === null || next_cursor === previousCursor || checkpoint !== after || revision < after) return fail();
      return { outcome: o.outcome, revision, scanned, checkpoint, next_cursor, ...hints };
    case 'unavailable': case 'cursor_expired':
      if (next_cursor !== null || checkpoint !== after) return fail();
      return { outcome: o.outcome, revision, scanned, checkpoint, next_cursor, ...hints };
    default: return fail();
  }
}
function row(v: unknown, c: MetadataContext, s?: MetadataStream['store']): MetadataRow {
  if (!record(v)) return fail();
  const deleted = flag(v.deleted);
  const o = object(v, deleted ? ['selection', 'revision', 'deleted'] : ['selection', 'revision', 'deleted', 'lifecycle', 'ownership', 'task_count', 'artifact_count', 'stamp', 'display', 'economics', ...(Object.hasOwn(v, 'activity') ? ['activity'] : [])]);
  const selected = parseMetadataSelection(o.selection, c);
  if (s && (selected.source !== s.source || selected.job_ref.state_id !== s.state_id)) return fail();
  const revision = integer(o.revision);
  if (deleted) return { selection: selected, revision, deleted: true };
  const ownership = object(o.ownership, ['origin', 'session_id', 'project_id']);
  return { selection: selected, revision, deleted: false, lifecycle: nullableText(o.lifecycle),
    ownership: { origin: nullableText(ownership.origin), session_id: nullableText(ownership.session_id), project_id: nullableText(ownership.project_id) },
    task_count: nullableCount(o.task_count), artifact_count: nullableCount(o.artifact_count), stamp: stamp(o.stamp), display: parseMetadataDisplay(o.display), economics: unavailable(o.economics) };
}
export function parseMetadataList(v: unknown, c: MetadataContext, stream: MetadataStream, traversal: Traversal): MetadataList {
  const o = object(v, ['version', 'context', 'store', 'mode', 'page', 'rows', 'coverage', 'missing', ...(record(v) && Object.hasOwn(v, 'lanes') ? ['lanes'] : [])]);
  if (o.lanes !== undefined) {
    const lanes = object(o.lanes, ['statuses', 'active_statuses', 'attention_statuses']);
    for (const values of Object.values(lanes)) array(values, 16).forEach(v => { if (typeof v !== 'string' || !pmStatuses.includes(v)) fail(); });
  }
  if (o.version !== 1 || o.mode !== traversal.mode) return fail();
  const parsedContext = expectedContext(o.context, c), parsedStore = store(o.store);
  if (parsedStore.source !== stream.store.source || parsedStore.state_id !== stream.store.state_id) return fail();
  const p = page(o.page, traversal.after_revision, traversal.cursor), rows = array(o.rows, 50).map(v => row(v, c, parsedStore));
  if (new Set(rows.map(r => metadataSelectionKey(r.selection))).size !== rows.length || rows.length > p.scanned
    || ((p.outcome === 'unavailable' || p.outcome === 'cursor_expired') && rows.length)
    || rows.some(r => r.revision > p.revision || (stream.status !== null && !r.deleted && r.lifecycle !== stream.status))) return fail();
  const coverage = object(o.coverage, ['membership', 'legacy', 'ordering', 'store_set']);
  if (coverage.membership !== 'known_metadata' || coverage.legacy !== 'unavailable' || coverage.ordering !== 'id' || coverage.store_set !== 'known_sources') return fail();
  return { version: 1, context: parsedContext, store: parsedStore, mode: traversal.mode, page: p, rows,
    coverage: { membership: 'known_metadata', legacy: 'unavailable', ordering: 'id', store_set: 'known_sources' }, missing: missing(o.missing) };
}
function task(v: unknown): MetadataTask {
  const o = object(v, ['id', 'status', 'stamp', 'revision', 'binding']);
  const taskId = text(o.id, 32768);
  let binding: MetadataTask['binding'] = null;
  if (o.binding !== null) {
    const b = object(o.binding, ['task_id', 'generation', 'lease_id', 'owner']);
    if (b.task_id !== taskId) return fail();
    binding = { task_id: taskId, generation: nullableCount(b.generation), lease_id: nullableText(b.lease_id), owner: nullableText(b.owner) };
  }
  return { id: taskId, status: nullableText(o.status), stamp: stamp(o.stamp), revision: integer(o.revision), binding };
}
function artifact(v: unknown): MetadataArtifact {
  const o = object(v, ['id', 'status', 'stamp', 'revision', 'task_id', 'type', 'sha256', 'presence', 'check_result']);
  const sha = nullableText(o.sha256);
  if (o.presence !== 'recorded' || o.check_result !== 'unavailable' || (sha !== null && !/^[a-f0-9]{64}$/.test(sha))) return fail();
  return { id: text(o.id, 32768), status: nullableText(o.status), stamp: stamp(o.stamp), revision: integer(o.revision),
    task_id: nullableText(o.task_id), type: nullableText(o.type), sha256: sha, presence: 'recorded', check_result: 'unavailable' };
}
function resourcePage<T extends { id: string; revision: number }>(v: unknown, previous: string | null, parse: (v: unknown) => T): { page: MetadataPage; rows: T[] } {
  const o = object(v, ['page', 'rows']), p = page(o.page, 0, previous), rows = array(o.rows, 50).map(parse);
  if (new Set(rows.map(r => r.id)).size !== rows.length || rows.length > p.scanned || rows.some(r => r.revision > p.revision)
    || ((p.outcome === 'unavailable' || p.outcome === 'cursor_expired') && rows.length)) return fail();
  return { page: p, rows };
}
export function parseMetadataDetail(v: unknown, c: MetadataContext, selected: MetadataSelection, cursors: DetailCursors): MetadataDetail {
  const optional = ['display', 'task_count', 'artifact_count'].filter(key => record(v) && Object.hasOwn(v, key));
  const o = object(v, ['version', 'selection', 'context', 'lifecycle', 'tasks', 'artifacts', 'history', 'cost', 'cancellation_authority', 'missing', ...optional]);
  const s = parseMetadataSelection(o.selection, c);
  if (o.version !== 1 || metadataSelectionKey(s) !== metadataSelectionKey(selected) || o.cancellation_authority !== false) return fail();
  const cost = record(o.cost) && o.cost.kind === 'unavailable' && Object.keys(o.cost).length === 2
    ? { kind: 'unavailable', reason: text(o.cost.reason) } satisfies MetadataDetail['cost'] : parseSelectedEconomics(o.cost, s.job_ref);
  return { version: 1, context: expectedContext(o.context, c), selection: s, lifecycle: nullableText(o.lifecycle),
    tasks: resourcePage(o.tasks, cursors.task_cursor, task), artifacts: resourcePage(o.artifacts, cursors.artifact_cursor, artifact),
    ...(o.display === undefined ? {} : { display: parseMetadataDisplay(o.display) }),
    ...(o.task_count === undefined ? {} : { task_count: nullableCount(o.task_count) }),
    ...(o.artifact_count === undefined ? {} : { artifact_count: nullableCount(o.artifact_count) }),
    history: parseSelectedHistory(o.history, s.job_ref, cursors), cost, cancellation_authority: false, missing: missing(o.missing) };
}
export function parseMetadataPins(v: unknown, c: MetadataContext, selections: MetadataSelection[]): MetadataPins {
  const o = object(v, ['version', 'context', 'results']);
  if (o.version !== 1) return fail();
  const results = array(o.results, 8).map(v => {
    const o = object(v, ['selection', 'result']), s = parseMetadataSelection(o.selection, c);
    if (!record(o.result)) return fail();
    switch (o.result.kind) {
      case 'present': {
        const result = object(o.result, ['kind', 'row']), r = row(result.row, c);
        if (r.deleted || metadataSelectionKey(r.selection) !== metadataSelectionKey(s)) return fail();
        return { selection: s, result: { kind: 'present', row: r } } satisfies MetadataPinResult;
      }
      case 'unavailable': {
        const result = object(o.result, ['kind', 'reason']);
        return { selection: s, result: { kind: 'unavailable', reason: text(result.reason) } } satisfies MetadataPinResult;
      }
      default: return fail();
    }
  });
  const keys = results.map(r => metadataSelectionKey(r.selection));
  if (new Set(keys).size !== keys.length || keys.length !== selections.length || selections.some(s => !keys.includes(metadataSelectionKey(s)))) return fail();
  return { version: 1, context: expectedContext(o.context, c), results };
}
export function metadataStreams(view: MetadataView, scope: MetadataTarget['scope']): MetadataStream[] {
  const primary = view.sources.filter(s => !s.cross_project);
  const siblings = scope === 'repo' ? [] : view.sources.filter(s => s.cross_project);
  return [
    ...primary.map(s => ({ store: { source: s.source, state_id: s.state_id }, status: null })),
    ...pmActiveStatuses.flatMap(status => primary.map(s => ({ store: { source: s.source, state_id: s.state_id }, status }))),
    ...pmActiveStatuses.flatMap(status => siblings.map(s => ({ store: { source: s.source, state_id: s.state_id }, status }))),
  ];
}
function parameters(c: MetadataContext): URLSearchParams { context(c); return new URLSearchParams(c); }
function addCursor(params: URLSearchParams, name: string, value: string | null): void { if (value !== null) params.set(name, cursor(value) ?? fail()); }
export function metadataListPath(c: MetadataContext, s: MetadataStream, t: Traversal): string {
  const p = parameters(c), identity = store(s.store);
  if ((t.mode !== 'snapshot' && t.mode !== 'changes') || (t.mode === 'snapshot' && t.after_revision !== 0)) return fail();
  integer(t.after_revision);
  p.set('source', identity.source); p.set('state_id', identity.state_id); p.set('mode', t.mode);
  if (s.status !== null) {
    if (!pmStatuses.includes(s.status)) return fail();
    p.set('status', s.status);
  }
  if (t.mode === 'changes') p.set('after_revision', String(t.after_revision));
  addCursor(p, 'cursor', t.cursor);
  return '/api/jobs/metadata?' + p;
}

export type MetadataObservation = { row: MetadataSummary; freshness: 'observed' | 'stale' };
export type MetadataStreamState = { stream: MetadataStream; checkpoint: number; missing: string[] } & (
  | { state: 'ready'; traversal: { mode: 'snapshot'; after_revision: 0; cursor: null } }
  | { state: 'partial'; traversal: Traversal & { cursor: string } }
  | { state: 'complete'; traversal: { mode: 'changes'; after_revision: number; cursor: null } }
  | { state: 'unavailable' | 'cursor_expired'; traversal: Traversal }
);
export function initialMetadataStream(stream: MetadataStream): MetadataStreamState {
  return { stream, traversal: { mode: 'snapshot', after_revision: 0, cursor: null }, checkpoint: 0, state: 'ready', missing: [] };
}
export function advanceMetadataStream(old: MetadataStreamState, response: MetadataList): MetadataStreamState {
  switch (response.page.outcome) {
    case 'complete': return { ...old, checkpoint: response.page.checkpoint, traversal: { mode: 'changes', after_revision: response.page.checkpoint, cursor: null }, state: 'complete', missing: response.missing };
    case 'partial': return { ...old, traversal: { ...old.traversal, cursor: response.page.next_cursor }, state: 'partial', missing: response.missing };
    case 'unavailable': case 'cursor_expired': return { ...old, state: response.page.outcome, missing: response.missing };
    default: { const exhaustive: never = response.page; return exhaustive; }
  }
}
export type MetadataRemoval = Extract<MetadataRow, { deleted: true }>;
export function mergeMetadataRows(old: MetadataObservation[], rows: MetadataRow[], options: {
  status?: MetadataStream['status']; removals?: MetadataRemoval[];
} = {}): { observations: MetadataObservation[]; removals: MetadataRemoval[]; truncated: boolean } {
  const retained = new Map(old.map(o => [metadataSelectionKey(o.row.selection), o]));
  const removals = new Map((options.removals ?? []).map(r => [metadataSelectionKey(r.selection), r]));
  for (const row of rows) {
    const key = metadataSelectionKey(row.selection), previous = retained.get(key), removed = removals.get(key);
    if ((previous && previous.row.revision > row.revision) || (removed && removed.revision > row.revision)) continue;
    if (row.deleted) {
      // Status streams represent a union. Equal-revision departure from
      // one status does not delete the observation already emitted by its successor.
      if (options.status && previous && previous.row.revision === row.revision && previous.row.lifecycle !== options.status) continue;
      if (options.status && previous) retained.set(key, { ...previous, freshness: 'stale' });
      else retained.delete(key);
      removals.delete(key); removals.set(key, row);
    } else { removals.delete(key); retained.delete(key); retained.set(key, { row, freshness: 'observed' }); }
  }
  const truncated = retained.size + removals.size > 200;
  while (retained.size + removals.size > 200) {
    const removed = removals.keys().next();
    if (!removed.done) removals.delete(removed.value);
    else { const first = retained.keys().next(); if (!first.done) retained.delete(first.value); }
  }
  return { observations: [...retained.values()], removals: [...removals.values()], truncated };
}

type Bridge = { endpointHeaders: true; requestJSON: (method: string, path: string, body: unknown, correlation: string, headers: Record<string, string>) => Promise<unknown> };
function isBridge(v: unknown): v is Bridge { return record(v) && v.endpointHeaders === true && typeof v.requestJSON === 'function'; }
function token(): string { return typeof window !== 'undefined' && '__HARNESS_TOKEN__' in window && typeof window.__HARNESS_TOKEN__ === 'string' ? window.__HARNESS_TOKEN__ : ''; }
function jsonResponse(v: unknown, limit: number): JSONResponse {
  if (!record(v)) return fail();
  if (v.kind === 'connection-error' && typeof v.code === 'string' && typeof v.message === 'string') return { kind: 'connection-error', code: v.code, message: v.message };
  if (v.kind !== 'response' || typeof v.text !== 'string' || typeof v.correlationId !== 'string' || typeof v.status !== 'number' || !Number.isInteger(v.status) || v.status < 100 || v.status > 599) return fail();
  if (new TextEncoder().encode(v.text).byteLength > limit) throw new MetadataError('bounds_exceeded');
  return { kind: 'response', status: v.status, text: v.text, correlationId: v.correlationId };
}
// The metadata lane stays bounded even if an owner disposes and recreates a client
// while an uncancellable native IPC request is still outstanding.
let metadataTransportBusy = false;

/** One pinned endpoint lifetime. No retries, discovery on failure, or write replay. */
export class JobMetadataClient {
  private readonly endpoint = new EndpointSessionClient();
  private pin: Awaited<ReturnType<EndpointSessionClient['connect']>> | null = null;
  private readonly bridge: unknown = getHarnessIpc();
  private readonly owner = token();
  private readonly origin = window.location.origin;
  private closed = false;
  private readonly timeoutMs: number;
  constructor(timeoutMs = 10000) {
    if (!Number.isFinite(timeoutMs) || timeoutMs < 1 || timeoutMs > 30000) throw new MetadataError('invalid_request');
    this.timeoutMs = timeoutMs;
  }
  close(): void { this.closed = true; }
  assertCurrent(): void {
    if (this.closed || this.bridge !== getHarnessIpc() || this.owner !== token() || this.origin !== window.location.origin
      || (this.pin && !this.endpoint.isCurrent(this.pin))) throw new MetadataError('endpoint_changed');
  }
  private async raw(method: 'GET' | 'POST', path: string, body: unknown, headers: Record<string, string>, limit: number): Promise<JSONResponse> {
    this.assertCurrent();
    if (metadataTransportBusy) throw new MetadataError('busy');
    if (body !== undefined && new TextEncoder().encode(JSON.stringify(body)).byteLength > 65536) throw new MetadataError('bounds_exceeded');
    metadataTransportBusy = true;
    let work: Promise<unknown>;
    if (this.bridge) {
      if (!isBridge(this.bridge)) { metadataTransportBusy = false; throw new MetadataError('endpoint_changed'); }
      work = Promise.resolve().then(() => {
        this.assertCurrent();
        if (!isBridge(this.bridge)) throw new MetadataError('endpoint_changed');
        return this.bridge.requestJSON(method, path, body, '', headers);
      });
    } else {
      if (desktopBridgeMissing()) { metadataTransportBusy = false; throw new MetadataError('endpoint_changed'); }
      work = browserResponse(path, { method, redirect: 'error', cache: 'no-store', credentials: 'omit',
        headers: { ...headers, 'X-Harness-Token': this.owner, ...(method === 'POST' ? { 'Content-Type': 'application/json' } : {}) },
        ...(method === 'POST' ? { body: JSON.stringify(body) } : {}) }, limit, this.timeoutMs);
    }
    // The native bridge cannot cancel. A timeout must not release its outstanding slot.
    const tracked = work.finally(() => { metadataTransportBusy = false; });
    try {
      const result = await controlDeadline(tracked, undefined, this.timeoutMs);
      this.assertCurrent();
      return jsonResponse(result, limit);
    } catch (e) {
      if (e instanceof MetadataError) throw e;
      if (e instanceof ControlRequestError) throw new MetadataError(e.code);
      throw new MetadataError('outcome_unknown');
    }
  }
  async connect(): Promise<void> {
    this.assertCurrent();
    if (this.pin) return;
    const pin = await this.endpoint.connect(() => this.raw('GET', '/api/endpoint', undefined, { 'X-Harness-Protocol': '1' }, 16384));
    this.assertCurrent();
    if (pin.kind !== 'versioned') { this.close(); throw new MetadataError('unavailable'); }
    this.pin = pin;
  }
  private async request(method: 'GET' | 'POST', path: string, body: unknown, limit: number): Promise<unknown> {
    this.assertCurrent();
    const pin = this.pin;
    if (!pin) throw new MetadataError('endpoint_changed');
    const request = this.endpoint.prepare(path, pin);
    const response = await this.raw(method, request.path, body, this.endpoint.headers(pin), limit);
    if (isEndpointMismatch(response)) { this.endpoint.invalidate(pin); this.close(); throw new MetadataError('endpoint_changed'); }
    if (response.kind !== 'response') throw new MetadataError('outcome_unknown');
    let value: unknown;
    try { value = JSON.parse(response.text); } catch { throw new MetadataError('invalid_metadata'); }
    if (response.status !== 200) {
      if (record(value) && response.status === 409 && value.code === 'view_changed') throw new MetadataError('view_changed');
      if (record(value) && response.status === 409 && value.code === 'refresh_in_progress') throw new MetadataError('refresh_in_progress');
      if (response.status === 400) throw new MetadataError('invalid_request');
      throw new MetadataError(response.status >= 500 ? 'outcome_unknown' : 'unavailable');
    }
    this.assertCurrent();
    return this.endpoint.accept(request, value);
  }
  async view(target: MetadataTarget): Promise<MetadataView> {
    const captured = validateMetadataTarget(target);
    return parseMetadataView(await this.request('GET', '/api/jobs/metadata/view', undefined, 16384), captured);
  }
  async refresh(c: MetadataContext): Promise<MetadataView> {
    const captured = context(c);
    const view = parseMetadataView(await this.request('POST', '/api/jobs/metadata/view/refresh', { view_generation: captured.view_generation }, 16384), captured);
    if (view.context.view_generation === captured.view_generation || view.refreshing) return fail();
    return view;
  }
  async list(c: MetadataContext, stream: MetadataStream, t: Traversal): Promise<MetadataList> {
    const captured = context(c), selected = { store: store(stream.store), status: stream.status }, traversal = { ...t };
    const path = metadataListPath(captured, selected, traversal);
    return parseMetadataList(await this.request('GET', path, undefined, 65536), captured, selected, traversal);
  }
  async localList(c: MetadataContext, incarnation: string, traversal: Traversal, lane: LocalLane = 'history'): Promise<LocalList> {
    const captured = context(c), t = { ...traversal };
    return parseLocalList(await this.request('GET', localPath(captured, t, lane), undefined, 32768), captured, incarnation, t, lane);
  }
  async localDetail(c: MetadataContext, selected: LocalRef, lane: LocalDetail['lane'], cursor: string | null, includeContext = false): Promise<LocalDetail> {
    const captured = context(c), s = { ...selected }, p = parameters(captured);
    if (includeContext) p.set('include_context', 'true');
    p.set('job_id', id(s.job_id)); p.set('incarnation', id(s.incarnation)); p.set('lane', lane);
    addCursor(p, 'cursor', cursor);
    return parseLocalDetail(await this.request('GET', '/api/jobs/metadata/local/detail?' + p, undefined, 32768), captured, s, lane, includeContext);
  }
  async pins(c: MetadataContext, selected: MetadataSelection[]): Promise<MetadataPins> {
    const captured = context(c);
    if (selected.length > 8) throw new MetadataError('invalid_request');
    const validated = selected.map(s => parseMetadataSelection(s, captured));
    if (validated.length > 8 || new Set(validated.map(metadataSelectionKey)).size !== validated.length) throw new MetadataError('invalid_request');
    return parseMetadataPins(await this.request('POST', '/api/jobs/metadata/pins', { ...captured, selections: validated }, 65536), captured, validated);
  }
  async detail(c: MetadataContext, selected: MetadataSelection, cursors: DetailCursors): Promise<MetadataDetail> {
    const captured = context(c), s = parseMetadataSelection(selected, captured), p = parameters(captured);
    const capturedCursors: DetailCursors = { task_cursor: cursor(cursors.task_cursor), artifact_cursor: cursor(cursors.artifact_cursor) };
    for (const name of Object.values(historyCursors)) if (cursors[name] !== undefined) capturedCursors[name] = cursor(cursors[name]);
    p.set('source', s.source);
    for (const [key, value] of Object.entries(jobRefQuery(s.job_ref))) p.set(key, value);
    addCursor(p, 'task_cursor', capturedCursors.task_cursor); addCursor(p, 'artifact_cursor', capturedCursors.artifact_cursor);
    for (const name of Object.values(historyCursors)) addCursor(p, name, capturedCursors[name] ?? null);
    return parseMetadataDetail(await this.request('GET', '/api/jobs/metadata/detail?' + p, undefined, 98304), captured, s, capturedCursors);
  }
}
