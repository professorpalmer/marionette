import { expect, it } from 'vitest';
import wire from './metadataActive.backend.json';
import expertWire from './nativeExpert.backend.json';
import { parseLocalList, parseLocalDetail } from '../lib/localJobMetadata';
import { context, initial, view, list, stream } from './jobMetadata.fixtures';
import { parseMetadataView, parseMetadataList } from '../lib/jobMetadata';
import { act, fireEvent, render, screen, cleanup, waitFor } from '@testing-library/react';
import { afterEach, vi } from 'vitest';
import { JobMetadataClient } from '../lib/jobMetadata';
import { JobMetadataStore } from '../lib/useJobMetadata';
import { handshake, response, summary, token } from './jobMetadata.fixtures';
import { JobMetadataContext, metadataJobs } from '../lib/jobMetadataContext';
import { MetadataInspection, NativeOperatorFacts } from '../components/MetadataJobs';
import { api } from '../lib/api';
import MetadataActivity from '../components/conversation/MetadataActivity';
import { selection } from './jobMetadata.fixtures';

it('accepts the live native descriptor and active revision domain', () => {
  expect(parseMetadataView({ ...view(), local: wire.descriptor }, context).local).toBeDefined();
  const page = parseLocalList(wire.active, context, wire.descriptor.incarnation, initial, 'active');
  expect(page.rows).toHaveLength(5);
  expect(page.rows[0].revision).toBeGreaterThan(page.page.revision);
});
it('parses detached selected operator facts and known zero', () => {
  const detail = parseLocalDetail(wire.detail, context, wire.detail.local_ref, 'actions');
  expect(detail.summary?.economics).toMatchObject({ kind: 'provider', spend_usd: 0 });
  expect(detail.summary?.usage).toMatchObject({ kind: 'reported', tokens: 0 });
});
it('accepts the PM lane descriptor', () => {
  expect(parseMetadataList({ ...list(), lanes: { statuses: ['running'], active_statuses: ['running'], attention_statuses: [] } }, context, stream, initial)).toBeDefined();
});


afterEach(() => { Reflect.deleteProperty(window, 'harnessIPC'); cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it.each(['zero', 'unknown', 'measured', 'estimated', 'excluded'])('renders real Python operator facts: %s', name => {
  const page = parseLocalList(wire.active, context, wire.descriptor.incarnation, initial, 'active');
  const row = page.rows.find(r => r.local_ref.job_id === `local-z-${name}`);
  if (!row || row.deleted) throw Error('missing fixture');
  render(<NativeOperatorFacts row={row} />);
  expect(screen.getByText(/Model: model/)).toBeVisible();
  expect(screen.getByText(/This view does not calculate totals/)).toBeVisible();
  if (name === 'zero') { expect(screen.getByText(/Provider spend: \$0/)).toBeVisible(); expect(screen.getByText(/0 combined tokens/)).toBeVisible(); }
  if (name === 'unknown' || name === 'excluded') { expect(screen.getByText('Spend unavailable')).toBeVisible(); expect(screen.getByText('Token usage unknown')).toBeVisible(); }
  if (name === 'measured') expect(screen.getByText(/Measured spend: \$0.25 · live/)).toBeVisible();
  if (name === 'estimated') expect(screen.getByText(/Estimated spend: \$0.25 · static/)).toBeVisible();
});
it('rejects mismatched provenance and aggregation authority', () => {
  const row = wire.active.rows.find(r => r.local_ref.job_id === 'local-z-zero');
  const envelope = (change: object) => ({ ...wire.active, rows: [{ ...row, ...change }] });
  expect(() => parseLocalList(envelope({ economics: { kind: 'measured', spend_usd: 1, estimated: false, cost_provenance: 'provider', source: 'financial_receipt' } }), context, wire.descriptor.incarnation, initial, 'active')).toThrow();
  expect(() => parseLocalList(envelope({ accounting: { kind: 'declared', aggregation_authority: true } }), context, wire.descriptor.incarnation, initial, 'active')).toThrow();
});

function install() {
  Reflect.deleteProperty(window, 'harnessIPC');
  let active: unknown = wire.active;
  let historyOverride: unknown;
  let incarnation = wire.descriptor.incarnation;
  let held: (() => void) | null = null;
  let hold = false;
  const paths: URL[] = [];
  let concurrent = 0, maxConcurrent = 0;
  const fetch = vi.fn(async (path: string) => {
    const url = new URL(path, 'http://test'); paths.push(url);
    concurrent++; maxConcurrent = Math.max(maxConcurrent, concurrent);
    try {
      if (hold) { hold = false; await new Promise<void>(r => { held = r; }); }
      if (url.pathname === '/api/endpoint') return response(handshake);
      if (url.pathname.endsWith('/view')) return response({ ...view(), local: { ...wire.descriptor, incarnation }, sources: [...view().sources,
        ...Array.from({ length: 32 }, (_, i) => ({ source: 'cli', state_id: `sibling-${i}`, cross_project: true, available: true }))] });
      if (url.pathname.endsWith('/local')) {
        if (url.searchParams.get('lane') === 'active') return response(active);
        // Replay real bounded history pages without hydrating execution bodies.
        if (historyOverride !== undefined) return response(historyOverride);
        const cursor = url.searchParams.get('cursor');
        const index = cursor ? wire.history_pages.findIndex(p => p.page.next_cursor === cursor) + 1 : 0;
        return response({ ...wire.history_pages[index % wire.history_pages.length], context: { ...context, scope: url.searchParams.get('scope') } });
      }
      if (url.pathname.endsWith('/detail') && url.pathname.includes('/local/')) {
        const lane = url.searchParams.get('lane');
        const detail = lane === 'tasks' ? expertWire.tasks : lane === 'routing' ? expertWire.routing : lane === 'actions' ? wire.detail : null;
        if (!detail || url.searchParams.get('job_id') !== wire.detail.local_ref.job_id || url.searchParams.get('incarnation') !== incarnation) throw Error('unexpected selected request ' + path);
        const local_ref = { ...wire.detail.local_ref, incarnation };
        return response({ ...detail, context: { ...context, scope: url.searchParams.get('scope') }, local_ref,
          rows: detail.rows.map(row => 'task_id' in row && row.task_id !== null ? { ...row, task_id: `${local_ref.job_id}-w0` } : row),
          summary: { ...wire.detail.summary, local_ref, task_count: detail.summary.task_count },
          page: { ...detail.page, revision: wire.detail.summary.revision, checkpoint: wire.detail.summary.revision } });
      }
      if (url.pathname.endsWith('/metadata')) {
        const status = url.searchParams.get('status');
        const row = summary();
        const rows = status === null ? Array.from({ length: 50 }, (_, i) => ({ ...summary(i + 1), lifecycle: 'complete' })) : [{ ...row, lifecycle: status }];
        return response({ ...list(), context: { ...context, scope: 'all' }, mode: url.searchParams.get('mode'),
          store: { source: url.searchParams.get('source'), state_id: url.searchParams.get('state_id') },
          rows: rows.map(r => ({ ...r, selection: { ...r.selection, source: url.searchParams.get('source'), job_ref: { ...r.selection.job_ref, state_id: url.searchParams.get('state_id') } } })),
          page: status === null ? { outcome: 'partial', scanned: 50, revision: 10000, checkpoint: 0, next_cursor: token(paths.length) } : { outcome: 'complete', scanned: 1, revision: 10000, checkpoint: 10000, next_cursor: null } });
      }
      throw Error('unexpected request ' + path);
    } finally { concurrent--; }
  });
  vi.stubGlobal('fetch', fetch);
  Object.defineProperty(window, 'harnessIPC', { configurable: true, value: { endpointHeaders: true, requestJSON: async (_method: string, path: string) => { const r = await fetch(path); return { kind: 'response', status: r.status, text: await r.text(), correlationId: '' }; } } });
  const store = new JobMetadataStore(new JobMetadataClient(1000));
  return { store, paths, get maxConcurrent() { return maxConcurrent; }, hold: () => { hold = true; }, release: () => held?.(),
    history: (value: unknown) => { historyOverride = value; },
    active: (value: unknown) => { active = value; }, incarnation: (value: string) => { incarnation = value; } };
}

it('discovers active IDs before history completes with 192 sibling streams and keeps IPC single-flight', async () => {
  const f = install();
  f.store.setTarget({ ...context, scope: 'all' });
  // Bind real Python envelopes to this UI scope.
  f.active({ ...wire.active, context: { ...wire.active.context, scope: 'all' } });
  await f.store.readView();
  for (let i = 0; i < 8; i++) expect(await f.store.advance()).toBe('applied');
  f.active({ ...wire.new_active, context: { ...wire.active.context, scope: 'all' } });
  for (let i = 0; i < 16; i++) expect(await f.store.advance()).toBe('applied');
  expect(f.store.getSnapshot().local.observations.some(o => o.row.local_ref.job_id === 'local-z-new')).toBe(true);
  const pmPages = f.paths.filter(p => p.pathname === '/api/jobs/metadata');
  expect(pmPages.some(p => p.searchParams.get('state_id') === 'store-A' && p.searchParams.get('status') === 'stitching')).toBe(true);
  expect(f.store.getSnapshot().streams.filter(s => s.stream.store.state_id.startsWith('sibling'))).toHaveLength(192);
  expect(f.paths.filter(p => p.pathname.endsWith('/local') && p.searchParams.get('lane') === 'history').length).toBeLessThan(64);
  expect(f.store.getSnapshot().observations.length + f.store.getSnapshot().local.observations.length + f.store.getSnapshot().removals.length).toBeLessThanOrEqual(200);
  f.hold(); const first = f.store.advance();
  expect(await f.store.advance()).toBe('skipped'); f.release(); await first;
  expect(f.maxConcurrent).toBe(1);
  f.store.dispose();
});

it('active removal retains history and a later stale history page cannot revive membership', async () => {
  const f = install(); f.store.setTarget(context); await f.store.readView(); await f.store.advance();
  f.active(wire.removed);
  for (let i = 0; i < 4; i++) await f.store.advance();
  const row = f.store.getSnapshot().local.observations.find(o => o.row.local_ref.job_id === 'local-z-zero');
  expect(row).toBeDefined(); expect(row?.freshness).toBe('stale');
  expect(f.store.getSnapshot().localActive.keys.some(k => k.includes('local-z-zero'))).toBe(false);
  const old = wire.active.rows.find(r => r.local_ref.job_id === 'local-z-zero');
  const historical = { ...wire.history, rows: [old], page: { outcome: 'complete', revision: 10000, checkpoint: 10000, scanned: 1, next_cursor: null } };
  f.history(historical);
  for (let i = 0; i < 8; i++) await f.store.advance();
  expect(f.store.getSnapshot().local.observations.find(o => o.row.local_ref.job_id === 'local-z-zero')?.freshness).toBe('stale');
  f.history({ ...historical, rows: [{ local_ref: wire.detail.local_ref, session_id: context.session_id, revision: 9999, deleted: true }] });
  for (let i = 0; i < 8; i++) await f.store.advance();
  expect(f.store.getSnapshot().local.observations.some(o => o.row.local_ref.job_id === 'local-z-zero')).toBe(false);
  f.store.dispose();
});

it('actual Stop click captures incarnation and replacement with the same ID gets a new immutable request', async () => {
  const f = install(); f.store.setTarget(context); await f.store.readView(); await f.store.advance();
  const job = metadataJobs(f.store.getSnapshot()).find(j => j.id === 'local-z-zero');
  if (!job) throw Error('missing job');
  const requests: unknown[] = [];
  let resolve: (value: { ok: boolean }) => void = () => {};
  const cancel = vi.spyOn(api, 'swarmCancel').mockImplementation(selection => { requests.push(structuredClone(selection)); return new Promise(r => { resolve = r; }); });
  const wrap = (selected = job) => <JobMetadataContext.Provider value={f.store}><MetadataInspection job={selected} /></JobMetadataContext.Provider>;
  const waitForSelectedReads = async (incarnation: string) => {
    await waitFor(() => {
      expect(f.store.getSnapshot()).toMatchObject({ working: false, localDetail: {
        selection: { job_id: job.id, incarnation }, summaryFreshness: 'observed', error: null,
        tasks: { page: { outcome: 'complete' } }, routing: { page: { outcome: 'complete' } },
      } });
      expect(screen.getByRole('button', { name: 'Request native stop' })).toBeEnabled();
      expect(f.store.getSnapshot().localDetail?.tasks?.rows.map(row => row.task_id)).toEqual([`${job.id}-w0`]);
      expect(f.store.getSnapshot().localDetail?.routing?.rows.map(row => row.task_id)).toEqual([`${job.id}-w0`]);
    });
  };
  const ui = render(wrap());
  await waitForSelectedReads(wire.descriptor.incarnation);
  fireEvent.click(screen.getByRole('button', { name: 'Request native stop' }));
  expect(requests[0]).toMatchObject({ local_incarnation: wire.descriptor.incarnation, job_ref: { job_id: 'local-z-zero', state_id: null } });
  const firstResolve = resolve;
  f.incarnation('replacement');
  f.active({ ...wire.active, incarnation: 'replacement', rows: wire.active.rows.map(r => ({ ...r, local_ref: { ...r.local_ref, incarnation: 'replacement' } })) });
  await act(async () => { await f.store.readView(); await f.store.advance(); });
  const replacement = metadataJobs(f.store.getSnapshot()).find(j => j.id === job.id);
  if (!replacement) throw Error('missing replacement');
  expect(f.store.getSnapshot().local.observations.every(o => o.row.local_ref.incarnation === 'replacement')).toBe(true);
  ui.rerender(wrap(replacement));
  await waitForSelectedReads('replacement');
  fireEvent.click(screen.getByRole('button', { name: 'Request native stop' }));
  expect(cancel).toHaveBeenCalledTimes(2);
  expect(requests[1]).toMatchObject({ local_incarnation: 'replacement' });
  await act(async () => firstResolve({ ok: true }));
  expect(screen.getByText('Awaiting stop acknowledgement.')).toBeVisible();
  await act(async () => resolve({ ok: false }));
  expect(screen.getByText('Stop was refused.')).toBeVisible();
  ui.unmount(); f.store.dispose();
});

it('parses actual public PM queued, running, stitching and attention envelopes', () => {
  for (const status of ['queued', 'running', 'stitching', 'stalled'] as const) {
    const c = { ...wire.pm.context, scope: 'session' } as const;
    const s = { store: { ...wire.pm.store, source: 'harness' }, status } as const;
    const page = parseMetadataList(wire.pm.pages[status], c, s, initial);
    expect(page.rows).toHaveLength(1);
    expect(page.rows[0].deleted).toBe(false);
    expect(page.page.scanned).toBe(1);
  }
});
it('accepts live payload revisions during active traversal and recognizes membership expiry', () => {
  const { incarnation, first, second, expired } = wire.traversal;
  const a = parseLocalList(first, context, incarnation, initial, 'active');
  const b = parseLocalList(second, context, incarnation, { ...initial, cursor: a.page.next_cursor }, 'active');
  expect(b.page.outcome).toBe('partial');
  expect(b.page.revision).toBe(a.page.revision);
  expect(parseLocalList(expired, context, incarnation, { ...initial, cursor: b.page.next_cursor }, 'active').page.outcome).toBe('expired');
});
it('automatically restarts expired active membership without restarting history', async () => {
  const f = install(); f.store.setTarget(context); await f.store.readView(); await f.store.advance();
  f.active({ ...wire.active, rows: [], page: { outcome: 'expired', revision: 0, checkpoint: wire.active.page.checkpoint, scanned: 0, next_cursor: null } });
  for (let i = 0; i < 4; i++) await f.store.advance();
  expect(f.store.getSnapshot().localActive.state).toBe('expired');
  const historyTraversal = f.store.getSnapshot().local.traversal;
  f.active(wire.active);
  for (let i = 0; i < 4; i++) await f.store.advance();
  const lastActive = f.paths.filter(p => p.searchParams.get('lane') === 'active').at(-1);
  expect(lastActive?.searchParams.get('mode')).toBe('snapshot');
  expect(lastActive?.searchParams.has('cursor')).toBe(false);
  expect(f.store.getSnapshot().localActive.state).toBe('complete');
  expect(f.store.getSnapshot().local.traversal).toEqual(historyTraversal);
  f.store.dispose();
});
it('unsupported native identities never issue Stop', async () => {
  const f = install(); f.store.setTarget(context); await f.store.readView(); await f.store.advance();
  const job = metadataJobs(f.store.getSnapshot())[0];
  const cancel = vi.spyOn(api, 'swarmCancel');
  render(<JobMetadataContext.Provider value={f.store}><MetadataInspection job={{ ...job, id: 'job_unsupported', local_ref: { job_id: 'job_unsupported', incarnation: wire.descriptor.incarnation } }} /></JobMetadataContext.Provider>);
  expect(screen.getByRole('button', { name: 'Request native stop' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: 'Request native stop' }));
  expect(cancel).not.toHaveBeenCalled();
  f.store.dispose();
});
it('a selected detached summary remains inspectable after its list observation leaves the window', async () => {
  const f = install(); f.store.setTarget(context); await f.store.readView();
  f.store.selectLocal(wire.detail.local_ref);
  expect(await f.store.readLocalDetail()).toBe('applied');
  expect(f.store.getSnapshot().local.observations).toHaveLength(0);
  const jobs = metadataJobs(f.store.getSnapshot());
  expect(jobs).toHaveLength(1);
  expect(jobs[0].goal).toBe('Provider worker · model');
  render(<JobMetadataContext.Provider value={f.store}><MetadataInspection job={jobs[0]} /></JobMetadataContext.Provider>);
  expect(screen.getByText(/Provider spend: \$0/)).toBeVisible();
  expect(screen.getByText(/actions: complete/)).toBeVisible();
  f.store.dispose();
});

it('stitching is active; stalled, unknown and stale observations are not counted as active', () => {
  render(<MetadataActivity jobs={[
    { id: 'one', goal: 'Stitch', status: 'stitching', metadata_key: 'one' },
    { id: 'two', goal: 'Stalled', status: 'stalled', metadata_key: 'two' },
    { id: 'three', goal: 'Unknown', status: 'unknown', metadata_key: 'three' },
    { id: 'four', goal: 'Stale', status: 'running', metadata_key: 'four', read_status: 'unavailable' },
  ]} />);
  expect(screen.getByText('At least 1 active jobs · Coverage incomplete')).toBeVisible();
});
it('active discovery, both histories and sibling work progress with all eight follow slots occupied', async () => {
  const f = install(); f.store.setTarget({ ...context, scope: 'all' });
  f.active({ ...wire.active, context: { ...context, scope: 'all' } });
  await f.store.readView();
  f.store.setPendingSelections(Array.from({ length: 7 }, (_, i) => selection(i + 1)), [wire.detail.local_ref]);
  // These selections are unresolved; errors remain bounded and cannot take discovery slots.
  for (let i = 0; i < 8; i++) await f.store.advance();
  f.active({ ...wire.new_active, context: { ...context, scope: 'all' } });
  for (let i = 0; i < 24; i++) await f.store.advance();
  const state = f.store.getSnapshot();
  expect(state.pins.length + state.followedLocal.length).toBe(8);
  expect(state.local.observations.some(o => o.row.local_ref.job_id === 'local-z-new')).toBe(true);
  expect(f.paths.some(p => p.pathname === '/api/jobs/metadata' && p.searchParams.get('state_id') === 'store-A' && !p.searchParams.has('status'))).toBe(true);
  expect(f.paths.some(p => p.searchParams.get('state_id')?.startsWith('sibling'))).toBe(true);
  expect(f.paths.filter(p => p.pathname.endsWith('/local') && p.searchParams.get('lane') === 'history')).toHaveLength(4);
  expect(f.maxConcurrent).toBe(1);
  f.store.dispose();
});


it.each(['completed', 'cancelled', 'failed'])('reconciles same-revision %s history after native active removal', async lifecycle => {
  const f = install(); f.store.setTarget(context); await f.store.readView(); await f.store.advance();
  f.active(wire.removed);
  for (let i = 0; i < 4; i++) await f.store.advance();
  const previous = wire.active.rows.find(r => r.local_ref.job_id === 'local-z-zero');
  const removal = wire.removed.rows.find(r => r.local_ref.job_id === 'local-z-zero');
  if (!previous || !removal) throw Error('Missing native transition fixture');
  const terminal = { ...previous, lifecycle, revision: removal.revision };
  f.history({ ...wire.history, rows: [terminal], page: { outcome: 'complete', revision: 10000, checkpoint: 10000, scanned: 1, next_cursor: null } });
  for (let i = 0; i < 8; i++) await f.store.advance();
  const current = f.store.getSnapshot().local.observations.find(o => o.row.local_ref.job_id === 'local-z-zero');
  expect(current).toMatchObject({ freshness: 'observed', row: { lifecycle, revision: removal.revision } });
  expect(f.store.getSnapshot().localActive.keys.some(k => k.includes('local-z-zero'))).toBe(false);
  f.store.dispose();
});
