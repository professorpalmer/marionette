import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { api } from '../lib/api';
import { fetchJobArtifacts } from '../lib/jobArtifacts';
import LeftRail from '../components/LeftRail';
import { expertDetail, expertMetadataFixture, expertSummary } from './metadataExpert.fixtures';
import type { MetadataDetail, MetadataSelection } from '../lib/jobMetadata';
import { clearSWRCache } from '../lib/useStaleWhileRevalidate';

vi.mock('../lib/api', () => ({ api: {
  getWorkspace: vi.fn().mockResolvedValue({ repo: '/workspace', branch: 'main', is_git: true,
    head_unborn: false, codegraph_status: 'ready', recents: [], home: '/home' }),
  workspaces: vi.fn().mockResolvedValue([{ name: 'main', active: true, dirty: false }]),
  sessions: vi.fn().mockResolvedValue([{ id: 'session-1', title: 'Current', active: true, repo: '/workspace' }]),
  jobs: vi.fn(),
} }));
vi.mock('../lib/jobArtifacts', async importOriginal => ({
  ...await importOriginal<typeof import('../lib/jobArtifacts')>(), fetchJobArtifacts: vi.fn(),
}));
vi.mock('../lib/usePolling', () => ({ usePolling: vi.fn() }));
vi.mock('../lib/useOperationalDiagnostic', () => ({ useOperationalDiagnostic: () => null }));

const selection: MetadataSelection = { job_ref: { job_id: 'job_one', state_id: 'state_one' },
  source: 'harness', repo: '/workspace', session_id: 'session-1' };
let metadata: Awaited<ReturnType<typeof expertMetadataFixture>>;
beforeEach(async () => {
  localStorage.clear();
  clearSWRCache();
  vi.mocked(fetchJobArtifacts).mockReset();
  metadata = await expertMetadataFixture([{ ...expertSummary(selection, 'Artifact test job'), lifecycle: 'complete' }], { browser: true });
});
afterEach(() => { cleanup(); metadata.dispose(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });
function artifacts(type?: string): MetadataDetail {
  const result = expertDetail(selection, metadata.context());
  result.artifacts = { ...result.artifacts, rows: type ? [{ ...result.artifacts.rows[0], type }] : [] };
  return result;
}
async function openCard() {
  render(<metadata.Provider><LeftRail jobsRefresh={0} /></metadata.Provider>);
  await waitFor(() => expect(metadata.store.getSnapshot().view.kind).toBe('target'));
  await metadata.observe();
  fireEvent.click(await screen.findByRole('button', { name: /Artifact test job/ }));
}
it('shows a recorded summaryless gate rather than empty', async () => {
  metadata.selected.mockResolvedValue(artifacts('gate'));
  await openCard();
  expect(await screen.findByText('1 artifact recorded without a summary')).toBeTruthy();
  expect(screen.queryByText('No artifacts recorded')).toBeNull();
  expect(metadata.selected).toHaveBeenCalledWith(selection, expect.objectContaining({ task_cursor: null, artifact_cursor: null }));
  expect(fetchJobArtifacts).not.toHaveBeenCalled();
  expect(api.jobs).not.toHaveBeenCalled();
});
it('shows failure with Retry, then genuine loaded empty', async () => {
  metadata.selected.mockRejectedValueOnce(new Error('network failed')).mockResolvedValueOnce(artifacts());
  await openCard();
  const retry = await screen.findByRole('button', { name: 'Retry', exact: true });
  expect(screen.queryByText('No artifacts recorded')).toBeNull();
  fireEvent.click(retry);
  expect(await screen.findByText('No artifacts recorded')).toBeTruthy();
});
it.each(['harness-project-selected', 'harness-session-changed'])('fences late results after %s', async event => {
  let finish: (value: MetadataDetail) => void = () => {};
  metadata.selected.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  await openCard();
  expect(screen.getByText('Loading artifacts...')).toBeTruthy();
  await act(async () => {
    window.dispatchEvent(new CustomEvent(event, { detail: '/other' }));
    finish({ ...artifacts('finding'), display: { kind: 'available', goal_preview: 'Old scope content', goal_preview_truncated: false, delivery: 'unverified', quality: 'unverified' } });
  });
  expect(screen.queryByText('Old scope content')).toBeNull();
});
it('renders an HTTP 503 through the real transport and retries the bound read', async () => {
  metadata.selected.mockRejectedValueOnce(new Error('Store unavailable')).mockResolvedValueOnce(artifacts('gate'));
  await openCard();
  fireEvent.click(await screen.findByRole('button', { name: 'Retry', exact: true }));
  expect(await screen.findByText('1 artifact recorded without a summary')).toBeTruthy();
  // The shared metadata owner already performed one handshake before the card opened.
  expect(metadata.browserFetch.mock.calls.filter(([path]) => String(path).endsWith('/api/endpoint'))).toHaveLength(1);
  const details = metadata.browserFetch.mock.calls.filter(([path]) => String(path).includes('/api/jobs/metadata/detail?'));
  expect(details).toHaveLength(2);
  expect(String(details[0][0])).toContain('state_id=state_one');
  expect(String(details[1][0])).toContain('state_id=state_one');
});
