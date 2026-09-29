import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import MetadataJobs, { MetadataInspection } from '../components/MetadataJobs';
import { JobMetadataContext, metadataJobs } from '../lib/jobMetadataContext';
import { JobMetadataStore } from '../lib/useJobMetadata';
import { JobMetadataClient, metadataSelectionKey } from '../lib/jobMetadata';
import type { MetadataSummary } from '../lib/jobMetadata';
import type { LocalSummary } from '../lib/localJobMetadata';
import { localKey } from '../lib/localJobMetadata';
import { clearPendingSwarmOpenJob, peekPendingSwarmNavigation, peekPendingSwarmOpenArtifact, peekPendingSwarmOpenJob, queuePendingSwarmNavigation, swarmNavigationTarget } from '../lib/pendingSwarmOpenJob';
import { context, detail, handshake, list, selection, summary, view } from './jobMetadata.fixtures';
import { nativeSummary } from './metadataMigration.fixtures';

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return {
    ...actual,
    api: {
      ...actual.api,
      dashboard: vi.fn().mockResolvedValue({
        ok: true, reused: true, host: '127.0.0.1', port: 8787,
        url: 'http://127.0.0.1:8787/?job=job_1&embed=1',
        embed_url: 'http://127.0.0.1:8787/?job=job_1&embed=1',
      }),
    },
  };
});

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
async function applyView() {
  await act(async () => {
    let result: Awaited<ReturnType<typeof store.readView>> = 'skipped';
    for (let i = 0; i < 8 && (result === 'skipped' || result === 'discarded'); i++) {
      result = await store.readView();
    }
    expect(result).toBe('applied');
  });
}
async function start() {
  await act(async () => { store.setTarget(target); });
  expect(store.getSnapshot().view.kind).not.toBe('idle');
  await applyView();
  await observe();
}
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
  fireEvent.click(row('job_1').getByRole('button', { name: /Dismiss from Jobs/ }));
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
  fireEvent.click(screen.getByRole('button', { name: 'All projects' }));
  fireEvent.click(screen.getByRole('button', { name: 'This session' }));
  fireEvent.click(screen.getByRole('button', { name: 'Hide finished' }));
  fireEvent.click(screen.getByRole('button', { name: 'All projects' }));
  expect(row('job_2').getByRole('button', { name: /failed/ })).toBeVisible();
  expect(preference().dismissed).toEqual([metadataSelectionKey(selection())]);
});
it('keeps interrupted and PM stalled finished, with recoverable and native stalled distinctions', async () => {
  pm = [{ ...summary(), lifecycle: 'interrupted' }, { ...summary(2), lifecycle: 'stalled' }];
  native = [{ ...nativeSummary(3), kind: 'provider', lifecycle: 'stalled' }];
  await start(); mount();
  fireEvent.change(screen.getByLabelText('Filter jobs'), { target: { value: 'attention' } });
  expect(row('job_1').getByRole('button', { name: /interrupted/ })).toBeVisible();
  expect(row('job_2').getByText(/recoverable/i)).toBeVisible();
  expect(row('job_3', 'local').getByText(/may still be active/i)).toBeVisible();
  expect(row('job_3', 'local').queryByRole('button', { name: /Dismiss/ })).toBeNull();
  fireEvent.change(screen.getByLabelText('Filter jobs'), { target: { value: 'finished' } });
  expect(row('job_1').getByRole('button', { name: /interrupted/ })).toBeVisible();
  expect(row('job_2').getByRole('button', { name: /stalled/ })).toBeVisible();
  expect(document.querySelector('[data-job-source="local"]')).toBeNull();
});
it('embeds the exact hidden target without resolving a colliding raw ID', async () => {
  pm.push({ ...summary(), selection: { ...selection(), source: 'cli', job_ref: { job_id: 'job_1', state_id: 'store-B' } } });
  await start(); mount();
  openTarget('job_1');
  expect(screen.getByText(/several sources/)).toBeVisible();
  expect(scroll).not.toHaveBeenCalled();
  fireEvent.click(row('job_1').getByRole('button', { name: /Dismiss/ }));
  openTarget('job_1', metadataSelectionKey(selection()));
  await waitFor(() => expect(row('job_1').getByRole('button', { name: /^PM harness job/ })).toHaveAttribute('aria-expanded', 'true'));
  expect(screen.queryByTestId('job-dashboard-host')).not.toBeInTheDocument();
  expect(scroll).toHaveBeenCalled();
});
it('embeds a PM job when artifact navigation is requested', async () => {
  await start();
  const queued = queuePendingSwarmNavigation(swarmNavigationTarget('job_1', { ...target, contextEpoch: store.getSnapshot().contextEpoch }, metadataJobs(store.getSnapshot())[0], 'artifact-missing'));
  mount();
  await waitFor(() => expect(row('job_1').getByRole('button', { name: /^PM harness job/ })).toHaveAttribute('aria-expanded', 'true'));
  expect(screen.queryByTestId('job-dashboard-host')).not.toBeInTheDocument();
  expect(peekPendingSwarmNavigation()).toBeNull();
  expect(queued.artifactId).toBe('artifact-missing');
});
it('holds an unobserved exact target until that exact row appears', async () => {
  await start(); mount(); openTarget('job_2', metadataSelectionKey(selection(2)));
  expect(screen.getByText(/Target job is not observed/)).toBeVisible(); expect(scroll).not.toHaveBeenCalled();
  pm.push(summary(2)); await observe();
  await waitFor(() => expect(row('job_2').getByRole('button', { name: /^PM/ })).toHaveAttribute('aria-expanded', 'true'));
  expect(screen.queryByTestId('job-dashboard-host')).not.toBeInTheDocument();
});
it('does not consume queued navigation while disabled', async () => {
  await start(); queuePendingSwarmNavigation(swarmNavigationTarget('job_1', { ...target, contextEpoch: store.getSnapshot().contextEpoch }, metadataJobs(store.getSnapshot())[0])); const mounted = mount(false);
  expect(peekPendingSwarmOpenJob()).toBe('job_1'); expect(scroll).not.toHaveBeenCalled();
  mounted.rerender(<JobMetadataContext.Provider value={store}><MetadataJobs /></JobMetadataContext.Provider>);
  await waitFor(() => expect(row('job_1').getByRole('button', { name: /^PM harness job/ })).toHaveAttribute('aria-expanded', 'true'));
  expect(screen.queryByTestId('job-dashboard-host')).not.toBeInTheDocument();
  expect(peekPendingSwarmOpenJob()).toBeNull();
});
it('does not persist PM dashboard focus and isolates native dismissal with compact labels', async () => {
  native = [{ ...nativeSummary(1), lifecycle: 'completed' }];
  await start(); const mounted = mount();
  const label = row('job_1').getByRole('button', { name: /^PM harness job/ });
  expect(label).not.toHaveTextContent('job_1');
  fireEvent.click(row('job_1', 'local').getByRole('button', { name: /Dismiss/ }));
  expect(preference().dismissed).toEqual([localKey(native[0].local_ref)]);
  fireEvent.click(label);
  expect(label).toHaveAttribute('aria-expanded', 'true');
  expect(screen.queryByTestId('job-dashboard-host')).not.toBeInTheDocument();
  mounted.unmount(); await start(); mount();
  expect(screen.queryByTestId('job-dashboard-host')).not.toBeInTheDocument();
  expect(row('job_1').getByRole('button', { name: /^PM harness job/ })).toHaveAttribute('aria-expanded', 'true');
  incarnation = 'native_process_2';
  await waitFor(() => expect(store.getSnapshot().working).toBe(false));
  await act(async () => { store.setTarget(target); });
  await applyView();
  await observe();
  expect(row('job_1', 'local').getByRole('button', { name: /completed/ })).toBeVisible();
});
it('renders accounting ownership separately from exclusion without asserting totals', async () => {
  native = ['declared', 'excluded', 'unresolved'].map((kind, i) => ({ ...nativeSummary(i + 1), kind: 'provider', accounting: { kind: kind === 'declared' ? 'declared' : kind === 'excluded' ? 'excluded' : 'unresolved', aggregation_authority: false } }));
  await start(); mount();
  for (const job of metadataJobs(store.getSnapshot()).filter(j => j.local_ref)) {
    render(<JobMetadataContext.Provider value={store}><MetadataInspection job={job} /></JobMetadataContext.Provider>);
  }
  expect(screen.getByText(/Accounting ownership: declared/)).toBeVisible();
  expect(screen.getByText(/Accounting exclusion: reported/)).toBeVisible();
  expect(screen.getAllByText(/This view does not calculate totals/)).toHaveLength(3);
  expect(screen.queryByText(/Not included in totals/)).toBeNull();
});

it.each(['attention', 'active'])('keeps Hide finished within the %s filter', async filter => {
  pm = [{ ...summary(), lifecycle: 'failed' }, { ...summary(2), lifecycle: 'complete', selection: { ...selection(2), source: 'cli', job_ref: { job_id: 'job_2', state_id: 'store-B' } } }];
  await start();
  pinned = [pm[1]]; store.setPendingSelections(pinned.map(r => r.selection), []); await store.refreshPins();
  mount();
  fireEvent.change(screen.getByLabelText('Filter jobs'), { target: { value: filter } });
  fireEvent.click(screen.getByRole('button', { name: 'Hide finished' }));
  fireEvent.change(screen.getByLabelText('Filter jobs'), { target: { value: 'all' } });
  expect(row('job_2', 'cli').getByRole('button', { name: /^PM CLI job/ })).toBeVisible();
  expect(preference().dismissed).toHaveLength(filter === 'active' ? 0 : 1);
});
it('keeps Hide finished within the repo filter', async () => {
  pm = [{ ...summary(), lifecycle: 'failed' }, { ...summary(2), lifecycle: 'complete', selection: { ...selection(2), source: 'cli', job_ref: { job_id: 'job_2', state_id: 'store-B' } } }];
  await start();
  pinned = [pm[1]]; store.setPendingSelections(pinned.map(r => r.selection), []); await store.refreshPins();
  mount();
  fireEvent.click(screen.getByRole('button', { name: 'This repo' }));
  fireEvent.click(screen.getByRole('button', { name: 'Hide finished' }));
  fireEvent.click(screen.getByRole('button', { name: 'All projects' }));
  expect(row('job_2', 'cli').getByRole('button', { name: /^PM CLI job/ })).toBeVisible();
  expect(preference().dismissed).toHaveLength(1);
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
  fireEvent.click(row('job_1').getByRole('button', { name: /^PM harness job/ }));
  expect(row('job_1').getByRole('button', { name: /^PM harness job/ })).toHaveAttribute('aria-expanded', 'true');
  expect(screen.queryByTestId('job-dashboard-host')).not.toBeInTheDocument();
  expect(preference().expanded).toHaveLength(8);
  fireEvent.click(row('job_1').getByRole('button', { name: /^PM harness job/ }));
  expect(row('job_1').getByRole('button', { name: /^PM harness job/ })).toHaveAttribute('aria-expanded', 'false');
  fireEvent.click(row('job_1', 'local').getByRole('button', { name: /Dismiss/ }));
  native[0] = { ...native[0], lifecycle: 'running' }; await observe();
  expect(preference().dismissed).not.toContain(localKey(native[0].local_ref));
  native[0] = { ...native[0], lifecycle: 'completed' }; await observe();
  expect(row('job_1', 'local').getByRole('button', { name: /completed/ })).toBeVisible();
});
it('embeds a PM job from a remounted artifact deep-link', async () => {
  await start();
  const mounted = mount(); openTarget('job_1', metadataSelectionKey(selection()), 'artifact-missing');
  await waitFor(() => expect(row('job_1').getByRole('button', { name: /^PM harness job/ })).toHaveAttribute('aria-expanded', 'true'));
  expect(screen.queryByTestId('job-dashboard-host')).not.toBeInTheDocument();
  expect(peekPendingSwarmNavigation()).toBeNull();
  mounted.unmount(); await start(); mount();
  expect(screen.queryByTestId('job-dashboard-host')).not.toBeInTheDocument();
  openTarget('job_1', metadataSelectionKey(selection()), 'artifact-missing');
  await waitFor(() => expect(row('job_1').getByRole('button', { name: /^PM harness job/ })).toHaveAttribute('aria-expanded', 'true'));
  expect(peekPendingSwarmNavigation()).toBeNull();
});
it('requires both job ID and exact selection key to match and never falls back from a missing exact key', async () => {
  await start(); mount(); openTarget('job_2', metadataSelectionKey(selection()));
  expect(scroll).not.toHaveBeenCalled();
  openTarget('job_1', metadataSelectionKey({ ...selection(), job_ref: { job_id: 'job_1', state_id: 'missing-store' } }));
  expect(scroll).not.toHaveBeenCalled();
  expect(row('job_1').getByRole('button', { name: /^PM harness job/ })).toHaveAttribute('aria-expanded', 'false');
  expect(screen.queryByTestId('job-dashboard-host')).not.toBeInTheDocument();
});
it('opens the Puppetmaster dashboard from a PM job row', async () => {
  pm = pm.map(row => ({ ...row, selection: { ...row.selection, job_ref: { ...row.selection.job_ref, version: 2, incarnation: '12345678-1234-4234-8234-123456789abc' } } }));
  await start(); mount();
  fireEvent.click(row('job_1').getByRole('button', { name: /^PM harness job/ }));
  expect(row('job_1').getByRole('button', { name: /^PM harness job/ })).toHaveAttribute('aria-expanded', 'true');
  expect(screen.queryByTestId('job-dashboard-host')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'See in Puppetmaster dashboard' })).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Open Puppetmaster board' })).toBeInTheDocument();
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
it('Clear is offered only while the Finished section is open (it clears what is shown)', async () => {
  pm[0] = { ...pm[0], lifecycle: 'complete' };
  await start(); mount();
  expect(screen.getByRole('button', { name: 'Hide finished' })).toBeVisible();
  fireEvent.click(screen.getByRole('button', { expanded: true, name: /Finished/ }));
  expect(screen.queryByText('Clear')).toBeNull();
});
it('the header check counts only completed runs, not failed ones', async () => {
  pm = [{ ...summary(), lifecycle: 'complete' }, { ...summary(2), lifecycle: 'failed' }];
  const { container } = (await start(), mount());
  const check = container.querySelector('.text-good\\/80');
  expect(check?.textContent?.trim()).toBe('1');
});
it('a finished job offers no Stop control', async () => {
  pm[0] = { ...pm[0], lifecycle: 'complete' };
  await start(); mount();
  fireEvent.click(row('job_1').getByRole('button', { name: /complete/ }));
  expect(screen.queryByRole('button', { name: 'Stop selected workers' })).toBeNull();
  expect(screen.queryByText(/Stop unavailable/)).toBeNull();
});
