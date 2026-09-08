import { act, fireEvent, render, screen, waitFor, cleanup } from "@testing-library/react";

import { beforeEach, expect, it, vi, afterEach } from "vitest";

import SwarmPane from "../components/SwarmPane";

import { api } from "../lib/api";

import { dispatchProjectSelected } from "../lib/panelTransition";

import { clearSWRCache } from "../lib/useStaleWhileRevalidate";

import { expertDetail, expertMetadataFixture, expertSummary } from "./metadataExpert.fixtures";

import { list } from "./jobMetadata.fixtures";

import type { MetadataDetail, MetadataSelection } from "../lib/jobMetadata";

vi.mock('../lib/api', async importOriginal => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return { ...actual, api: { ...actual.api, swarmLive: vi.fn(), sessions: vi.fn() } };
});

beforeEach(() => {
  vi.resetAllMocks(); localStorage.clear(); sessionStorage.clear(); clearSWRCache();
  dispatchProjectSelected('/A');
  vi.mocked(api.sessions).mockResolvedValue([{ id: 'A', active: true, title: 'A' }]);
});

const selection: MetadataSelection = { repo: '/A', session_id: 'A', source: 'harness',
  job_ref: { job_id: 'job_same', state_id: 'state_a' } };

let metadata: Awaited<ReturnType<typeof expertMetadataFixture>> | undefined;

afterEach(() => { cleanup(); metadata?.dispose(); metadata = undefined; vi.unstubAllGlobals(); });

async function setup() {
  const fixture = await expertMetadataFixture([expertSummary(selection, 'Inspect A')], { browser: true });
  metadata = fixture;
  render(<fixture.Provider><SwarmPane /></fixture.Provider>);
  return fixture;
}

async function inspect(goal: string) {
  fireEvent.click(await screen.findByRole('button', { name: new RegExp(goal) }));
  fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' }));
}

it('shows exhausted reads and retries even when the successful row is otherwise identical', async () => {
  const f = await setup();
  const result = expertDetail(selection, f.context());
  result.artifacts.rows = [];
  f.selected.mockResolvedValue({ ...result, artifacts: { ...result.artifacts,
    page: { ...result.artifacts.page, outcome: 'unavailable', checkpoint: 0, scanned: 0 } } });
  const rows = f.store.getSnapshot().observations.map(o => o.row);
  await inspect('Inspect A');
  expect(await screen.findByRole('alert')).toHaveTextContent('Selected read unavailable');
  expect(screen.queryByText(/^No artifacts recorded\.?$/)).not.toBeInTheDocument();
  const retry = screen.getByRole('button', { name: 'Retry', exact: true });
  await waitFor(() => expect(retry).toBeEnabled());
  f.selected.mockResolvedValue(result);
  fireEvent.click(retry);
  await screen.findByText('No artifacts recorded');
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(f.store.getSnapshot().observations.map(o => o.row)).toEqual(rows);
  expect(f.selected).toHaveBeenCalledTimes(2);
  expect(api.swarmLive).not.toHaveBeenCalled();
});

it('shows transport failure instead of an empty tracker and retries', async () => {
  const f = await setup();
  const request = f.request.getMockImplementation();
  if (!request) throw Error('Missing wire fixture');
  f.request.mockRejectedValue(new Error('offline'));
  await act(async () => { f.store.setTarget(f.context()); await f.store.readView(); });
  expect(await screen.findByRole('alert')).toHaveTextContent('Job updates unavailable');
  expect(screen.queryByText(/^No jobs observed in this view/)).not.toBeInTheDocument();
  expect(screen.getByText(/an empty view does not establish no work/)).toBeInTheDocument();
  f.request.mockImplementation(async (method, path) => {
    const result = await request(method, path);
    if (new URL(path, 'http://fixture').pathname === '/api/jobs/metadata') {
      const url = new URL(path, 'http://fixture');
      return { ...list([]), context: f.context(), store: { source: url.searchParams.get('source'), state_id: url.searchParams.get('state_id') }, mode: url.searchParams.get('mode') };
    }
    return result;
  });
  fireEvent.click(screen.getByRole('button', { name: 'Retry updates' }));
  await screen.findByText(/^No jobs observed in this view/);
  fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
  await waitFor(() => expect(f.store.getSnapshot().working).toBe(false));
  expect(f.store.getSnapshot().streams.some(s => s.state === 'complete')).toBe(true);
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(api.swarmLive).not.toHaveBeenCalled();
});

it('does not clear a new scope error when an old retry succeeds', async () => {
  const f = await setup();
  f.selected.mockRejectedValueOnce(new Error('offline'));
  await inspect('Inspect A');
  await screen.findByText('Selected read unavailable. Retry inspection.');
  let release: (value: MetadataDetail) => void = () => {};
  const oldResult = expertDetail(selection, f.context());
  f.selected.mockImplementationOnce(() => new Promise(resolve => { release = resolve; }));
  fireEvent.click(screen.getByRole('button', { name: 'Retry', exact: true }));
  await waitFor(() => expect(f.selected).toHaveBeenCalledTimes(2));
  act(() => window.dispatchEvent(new CustomEvent('harness-session-changed', { detail: { sessionId: 'B' } })));
  // The shared owner serializes reads; let the abandoned transport hit its bounded timeout.
  await waitFor(() => expect(f.store.getSnapshot().working).toBe(false), { timeout: 2000 });
  const next = { ...selection, session_id: 'B' };
  await f.replace([expertSummary(next, 'Inspect B')]);
  f.selected.mockRejectedValueOnce(new Error('new scope offline'));
  await inspect('Inspect B');
  await screen.findByText('Selected read unavailable. Retry inspection.');
  const current = f.store.getSnapshot();
  await act(async () => { release(oldResult); });
  expect(screen.getByText('Selected read unavailable. Retry inspection.')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: /Inspect B/ })).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /Inspect A/ })).not.toBeInTheDocument();
  expect(f.store.getSnapshot()).toBe(current);
  expect(api.swarmLive).not.toHaveBeenCalled();
});

it("retries unavailable metadata summary and restores identical retained rows", async () => {
  const f = await setup();
  const rows = f.store.getSnapshot().observations.map(o => o.row);
  const original = f.request.getMockImplementation();
  if (!original) throw Error('Missing wire responder');
  f.request.mockImplementation(async (method, path) => {
    const result = await original(method, path);
    if (new URL(path, 'http://fixture').pathname !== '/api/jobs/metadata') return result;
    if (!result || typeof result !== 'object') throw Error('Missing metadata list');
    return { ...result, rows: [], page: { outcome: 'unavailable', revision: 10000, checkpoint: Number(new URL(path, 'http://fixture').searchParams.get('after_revision') ?? 0), scanned: 0, next_cursor: null } };
  });
  for (let i = 0; i < 12 && !f.store.getSnapshot().observations.some(o => o.freshness === 'stale'); i++) {
    await act(async () => { await f.store.advance(); });
  }
  expect(screen.getByText('Retained observation is stale; current lifecycle is unconfirmed.')).toBeVisible();
  expect(f.store.getSnapshot().streams.some(stream => stream.state === 'unavailable')).toBe(true);
  f.request.mockImplementation(original);
  fireEvent.click(screen.getByRole('button', { name: 'Retry updates' }));
  await waitFor(() => expect(f.store.getSnapshot().working).toBe(false));
  fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
  await waitFor(() => expect(screen.queryByText('Retained observation is stale; current lifecycle is unconfirmed.')).not.toBeInTheDocument());
  expect(screen.getByRole('button', { name: /^Inspect A · running/ })).toBeVisible();
  expect(f.store.getSnapshot().observations.map(o => o.row)).toEqual(rows);
  expect(f.store.getSnapshot().streams.some(stream => stream.state === 'unavailable')).toBe(false);
  expect(api.swarmLive).not.toHaveBeenCalled();
});
