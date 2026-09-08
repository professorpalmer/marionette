import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import MetadataJobs from '../components/MetadataJobs';
import { JobMetadataContext } from '../lib/jobMetadataContext';
import { JobMetadataClient, metadataSelectionKey } from '../lib/jobMetadata';
import type { MetadataSummary } from '../lib/jobMetadata';
import { JobMetadataStore } from '../lib/useJobMetadata';
import { localKey, nativeActiveStatuses, parseLocalList } from '../lib/localJobMetadata';
import type { LocalSummary } from '../lib/localJobMetadata';
import { clearPendingSwarmOpenJob } from '../lib/pendingSwarmOpenJob';
import wire from './metadataActive.backend.json';
import nativeWire from './nativeExpert.backend.json';
import { context, handshake, initial, list, summary, token, view } from './jobMetadata.fixtures';

let store: JobMetadataStore;
let native: LocalSummary[];
let pm: MetadataSummary[];
let partial: boolean;
let unavailable: boolean;
let nativeUnavailable: boolean;
let revision: number;
let target = { ...context, scope: 'all' as const };
let paths: string[];
const parsed = parseLocalList(wire.active, context, wire.descriptor.incarnation, initial, 'active');
function sample(id: string, lifecycle: string, created_at: number | null): LocalSummary {
  const row = parsed.rows[0];
  if (row.deleted) throw Error('Expected live backend fixture');
  return { ...row, local_ref: { ...row.local_ref, job_id: id }, lifecycle, created_at,
    display: { label: 'Provider worker', model: id, adapter: 'native', truncated: false } };
}
function row(id: string, source = 'local') {
  const element = document.querySelector(`[data-job-id="${id}"][data-job-source="${source}"]`);
  if (!(element instanceof HTMLElement)) throw Error(`Missing ${source}/${id}`);
  return element;
}
function toggle(id: string, source = 'local') {
  return within(row(id, source)).getAllByRole('button')[0];
}
function filter(value: string) { fireEvent.change(screen.getByLabelText('Filter swarms'), { target: { value } }); }
function order() { return [...document.querySelectorAll('[data-job-id]')].filter(e => e instanceof HTMLElement && !e.hidden).map(e => e.getAttribute('data-job-id')); }
function mount(enabled = true) { return render(<JobMetadataContext.Provider value={store}><MetadataJobs enabled={enabled} /></JobMetadataContext.Provider>); }
async function observe() {
  await waitFor(() => expect(store.getSnapshot().working).toBe(false));
  revision++;
  act(() => store.restartTraversal());
  for (let i = 0; i < 24; i++) await act(async () => { expect(await store.advance()).toBe('applied'); });
}
async function start() {
  await act(async () => { store.setTarget(target); expect(await store.readView()).toBe('applied'); });
  await observe();
}
beforeEach(() => {
  localStorage.clear(); clearPendingSwarmOpenJob();
  target = { ...context, scope: 'all' }; partial = false; unavailable = false; nativeUnavailable = false; revision = 10000; paths = [];
  native = [sample('older-active', 'running', 10), sample('newest-active', 'running', 30),
    sample('older-complete', 'completed', 20), sample('newest-failed', 'failed', 40), sample('undated-active', 'running', null)];
  pm = [{ ...summary(), lifecycle: 'complete' }];
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() });
  Object.defineProperty(window, 'harnessIPC', { configurable: true, value: { endpointHeaders: true,
    requestJSON: async (_method: string, path: string) => {
      paths.push(path); const url = new URL(path, 'http://fixture'); let body: unknown;
      if (url.pathname === '/api/endpoint') body = handshake;
      else if (url.pathname.endsWith('/view')) {
        if (unavailable) throw Error('offline');
        body = { ...view(), context: { repo: target.repo, session_id: target.session_id, view_generation: target.view_generation }, local: wire.descriptor, sources: [...view().sources, { source: 'cli', state_id: 'store-B', cross_project: false, available: true }] };
      } else if (url.pathname.endsWith('/local/detail')) {
        const selected = native.find(row => row.local_ref.job_id === url.searchParams.get('job_id'));
        if (!selected) throw Error('Missing selected native job');
        const lane = url.searchParams.get('lane');
        body = { ...nativeWire.tasks, context: target, local_ref: selected.local_ref,
          summary: { ...selected, session_id: target.session_id, revision }, lane, rows: [], total: 0,
          page: { outcome: 'complete', scanned: 0, revision, checkpoint: revision, next_cursor: null } };
      } else if (url.pathname.endsWith('/local')) {
        const active = url.searchParams.get('lane') === 'active';
        const rows = native.filter(r => !active || nativeActiveStatuses.includes(r.lifecycle)).map(r => ({ ...r, session_id: target.session_id, revision }));
        body = { ...(active ? wire.active : wire.history), context: target, rows: nativeUnavailable ? [] : rows, missing: [], page: nativeUnavailable ? { outcome: 'unavailable', scanned: 0, revision, checkpoint: Number(url.searchParams.get('after_revision') || 0), next_cursor: null } : { outcome: 'complete', scanned: rows.length, revision, checkpoint: revision, next_cursor: null } };
      } else if (url.pathname === '/api/jobs/metadata') {
        const source = url.searchParams.get('source'), state_id = url.searchParams.get('state_id'), status = url.searchParams.get('status');
        const rows = pm.filter(r => r.selection.source === source && r.selection.job_ref.state_id === state_id && (!status || status === r.lifecycle)).map(r => ({ ...r, revision, selection: { ...r.selection, repo: target.repo, session_id: target.session_id } }));
        body = { ...list(rows), context: target, store: { source, state_id }, mode: url.searchParams.get('mode'), page: partial && !status
          ? { outcome: 'partial', scanned: rows.length, revision, checkpoint: 0, next_cursor: token(paths.length) }
          : { outcome: 'complete', scanned: rows.length, revision, checkpoint: revision, next_cursor: null } };
      } else throw Error(`Unexpected automatic read: ${path}`);
      return { kind: 'response', status: 200, correlationId: '', text: JSON.stringify(body) };
    },
  } });
  store = new JobMetadataStore(new JobMetadataClient(1000));
});
afterEach(() => { cleanup(); store.dispose(); vi.restoreAllMocks(); clearPendingSwarmOpenJob(); Reflect.deleteProperty(window, 'harnessIPC'); });

it('restores a balanced filter/sort toolbar and newest-first lifecycle groups using only known creation times', async () => {
  await start(); mount();
  const control = screen.getByLabelText('Filter swarms');
  expect(control.parentElement).toHaveClass('grid', 'grid-cols-2');
  expect(control).toHaveClass('w-full');
  expect(screen.getByRole('button', { name: /Sort swarms/ })).toHaveClass('w-full');
  expect(screen.getByRole('button', { name: /^Active/ })).toHaveAttribute('aria-expanded', 'true');
  expect(screen.getByRole('button', { name: /^Finished/ })).toHaveAttribute('aria-expanded', 'true');
  expect(order()).toEqual(['newest-active', 'older-active', 'undated-active', 'newest-failed', 'older-complete', 'job_1']);
  fireEvent.click(screen.getByRole('button', { name: /Sort swarms/ }));
  expect(order()).toEqual(['older-active', 'newest-active', 'undated-active', 'older-complete', 'newest-failed', 'job_1']);
});
it('never presents partial history ordering or quality filtering as complete', async () => {
  partial = true; await start(); mount();
  expect(store.getSnapshot().streams.some(s => s.state === 'partial')).toBe(true);
  expect(screen.getByText(/Sorting and filters apply only to observed jobs/)).toBeVisible();
  expect(screen.getByText(/PM creation times are unavailable/)).toBeVisible();
  filter('failed');
  expect(order()).toEqual(['newest-failed']);
  filter('untrustworthy');
  expect(screen.getByText(/Quality cannot be assessed/)).toBeVisible();
  expect(screen.queryByText('No jobs observed in this filter. Coverage may be incomplete.')).toBeNull();
  expect(order()).toEqual([]);
});
it('separates failed, cancelled, completed lifecycle and unknown quality, with clear filter recovery', async () => {
  native.push(sample('cancelled', 'cancelled', 50), sample('partial-result', 'partial', 60));
  await start(); mount(); filter('failed'); expect(order()).toEqual(['newest-failed']);
  filter('cancelled'); expect(order()).toEqual(['cancelled']);
  filter('complete'); expect(order()).toEqual(['older-complete', 'job_1']);
  filter('untrustworthy'); expect(order()).toEqual([]);
  fireEvent.click(screen.getByRole('button', { name: 'Clear filter' }));
  expect(toggle('newest-active')).toBeVisible();
});
it('distinguishes unknown activity from active and terminal observations', async () => {
  native.push(sample('stalled-native', 'stalled', 50), sample('unknown-native', 'unknown', 60));
  pm.push({ ...summary(2), lifecycle: 'stalled' });
  await start(); mount();
  expect(screen.getByRole('button', { name: /^Activity unconfirmed/ })).toBeVisible();
  expect(within(row('stalled-native')).queryByRole('button', { name: /Dismiss/ })).toBeNull();
  expect(within(row('job_2', 'harness')).getByRole('button', { name: /Dismiss/ })).toBeVisible();
  filter('active'); expect(order()).toEqual(['newest-active', 'older-active', 'undated-active']);
});
it('keeps the same focused expanded row when chronology and lifecycle change', async () => {
  await start(); mount(); const selected = toggle('older-active'); fireEvent.click(selected); selected.focus();
  native[0] = { ...native[0], lifecycle: 'completed', created_at: 100 };
  await observe();
  expect(toggle('older-active')).toBe(selected); expect(selected).toHaveFocus();
  expect(selected).toHaveAttribute('aria-expanded', 'true');
  expect(within(row('older-active')).getByText(/Lifecycle: completed/)).toBeVisible();
});
it('keeps group collapse independent from expansion and opens exact pending targets', async () => {
  await start(); mount(); fireEvent.click(toggle('older-complete'));
  fireEvent.click(screen.getByRole('button', { name: /^Finished/ }));
  expect(row('older-complete')).not.toBeVisible();
  act(() => window.dispatchEvent(new CustomEvent('harness-open-swarm-job', { detail: { jobId: 'older-complete', metadataKey: localKey(native[2].local_ref) } })));
  expect(toggle('older-complete')).toHaveFocus(); expect(toggle('older-complete')).toHaveAttribute('aria-expanded', 'true');
});
it('hides only expanded lifecycle groups that are displayed and preserves other terminal rows', async () => {
  await start(); mount(); fireEvent.click(screen.getByRole('button', { name: /^Finished/ }));
  fireEvent.click(screen.getByRole('button', { name: 'Hide finished' }));
  fireEvent.click(screen.getByRole('button', { name: /^Finished/ }));
  expect(toggle('older-complete')).toBeVisible();
  filter('failed'); fireEvent.click(screen.getByRole('button', { name: 'Hide finished' }));
  filter('all'); expect(toggle('older-complete')).toBeVisible();
  expect(document.querySelector('[data-job-id="newest-failed"]')).toBeNull();
});
it('dismisses a terminal collision by exact store and restores expansion across remounts', async () => {
  native = [];
  pm.push({ ...pm[0], selection: { ...pm[0].selection, source: 'cli', job_ref: { job_id: 'job_1', state_id: 'store-B' } } });
  await start(); const ui = mount(); fireEvent.click(toggle('job_1', 'harness'));
  fireEvent.click(within(row('job_1', 'cli')).getByRole('button', { name: /Dismiss/ }));
  expect(toggle('job_1', 'harness')).toHaveAttribute('aria-expanded', 'true');
  const preferences = JSON.parse(localStorage.getItem(`pmharness.metadata.jobs:${JSON.stringify([target.repo, target.session_id])}`) || '{}');
  expect(preferences.dismissed).toEqual([metadataSelectionKey(pm[1].selection)]);
  ui.unmount(); await start(); mount(); expect(toggle('job_1', 'harness')).toHaveAttribute('aria-expanded', 'true');
  expect(document.querySelector('[data-job-source="cli"]')).toBeNull();
});
it('stays passive while hidden and does not consume navigation or start extra reads', async () => {
  await start(); const baseline = paths.length; const ui = mount(false);
  act(() => window.dispatchEvent(new CustomEvent('harness-open-swarm-job', { detail: { jobId: 'older-active', metadataKey: localKey(native[0].local_ref) } })));
  expect(paths).toHaveLength(baseline); expect(HTMLElement.prototype.scrollIntoView).not.toHaveBeenCalled();
  ui.rerender(<JobMetadataContext.Provider value={store}><MetadataJobs /></JobMetadataContext.Provider>);
  expect(toggle('older-active')).toHaveFocus();
  filter('failed'); fireEvent.click(screen.getByRole('button', { name: /Sort swarms/ }));
  expect(paths).toHaveLength(baseline);
});
it('distinguishes cold loading, failed discovery, and a known empty observed window', async () => {
  store.setTarget(target); mount(); expect(screen.getByText(/Waiting for job metadata/)).toBeVisible();
  unavailable = true; await act(async () => { await store.readView(); });
  expect(screen.getByRole('alert')).toBeVisible();
  expect(screen.queryByText(/No jobs observed in this filter/)).toBeNull();
  unavailable = false; native = []; pm = []; await start();
  expect(screen.getByText(/No jobs observed in this view/)).toBeVisible();
  expect(screen.getByText(/Older or undiscovered work may still exist/)).toBeVisible();
});
it('clears an empty matching filter without implying the retained window is all history', async () => {
  await start(); mount(); filter('cancelled');
  expect(screen.getByText(/No jobs observed in this filter/)).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: 'Clear filter' }));
  expect(toggle('older-active')).toBeVisible();
});
it('reveals a focused row completing into a collapsed group without replacing its inspector', async () => {
  await start(); mount(); fireEvent.click(screen.getByRole('button', { name: /^Finished/ }));
  const selected = toggle('older-active'); fireEvent.click(selected); selected.focus();
  native[0] = { ...native[0], lifecycle: 'completed' }; await observe();
  expect(toggle('older-active')).toBe(selected); expect(selected).toHaveFocus(); expect(selected).toBeVisible();
  expect(selected).toHaveAttribute('aria-expanded', 'true');
});
it('does not move focus for navigation while the document is hidden', async () => {
  await start(); mount();
  const visibility = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
  act(() => document.dispatchEvent(new Event('visibilitychange')));
  act(() => window.dispatchEvent(new CustomEvent('harness-open-swarm-job', { detail: { jobId: 'older-active', metadataKey: localKey(native[0].local_ref) } })));
  expect(HTMLElement.prototype.scrollIntoView).not.toHaveBeenCalled();
  visibility.mockReturnValue(false); act(() => document.dispatchEvent(new Event('visibilitychange')));
  expect(toggle('older-active')).toHaveFocus();
});

it('moves stale lifecycle observations into unconfirmed activity without claiming running jobs', async () => {
  await start(); mount(); nativeUnavailable = true;
  act(() => store.restartTraversal());
  for (let i = 0; i < 24; i++) await act(async () => { await store.advance(); });
  expect(store.getSnapshot().local.observations.every(o => o.freshness === 'stale')).toBe(true);
  expect(screen.getByRole('button', { name: 'Activity unconfirmed (5 observed)' })).toBeVisible();
  expect(screen.queryByRole('button', { name: /^Active/ })).toBeNull();
  filter('active'); expect(order()).toEqual([]);
});
