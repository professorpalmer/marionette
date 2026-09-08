import { act, fireEvent, render, screen, waitFor, within, cleanup } from "@testing-library/react";

import { beforeEach, expect, it, vi, afterEach } from "vitest";

import SwarmPane from "../components/SwarmPane";

import { api, type Job } from "../lib/api";

import { fetchJobArtifacts } from "../lib/jobArtifacts";

import { dispatchProjectSelected } from "../lib/panelTransition";

import { clearSWRCache } from "../lib/useStaleWhileRevalidate";

import { expertDetail, expertMetadataFixture, expertSummary } from "./metadataExpert.fixtures";

import { metadataSelectionKey } from "../lib/jobMetadata";

import type { MetadataSummary, MetadataSelection } from "../lib/jobMetadata";

import nativeWire from "./nativeSelectedContext.backend.json";

vi.mock('../lib/jobArtifacts', async importOriginal => ({
  ...await importOriginal<typeof import('../lib/jobArtifacts')>(), fetchJobArtifacts: vi.fn(),
}));

vi.mock('../lib/api', async importOriginal => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return { ...actual, api: { ...actual.api, requestCancellation: vi.fn(), cancellationReceipt: vi.fn(), swarmCancel: vi.fn(), swarmLive: vi.fn(), sessions: vi.fn(), artifacts: vi.fn() } };
});

const base: Job = { id: 'job_same', job_ref: { job_id: 'job_same', state_id: 'state_a' },
  source: 'harness', session_id: 'A', status: 'running', goal: 'Inspect A', artifacts_complete: false,
  cancellation_view: { status: 'complete', limit: 200, bindings: [{ task_id: 'task', generation: 1, lease_id: 'lease', owner: 'owner' }] } };

let serial = 0;

function rows(jobs: Job[]) {
  vi.mocked(api.swarmLive).mockResolvedValue({ session: { tokens_used: 0, est_cost_usd: 0 }, jobs });
}

async function expand(name: string) {
  const row = await screen.findByRole('button', { name: new RegExp(name) });
  if (row.getAttribute('aria-expanded') === 'false') fireEvent.click(row);
}

beforeEach(() => {
  base.id = `job_same_${++serial}`;
  base.job_ref = { job_id: base.id, state_id: `state_${serial}` };
  vi.resetAllMocks(); localStorage.clear(); sessionStorage.clear(); clearSWRCache();
  dispatchProjectSelected('/A');
  vi.mocked(api.sessions).mockResolvedValue([{ id: 'A', active: true, title: 'A' }]);
  rows([base]);
  vi.mocked(fetchJobArtifacts).mockResolvedValue([]);
});

let metadata: Awaited<ReturnType<typeof expertMetadataFixture>> | undefined;

afterEach(() => { cleanup(); metadata?.dispose(); metadata = undefined; vi.unstubAllGlobals(); });

function selectedIdentity(source: 'harness' | 'cli' = 'harness'): MetadataSelection {
  return { repo: '/A', session_id: 'A', source,
    job_ref: { version: 2, job_id: base.id, state_id: source === 'cli' ? 'state_cli' : `state_${serial}`, incarnation: '11111111-1111-4111-8111-111111111111' } };
}

async function controlsFixture() {
  const fixture = await expertMetadataFixture([
    expertSummary(selectedIdentity(), 'Inspect A'), expertSummary(selectedIdentity('cli'), 'Inspect CLI'),
  ]);
  metadata = fixture;
  return fixture;
}

function controlRow(goal = 'Inspect A', source: 'harness' | 'cli' = 'harness'): MetadataSummary {
  return expertSummary({ repo: '/A', session_id: 'A', source, job_ref: {
    version: 2, job_id: base.id, state_id: source === 'cli' ? 'state_cli' : `state_${serial}`,
    incarnation: '12345678-1234-1234-1234-123456789abc',
  } }, goal);
}

async function mountControls(rows: MetadataSummary[]) {
  const fixture = await expertMetadataFixture(rows);
  metadata = fixture;
  render(<fixture.Provider><SwarmPane /></fixture.Provider>);
  return fixture;
}

function card(name: string) {
  const element = screen.getByRole('button', { name: new RegExp(`^${name} ·`) }).closest('[data-job-id]');
  if (!(element instanceof HTMLElement)) throw Error('Missing job card');
  return within(element);
}

async function inspectControl(name: string) {
  await expand(name);
  fireEvent.click(card(name).getByRole('button', { name: 'Inspect tasks and artifacts' }));
  await waitFor(() => expect(metadata?.store.getSnapshot().working).toBe(false));
  return card(name).getByRole('button', { name: 'Stop selected workers' });
}

it('cancels only one of two rows sharing a job id', async () => {
  const cliRow = controlRow('Inspect CLI', 'cli');
  const f = await mountControls([controlRow(), cliRow]);
  vi.mocked(api.requestCancellation).mockReturnValue(new Promise(() => {}));
  const otherCancel = await inspectControl('Inspect A');
  const cancel = await inspectControl('Inspect CLI');
  expect(cancel).toHaveAttribute('aria-disabled', 'false');
  fireEvent.click(cancel);
  expect(api.requestCancellation).toHaveBeenCalledWith({ request_id: expect.any(String), selection: {
    version: 2, ...cliRow.selection, bindings: expertDetail(cliRow.selection, f.context()).tasks.rows.map(t => t.binding),
  } });
  expect(screen.getAllByText(/Awaiting cancellation acknowledgement/)).toHaveLength(1);
  expect(otherCancel).toHaveAttribute('aria-disabled', 'false');
  expect(api.swarmLive).not.toHaveBeenCalled();
  expect(fetchJobArtifacts).not.toHaveBeenCalled();
});

it('dismisses one colliding terminal row and persists only scoped keys', async () => {
  const cliRow = { ...controlRow('Inspect CLI', 'cli'), lifecycle: 'failed' };
  localStorage.setItem('swarm.dismissed.v2', JSON.stringify({ '/A': [base.id] }));
  await mountControls([{ ...controlRow(), lifecycle: 'failed' }, cliRow]);
  fireEvent.click(card('Inspect CLI').getByRole('button', { name: /Dismiss from tracker/ }));
  expect(screen.queryByRole('button', { name: /^Inspect CLI ·/ })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: /^Inspect A ·/ })).toBeInTheDocument();
  expect(JSON.parse(localStorage.getItem('pmharness.metadata.jobs:["/A","A"]') || 'null')).toEqual({
    expanded: [], dismissed: [metadataSelectionKey(cliRow.selection)],
  });
  expect(localStorage.getItem('swarm.dismissed.v2')).toContain(base.id);
  fireEvent.click(screen.getByRole('button', { name: 'Show 1 hidden' }));
  expect(screen.getByRole('button', { name: /^Inspect CLI ·/ })).toBeVisible();
});

it('fences cancel rejection after switching sessions and starting another cancel', async () => {
  let reject: (error: Error) => void = () => {};
  vi.mocked(api.requestCancellation).mockReturnValueOnce(new Promise((_resolve, fail) => { reject = fail; }))
    .mockReturnValue(new Promise(() => {}));
  const f = await mountControls([controlRow()]);
  fireEvent.click(await inspectControl('Inspect A'));
  const next = controlRow('Inspect B');
  next.selection = { ...next.selection, session_id: 'B' };
  next.ownership = { ...next.ownership, session_id: 'B' };
  act(() => window.dispatchEvent(new CustomEvent('harness-session-changed', { detail: { sessionId: 'B' } })));
  await f.replace([next]);
  fireEvent.click(await inspectControl('Inspect B'));
  expect(api.requestCancellation).toHaveBeenCalledTimes(2);
  expect(vi.mocked(api.requestCancellation).mock.calls[1][0].selection.session_id).toBe('B');
  const reads = f.request.mock.calls.length;
  await act(async () => reject(new Error('Old session stop rejected')));
  expect(screen.getAllByText(/Awaiting cancellation acknowledgement/)).toHaveLength(1);
  expect(screen.queryByText(/Old session stop rejected/)).not.toBeInTheDocument();
  expect(f.request).toHaveBeenCalledTimes(reads);
  expect(api.swarmLive).not.toHaveBeenCalled();
});

it('sends a session-scoped local selection without a durable reference', async () => {
  const f = await expertMetadataFixture([controlRow()]); metadata = f;
  const request = f.request.getMockImplementation();
  const local_ref = { job_id: 'local-test', incarnation: nativeWire.descriptor.incarnation };
  f.request.mockImplementation(async (method, path) => {
    const url = new URL(path, 'http://fixture');
    if (url.pathname.endsWith('/view')) {
      const result = await request?.(method, path);
      if (!result || typeof result !== 'object') throw Error('Missing view response');
      return { ...result, local: nativeWire.descriptor };
    }
    if (url.pathname.endsWith('/local')) return {
      ...(url.searchParams.get('lane') === 'active' ? nativeWire.active : nativeWire.history), context: f.context(),
      rows: [{ ...nativeWire.active.rows[0], local_ref, session_id: 'A', display: nativeWire.active.rows[0].display }],
    };
    return request?.(method, path);
  });
  await f.observe();
  for (let i = 0; i < 8; i++) await act(async () => { await f.store.advance(); });
  expect(f.store.getSnapshot().local.observations).toHaveLength(1);
  vi.mocked(api.swarmCancel).mockReturnValue(new Promise(() => {}));
  render(<f.Provider><SwarmPane /></f.Provider>);
  await expand('Command');
  const cancel = screen.getByRole('button', { name: 'Request native stop' });
  expect(cancel).toBeEnabled(); fireEvent.click(cancel);
  expect(api.swarmCancel).toHaveBeenCalledWith({ version: 1, source: 'local', repo: '/A', session_id: 'A',
    local_incarnation: local_ref.incarnation, job_ref: { job_id: 'local-test', state_id: null } });
  expect(api.requestCancellation).not.toHaveBeenCalled();
  expect(api.swarmLive).not.toHaveBeenCalled();
});

it.each(['legacy reference', 'cross project', 'foreign session', 'mismatched selected reference'])(
  'disables cancellation when the row cannot prove its identity: %s', async mode => {
    const row = controlRow('Inspect A', mode === 'cross project' ? 'cli' : 'harness');
    if (mode === 'legacy reference') row.selection = { ...row.selection, job_ref: { job_id: base.id, state_id: `state_${serial}` } };
    if (mode === 'foreign session') row.ownership = { ...row.ownership, session_id: 'foreign' };
    const f = await expertMetadataFixture([row]); metadata = f;
    if (mode === 'cross project') {
      const request = f.request.getMockImplementation();
      f.request.mockImplementation(async (method, path) => {
        const result = await request?.(method, path);
        if (!result || typeof result !== 'object') throw Error('Missing metadata response');
        if (path.includes('/view')) return { ...result, sources: [{ source: row.selection.source, state_id: row.selection.job_ref.state_id, cross_project: true, available: true }] };
        return result;
      });
      await f.observe();
    }
    if (mode === 'mismatched selected reference') f.selected.mockImplementation(async () => ({
      ...expertDetail(row.selection, f.context()), selection: { ...row.selection, job_ref: { ...row.selection.job_ref, job_id: 'different' } },
    }));
    render(<f.Provider><SwarmPane /></f.Provider>);
    const cancel = await inspectControl('Inspect A');
    expect(cancel).toHaveAttribute('aria-disabled', 'true');
    fireEvent.click(cancel);
    expect(api.requestCancellation).not.toHaveBeenCalled();
    if (mode === 'cross project' || mode === 'foreign session') {
      expect(f.selected).not.toHaveBeenCalled();
      expect(f.request.mock.calls.filter(([, path]) => new URL(path, 'http://fixture').pathname.endsWith('/detail'))).toHaveLength(0);
    } else {
      expect(f.selected).toHaveBeenCalled();
    }
    expect(api.swarmLive).not.toHaveBeenCalled();
  });

it('persists expansion separately for each store', async () => {
  const fixture = await controlsFixture();
  const first = render(<fixture.Provider><SwarmPane /></fixture.Provider>); await expand('Inspect A');
  expect(screen.getByRole('button', { name: /Inspect CLI/ })).toHaveAttribute('aria-expanded', 'false');
  first.unmount(); clearSWRCache();
  await fixture.observe();
  render(<fixture.Provider><SwarmPane /></fixture.Provider>);
  await waitFor(() => expect(screen.getByRole('button', { name: /Inspect A/ })).toHaveAttribute('aria-expanded', 'true'));
  expect(screen.getByRole('button', { name: /Inspect CLI/ })).toHaveAttribute('aria-expanded', 'false');
  expect(api.swarmLive).not.toHaveBeenCalled();
});

it('retains the original request after ambiguous transport failure on the selected row', async () => {
  const fixture = await controlsFixture();
  vi.mocked(api.requestCancellation).mockRejectedValue(new Error('Connection lost'));
  render(<fixture.Provider><SwarmPane /></fixture.Provider>);
  await expand('Inspect CLI');
  const cli = screen.getByRole('button', { name: /Inspect CLI/ }).parentElement!;
  fireEvent.click(within(cli).getByRole('button', { name: 'Inspect tasks and artifacts' }));
  const cancel = within(cli).getByRole('button', { name: 'Stop selected workers' });
  await waitFor(() => expect(cancel).toHaveAttribute('aria-disabled', 'false'));
  fireEvent.click(cancel);
  const message = await screen.findByText(/Stop unconfirmed/);
  expect(cli).toContainElement(message);
  const original = vi.mocked(api.requestCancellation).mock.calls[0][0];
  expect(original.selection).toEqual({ ...selectedIdentity('cli'), version: 2,
    bindings: expertDetail(selectedIdentity('cli'), fixture.context()).tasks.rows.map(task => task.binding) });
  fireEvent.click(cancel);
  await waitFor(() => expect(api.requestCancellation).toHaveBeenCalledTimes(2));
  expect(vi.mocked(api.requestCancellation).mock.calls[1][0]).toEqual(original);
  expect(api.swarmLive).not.toHaveBeenCalled();
});
