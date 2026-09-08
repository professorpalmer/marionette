import { context, detail, handshake, list, summary, token, view } from './jobMetadata.fixtures';
import type { LocalSummary } from '../lib/localJobMetadata';
import type { MetadataContext } from '../lib/jobMetadata';

export const incarnation = 'native_process_1';
export function nativeSummary(n: number): LocalSummary {
  return { local_ref: { job_id: `job_${n}`, incarnation }, revision: n + 1, deleted: false,
    lifecycle: 'running', kind: n % 3 === 0 ? 'run_command' : n % 3 === 1 ? 'provider' : 'parallel_wave',
    session_id: context.session_id, parent_ref: null, task_count: null, action_count: 60,
    artifact_count: null, child_count: null, created_at: 1, updated_at: 2,
    receipts: { launch: true, terminal: false, recovery: false, child: false }, economics: { kind: 'unavailable' } };
}
const coverage = { membership: 'retained_local_history', ordering: 'id', historical: 'unavailable' };
/** A stateful wire fixture. It exercises the real parser, transport, owner and consumers. */
export class CombinedMetadataFixture {
  target = { ...context };
  calls: { method: string; path: string }[] = [];
  total = 1101;
  pmLifecycle = 'running';
  nativeOutcome: 'normal' | 'expired' | 'unavailable' = 'normal';
  pmOffset = 0;
  pmOffsets = new Map<string, number>();
  localOffset = 0;
  revision = 10000;
  deletedNative: number | null = null;
  active = 0;
  maximumActive = 0;
  nextRelease: (() => void) | null = null;
  holdNext = false;
  sources = view().sources;
  switchTarget(session: string, repo: string) { this.target = { ...this.target, session_id: session, repo, view_generation: `g_${++this.revision}` }; this.pmOffset = 0; this.localOffset = 0; }
  async request(method: string, path: string, _body?: unknown): Promise<{ kind: 'response'; status: number; correlationId: string; text: string }> {
    this.calls.push({ method, path }); this.active++; this.maximumActive = Math.max(this.active, this.maximumActive);
    const captured = { ...this.target };
    try {
      if (this.holdNext) { this.holdNext = false; await new Promise<void>(resolve => { this.nextRelease = resolve; }); }
      const url = new URL(path, 'http://fixture');
      if (['/api/swarm/live', '/api/jobs', '/api/jobs/artifacts/v1', '/api/jobs/evidence'].includes(url.pathname)) throw Error('Automatic body read: ' + path);
      if (url.pathname === '/api/endpoint') return this.response(handshake);
      if (url.pathname === '/api/jobs/metadata/view') return this.response({ ...view(captured.view_generation), context: { session_id: captured.session_id, repo: captured.repo, view_generation: captured.view_generation }, local: { available: true, incarnation, version: 1 }, sources: this.sources });
      if (url.pathname === '/api/jobs/metadata/view/refresh') { this.target.view_generation = `g_${++this.revision}`; return this.response({ ...view(this.target.view_generation), local: { available: true, incarnation, version: 1 }, sources: this.sources }); }
      const queryContext: MetadataContext = { ...captured, scope: url.searchParams.get('scope') === 'all' ? 'all' : 'session' };
      if (url.searchParams.has('view_generation') && url.searchParams.get('view_generation') !== captured.view_generation) return this.response({ code: 'view_changed' }, 409);
      if (url.pathname === '/api/jobs/metadata') {
        const mode = url.searchParams.get('mode') || 'snapshot';
        const after = Number(url.searchParams.get('after_revision') || 0);
        const streamKey = JSON.stringify([url.searchParams.get('source'), url.searchParams.get('state_id'), url.searchParams.get('status')]);
        const offset = url.searchParams.has('cursor') ? this.pmOffsets.get(streamKey) ?? 0 : 0;
        const end = Math.min(this.total, offset + 50);
        this.pmOffset = end; this.pmOffsets.set(streamKey, end);
        const status = url.searchParams.get('status');
        const rows = mode === 'changes' || (status !== null && status !== this.pmLifecycle) ? [] : Array.from({ length: end - offset }, (_, i) => ({ ...summary(offset + i + 1), lifecycle: status || this.pmLifecycle, selection: { ...summary(offset + i + 1).selection, session_id: captured.session_id, repo: captured.repo, source: url.searchParams.get('source'), job_ref: { job_id: `job_${offset + i + 1}`, state_id: url.searchParams.get('state_id'), version: 2, incarnation: '12345678-1234-4234-8234-123456789abc' } } }));
        const complete = end === this.total || mode === 'changes' || (status !== null && status !== this.pmLifecycle);
        return this.response({ ...list(), context: queryContext, mode, store: { source: url.searchParams.get('source'), state_id: url.searchParams.get('state_id') }, rows, page: { outcome: complete ? 'complete' : 'partial', revision: this.revision, checkpoint: complete ? this.revision : after, scanned: rows.length, next_cursor: complete ? null : token(end) } });
      }
      if (url.pathname === '/api/jobs/metadata/local') {
        const after = Number(url.searchParams.get('after_revision') || 0);
        if (this.nativeOutcome !== 'normal') return this.response({ version: 1, context: queryContext, incarnation, coverage, missing: ['unretained_history'], rows: [], page: { outcome: this.nativeOutcome, revision: 0, checkpoint: after, scanned: 0, next_cursor: null } });
        const offset = url.searchParams.has('cursor') ? this.localOffset : 0, end = Math.min(this.total, offset + 40);
        this.localOffset = end;
        const changes = url.searchParams.get('mode') === 'changes';
        const rows = changes ? this.deletedNative === null ? [] : [{ local_ref: nativeSummary(this.deletedNative).local_ref, revision: this.revision, deleted: true, session_id: captured.session_id }] : Array.from({ length: end - offset }, (_, i) => ({ ...nativeSummary(offset + i), session_id: captured.session_id }));
        const complete = end === this.total || changes;
        return this.response({ version: 1, context: queryContext, incarnation, coverage, missing: ['unretained_history'], rows, page: { outcome: complete ? 'complete' : 'partial', revision: this.revision, checkpoint: complete ? this.revision : after, scanned: rows.length, next_cursor: complete ? null : token(end) } });
      }
      if (url.pathname === '/api/jobs/metadata/local/detail') {
        const lane = url.searchParams.get('lane'), next = url.searchParams.has('cursor');
        const rows = lane === 'actions' ? Array.from({ length: next ? 10 : 50 }, (_, i) => ({ action_id: `action_${i + (next ? 50 : 0)}`, worker_id: url.searchParams.get('job_id'), kind: 'read_file', goal: 'Inspect fixture', status: 'done', error: '', duration_ms: 1, truncated: false }))
          : lane === 'output' ? [{ offset: 0, text: 'Native output fixture' }] : [{ local_ref: nativeSummary(2).local_ref }];
        const complete = lane !== 'actions' || next;
        const selected = nativeSummary(Number(url.searchParams.get('job_id')?.replace(/^job_/, '')));
        return this.response({ version: 1, context: queryContext, local_ref: { job_id: url.searchParams.get('job_id'), incarnation },
          summary: { ...selected, revision: this.revision, session_id: captured.session_id }, lane, coverage, missing: ['unretained_history'], cancellation_authority: false, rows, total: lane === 'actions' ? 60 : 1,
          ...(lane === 'output' ? { output: { coverage: 'in_memory_only', source_chars: 21, spilled: false } } : {}),
          page: { outcome: complete ? 'complete' : 'partial', revision: this.revision, checkpoint: complete ? this.revision : 0, scanned: rows.length, next_cursor: complete ? null : token(50) } });
      }
      if (url.pathname === '/api/jobs/metadata/detail') { const d = detail(); return this.response({ ...d, context: queryContext, selection: { ...d.selection, session_id: captured.session_id, repo: captured.repo, job_ref: { job_id: url.searchParams.get('job_id'), state_id: url.searchParams.get('state_id'), version: 2, incarnation: '12345678-1234-4234-8234-123456789abc' }, source: url.searchParams.get('source') } }); }
      if (url.pathname === '/api/jobs/metadata/pins') { const body = _body as { selections: unknown[] }; return this.response({ version: 1, context: queryContext, results: body.selections.map(selection => ({ selection, result: { kind: 'unavailable', reason: 'fixture_missing' } })) }); }
      return this.response([]);
    } finally { this.active--; }
  }
  response(value: unknown, status = 200) { return { kind: 'response' as const, status, correlationId: '', text: JSON.stringify(value) }; }
  installIPC() { Object.defineProperty(window, 'harnessIPC', { value: { endpointHeaders: true, requestJSON: this.request.bind(this) }, configurable: true }); }
}
