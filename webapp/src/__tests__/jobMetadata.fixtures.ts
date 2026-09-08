import type { MetadataContext, MetadataDetail, MetadataList, MetadataSelection, MetadataStream, MetadataSummary, MetadataView, Traversal } from '../lib/jobMetadata';
export const context: MetadataContext = { session_id: 'session-A', repo: '/repo', view_generation: 'generation-1', scope: 'session' };
export const stream: MetadataStream = { store: { source: 'harness', state_id: 'store-A' }, status: null };
export const initial = { mode: 'snapshot', after_revision: 0, cursor: null } satisfies Traversal;
export function token(n = 1): string { return btoa(JSON.stringify({ signature: 'x'.repeat(64), page: n })); }
export function selection(n = 1): MetadataSelection { return { session_id: context.session_id, repo: context.repo, source: 'harness', job_ref: { job_id: `job_${n}`, state_id: 'store-A' } }; }
export function summary(n = 1): MetadataSummary { return { selection: selection(n), revision: n, deleted: false, lifecycle: 'running',
  ownership: { origin: 'marionette', session_id: context.session_id, project_id: null }, task_count: null, artifact_count: null, stamp: 'known', display: { kind: 'unavailable' }, economics: { kind: 'unavailable' } }; }
export function view(generation = context.view_generation): MetadataView { return { version: 1,
  context: { session_id: context.session_id, repo: context.repo, view_generation: generation }, availability: 'known',
  sources: [{ ...stream.store, cross_project: false, available: true }], missing: [], refreshing: false }; }
export function list(rows = [summary()]): MetadataList { return { version: 1, context: { ...context }, store: { ...stream.store }, mode: 'snapshot',
  page: { outcome: 'complete', revision: 10000, checkpoint: 10000, scanned: rows.length, next_cursor: null }, rows,
  coverage: { membership: 'known_metadata', legacy: 'unavailable', ordering: 'id', store_set: 'known_sources' }, missing: ['legacy_ownership', 'display', 'economics'] }; }
export function detail(): MetadataDetail { return { version: 1, context: { ...context }, selection: selection(), lifecycle: 'running',
  tasks: { page: { outcome: 'complete', revision: 100, scanned: 1, checkpoint: 100, next_cursor: null }, rows: [
    { id: 'task-1', status: 'running', stamp: 'known', revision: 2, binding: { task_id: 'task-1', generation: 3, lease_id: 'lease', owner: 'worker' } },
  ] }, artifacts: { page: { outcome: 'complete', revision: 100, scanned: 1, checkpoint: 100, next_cursor: null }, rows: [
    { id: 'artifact-1', status: null, stamp: 'known', revision: 3, task_id: 'task-1', type: 'finding', sha256: 'a'.repeat(64), presence: 'recorded', check_result: 'unavailable' },
  ] }, history: { kind: 'unavailable', reason: 'public_read_unbounded' }, cost: { kind: 'unavailable', reason: 'not_in_metadata' }, cancellation_authority: false, missing: ['history', 'cost'] }; }
export const handshake = { ok: true, protocol_version: 1, endpoint_id: 'endpoint-1', boot_id: 'boot-1', capabilities: ['endpoint_fence_v1'] };
export function response(value: unknown, status = 200): Response { return new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } }); }
