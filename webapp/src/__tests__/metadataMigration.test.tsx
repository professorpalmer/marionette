import type { ReactNode } from 'react';
import { api } from '../lib/api';
import backend from './localMetadata.backend.json';
import { parseLocalDetail } from '../lib/localJobMetadata';
import { act, fireEvent, render, screen, cleanup } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { JobMetadataClient } from '../lib/jobMetadata';
import { JobMetadataStore } from '../lib/useJobMetadata';
import { JobMetadataContext, JobMetadataOwner, metadataJobs, useSharedJobMetadata } from '../lib/jobMetadataContext';
import MetadataJobs, { MetadataInspection } from '../components/MetadataJobs';
import SwarmPane from '../components/SwarmPane';
import { CombinedMetadataFixture, nativeSummary } from './metadataMigration.fixtures';
import { context, selection } from './jobMetadata.fixtures';
import { parseLocalList } from '../lib/localJobMetadata';

let fixture: CombinedMetadataFixture;
let store: JobMetadataStore;
beforeEach(() => {
  localStorage.clear();
  fixture = new CombinedMetadataFixture(); fixture.installIPC();
  Object.defineProperty(document, 'hidden', { configurable: true, value: false });
  store = new JobMetadataStore(new JobMetadataClient(1000));
});
afterEach(() => { cleanup(); store.dispose(); vi.useRealTimers(); vi.restoreAllMocks(); Reflect.deleteProperty(window, 'harnessIPC'); });
async function open() { store.setTarget(context); expect(await store.readView()).toBe('applied'); }

it('bounds 1101 native and PM jobs together, preserves collisions and applies exact native removal', async () => {
  await open();
  for (let i = 0; i < 256; i++) {
    const before = fixture.calls.length;
    expect(await store.advance()).toBe('applied');
    expect(fixture.calls).toHaveLength(before + 1);
    const s = store.getSnapshot();
    expect(s.local.observations.length + s.observations.length + s.removals.length).toBeLessThanOrEqual(200);
  }
  const jobs = metadataJobs(store.getSnapshot());
  const collision = jobs.filter(j => j.id === 'job_1090');
  expect(collision).toHaveLength(2);
  expect(collision.find(j => j.source === 'local')?.job_ref).toBeUndefined();
  expect(new Set(collision.map(j => j.metadata_key)).size).toBe(2);
  fixture.deletedNative = 1090; fixture.revision++;
  for (let i = 0; i < 8; i++) await store.advance();
  expect(metadataJobs(store.getSnapshot()).filter(j => j.id === 'job_1090').map(j => j.source)).toEqual(['harness']);
  expect(fixture.maximumActive).toBe(1);
  expect(fixture.calls.some(c => /^\/api\/(jobs|swarm\/live)$/.test(c.path))).toBe(false);
});
it.each(['expired', 'unavailable'] as const)('retains native rows as stale after %s and requires intentional retry', async outcome => {
  await open(); await store.advance();
  fixture.nativeOutcome = outcome;
  for (let i = 0; i < 8; i++) await store.advance();
  expect(store.getSnapshot().local.state).toBe(outcome);
  expect(store.getSnapshot().local.observations.every(o => o.freshness === 'stale')).toBe(true);
  const nativeCalls = fixture.calls.filter(c => c.path.startsWith('/api/jobs/metadata/local?')).length;
  for (let i = 0; i < 6; i++) await store.advance();
  expect(fixture.calls.filter(c => c.path.startsWith('/api/jobs/metadata/local?'))).toHaveLength(nativeCalls);
  fixture.nativeOutcome = 'normal'; store.restartTraversal();
  for (let i = 0; i < 8; i++) await store.advance();
  expect(store.getSnapshot().local.state).toBe('partial');
});
it('rejects foreign incarnation, oversized examination and false completeness', async () => {
  const c = { ...context };
  const wire = JSON.parse((await fixture.request('GET', '/api/jobs/metadata/local?' + new URLSearchParams(c))).text);
  const traversal = { mode: 'snapshot', cursor: null, after_revision: 0 } as const;
  expect(() => parseLocalList({ ...wire, incarnation: 'other' }, c, nativeSummary(1).local_ref.incarnation, traversal)).toThrow();
  expect(() => parseLocalList({ ...wire, page: { ...wire.page, scanned: 52 } }, c, wire.incarnation, traversal)).toThrow();
  expect(() => parseLocalList({ ...wire, page: { ...wire.page, outcome: 'complete' } }, c, wire.incarnation, traversal)).toThrow();
});
it('holds the physical IPC slot across timeout and A-B-A, then recovers its current view', async () => {
  await open(); vi.useFakeTimers(); fixture.holdNext = true;
  const first = store.advance(); await Promise.resolve(); await Promise.resolve();
  store.setTarget({ ...context, repo: '/other' }); store.setTarget(context);
  await vi.advanceTimersByTimeAsync(1001); expect(await first).toBe('discarded');
  const before = fixture.calls.length;
  expect(await store.tick()).toBe('skipped'); expect(fixture.calls).toHaveLength(before);
  fixture.nextRelease?.(); await vi.advanceTimersByTimeAsync(1);
  expect(await store.tick()).toBe('applied'); for (let i = 0; i < 3; i++) expect(await store.tick()).toBe('applied');
  expect(store.getSnapshot().local.observations.length).toBeGreaterThan(0);
  expect(fixture.maximumActive).toBe(1);
});
it('invalidated owner resumes with a GET, hidden owner pauses, teardown does not restart', async () => {
  vi.useFakeTimers();
  function Consumer() { const { state } = useSharedJobMetadata(); return <span data-testid="view">{state.view.kind}</span>; }
  const mounted = render(<JobMetadataOwner repo={context.repo} sessionId={context.session_id}><Consumer /><SwarmPane /></JobMetadataOwner>);
  await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
  expect(screen.getByTestId('view')).toHaveTextContent('view');
  act(() => window.dispatchEvent(new Event('harness-config-changed')));
  expect(screen.getByTestId('view')).toHaveTextContent('target');
  await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
  expect(screen.getByTestId('view')).toHaveTextContent('view');
  Object.defineProperty(document, 'hidden', { configurable: true, value: true });
  const before = fixture.calls.length;
  await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
  expect(fixture.calls).toHaveLength(before);
  Object.defineProperty(document, 'hidden', { configurable: true, value: false });
  await act(async () => { document.dispatchEvent(new Event('visibilitychange')); await vi.advanceTimersByTimeAsync(1); });
  expect(fixture.calls.length).toBe(before + 1);
  mounted.unmount(); const stopped = fixture.calls.length;
  await vi.advanceTimersByTimeAsync(10000); expect(fixture.calls).toHaveLength(stopped);
  expect(fixture.calls.filter(c => c.method === 'POST')).toHaveLength(0);
});
it('selected native action pages and output are bounded and explicit, PM controls use complete task bindings', async () => {
  await open(); for (let i = 0; i < 8; i++) await store.advance();
  const job = metadataJobs(store.getSnapshot()).find(j => j.source === 'local')!;
  render(<JobMetadataContext.Provider value={store}><MetadataInspection job={job} /></JobMetadataContext.Provider>);
  const before = fixture.calls.length;
  expect(fixture.calls).toHaveLength(before);
  await act(async () => { fireEvent.click(screen.getByText('Inspect actions')); });
  expect(screen.getByText(/actions: partial/)).toBeInTheDocument();
  expect(fixture.calls).toHaveLength(before + 1);
  await act(async () => { fireEvent.click(screen.getByText('Next selected page')); });
  expect(screen.getByText(/actions: complete/)).toBeInTheDocument();
  expect(store.getSnapshot().localDetail?.observation?.rows).toHaveLength(10);
  await act(async () => { fireEvent.click(screen.getByText('Inspect output')); });
  expect(screen.getByText('Native output fixture')).toBeInTheDocument();
  cleanup();
  await open(); for (let i = 0; i < 8; i++) await store.advance();
  const pm = metadataJobs(store.getSnapshot()).find(j => j.source === 'harness')!;
  render(<JobMetadataContext.Provider value={store}><MetadataInspection job={pm} /></JobMetadataContext.Provider>);
  expect(screen.getByRole('button', { name: 'Stop selected workers' })).toHaveAttribute('aria-disabled', 'true');
  await act(async () => { fireEvent.click(screen.getByText('Inspect tasks and artifacts')); });
  expect(screen.getByRole('button', { name: 'Stop selected workers' })).toHaveAttribute('aria-disabled', 'false');
  expect(screen.getByText(/Historical receipts/)).toBeInTheDocument();
});
it('selected local A-B-A discards held output without restoring a previous detail', async () => {
  await open(); store.selectLocal(nativeSummary(1).local_ref, 'output'); fixture.holdNext = true;
  const first = store.readLocalDetail(); await Promise.resolve(); await Promise.resolve();
  store.selectLocal(nativeSummary(2).local_ref, 'output'); store.selectLocal(nativeSummary(1).local_ref, 'output');
  fixture.nextRelease?.(); expect(await first).toBe('discarded');
  expect(store.getSnapshot().localDetail?.observation).toBeNull();
});
it('reserves native and primary cadence with 192 sibling streams and bounded pending selections', async () => {
  fixture.total = 1;
  fixture.sources = [ ...fixture.sources, { source: 'cli', state_id: 'primary_cli', cross_project: false, available: true },
    ...Array.from({ length: 32 }, (_, i) => ({ source: 'cli' as const, state_id: `sibling_${i}`, cross_project: true, available: true })) ];
  store.setTarget({ ...context, scope: 'all' }); await store.readView();
  const primaryVisits: number[][] = [[], []]; let nativeVisits = 0;
  for (let i = 0; i < 540; i++) {
    await store.advance();
    const u = new URL(fixture.calls.at(-1)!.path, 'http://fixture');
    if (u.pathname === '/api/jobs/metadata/local') nativeVisits++;
    if (u.searchParams.get('state_id') === 'store-A') primaryVisits[0].push(i);
    if (u.searchParams.get('state_id') === 'primary_cli') primaryVisits[1].push(i);
  }
  expect(nativeVisits).toBe(68);
  for (const visits of primaryVisits) expect(Math.max(...visits.slice(1).map((v, i) => v - visits[i]))).toBeLessThanOrEqual(8);
  expect(new Set(fixture.calls.map(c => new URL(c.path, 'http://fixture').searchParams.get('state_id')).filter(s => s?.startsWith('sibling'))).size).toBe(32);
  store.setPendingSelections([selection()], [nativeSummary(1).local_ref]);
  expect(store.getSnapshot().pins.length + store.getSnapshot().followedLocal.length).toBe(2);
});
it('two passive job panes do not create another schedule or automatically hydrate detail', async () => {
  await open(); for (let i = 0; i < 8; i++) await store.advance();
  const before = fixture.calls.length;
  render(<JobMetadataContext.Provider value={store}><MetadataJobs /><SwarmPane /></JobMetadataContext.Provider>);
  expect(fixture.calls).toHaveLength(before);
  expect(screen.getAllByText(/coverage incomplete/i).length).toBeGreaterThan(0);
});

it('accepts actual Python index envelopes for all native lanes', () => {
  const parsed = parseLocalList(backend.page, context, backend.incarnation, { mode: 'snapshot', after_revision: 0, cursor: null });
  expect(parsed.rows.length).toBeGreaterThan(0);
  expect(parsed.page.scanned).toBeLessThanOrEqual(51);
  for (const lane of ['actions', 'output', 'children'] as const) {
    const selected = backend[lane];
    expect(parseLocalDetail(selected, context, selected.local_ref, lane).lane).toBe(lane);
  }
});
it('context epochs fence target changes without discarding result drains for detail/pin selection changes', async () => {
  await open(); const before = store.getSnapshot().contextEpoch;
  store.select(selection()); store.setPins([selection()]);
  expect(store.getSnapshot().contextEpoch).toBe(before);
  store.setTarget({ ...context, repo: '/B' }); store.setTarget(context);
  expect(store.getSnapshot().contextEpoch).toBeGreaterThan(before);
});

it('cancels only the selected PM store when raw IDs collide and retains ambiguous receipts', async () => {
  fixture.total = 1;
  fixture.sources.push({ source: 'cli', state_id: 'collision_control_cli', cross_project: false, available: true });
  await open(); for (let i = 0; i < 6; i++) await store.advance();
  const request = vi.spyOn(api, 'requestCancellation').mockRejectedValue(new Error('Fixture lost acknowledgement'));
  render(<JobMetadataContext.Provider value={store}><MetadataJobs /></JobMetadataContext.Provider>);
  fireEvent.click(screen.getByRole('button', { name: /PM CLI job/ }));
  await act(async () => { fireEvent.click(screen.getByText('Inspect tasks and artifacts')); });
  const cancel = screen.getByRole('button', { name: 'Stop selected workers' });
  await act(async () => { fireEvent.click(cancel); });
  expect(request).toHaveBeenCalledTimes(1);
  expect(request.mock.calls[0][0].selection).toMatchObject({ source: 'cli', repo: context.repo, session_id: context.session_id, job_ref: { job_id: 'job_1', state_id: 'collision_control_cli' } });
  expect(screen.getByText(/Stop unconfirmed/)).toBeInTheDocument();
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Stop selected workers' })); });
  expect(request.mock.calls[1][0]).toEqual(request.mock.calls[0][0]);
});
it('keeps expansion and individual dismissal separate for colliding stores', async () => {
  fixture.total = 1; fixture.pmLifecycle = 'failed';
  fixture.sources.push({ source: 'cli', state_id: 'collision_preferences_cli', cross_project: false, available: true });
  await open(); for (let i = 0; i < 6; i++) await store.advance();
  const wrapper = (children: ReactNode) => <JobMetadataContext.Provider value={store}>{children}</JobMetadataContext.Provider>;
  const first = render(wrapper(<MetadataJobs />));
  fireEvent.click(screen.getByRole('button', { name: /^PM CLI job/ }));
  expect(screen.getByRole('button', { name: /^PM harness/ })).toHaveAttribute('aria-expanded', 'false');
  first.unmount(); await open(); for (let i = 0; i < 6; i++) await store.advance();
  render(wrapper(<MetadataJobs />));
  expect(screen.getByRole('button', { name: /^PM CLI job/ })).toHaveAttribute('aria-expanded', 'true');
  fireEvent.click(screen.getByRole('button', { name: /^Dismiss from tracker: PM CLI/ }));
  expect(screen.queryByRole('button', { name: /^PM CLI job/ })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: /^PM harness/ })).toBeInTheDocument();
});
