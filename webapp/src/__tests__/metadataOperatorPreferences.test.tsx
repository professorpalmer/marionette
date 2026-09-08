import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import MetadataJobs from '../components/MetadataJobs';
import { JobMetadataContext, metadataJobs } from '../lib/jobMetadataContext';
import { JobMetadataStore } from '../lib/useJobMetadata';
import { JobMetadataClient, metadataSelectionKey } from '../lib/jobMetadata';
import type { MetadataSummary } from '../lib/jobMetadata';
import type { LocalSummary } from '../lib/localJobMetadata';
import { localKey } from '../lib/localJobMetadata';
import { clearPendingSwarmOpenJob, peekPendingSwarmNavigation, peekPendingSwarmOpenArtifact, peekPendingSwarmOpenJob, queuePendingSwarmNavigation, swarmNavigationTarget } from '../lib/pendingSwarmOpenJob';
import { context, detail, handshake, list, selection, summary, view } from './jobMetadata.fixtures';
import { nativeSummary } from './metadataMigration.fixtures';

let store: JobMetadataStore;
let pm: MetadataSummary[];
let pinned: MetadataSummary[];
let native: LocalSummary[];
let target = { ...context };
let incarnation = 'native_process_1';
let revision = 1;
let selectedDetail = detail();
let scroll: ReturnType<typeof vi.fn>;
const preferenceKey = () => `pmharness.metadata.jobs:${JSON.stringify([target.repo, target.session_id])}`;
const preference = () => JSON.parse(localStorage.getItem(preferenceKey()) || '{}');
function mount(enabled = true) { return render(<JobMetadataContext.Provider value={store}><MetadataJobs enabled={enabled} /></JobMetadataContext.Provider>); }
function row(id: string, source = 'harness') {
  const element = document.querySelector(`[data-job-id="${id}"][data-job-source="${source}"]`);
  if (!(element instanceof HTMLElement)) throw Error(`Missing ${source} ${id}`);
  return within(element);
}
function openTarget(jobId: string, metadataKey?: string, artifactId?: string) {
  act(() => { window.dispatchEvent(new CustomEvent('harness-open-swarm-job', { detail: { jobId, metadataKey, artifactId } })); });
}
async function observe() {
  revision++;
  act(() => { store.restartTraversal(); });
  for (let i = 0; i < 48; i++) await act(async () => { await store.advance(); });
}
async function start() { await act(async () => { store.setTarget(target); expect(await store.readView()).toBe('applied'); }); await observe(); }
beforeEach(() => {
  localStorage.clear(); clearPendingSwarmOpenJob();
  target = { ...context, scope: 'all' }; incarnation = 'native_process_1'; revision = 1;
  pm = [{ ...summary(), lifecycle: 'complete' }]; native = []; pinned = []; selectedDetail = detail();
  scroll = vi.fn(); Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: scroll });
  Object.defineProperty(window, 'harnessIPC', { configurable: true, value: { endpointHeaders: true,
    requestJSON: async (_method: string, path: string) => {
      const url = new URL(path, 'http://fixture');
      let body: unknown;
      if (url.pathname === '/api/endpoint') body = handshake;
      else if (url.pathname.endsWith('/view')) body = { ...view(), context: { repo: target.repo, session_id: target.session_id, view_generation: target.view_generation }, local: { available: true, incarnation, version: 1 }, sources: [
        ...view().sources, { source: 'cli', state_id: 'store-B', cross_project: true, available: true },
      ] };
      else if (url.pathname.endsWith('/local')) {
        const active = url.searchParams.get('lane') === 'active';
        const rows = active ? [] : native.map(r => ({ ...r, session_id: target.session_id, revision, local_ref: { ...r.local_ref, incarnation } }));
        body = { version: 1, context: target, incarnation, rows, missing: [], coverage: { membership: active ? 'retained_local_active' : 'retained_local_history', ...(active ? { metadata: 'live_during_traversal' } : {}), ordering: 'id', historical: 'unavailable' }, page: { outcome: 'complete', scanned: rows.length, revision, checkpoint: revision, next_cursor: null } };
      } else if (url.pathname.endsWith('/pins')) body = { version: 1, context: target, results: pinned.map(row => ({ selection: row.selection, result: { kind: 'present', row: { ...row, revision } } })) };
      else if (url.pathname.endsWith('/detail')) body = { ...selectedDetail, context: target, selection: pm.find(row => row.selection.job_ref.job_id === url.searchParams.get('job_id') && row.selection.job_ref.state_id === url.searchParams.get('state_id'))?.selection ?? detail().selection };
      else if (url.pathname === '/api/jobs/metadata') {
        const source = url.searchParams.get('source'), stateId = url.searchParams.get('state_id'), status = url.searchParams.get('status');
        const rows = pm.filter(r => r.selection.source === source && r.selection.job_ref.state_id === stateId && (!status || status === r.lifecycle)).map(r => ({ ...r, revision, selection: { ...r.selection, repo: target.repo, session_id: target.session_id } }));
        body = { ...list(rows), context: target, store: { source, state_id: stateId }, mode: url.searchParams.get('mode'), page: { outcome: 'complete', scanned: rows.length, revision, checkpoint: revision, next_cursor: null } };
      } else throw Error(`Unexpected request ${path}`);
      return { kind: 'response', status: 200, correlationId: '', text: JSON.stringify(body) };
    },
  } });
  store = new JobMetadataStore(new JobMetadataClient(1000));
});
afterEach(() => { cleanup(); store.dispose(); clearPendingSwarmOpenJob(); vi.restoreAllMocks(); Reflect.deleteProperty(window, 'harnessIPC'); });

it('drops dismissal on observed live reappearance so later completion stays visible', async () => {
  await start(); mount();
  fireEvent.click(row('job_1').getByRole('button', { name: /Dismiss from tracker/ }));
  expect(preference().dismissed).toHaveLength(1);
  pm[0] = { ...pm[0], lifecycle: 'running' }; await observe();
  expect(row('job_1').getByRole('button', { name: /running/ })).toBeVisible();
  expect(preference().dismissed).toEqual([]);
  pm[0] = { ...pm[0], lifecycle: 'complete' }; await observe();
  expect(row('job_1').getByRole('button', { name: /complete/ })).toBeVisible();
});
it('hides finished only within the shown session filter', async () => {
  pm.push({ ...summary(2), lifecycle: 'failed', ownership: { ...summary(2).ownership, session_id: 'other' } });
  await start(); mount();
  fireEvent.change(screen.getByRole('combobox'), { target: { value: 'session' } });
  fireEvent.click(screen.getByRole('button', { name: 'Hide finished' }));
  fireEvent.change(screen.getByRole('combobox'), { target: { value: 'all' } });
  expect(row('job_2').getByRole('button', { name: /failed/ })).toBeVisible();
  expect(preference().dismissed).toEqual([metadataSelectionKey(selection())]);
});
it('keeps interrupted and PM stalled finished, with recoverable and native stalled distinctions', async () => {
  pm = [{ ...summary(), lifecycle: 'interrupted' }, { ...summary(2), lifecycle: 'stalled' }];
  native = [{ ...nativeSummary(3), lifecycle: 'stalled' }];
  await start(); mount();
  fireEvent.change(screen.getByRole('combobox'), { target: { value: 'attention' } });
  expect(row('job_1').getByRole('button', { name: /interrupted/ })).toBeVisible();
  expect(row('job_2').getByText(/recoverable/i)).toBeVisible();
  expect(row('job_3', 'local').getByText(/may still be active/i)).toBeVisible();
  expect(row('job_3', 'local').queryByRole('button', { name: /Dismiss/ })).toBeNull();
  fireEvent.change(screen.getByRole('combobox'), { target: { value: 'finished' } });
  expect(row('job_1').getByRole('button', { name: /interrupted/ })).toBeVisible();
  expect(row('job_2').getByRole('button', { name: /stalled/ })).toBeVisible();
  expect(document.querySelector('[data-job-source="local"]')).toBeNull();
});
it('expands, focuses and scrolls the exact hidden target without resolving a colliding raw ID', async () => {
  pm.push({ ...summary(), selection: { ...selection(), source: 'cli', job_ref: { job_id: 'job_1', state_id: 'store-B' } } });
  await start(); mount();
  openTarget('job_1');
  expect(screen.getByText(/several sources/)).toBeVisible();
  expect(scroll).not.toHaveBeenCalled();
  fireEvent.click(row('job_1').getByRole('button', { name: /Dismiss/ }));
  openTarget('job_1', metadataSelectionKey(selection()));
  const selected = row('job_1').getByRole('button', { name: /^PM harness job/ });
  expect(selected).toHaveAttribute('aria-expanded', 'true'); expect(selected).toHaveFocus();
  expect(scroll).toHaveBeenCalledWith({ block: 'nearest' });
  expect(row('job_1', 'cli').getByRole('button', { name: /^PM CLI job/ })).toHaveAttribute('aria-expanded', 'false');
});
it('retains artifact navigation and states its unavailability without stealing focus on updates', async () => {
  await start();
  const read = vi.spyOn(store, 'readDetail');
  const queued = queuePendingSwarmNavigation(swarmNavigationTarget('job_1', { ...target, contextEpoch: store.getSnapshot().contextEpoch }, metadataJobs(store.getSnapshot())[0], 'artifact-missing'));
  mount();
  await screen.findByRole('region', { name: 'Artifacts' });
  expect(read).toHaveBeenCalledTimes(1);
  expect(peekPendingSwarmNavigation()).toBe(queued);
  expect(peekPendingSwarmOpenJob()).toBe('job_1'); expect(peekPendingSwarmOpenArtifact()).toBe('artifact-missing');
  expect(screen.getByText(/Requested artifact artifact-missing is not in the loaded records/)).toBeVisible();
  expect(row('job_1').getByRole('button', { name: /^PM harness job/ })).toHaveFocus();
  const otherArtifact = document.querySelector('[data-artifact-ids="artifact-1"]');
  expect(otherArtifact).not.toHaveAttribute('open');
  screen.getByRole('combobox').focus(); await observe();
  expect(screen.getByRole('combobox')).toHaveFocus();
  expect(peekPendingSwarmNavigation()).toBe(queued);
  expect(screen.getByText(/Requested artifact artifact-missing is not in the loaded records/)).toBeVisible();
  expect(read).toHaveBeenCalledTimes(1);
  selectedDetail.artifacts.rows.push({ ...selectedDetail.artifacts.rows[0], id: 'artifact-missing' });
  selectedDetail.artifacts.page.scanned = selectedDetail.artifacts.rows.length;
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' })); });
  const exactArtifact = document.querySelector('[data-artifact-ids="artifact-missing"]');
  expect(exactArtifact).toHaveAttribute('open'); expect(exactArtifact).toHaveFocus();
  expect(otherArtifact).not.toHaveAttribute('open');
  expect(peekPendingSwarmNavigation()).toBeNull();
  expect(screen.queryByText(/Requested artifact artifact-missing is not in the loaded records/)).toBeNull();
  expect(screen.getAllByText(/Artifact body unavailable/)).toHaveLength(2);
  screen.getByRole('combobox').focus(); await observe();
  expect(screen.getByRole('combobox')).toHaveFocus(); expect(read).toHaveBeenCalledTimes(2);
});
it('holds an unobserved exact target until that exact row appears', async () => {
  await start(); mount(); openTarget('job_2', metadataSelectionKey(selection(2)));
  expect(screen.getByText(/Target job is not observed/)).toBeVisible(); expect(scroll).not.toHaveBeenCalled();
  pm.push(summary(2)); await observe();
  expect(row('job_2').getByRole('button', { name: /^PM harness job/ })).toHaveFocus();
});
it('does not consume queued navigation while disabled', async () => {
  await start(); queuePendingSwarmNavigation(swarmNavigationTarget('job_1', { ...target, contextEpoch: store.getSnapshot().contextEpoch }, metadataJobs(store.getSnapshot())[0])); const mounted = mount(false);
  expect(peekPendingSwarmOpenJob()).toBe('job_1'); expect(scroll).not.toHaveBeenCalled();
  mounted.rerender(<JobMetadataContext.Provider value={store}><MetadataJobs /></JobMetadataContext.Provider>);
  expect(row('job_1').getByRole('button', { name: /^PM harness job/ })).toHaveFocus();
  expect(peekPendingSwarmOpenJob()).toBeNull();
});
it('preserves expansion across remount and isolates source and incarnation dismissal with compact labels', async () => {
  native = [{ ...nativeSummary(1), lifecycle: 'completed' }];
  await start(); const mounted = mount();
  const label = row('job_1').getByRole('button', { name: /^PM harness job/ });
  expect(label).not.toHaveTextContent('job_1'); fireEvent.click(label);
  fireEvent.click(row('job_1', 'local').getByRole('button', { name: /Dismiss/ }));
  expect(preference().dismissed).toEqual([localKey(native[0].local_ref)]);
  mounted.unmount(); await start(); mount();
  expect(row('job_1').getByRole('button', { name: /^PM harness job/ })).toHaveAttribute('aria-expanded', 'true');
  incarnation = 'native_process_2'; await act(async () => { store.setTarget(target); await store.readView(); }); await observe();
  expect(row('job_1', 'local').getByRole('button', { name: /completed/ })).toBeVisible();
});
it('renders accounting ownership separately from exclusion without asserting totals', async () => {
  native = ['declared', 'excluded', 'unresolved'].map((kind, i) => ({ ...nativeSummary(i + 1), accounting: { kind: kind === 'declared' ? 'declared' : kind === 'excluded' ? 'excluded' : 'unresolved', aggregation_authority: false } }));
  await start(); mount();
  for (const job of metadataJobs(store.getSnapshot()).filter(j => j.local_ref)) openTarget(job.id, job.metadata_key);
  expect(screen.getByText(/Accounting ownership: declared/)).toBeVisible();
  expect(screen.getByText(/Accounting exclusion: reported/)).toBeVisible();
  expect(screen.getAllByText(/This view does not calculate totals/)).toHaveLength(3);
  expect(screen.queryByText(/Not included in totals/)).toBeNull();
});

it.each(['repo', 'attention', 'active'])('keeps Hide finished within the %s filter', async filter => {
  pm = [{ ...summary(), lifecycle: 'failed' }, { ...summary(2), lifecycle: 'complete', selection: { ...selection(2), source: 'cli', job_ref: { job_id: 'job_2', state_id: 'store-B' } } }];
  await start();
  pinned = [pm[1]]; store.setPendingSelections(pinned.map(r => r.selection), []); await store.refreshPins();
  mount();
  fireEvent.change(screen.getByRole('combobox'), { target: { value: filter } });
  fireEvent.click(screen.getByRole('button', { name: 'Hide finished' }));
  fireEvent.change(screen.getByRole('combobox'), { target: { value: 'all' } });
  expect(row('job_2', 'cli').getByRole('button', { name: /^PM CLI job/ })).toBeVisible();
  expect(preference().dismissed).toHaveLength(filter === 'active' ? 0 : 1);
});
it.each(['session', 'repo'])('isolates persisted preferences after a %s switch and restores the original scope', async scope => {
  native = [{ ...nativeSummary(1), lifecycle: 'completed' }];
  await start(); mount();
  fireEvent.click(row('job_1', 'local').getByRole('button', { name: /Dismiss/ }));
  const original = { ...target };
  target = scope === 'repo' ? { ...target, repo: '/other' } : { ...target, session_id: 'session-B' };
  await start();
  expect(row('job_1', 'local').getByRole('button', { name: /completed/ })).toBeVisible();
  expect(preference().dismissed).toEqual([]);
  target = original; await start();
  expect(document.querySelector('[data-job-source="local"]')).toBeNull();
  expect(screen.getByRole('button', { name: 'Show 1 hidden' })).toBeVisible();
});
it.each(['{broken', JSON.stringify({ expanded: 'wrong', dismissed: {} })])('recovers malformed preferences without losing jobs', async saved => {
  localStorage.setItem(preferenceKey(), saved); await start(); mount();
  expect(row('job_1').getByRole('button', { name: /^PM harness job/ })).toBeVisible();
  expect(preference()).toEqual({ expanded: [], dismissed: [] });
});
it('bounds stored and user-expanded preferences and keeps native resumed completion visible', async () => {
  localStorage.setItem(preferenceKey(), JSON.stringify({ expanded: Array.from({ length: 20 }, (_, i) => `expanded-${i}`), dismissed: Array.from({ length: 250 }, (_, i) => `dismissed-${i}`) }));
  pm = Array.from({ length: 10 }, (_, i) => ({ ...summary(i + 1), lifecycle: 'complete' }));
  native = [{ ...nativeSummary(1), lifecycle: 'completed' }];
  await start(); mount();
  expect(preference().expanded).toHaveLength(8); expect(preference().dismissed).toHaveLength(200);
  for (let i = 1; i <= 10; i++) fireEvent.click(row(`job_${i}`).getByRole('button', { name: /^PM harness job/ }));
  expect(preference().expanded).toHaveLength(8);
  expect(row('job_1').getByRole('button', { name: /^PM harness job/ })).toHaveAttribute('aria-expanded', 'false');
  fireEvent.click(row('job_1', 'local').getByRole('button', { name: /Dismiss/ }));
  native[0] = { ...native[0], lifecycle: 'running' }; await observe();
  expect(preference().dismissed).not.toContain(localKey(native[0].local_ref));
  native[0] = { ...native[0], lifecycle: 'completed' }; await observe();
  expect(row('job_1', 'local').getByRole('button', { name: /completed/ })).toBeVisible();
});
it('preserves pending artifact IDs supplied directly by an event through remount', async () => {
  await start();
  const read = vi.spyOn(store, 'readDetail');
  const mounted = mount(); openTarget('job_1', metadataSelectionKey(selection()), 'artifact-missing');
  const queued = peekPendingSwarmNavigation();
  await waitFor(() => expect(store.getSnapshot().working).toBe(false));
  expect(read).toHaveBeenCalledTimes(1);
  expect(peekPendingSwarmOpenArtifact()).toBe('artifact-missing');
  mounted.unmount(); await start(); mount();
  await waitFor(() => expect(store.getSnapshot().working).toBe(false));
  expect(read).toHaveBeenCalledTimes(2);
  expect(peekPendingSwarmNavigation()).toBe(queued);
  expect(screen.getByText(/Requested artifact artifact-missing is not in the loaded records/)).toBeVisible();
  selectedDetail.artifacts.rows.push({ ...selectedDetail.artifacts.rows[0], id: 'artifact-missing' });
  selectedDetail.artifacts.page.scanned = selectedDetail.artifacts.rows.length;
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' })); });
  const exactArtifact = document.querySelector('[data-artifact-ids="artifact-missing"]');
  expect(exactArtifact).toHaveAttribute('open'); expect(exactArtifact).toHaveFocus();
  expect(peekPendingSwarmNavigation()).toBeNull();
});
it('requires both job ID and exact selection key to match and never falls back from a missing exact key', async () => {
  await start(); mount(); openTarget('job_2', metadataSelectionKey(selection()));
  expect(scroll).not.toHaveBeenCalled();
  openTarget('job_1', metadataSelectionKey({ ...selection(), job_ref: { job_id: 'job_1', state_id: 'missing-store' } }));
  expect(scroll).not.toHaveBeenCalled();
  expect(row('job_1').getByRole('button', { name: /^PM harness job/ })).toHaveAttribute('aria-expanded', 'false');
});
it('keeps Inspect tasks and receipt-backed Stop reachable through the real row', async () => {
  pm = pm.map(row => ({ ...row, selection: { ...row.selection, job_ref: { ...row.selection.job_ref, version: 2, incarnation: '12345678-1234-4234-8234-123456789abc' } } }));
  await start(); mount();
  fireEvent.click(row('job_1').getByRole('button', { name: /^PM harness job/ }));
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' })); });
  expect(screen.getByText('task-1: running')).toBeVisible();
  expect(screen.getByText(/finding \/ artifact-1/)).toBeVisible();
  expect(screen.getByRole('button', { name: /Stop|Cancel/i })).toBeVisible();
});

it('keeps dismissal on a stale running observation and removes it only after a live observation', async () => {
  pm[0] = { ...pm[0], lifecycle: 'running' }; await start();
  store.restartTraversal();
  localStorage.setItem(preferenceKey(), JSON.stringify({ expanded: [], dismissed: [metadataSelectionKey(selection())] }));
  mount();
  expect(row('job_1').getByRole('button', { name: /^PM harness job/ })).toBeVisible();
  expect(preference().dismissed).toEqual([metadataSelectionKey(selection())]);
  await observe();
  expect(preference().dismissed).toEqual([]);
});
