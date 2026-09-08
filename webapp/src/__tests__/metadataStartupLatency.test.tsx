import { writeFileSync } from 'node:fs';
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import wire from './metadataActive.backend.json';
import { JobMetadataOwner, metadataJobs, useSharedJobMetadata } from '../lib/jobMetadataContext';
import type { JobMetadataStore } from '../lib/useJobMetadata';
import { context, handshake, list, response, selection, summary, token, view } from './jobMetadata.fixtures';

let shared: JobMetadataStore;
function Observed() {
  const { store, state } = useSharedJobMetadata(); shared = store;
  return <output>{metadataJobs(state).map(job => job.id).join(',')}</output>;
}
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
function fixture(latencyMs = 0) {
  vi.useFakeTimers(); vi.setSystemTime(0); Reflect.deleteProperty(window, 'harnessIPC');
  let discovered = false, concurrent = 0, maxConcurrent = 0;
  let holdPath = '', release: () => void = () => {};
  let failure = '', partial = false, pages = false;
  const calls: { path: string; status: string | null; state: string | null; lane: string | null; time: number; method: string }[] = [];
  const request = vi.fn(async (path: string, init?: RequestInit) => {
    const url = new URL(path, 'http://fixture');
    const method = init?.method ?? 'GET';
    calls.push({ path: url.pathname, status: url.searchParams.get('status'), state: url.searchParams.get('state_id'), lane: url.searchParams.get('lane'), time: Date.now(), method });
    concurrent++; maxConcurrent = Math.max(maxConcurrent, concurrent);
    try {
      if (latencyMs) await new Promise<void>(resolve => setTimeout(resolve, latencyMs));
      if (url.pathname === holdPath) { holdPath = ''; await new Promise<void>(r => { release = r; }); }
      if (url.pathname === failure) throw Error('fixture failure');
      if (url.pathname === '/api/endpoint') return response(handshake);
      const current = { ...view(discovered ? 'discovered' : context.view_generation), local: wire.descriptor,
        sources: discovered ? [...view().sources, { source: 'cli', state_id: 'store-B', cross_project: false, available: true },
          ...Array.from({ length: 32 }, (_, i) => ({ source: 'cli', state_id: `sibling-${i}`, cross_project: true, available: true }))] : [],
        availability: discovered ? 'known' : 'unavailable', missing: discovered ? [] : ['sources_not_refreshed'] };
      if (url.pathname === '/api/jobs/metadata/view/refresh') {
        expect(method).toBe('POST'); expect(JSON.parse(String(init?.body))).toEqual({ view_generation: context.view_generation });
        discovered = true;
        return response({ ...current, context: { ...current.context, view_generation: 'discovered' }, availability: 'known', missing: [],
          sources: [...view().sources, { source: 'cli', state_id: 'store-B', cross_project: false, available: true },
            ...Array.from({ length: 32 }, (_, i) => ({ source: 'cli', state_id: `sibling-${i}`, cross_project: true, available: true }))] });
      }
      if (url.pathname === '/api/jobs/metadata/pins') {
        expect(method).toBe('POST');
        const selections = Array.from({ length: 7 }, (_, i) => selection(i + 1));
        const c = { ...current.context, scope: 'all' };
        expect(JSON.parse(String(init?.body))).toEqual({ ...c, selections });
        return response({ version: 1, context: c, results: selections.map(selection => ({ selection, result: { kind: 'unavailable', reason: 'fixture' } })) });
      }
      expect(method).toBe('GET');
      if (url.pathname === '/api/jobs/metadata/view') return response(current);
      expect(new Headers(init?.headers).get('X-Harness-Endpoint')).toBeTruthy();
      const c = { ...current.context, scope: url.searchParams.get('scope') };
      if (url.pathname === '/api/jobs/metadata/local/detail') return response({ ...wire.detail, context: c });
      if (url.pathname === '/api/jobs/metadata/local') {
        const base = url.searchParams.get('lane') === 'active' ? wire.active : { ...wire.history, page: { ...wire.history.page, outcome: 'complete', next_cursor: null, checkpoint: wire.history.page.revision } };
        return response({ ...base, context: c, mode: url.searchParams.get('mode') });
      }
      if (url.pathname === '/api/jobs/metadata') {
        expect(discovered).toBe(true);
        const status = url.searchParams.get('status');
        const source = url.searchParams.get('source'), state_id = url.searchParams.get('state_id');
        const row = summary();
        const rows = state_id === 'store-B' && status === 'queued' ? [{ ...row, lifecycle: 'queued', selection: { ...row.selection, source, job_ref: { state_id, job_id: 'job_existing-queued' } } }] : [];
        const base = list();
        return response({ ...base, context: c, mode: url.searchParams.get('mode'), store: { source, state_id }, rows,
          page: pages ? { outcome: 'partial', revision: 10000, checkpoint: 0, scanned: 1, next_cursor: token() } : partial ? { outcome: 'cursor_expired', revision: 0, checkpoint: 0, scanned: 0, next_cursor: null } : base.page });
      }
      throw Error(`Unexpected ${method} ${path}`);
    } finally { concurrent--; }
  });
  vi.stubGlobal('fetch', request);
  const mount = () => render(<JobMetadataOwner repo={context.repo} sessionId={context.session_id}><Observed /></JobMetadataOwner>);
  return { calls, mount, hold: (path: string) => { holdPath = path; }, release: () => release(), fail: (path: string) => { failure = path; }, expire: () => { partial = true; }, pages: () => { pages = true; }, get maxConcurrent() { return maxConcurrent; } };
}
async function advance(ms: number) { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); }
it('measures the real owner first queued observation and finite startup request sequence', async () => {
  const f = fixture(); f.mount(); await advance(1999);
  const startup = f.calls.filter(c => c.path.endsWith('/metadata') || c.path.endsWith('/local'));
  expect(shared.getSnapshot().streams.filter(s => s.initialized)).toHaveLength(12);
  expect(screen.getByRole('status')).toHaveTextContent('job_existing-queued');
  expect(startup).toHaveLength(13);
  expect(startup.every(c => c.lane === 'active' || c.status !== null)).toBe(true);
  expect(startup.some(c => c.state?.startsWith('sibling'))).toBe(false);
  expect(f.calls.filter(c => c.method === 'POST')).toHaveLength(1);
  expect(f.maxConcurrent).toBe(1);
  const before = f.calls.length; await advance(32000);
  expect(f.calls.length - before).toBe(16);
  expect(f.calls.some(c => c.lane === 'history')).toBe(true);
  expect(f.calls.some(c => c.state?.startsWith('sibling'))).toBe(true);
});
it('records the baseline owner delay', async () => {
  const f = fixture(); f.mount(); await advance(0);
  for (let i = 0; i < 40 && !screen.getByRole('status').textContent?.includes('job_existing-queued'); i++) await advance(1000);
  if (process.env.METADATA_TIMING_OUTPUT) writeFileSync(process.env.METADATA_TIMING_OUTPUT, JSON.stringify({ visibleByMs: Date.now(), calls: f.calls }, null, 2));
  expect(screen.getByRole('status')).toHaveTextContent('job_existing-queued');
});
it.each(['hidden', 'dispose', 'ABA'] as const)('held request admits no backlog and %s stops its startup continuation', async mode => {
  const f = fixture(); f.hold('/api/jobs/metadata'); const app = f.mount(); await advance(4000);
  const count = f.calls.length;
  await advance(4000); expect(f.calls).toHaveLength(count);
  if (mode === 'hidden') vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
  else if (mode === 'dispose') shared.dispose();
  else { shared.setTarget({ ...context, session_id: 'B' }); shared.setTarget(context); }
  await act(async () => { f.release(); });
  expect(f.calls).toHaveLength(count); expect(f.maxConcurrent).toBe(1);
  app.unmount(); await advance(10000); expect(f.calls).toHaveLength(count);
});
it.each(['failure', 'expired'] as const)('%s cannot start a retry burst', async mode => {
  const f = fixture(); if (mode === 'failure') f.fail('/api/jobs/metadata'); else f.expire();
  f.mount(); await advance(1999);
  expect(f.calls.filter(c => c.path === '/api/jobs/metadata')).toHaveLength(1);
  const before = f.calls.length; await advance(10000);
  expect(f.calls.length - before).toBeLessThanOrEqual(5);
  expect(f.calls.filter(c => c.method === 'POST')).toHaveLength(1);
});

it('serial HTTP latency fits a finite startup budget without draining partial streams', async () => {
  const f = fixture(10); f.pages(); f.mount(); await advance(1999);
  expect(screen.getByRole('status')).toHaveTextContent('job_existing-queued');
  expect(f.calls).toHaveLength(16);
  expect(f.calls.at(-1)?.time).toBeLessThanOrEqual(160);
  expect(f.calls.filter(c => c.path === '/api/jobs/metadata')).toHaveLength(12);
  expect(shared.getSnapshot().streams.filter(s => s.initialized).every(s => s.state === 'partial')).toBe(true);
  expect(f.maxConcurrent).toBe(1);
  const count = f.calls.length;
  await act(async () => { shared.restartTraversal(); });
  await advance(2000); expect(f.calls).toHaveLength(count + 1);
});
it('hiding while the endpoint handshake is held prevents the subsequent view admission', async () => {
  const f = fixture(); f.hold('/api/endpoint'); f.mount(); await advance(100);
  vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
  await act(async () => { f.release(); });
  expect(f.calls).toHaveLength(1);
});

it('the real owner retains active freshness and sibling/history/pin fairness after startup', async () => {
  const f = fixture(); f.mount(); await advance(0);
  await act(async () => { shared.setPendingSelections(Array.from({ length: 7 }, (_, i) => selection(i + 1)), [wire.detail.local_ref]); });
  const count = f.calls.length; await advance(64000);
  const normal = f.calls.slice(count);
  expect(normal).toHaveLength(32);
  expect(normal.filter(c => c.lane === 'active')).toHaveLength(8);
  expect(normal.filter(c => c.lane === 'history')).toHaveLength(4);
  expect(normal.filter(c => c.path === '/api/jobs/metadata/pins')).toHaveLength(2);
  expect(normal.filter(c => c.path === '/api/jobs/metadata/local/detail')).toHaveLength(4);
  expect(normal.filter(c => c.state?.startsWith('sibling'))).toHaveLength(4);
  expect(normal.filter(c => c.state === 'store-A' && c.status === null).length).toBeGreaterThan(0);
  expect(normal.filter(c => c.status !== null && ['store-A', 'store-B'].includes(c.state ?? ''))).toHaveLength(8);
  expect(f.maxConcurrent).toBe(1);
});
