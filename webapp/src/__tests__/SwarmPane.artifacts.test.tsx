import { evidenceFixture, evidenceHash, evidenceSelection } from './frontend52-evidence.fixtures';
import { within } from '@testing-library/react';
import { act, fireEvent, render, screen, waitFor, cleanup } from "@testing-library/react";

import { beforeEach, expect, it, vi, afterEach } from "vitest";

import SwarmPane from "../components/SwarmPane";

import { api, type Job } from "../lib/api";

import { fetchJobArtifacts } from "../lib/jobArtifacts";

import { dispatchProjectSelected } from "../lib/panelTransition";

import { clearSWRCache } from "../lib/useStaleWhileRevalidate";

import { MetadataInspection } from "../components/MetadataJobs";

import { expertDetail, expertMetadataFixture, expertSummary } from "./metadataExpert.fixtures";

import type { MetadataDetail, MetadataSelection } from "../lib/jobMetadata";

import { metadataSelectionKey } from "../lib/jobMetadata";

vi.mock('../lib/jobArtifacts', async importOriginal => ({
  ...await importOriginal<typeof import('../lib/jobArtifacts')>(), fetchJobArtifacts: vi.fn(),
}));

vi.mock('../lib/api', async importOriginal => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return { ...actual, api: { ...actual.api, swarmLive: vi.fn(), sessions: vi.fn(), artifacts: vi.fn() } };
});

const base: Job = { id: 'job_same', job_ref: { job_id: 'job_same', state_id: 'state_a' },
  source: 'harness', session_id: 'A', status: 'running', goal: 'Inspect A', artifacts_complete: false };

function rows(jobs: Job[]) {
  vi.mocked(api.swarmLive).mockResolvedValue({ session: { tokens_used: 0, est_cost_usd: 0 }, jobs });
}

async function expand(name: string) {
  const row = await screen.findByRole('button', { name: new RegExp(name) });
  if (row.getAttribute('aria-expanded') === 'false') fireEvent.click(row);
}

beforeEach(() => {
  vi.resetAllMocks(); localStorage.clear(); sessionStorage.clear(); clearSWRCache();
  dispatchProjectSelected('/A');
  vi.mocked(api.sessions).mockResolvedValue([{ id: 'A', active: true, title: 'A' }]);
  rows([base]);
});

const selection: MetadataSelection = { job_ref: { job_id: base.id, state_id: 'state_a' },
  source: 'harness', repo: '/A', session_id: 'A' };

let metadata: Awaited<ReturnType<typeof expertMetadataFixture>> | undefined;

afterEach(() => { cleanup(); metadata?.dispose(); metadata = undefined; vi.restoreAllMocks(); });

it('retries locally and treats a successful empty response as loaded', async () => {
  metadata = await expertMetadataFixture([expertSummary(selection, 'Inspect A')]);
  const fixture = metadata;
  const empty = expertDetail(selection, fixture.context());
  empty.artifacts = { page: { ...empty.artifacts.page, scanned: 0 }, rows: [] };
  fixture.selected.mockRejectedValueOnce(new Error('store offline')).mockResolvedValueOnce(empty);
  render(<fixture.Provider><SwarmPane /></fixture.Provider>); await expand('Inspect A');
  fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' }));
  const retry = await screen.findByRole('button', { name: 'Retry', exact: true });
  expect(screen.queryByText('No artifacts recorded')).not.toBeInTheDocument();
  fireEvent.click(retry);
  expect(await screen.findByText('No artifacts recorded')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Retry', exact: true })).not.toBeInTheDocument();
  expect(fixture.selected).toHaveBeenCalledTimes(2);
  expect(fixture.selected).toHaveBeenLastCalledWith(selection, expect.objectContaining({ task_cursor: null, artifact_cursor: null }));
  // Reopening a loaded empty inspector must not trigger another read.
  fireEvent.click(screen.getByRole('button', { name: /Inspect A/ }));
  await expand('Inspect A');
  expect(screen.getByText('No artifacts recorded')).toBeInTheDocument();
  expect(fixture.selected).toHaveBeenCalledTimes(2);
  expect(fetchJobArtifacts).not.toHaveBeenCalled();
  expect(api.swarmLive).not.toHaveBeenCalled();
  expect(api.artifacts).not.toHaveBeenCalled();
});

it.each([{ ...base, job_ref: undefined }, { ...base, cross_project: true }, { ...base, session_id: 'foreign' }])(
  'refuses an unselectable artifact preview without an id-only fallback', async job => {
    metadata = await expertMetadataFixture([expertSummary(selection, 'Inspect A')]);
    const fixture = metadata;
    // Exercise the actual expanded-row component with a colliding observed identity.
    // Missing references cannot enter the metadata parser; the UI boundary must also refuse them.
    render(<fixture.Provider><MetadataInspection job={{ ...job, metadata_key: metadataSelectionKey(selection) }} /></fixture.Provider>);
    expect(await screen.findByText(/Artifact preview is unavailable/)).toBeInTheDocument();
    const inspect = screen.getByRole('button', { name: 'Inspect tasks and artifacts' });
    expect(inspect).toBeDisabled();
    fireEvent.click(inspect);
    expect(fixture.selected).not.toHaveBeenCalled();
    expect(fetchJobArtifacts).not.toHaveBeenCalled(); expect(api.artifacts).not.toHaveBeenCalled();
    expect(api.swarmLive).not.toHaveBeenCalled();
  });

it('fences an old response when a colliding job is selected in another session', async () => {
  metadata = await expertMetadataFixture([expertSummary(selection, 'Inspect A')]);
  const fixture = metadata;
  const old = expertDetail(selection, fixture.context());
  old.artifacts.rows[0].id = 'Private A result';
  let release: (value: MetadataDetail) => void = () => {};
  fixture.selected.mockReturnValueOnce(new Promise(resolve => { release = resolve; }));
  render(<fixture.Provider><SwarmPane /></fixture.Provider>); await expand('Inspect A');
  fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' }));
  await waitFor(() => expect(fixture.selected).toHaveBeenCalledTimes(1));
  const next: MetadataSelection = { ...selection, session_id: 'B', job_ref: { job_id: base.id, state_id: 'state_b' } };
  // The shared owner serializes reads; change context before releasing the old wire response.
  act(() => fixture.store.setTarget({ repo: next.repo, session_id: next.session_id, scope: 'all' }));
  await act(async () => { release(old); });
  await waitFor(() => expect(fixture.store.getSnapshot().working).toBe(false));
  expect(fixture.store.getSnapshot().detailCache).toEqual({});
  await fixture.replace([expertSummary(next, 'Inspect B')]);
  await expand('Inspect B');
  fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' }));
  await waitFor(() => expect(fixture.selected).toHaveBeenCalledTimes(2));
  await screen.findByRole('region', { name: 'Selected job inspector' });
  expect(screen.queryByText(/Private A result/)).not.toBeInTheDocument();
  expect(fixture.store.getSnapshot().detailCache[metadataSelectionKey(selection)]).toBeUndefined();
  expect(fixture.selected).toHaveBeenLastCalledWith(next, expect.objectContaining({ task_cursor: null, artifact_cursor: null }));
  expect(fetchJobArtifacts).not.toHaveBeenCalled();
  expect(api.swarmLive).not.toHaveBeenCalled();
  expect(api.artifacts).not.toHaveBeenCalled();
});

it("hydrates exact artifact identities and hashes independently across colliding sources", async () => {
  const cli: MetadataSelection = { ...evidenceSelection, source: 'cli', job_ref: { ...evidenceSelection.job_ref, state_id: 'state-cli' } };
  metadata = await evidenceFixture('Inspect A', [evidenceSelection, cli]);
  const fixture = metadata;
  render(<fixture.Provider><SwarmPane /></fixture.Provider>);
  await expand('^Inspect A · complete$');
  fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' }));
  await screen.findByText('Findings (1)');
  fireEvent.click(screen.getByRole('button', { name: 'Artifacts', exact: true }));
  fireEvent.click(screen.getByText('finding / harness-finding: unknown'));
  expect(screen.getByText(evidenceHash)).toBeVisible();
  expect(screen.queryByText(/cli-finding/)).not.toBeInTheDocument();
  await expand('^CLI evidence job · complete$');
  const cliCard = screen.getByRole('button', { name: 'CLI evidence job · complete' }).closest('[data-job-id]');
  if (!(cliCard instanceof HTMLElement)) throw Error('CLI job card missing');
  fireEvent.click(within(cliCard).getByRole('button', { name: 'Inspect tasks and artifacts' }));
  await waitFor(() => expect(fixture.selected).toHaveBeenCalledTimes(2));
  const inspectors = await screen.findAllByRole('region', { name: 'Selected job inspector' });
  expect(inspectors).toHaveLength(2);
  const cliInspector = inspectors.find(element => within(element).queryByText(/cli-finding/));
  expect(cliInspector).toBeDefined();
  if (!cliInspector) throw Error('CLI detail not rendered');
  fireEvent.click(within(cliInspector).getByRole('button', { name: 'Artifacts', exact: true }));
  fireEvent.click(within(cliInspector).getByText('finding / cli-finding: unknown'));
  expect(within(cliInspector).getByText('c'.repeat(64))).toBeVisible();
  expect(within(cliInspector).queryByText(evidenceHash)).not.toBeInTheDocument();
  expect(screen.getAllByText(evidenceHash)).toHaveLength(1);
  expect(fixture.selected).toHaveBeenNthCalledWith(1, evidenceSelection, expect.objectContaining({ artifact_cursor: null }));
  expect(fixture.selected).toHaveBeenNthCalledWith(2, cli, expect.objectContaining({ artifact_cursor: null }));
  expect(fetchJobArtifacts).not.toHaveBeenCalled();
  expect(api.swarmLive).not.toHaveBeenCalled();
  expect(api.artifacts).not.toHaveBeenCalled();
});
