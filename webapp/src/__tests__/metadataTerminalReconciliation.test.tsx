import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { JobMetadataOwner, metadataJobs, useSharedJobMetadata } from '../lib/jobMetadataContext';
import { context, detail, handshake, list, response, selection, summary, view } from './jobMetadata.fixtures';
import wire from './metadataActive.backend.json';

function Projection() {
  const { state } = useSharedJobMetadata();
  return <output>{metadataJobs(state).map(job => `${job.id}:${job.status === 'running' ? 'Active' : job.status === 'done' ? 'Finished' : job.status}`).join(',')}</output>;
}
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
async function advance(ms: number) { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); }
function setup(kind: 'native' | 'pm' | 'alias' | 'alias_pair') {
  vi.useFakeTimers(); vi.setSystemTime(0); Reflect.deleteProperty(window, 'harnessIPC');
  let terminal = false, hold = false, failHistory = false, failPmHistory = false, stalePmHistory = false, stalePmReads = 0, historyExpiryStage = -1, release = () => {}, concurrent = 0, maxConcurrent = 0;
  const detailCalls = new Map<number, number>();
  const calls: string[] = [];
  const alias = (n: number) => ({ source: 'harness', job_ref: { ...selection(n).job_ref, version: 2, incarnation: '7e62abcd-1234-4234-8234-123456789abc' }, session_id: context.session_id, dispatch_id: `dispatch-${n}` });
  const local = { ...wire.active.rows[0], local_ref: { ...wire.active.rows[0].local_ref, job_id: 'native-job' }, kind: 'provider', lifecycle: 'running', revision: 1,
    ...(['alias', 'alias_pair'].includes(kind) ? { canonical: alias(1) } : {}) };
  const locals = kind === 'alias_pair' ? [local, { ...local, local_ref: { ...local.local_ref, job_id: 'native-job-2' }, canonical: alias(2) }] : [local];
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    const url = new URL(path, 'http://fixture'); calls.push(path); concurrent++; maxConcurrent = Math.max(maxConcurrent, concurrent);
    try {
      if (hold) { hold = false; await new Promise<void>(resolve => { release = resolve; }); }
      const c = { ...context, scope: 'all' };
      if (url.pathname === '/api/endpoint') return response(handshake);
      if (url.pathname.endsWith('/view')) return response({ ...view(), ...(kind !== 'pm' ? { local: wire.descriptor } : {}) });
      if (url.pathname.endsWith('/pins')) return response({ version: 1, context: c, results: [] });
      if (url.pathname.endsWith('/local')) {
        const active = url.searchParams.get('lane') === 'active';
        if (!active && failHistory) { failHistory = false; return response({ error: 'temporary' }, 503); }
        if (!active && historyExpiryStage >= 0) {
          const cursor = url.searchParams.get('cursor');
          if (historyExpiryStage === 0 && !cursor) {
            historyExpiryStage = 1;
            return response({ ...wire.history, context: c, incarnation: wire.descriptor.incarnation, lane: 'history', mode: url.searchParams.get('mode'), rows: [], page: { ...wire.history.page, outcome: 'partial', next_cursor: 'YWJj' } });
          }
          if (cursor) {
            historyExpiryStage = 2;
            return response({ ...wire.history, context: c, incarnation: wire.descriptor.incarnation, lane: 'history', mode: url.searchParams.get('mode'), rows: [], page: { ...wire.history.page, outcome: 'expired', next_cursor: null } });
          }
        }
        const row = terminal && kind !== 'alias' ? { ...local, revision: 2, lifecycle: active ? 'running' : 'done', activity: active ? 'active' : 'terminal', deleted: active } : local;
        return response({ ...(active ? wire.active : wire.history), context: c, incarnation: wire.descriptor.incarnation, lane: active ? 'active' : 'history', mode: url.searchParams.get('mode'), rows: kind === 'alias_pair' ? locals : [row], page: { ...wire.active.page, revision: terminal ? 2 : 1, checkpoint: terminal ? 2 : 1 } });
      }
      if (url.pathname.endsWith('/detail')) {
        const n = url.searchParams.get('job_id') === 'job_2' ? 2 : 1;
        const attempts = (detailCalls.get(n) ?? 0) + 1; detailCalls.set(n, attempts);
        if (kind === 'alias_pair' && n === 1 && attempts > 1) return response({
          ...detail(), context: c, selection: { ...selection(n), job_ref: alias(n).job_ref },
          tasks: { ...detail().tasks, page: { ...detail().tasks.page, outcome: 'unavailable' } },
        });
        return response({ ...detail(), context: c, selection: { ...selection(n), job_ref: alias(n).job_ref }, lifecycle: terminal ? 'done' : 'running' });
      }
      if (url.pathname.endsWith('/metadata')) {
        const status = url.searchParams.get('status');
        if (kind === 'pm' && !status && failPmHistory) { failPmHistory = false; return response({ error: 'temporary' }, 503); }
        let revision = terminal && stalePmHistory ? 10 : terminal ? 2 : 1;
        let rows = kind === 'pm' && (status === 'running' || !status) ? [terminal && status === 'running' ? { selection: selection(), revision, deleted: true as const } : { ...summary(), revision, lifecycle: terminal ? 'done' : 'running' }] : [];
        if (kind === 'pm' && terminal && stalePmHistory && !status) {
          stalePmReads++;
          if (stalePmReads === 1) { revision = 2; rows = [{ ...summary(), revision, lifecycle: 'queued' }]; }
        }
        return response({ ...list(rows), context: c, mode: url.searchParams.get('mode'), page: { ...list().page, revision, checkpoint: revision } });
      }
      throw Error(`Unexpected ${path}`);
    } finally { concurrent--; }
  }));
  render(<JobMetadataOwner repo={context.repo} sessionId={context.session_id}><Projection /></JobMetadataOwner>);
  return { calls, settle: () => { terminal = true; }, settleWithStalePmHistory: () => { terminal = true; stalePmHistory = true; }, failHistoryOnce: () => { failHistory = true; }, failPmHistoryOnce: () => { failPmHistory = true; }, expireHistoryCursorOnce: () => { historyExpiryStage = 0; }, hold: () => { hold = true; }, release: () => release(), get maxConcurrent() { return maxConcurrent; } };
}
it.each(['native', 'pm'] as const)('%s active removal settles from authoritative history without selection', async kind => {
  const f = setup(kind); await advance(16000);
  expect(screen.getByRole('status')).toHaveTextContent('Active');
  f.settle(); await advance(48000);
  expect(screen.getByRole('status')).toHaveTextContent('Finished');
  expect(screen.getByRole('status')).not.toHaveTextContent('Active');
  expect(f.maxConcurrent).toBe(1);
  const before = f.calls.length; await advance(32000);
  expect(f.calls.slice(before).some(path => {
    const url = new URL(path, 'http://fixture');
    return url.searchParams.get('lane') === 'history' || (url.pathname.endsWith('/metadata') && !url.searchParams.has('status'));
  })).toBe(false);
});
it('native terminal reconciliation retries after a transient history failure', async () => {
  const f = setup('native'); await advance(16000);
  f.settle(); f.failHistoryOnce(); await advance(48000);
  expect(screen.getByRole('status')).toHaveTextContent('Finished');
});
it('native terminal reconciliation restarts an expired history cursor', async () => {
  const f = setup('native'); await advance(16000);
  f.settle(); f.expireHistoryCursorOnce(); await advance(80000);
  expect(screen.getByRole('status')).toHaveTextContent('Finished');
});
it('Puppetmaster terminal reconciliation retries after a transient history failure', async () => {
  const f = setup('pm'); await advance(16000);
  f.settle(); f.failPmHistoryOnce(); await advance(64000);
  expect(screen.getByRole('status')).toHaveTextContent('Finished');
});
it('an older active lifecycle cannot clear a newer Puppetmaster removal fence', async () => {
  const f = setup('pm'); await advance(16000);
  f.settleWithStalePmHistory(); await advance(80000);
  expect(screen.getByRole('status')).toHaveTextContent('Finished');
});
it('collapsed canonical alias settles from an aged detail without list revision changes', async () => {
  const f = setup('alias'); await advance(16000);
  expect(f.calls.some(path => new URL(path, 'http://fixture').pathname.endsWith('/detail'))).toBe(true);
  expect(screen.getByRole('status')).toHaveTextContent('Active');
  f.settle(); await advance(32000);
  expect(screen.getByRole('status')).toHaveTextContent('Finished');
});
it('a failing collapsed alias cannot starve another alias terminal refresh', async () => {
  const f = setup('alias_pair'); await advance(24000);
  expect(f.calls.some(path => path.includes('job_id=job_2'))).toBe(true);
  f.settle(); await advance(40000);
  expect(screen.getByRole('status')).toHaveTextContent('native-job-2:Finished');
});
it('visibility restoration catches up after a hidden terminal transition', async () => {
  const f = setup('native'); await advance(16000);
  const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
  f.settle(); const before = f.calls.length; await advance(32000); expect(f.calls).toHaveLength(before);
  hidden.mockReturnValue(false);
  await act(async () => { document.dispatchEvent(new Event('visibilitychange')); });
  await advance(32000); expect(screen.getByRole('status')).toHaveTextContent('Finished');
});
it('an overlapping request skips ticks without stopping future reconciliation', async () => {
  const f = setup('native'); await advance(16000); f.hold(); await advance(2000);
  const before = f.calls.length; await advance(10000); expect(f.calls).toHaveLength(before);
  f.settle(); await act(async () => { f.release(); }); await advance(48000);
  expect(f.calls.length).toBeGreaterThan(before);
  expect(screen.getByRole('status')).toHaveTextContent('Finished'); expect(f.maxConcurrent).toBe(1);
});
