import { metadataSelectionKey, parseMetadataSelection, sameMetadataContext } from './jobMetadata';
import type { MetadataContext, MetadataSelection } from './jobMetadata';
import { jobRefQuery } from './publicJobRef';
import { getJSON } from './transport';

export type WorkerVerdict = 'PASS' | 'FAIL' | 'PARTIAL' | 'unknown';
export type OperationCapability = { state: 'available'; reason: null } | { state: 'unsupported'; reason: string };
export type OperationFeature = 'verdicts' | 'cleanup' | 'quality_loop' | 'failure_routes' | 'steering' | 'claims';
export type VerdictRecord = { kind: 'verdict'; id: string; task_id: string; created_at: string;
  verdict: WorkerVerdict; reason: string; reason_truncated: boolean; advisory: true };
export type WorkerOperationsSnapshot = {
  version: 1; context: MetadataContext; selection: MetadataSelection; kernel_version: string; lifecycle: string;
  capabilities: Record<OperationFeature, OperationCapability>;
  ledger: { outcome: 'complete' | 'partial' | 'unavailable' | 'cursor_expired'; rows: VerdictRecord[];
    next_cursor: string | null; scanned: number; coverage: 'page'; reason: string | null };
  verdict: { state: 'recorded' | 'missing'; authority: 'worker_advisory' }; accounting: 'references_only';
};
export type QualityLoopOptions = { mode: 'goal' | 'review_pass'; max_iterations: number; cost_cap_usd: number; cleanup: boolean };

function fail(): never { throw new Error('Worker operations are unavailable for this selection. Refresh the selected job.'); }
function record(v: unknown): v is Record<string, unknown> { return v !== null && typeof v === 'object' && !Array.isArray(v); }
function object(v: unknown, keys: readonly string[]): Record<string, unknown> {
  if (!record(v) || Object.keys(v).length !== keys.length || keys.some(key => !Object.hasOwn(v, key))) return fail();
  return v;
}
function text(v: unknown, max = 256): string {
  if (typeof v !== 'string' || !v || new TextEncoder().encode(v).length > max) return fail();
  return v;
}
function nullableText(v: unknown, max = 256): string | null { return v === null ? null : text(v, max); }
function integer(v: unknown, max: number): number { if (typeof v !== 'number' || !Number.isSafeInteger(v) || v < 0 || v > max) return fail(); return v; }
function choice<T extends string>(v: unknown, options: readonly T[]): T { for (const option of options) if (v === option) return option; return fail(); }
function capability(v: unknown): OperationCapability {
  const r = object(v, ['state', 'reason']);
  if (r.state === 'available' && r.reason === null) return { state: 'available', reason: null };
  if (r.state === 'unsupported') return { state: 'unsupported', reason: text(r.reason) };
  return fail();
}
function verdict(v: unknown): VerdictRecord {
  const r = object(v, ['kind', 'id', 'task_id', 'created_at', 'verdict', 'reason', 'reason_truncated', 'advisory']);
  if (r.kind !== 'verdict' || r.advisory !== true || typeof r.reason_truncated !== 'boolean') return fail();
  return { kind: 'verdict', id: text(r.id), task_id: text(r.task_id), created_at: text(r.created_at),
    verdict: choice(r.verdict, ['PASS', 'FAIL', 'PARTIAL', 'unknown']), reason: text(r.reason, 2048), reason_truncated: r.reason_truncated, advisory: true };
}
export function operationsKey(context: MetadataContext, selection: MetadataSelection): string {
  return JSON.stringify([context.view_generation, context.scope, metadataSelectionKey(selection)]);
}
export function parseWorkerOperations(value: unknown, context: MetadataContext, selection: MetadataSelection,
  previousCursor: string | null = null): WorkerOperationsSnapshot {
  const r = object(value, ['version', 'context', 'selection', 'kernel_version', 'lifecycle', 'capabilities', 'ledger', 'verdict', 'accounting']);
  if (r.version !== 1 || r.accounting !== 'references_only' || selection.job_ref.version !== 2) return fail();
  const c = object(r.context, ['session_id', 'repo', 'scope', 'view_generation']);
  const parsedContext = { session_id: text(c.session_id), repo: text(c.repo, 1024),
    scope: choice(c.scope, ['session', 'repo', 'all']), view_generation: text(c.view_generation) };
  if (!sameMetadataContext(parsedContext, context)) return fail();
  const selected = parseMetadataSelection(r.selection, context);
  if (metadataSelectionKey(selected) !== metadataSelectionKey(selection)) return fail();
  const caps = object(r.capabilities, ['verdicts', 'cleanup', 'quality_loop', 'failure_routes', 'steering', 'claims']);
  const capabilities = { verdicts: capability(caps.verdicts), cleanup: capability(caps.cleanup),
    quality_loop: capability(caps.quality_loop), failure_routes: capability(caps.failure_routes),
    steering: capability(caps.steering), claims: capability(caps.claims) };
  const l = object(r.ledger, ['outcome', 'rows', 'next_cursor', 'scanned', 'coverage', 'reason']);
  if (l.coverage !== 'page' || !Array.isArray(l.rows) || l.rows.length > 20) return fail();
  const rows = l.rows.map(verdict), scanned = integer(l.scanned, 21);
  const outcome = choice(l.outcome, ['complete', 'partial', 'unavailable', 'cursor_expired']);
  const next_cursor = nullableText(l.next_cursor, 8192);
  if ((outcome === 'partial') !== (next_cursor !== null) || (next_cursor !== null && next_cursor === previousCursor)
      || rows.length > scanned || new Set(rows.map(row => row.id)).size !== rows.length
      || ((outcome === 'unavailable' || outcome === 'cursor_expired') && rows.length !== 0)) return fail();
  const summary = object(r.verdict, ['state', 'authority']);
  const state = choice(summary.state, ['recorded', 'missing']);
  if (summary.authority !== 'worker_advisory' || (state === 'recorded') !== (rows.length > 0)) return fail();
  return { version: 1, context: parsedContext, selection: selected, kernel_version: text(r.kernel_version),
    lifecycle: text(r.lifecycle), capabilities,
    ledger: { outcome, rows, next_cursor, scanned, coverage: 'page', reason: nullableText(l.reason) },
    verdict: { state, authority: 'worker_advisory' }, accounting: 'references_only' };
}
export async function fetchWorkerOperations(context: MetadataContext, selection: MetadataSelection, cursor: string | null = null): Promise<WorkerOperationsSnapshot> {
  const capturedContext = { ...context }, capturedSelection = { ...selection, job_ref: { ...selection.job_ref } };
  const query = new URLSearchParams({ ...capturedContext, ...jobRefQuery(capturedSelection.job_ref), source: capturedSelection.source,
    ...(cursor === null ? {} : { cursor }) });
  const value = await getJSON<unknown>(`/api/jobs/operations/v1?${query}`, { sessionId: context.session_id, repo: context.repo });
  return parseWorkerOperations(value, capturedContext, capturedSelection, cursor);
}
