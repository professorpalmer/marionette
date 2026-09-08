import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import wire from './nativeSelectedContext.backend.json';
import MetadataActivity from '../components/conversation/MetadataActivity';
import { JobMetadataContext, metadataJobs, useSharedJobMetadata } from '../lib/jobMetadataContext';
import { JobMetadataStore } from '../lib/useJobMetadata';
import { JobMetadataClient } from '../lib/jobMetadata';
import { localKey } from '../lib/localJobMetadata';
import type { LocalSummary } from '../lib/localJobMetadata';
import { context, handshake, list, view } from './jobMetadata.fixtures';

function deferred<T>() {
  let resolve: (value: T) => void = () => {};
  const promise = new Promise<T>(r => { resolve = r; });
  return { promise, resolve };
}
function Activity() {
  const { state } = useSharedJobMetadata();
  return <MetadataActivity jobs={metadataJobs(state)} />;
}
const stores: JobMetadataStore[] = [];
afterEach(() => { cleanup(); for (const store of stores.splice(0)) store.dispose(); Reflect.deleteProperty(window, 'harnessIPC'); vi.restoreAllMocks(); });

async function fixture() {
  let incarnation = wire.descriptor.incarnation;
  let revision = 10000;
  const selected: LocalSummary = { ...wire.detail.summary, lifecycle: 'running', kind: 'run_command', display: { label: 'Command', model: '', adapter: 'command', truncated: false } };
  let rows: LocalSummary[] = [selected];
  let removeSelected = false;
  let detailSummary = selected;
  let detailOutcome: 'complete' | 'unavailable' = 'complete';
  let detailStatus = 200;
  let omitSummary = false;
  let stopError = false;
  let stop = Promise.resolve({ ok: true });
  let holdList: Promise<void> | null = null;
  let holdDetail: Promise<void> | null = null;
  let metadataActive = 0, maxMetadataActive = 0;
  const requests: { method: string; url: URL; body: unknown }[] = [];
  Object.defineProperty(window, 'harnessIPC', { configurable: true, value: { endpointHeaders: true,
    requestJSON: async (method: string, path: string, body: unknown) => {
      const url = new URL(path, 'http://fixture'); requests.push({ method, url, body });
      const metadata = url.pathname.startsWith('/api/jobs/metadata');
      if (metadata) { metadataActive++; maxMetadataActive = Math.max(maxMetadataActive, metadataActive); }
      try {
        let value: unknown;
        let status = 200;
        if (url.pathname === '/api/endpoint') value = handshake;
        else if (url.pathname === '/api/swarm/cancel') { if (stopError) throw Error('Acknowledgement lost'); value = await stop; }
        else if (url.pathname.endsWith('/view')) value = { ...view(), local: { ...wire.descriptor, incarnation } };
        else if (url.pathname.endsWith('/local/detail')) {
          const captured = detailSummary;
          if (holdDetail) { const held = holdDetail; holdDetail = null; await held; }
          status = detailStatus;
          value = { ...wire.detail, local_ref: captured.local_ref, summary: omitSummary ? undefined : captured, selected_context: url.searchParams.get('include_context') === 'true' ? wire.detail.selected_context : undefined, rows: detailOutcome === 'unavailable' ? [] : url.searchParams.get('lane') === 'children' ? wire.children.rows : [], total: url.searchParams.get('lane') === 'children' ? 1 : 0,
            lane: url.searchParams.get('lane'), page: { outcome: detailOutcome, revision: captured.revision, checkpoint: captured.revision, scanned: detailOutcome === 'complete' && url.searchParams.get('lane') === 'children' ? 1 : 0, next_cursor: null } };
          if (status === 409) value = { code: 'endpoint_mismatch' };
        } else if (url.pathname.endsWith('/local')) {
          if (holdList) { const held = holdList; holdList = null; await held; }
          const active = url.searchParams.get('lane') === 'active';
          const pageRows = active && removeSelected ? [...rows, { local_ref: selected.local_ref, revision, session_id: context.session_id, deleted: true }] : rows;
          if (active) removeSelected = false;
          value = { ...(active ? wire.active : wire.history), incarnation, rows: pageRows,
            page: { outcome: 'complete', revision, checkpoint: revision, scanned: pageRows.length, next_cursor: null } };
        } else if (url.pathname === '/api/jobs/metadata') value = { ...list([]), mode: url.searchParams.get('mode') };
        else throw Error(`Unexpected request ${method} ${path}`);
        return { kind: 'response', status, correlationId: '', text: JSON.stringify(value) };
      } finally { if (metadata) metadataActive--; }
    },
  } });
  const store = new JobMetadataStore(new JobMetadataClient(1000)); stores.push(store);
  store.setTarget(context); expect(await store.readView()).toBe('applied'); expect(await store.advance()).toBe('applied');
  render(<JobMetadataContext.Provider value={store}><Activity /></JobMetadataContext.Provider>);
  const open = async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Command · running' }));
    fireEvent.click(screen.getByRole('button', { name: 'Inspect actions' }));
    await screen.findByText(/actions: complete/);
  };
  const advance = async (count = 8) => { for (let i = 0; i < count; i++) await act(async () => { expect(await store.advance()).toBe('applied'); }); };
  const finish = () => { detailSummary = { ...wire.cancelled.summary, kind: 'run_command', receipts: { ...wire.cancelled.summary.receipts, terminal: true } }; };
  return { store, selected, open, advance, finish, requests,
    get maxMetadataActive() { return maxMetadataActive; },
    get detailReads() { return requests.filter(r => r.url.pathname.endsWith('/local/detail')); },
    get stops() { return requests.filter(r => r.url.pathname === '/api/swarm/cancel'); },
    stop: (value: Promise<{ ok: boolean }>) => { stop = value; },
    holdList: (value: Promise<void>) => { holdList = value; },
    holdDetail: (value: Promise<void>) => { holdDetail = value; },
    failDetail: (outcome: 'complete' | 'unavailable', status = 200) => { detailOutcome = outcome; detailStatus = status; },
    omitSummary: () => { omitSummary = true; },
    regressSummary: () => { detailSummary = { ...selected, revision: selected.revision - 1 }; },
    loseAcknowledgement: () => { stopError = true; },
    removeSelected: () => { removeSelected = true; },
    replaceRows: (count: number, offset = 0) => {
      revision++;
      rows = Array.from({ length: count }, (_, i) => ({ ...selected, revision, local_ref: { job_id: `local-other-${offset + i}`, incarnation }, display: { label: 'Command', model: `other-${offset + i}`, adapter: 'command', truncated: false } }));
    },
    replaceContext: async (nextIncarnation = incarnation) => {
      incarnation = nextIncarnation;
      rows = [{ ...selected, local_ref: { ...selected.local_ref, incarnation } }];
      await act(async () => { store.setTarget(context); expect(await store.readView()).toBe('applied'); expect(await store.advance()).toBe('applied'); });
    },
  };
}

it('adopts cancelled summary while unavailable children retain visibly stale rows', async () => {
  const f = await fixture(); await f.open();
  fireEvent.click(screen.getByRole('button', { name: 'Inspect children' }));
  await screen.findByText(/children: complete/);
  f.finish(); f.failDetail('unavailable');
  fireEvent.click(screen.getByRole('button', { name: 'Request native stop' }));
  await screen.findByText(/Lifecycle: cancelled/);
  expect(screen.getByRole('button', { name: 'Request native stop' })).toBeDisabled();
  expect(screen.getByText(/Selected lane is stale/)).toBeVisible();
  expect(screen.getByText(/Lifecycle: cancelled/)).not.toHaveTextContent('Observation is stale');
  expect(f.store.getSnapshot().localDetail?.observation?.summary?.lifecycle).toBe('cancelled');
  expect(screen.getByText('local-child / fixture-native')).toBeVisible();
  expect(f.maxMetadataActive).toBe(1);
});

it('refreshes the exact native selection after Stop and retains its result beyond ranking and list eviction', async () => {
  const f = await fixture(); await f.open(); f.finish();
  fireEvent.click(screen.getByRole('button', { name: 'Request native stop' }));
  await screen.findByText(/Lifecycle: cancelled/);
  expect(f.detailReads).toHaveLength(2);
  expect(f.detailReads[1].url.searchParams.get('job_id')).toBe(f.selected.local_ref.job_id);
  expect(f.detailReads[1].url.searchParams.get('incarnation')).toBe(f.selected.local_ref.incarnation);
  expect(f.detailReads[1].url.searchParams.has('cursor')).toBe(false);
  expect(f.stops).toHaveLength(1);
  expect(f.stops[0].body).toMatchObject({ selection: { local_incarnation: f.selected.local_ref.incarnation, job_ref: { job_id: f.selected.local_ref.job_id, state_id: null } } });
  f.removeSelected(); f.replaceRows(8); await f.advance();
  expect(screen.queryByRole('button', { name: 'Command · running' })).not.toBeInTheDocument();
  expect(screen.getByText(/Lifecycle: cancelled/)).toBeVisible();
  for (const offset of [10, 35, 60, 85, 110]) { f.replaceRows(25, offset); await f.advance(); }
  const state = f.store.getSnapshot();
  expect(state.local.observations.some(o => localKey(o.row.local_ref) === localKey(f.selected.local_ref))).toBe(false);
  expect(state.local.observations.length).toBeLessThanOrEqual(100);
  expect(state.local.observations.length + state.observations.length + state.removals.length).toBeLessThanOrEqual(200);
  expect(state.pins.length + state.followedLocal.length).toBe(0);
  expect(screen.getByText(/Lifecycle: cancelled/)).toBeVisible();
  expect(screen.getByText(/Receipt presence: terminal/)).toBeVisible();
  expect(f.maxMetadataActive).toBe(1);
  const close = screen.getByRole('button', { name: 'Close selected inspection' });
  close.focus(); expect(close).toHaveFocus(); fireEvent.click(close);
  expect(screen.queryByText(/Lifecycle: cancelled/)).not.toBeInTheDocument();
  expect(f.store.getSnapshot().localDetail).toBeNull();
});

it('keeps an expanded inspection visible when ranking changes before Stop finishes', async () => {
  const f = await fixture(); await f.open();
  const ack = deferred<{ ok: boolean }>(); f.stop(ack.promise);
  fireEvent.click(screen.getByRole('button', { name: 'Request native stop' }));
  await waitFor(() => expect(f.stops).toHaveLength(1));
  f.removeSelected(); f.replaceRows(8); await f.advance();
  expect(screen.queryByRole('button', { name: 'Command · running' })).not.toBeInTheDocument();
  expect(screen.getByText('Awaiting stop acknowledgement.')).toBeVisible();
  expect(screen.getByRole('button', { name: 'Close selected inspection' })).toBeVisible();
  f.finish(); await act(async () => { ack.resolve({ ok: true }); });
  await screen.findByText(/Lifecycle: cancelled/);
});

it.each(['unavailable', 'error', 'running', 'missing summary'] as const)('does not infer terminal state from an accepted Stop with %s detail', async mode => {
  const f = await fixture(); await f.open();
  if (mode === 'unavailable') f.failDetail('unavailable');
  if (mode === 'error') f.failDetail('complete', 500);
  if (mode === 'missing summary') f.omitSummary();
  fireEvent.click(screen.getByRole('button', { name: 'Request native stop' }));
  await waitFor(() => expect(f.detailReads).toHaveLength(2));
  await screen.findByText(/Stop request accepted;.*(?:Retry inspection|awaiting lifecycle observation)/);
  expect(screen.getByText(/Lifecycle: running/)).toBeVisible();
  expect(screen.queryByText(/Lifecycle: cancelled/)).not.toBeInTheDocument();
  expect(f.stops).toHaveLength(1);
});

it('keeps a lost acknowledgement unconfirmed and never automatically replays Stop', async () => {
  const f = await fixture(); await f.open(); f.loseAcknowledgement();
  fireEvent.click(screen.getByRole('button', { name: 'Request native stop' }));
  await screen.findByText('Stop outcome is unconfirmed. Inspect the job before retrying.');
  expect(screen.getByText(/Lifecycle: running/)).toBeVisible();
  expect(f.detailReads).toHaveLength(1);
  await f.advance();
  expect(f.detailReads).toHaveLength(1);
  expect(f.stops).toHaveLength(1);
  expect(f.stops[0].method).toBe('POST');
  expect(f.stops[0].body).toEqual({ selection: {
    version: 1, source: 'local', repo: context.repo, session_id: context.session_id,
    local_incarnation: f.selected.local_ref.incarnation,
    job_ref: { job_id: f.selected.local_ref.job_id, state_id: null },
  } });
  expect(f.stops[0].url.pathname).toBe('/api/swarm/cancel');
  f.finish();
  fireEvent.click(screen.getByRole('button', { name: 'Inspect actions' }));
  await screen.findByText(/Lifecycle: cancelled/);
  expect(f.detailReads).toHaveLength(2);
  expect(f.detailReads[1].method).toBe('GET');
  expect(f.detailReads[1].body).toBeUndefined();
  expect(f.detailReads[1].url.href).toBe(f.detailReads[0].url.href);
  expect(f.detailReads[1].url.pathname).toBe('/api/jobs/metadata/local/detail');
  expect(f.detailReads[1].url.searchParams.get('job_id')).toBe(f.selected.local_ref.job_id);
  expect(f.detailReads[1].url.searchParams.get('incarnation')).toBe(f.selected.local_ref.incarnation);
  expect(f.detailReads[1].url.searchParams.get('session_id')).toBe(context.session_id);
  expect(f.detailReads[1].url.searchParams.get('repo')).toBe(context.repo);
  expect(f.detailReads[1].url.searchParams.get('view_generation')).toBe(context.view_generation);
  expect(f.detailReads[1].url.searchParams.get('lane')).toBe('actions');
  expect(f.detailReads[1].url.searchParams.has('cursor')).toBe(false);
  expect(screen.getByText('Stop outcome is unconfirmed. Inspect the job before retrying.')).toBeVisible();
  expect(screen.queryByText(/Stop request accepted/)).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Request native stop' })).toBeDisabled();
  expect(f.stops).toHaveLength(1);
});

it.each(['same context', 'replacement incarnation', 'close and reopen'])('fences a lost Stop acknowledgement after %s', async transition => {
  const f = await fixture(); await f.open();
  let rejectStop: (error: Error) => void = () => {};
  f.stop(new Promise((_resolve, reject) => { rejectStop = reject; }));
  fireEvent.click(screen.getByRole('button', { name: 'Request native stop' }));
  await waitFor(() => expect(f.stops).toHaveLength(1));
  if (transition === 'close and reopen') {
    fireEvent.click(screen.getByRole('button', { name: 'Close selected inspection' }));
  } else {
    await f.replaceContext(transition === 'replacement incarnation' ? 'replacement' : undefined);
  }
  fireEvent.click(screen.getByRole('button', { name: 'Command · running' }));
  const before = f.store.getSnapshot();
  const requests = [...f.requests];
  await act(async () => { rejectStop(new Error('Old acknowledgement lost')); });
  expect(f.store.getSnapshot()).toBe(before);
  expect(f.requests).toEqual(requests);
  expect(f.stops).toHaveLength(1);
  expect(f.detailReads).toHaveLength(1);
  expect(screen.queryByText(/Stop outcome is unconfirmed|Awaiting stop acknowledgement|Stop request accepted/)).not.toBeInTheDocument();
  expect(screen.getByText(/Lifecycle: running/)).toBeVisible();
  expect(screen.getByRole('button', { name: 'Request native stop' })).toBeEnabled();
});

it('supports focus entry, Escape close and focus return without retaining extra selections', async () => {
  const f = await fixture();
  const opener = screen.getByRole('button', { name: 'Command · running' });
  fireEvent.click(opener);
  const close = screen.getByRole('button', { name: 'Close selected inspection' });
  expect(close).toHaveFocus();
  const region = screen.getByRole('region', { name: 'Selected job inspection' });
  expect(opener.getAttribute('aria-controls')).toBe(region.id);
  fireEvent.keyDown(close, { key: 'Escape' });
  expect(opener).toHaveFocus();
  expect(screen.queryByRole('region', { name: 'Selected job inspection' })).not.toBeInTheDocument();
  expect(f.store.getSnapshot().localDetail).toBeNull();
});

it('discards a selected read crossing context ABA without releasing the physical slot early', async () => {
  const f = await fixture(); await f.open(); f.finish();
  const held = deferred<void>(); f.holdDetail(held.promise);
  fireEvent.click(screen.getByRole('button', { name: 'Request native stop' }));
  await waitFor(() => expect(f.detailReads).toHaveLength(2));
  act(() => { f.store.setTarget(context); });
  expect(await f.store.readView()).toBe('skipped');
  expect(screen.queryByRole('region', { name: 'Selected job inspection' })).not.toBeInTheDocument();
  await act(async () => { held.resolve(); });
  await waitFor(() => expect(f.store.getSnapshot().working).toBe(false));
  expect(f.store.getSnapshot().localDetail).toBeNull();
  expect(screen.queryByText(/Lifecycle: cancelled/)).not.toBeInTheDocument();
  expect(f.maxMetadataActive).toBe(1);
});

it('retains the last canonical summary when explicit inspection returns an older revision', async () => {
  const f = await fixture(); await f.open(); f.finish();
  fireEvent.click(screen.getByRole('button', { name: 'Request native stop' }));
  await screen.findByText(/Lifecycle: cancelled/);
  f.regressSummary();
  fireEvent.click(screen.getByRole('button', { name: 'Inspect actions' }));
  await screen.findByText('Selected read unavailable. Retry inspection.');
  expect(screen.getByText(/Lifecycle: cancelled.*Observation is stale/)).toBeVisible();
  expect(f.store.getSnapshot().localDetail?.observation?.summary?.revision).toBe(f.selected.revision + 1);
  expect(f.stops).toHaveLength(1);
});

it('closes the old selected inspection on endpoint rejection without replaying Stop or following into the new view', async () => {
  const f = await fixture(); await f.open(); f.failDetail('complete', 409);
  fireEvent.click(screen.getByRole('button', { name: 'Request native stop' }));
  await waitFor(() => expect(f.store.getSnapshot().view).toMatchObject({ kind: 'target', reason: 'endpoint_changed' }));
  expect(screen.queryByRole('region', { name: 'Selected job inspection' })).not.toBeInTheDocument();
  f.failDetail('complete'); await f.replaceContext();
  fireEvent.click(screen.getByRole('button', { name: 'Command · running' }));
  expect(screen.queryByText(/Stop request accepted/)).not.toBeInTheDocument();
  expect(f.detailReads).toHaveLength(2);
  expect(f.stops).toHaveLength(1);
});

it('reports a skipped refresh while a list occupies the physical metadata request slot', async () => {
  const f = await fixture(); await f.open();
  const ack = deferred<{ ok: boolean }>(); f.stop(ack.promise);
  fireEvent.click(screen.getByRole('button', { name: 'Request native stop' }));
  await waitFor(() => expect(f.stops).toHaveLength(1));
  // Advance to the native history slot, then hold its physical request.
  await f.advance(1);
  const held = deferred<void>(); f.holdList(held.promise);
  let reading: Promise<unknown> = Promise.resolve();
  act(() => { reading = f.store.advance(); });
  try {
    f.finish(); await act(async () => { ack.resolve({ ok: true }); });
    await screen.findByText(/Stop request accepted;.*Retry inspection/);
    expect(f.detailReads).toHaveLength(1);
    expect(f.maxMetadataActive).toBe(1);
  } finally { await act(async () => { held.resolve(); await reading; }); }
  fireEvent.click(screen.getByRole('button', { name: 'Inspect actions' }));
  await screen.findByText(/Lifecycle: cancelled/);
  expect(f.stops).toHaveLength(1);
});

it.each(['same', 'replacement'] as const)('fences old Stop acknowledgements across %s context transitions', async kind => {
  const f = await fixture(); await f.open();
  const ack = deferred<{ ok: boolean }>(); f.stop(ack.promise);
  fireEvent.click(screen.getByRole('button', { name: 'Request native stop' }));
  await waitFor(() => expect(f.stops).toHaveLength(1));
  await f.replaceContext(kind === 'same' ? undefined : 'replacement');
  expect(screen.queryByRole('button', { name: 'Request native stop' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Command · running' }));
  await act(async () => { ack.resolve({ ok: true }); });
  expect(f.detailReads).toHaveLength(1);
  expect(screen.queryByText(/Stop request accepted/)).not.toBeInTheDocument();
  expect(screen.getByText(/Lifecycle: running/)).toBeVisible();
});

it('fences selection close and reopen ABA before an acknowledgement', async () => {
  const f = await fixture(); await f.open();
  const ack = deferred<{ ok: boolean }>(); f.stop(ack.promise);
  fireEvent.click(screen.getByRole('button', { name: 'Request native stop' }));
  await waitFor(() => expect(f.stops).toHaveLength(1));
  fireEvent.click(screen.getByRole('button', { name: 'Close selected inspection' }));
  fireEvent.click(screen.getByRole('button', { name: 'Command · running' }));
  await act(async () => { ack.resolve({ ok: true }); });
  expect(f.detailReads).toHaveLength(1);
  expect(screen.queryByText(/Stop request accepted/)).not.toBeInTheDocument();
  const inspection = screen.getByRole('region', { name: 'Selected job inspection' });
  expect(within(inspection).getByText(/Lifecycle: running/)).toBeVisible();
});

it('shows owned context only on explicit reads while followed action reads stay private', async () => {
  const f = await fixture();
  act(() => { f.store.setPendingSelections([], [f.selected.local_ref]); });
  await f.advance(12);
  expect(f.detailReads.length).toBeGreaterThan(0);
  expect(f.detailReads.every(r => !r.url.searchParams.has('include_context'))).toBe(true);
  expect(f.store.getSnapshot().actionPage?.observation).not.toHaveProperty('selected_context');
  act(() => { f.store.setPendingSelections([], []); });
  await f.open();
  expect(f.detailReads.at(-1)?.url.searchParams.get('include_context')).toBe('true');
  expect(screen.getByText('printf selected-context')).toBeVisible();
  expect(screen.getByText(/No model \(native execution\)/)).toBeVisible();
  expect(screen.queryByText(/Model: command/)).toBeNull();
});

it('keeps canonical selected summary across lane switches when a new lane returns an older revision', async () => {
  const f = await fixture(); await f.open(); f.finish();
  fireEvent.click(screen.getByRole('button', { name: 'Request native stop' }));
  await screen.findByText(/Lifecycle: cancelled/);
  f.regressSummary();
  fireEvent.click(screen.getByRole('button', { name: 'Inspect children' }));
  await screen.findByText('Selected read unavailable. Retry inspection.');
  expect(screen.getByText(/Lifecycle: cancelled/)).toBeVisible();
  expect(screen.getByRole('button', { name: 'Request native stop' })).toBeDisabled();
});
