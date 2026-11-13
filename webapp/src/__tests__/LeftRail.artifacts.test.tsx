import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import LeftRail from '../components/LeftRail';
import { openAgentSwarmJob } from '../lib/agentLinks';
import { expertMetadataFixture, expertSummary } from './metadataExpert.fixtures';
import type { MetadataSelection } from '../lib/jobMetadata';
import { clearSWRCache } from '../lib/useStaleWhileRevalidate';

vi.mock('../lib/api', () => ({
  api: {
    getWorkspace: vi.fn().mockResolvedValue({
      repo: '/workspace',
      branch: 'main',
      is_git: true,
      head_unborn: false,
      codegraph_status: 'ready',
      recents: [],
      home: '/home',
    }),
    workspaces: vi.fn().mockResolvedValue([{ name: 'main', active: true, dirty: false }]),
    sessions: vi.fn().mockResolvedValue([
      { id: 'session-1', title: 'Current', active: true, repo: '/workspace' },
    ]),
    jobs: vi.fn(),
  },
}));
vi.mock('../lib/agentLinks', () => ({ openAgentSwarmJob: vi.fn() }));
vi.mock('../lib/usePolling', () => ({ usePolling: vi.fn() }));
vi.mock('../lib/useOperationalDiagnostic', () => ({ useOperationalDiagnostic: () => null }));

const selection: MetadataSelection = {
  job_ref: { job_id: 'job_one', state_id: 'state_one' },
  source: 'harness',
  repo: '/workspace',
  session_id: 'session-1',
};

let metadata: Awaited<ReturnType<typeof expertMetadataFixture>>;

beforeEach(async () => {
  localStorage.clear();
  clearSWRCache();
  vi.mocked(openAgentSwarmJob).mockReset();
  metadata = await expertMetadataFixture(
    [{ ...expertSummary(selection, 'Artifact test job'), lifecycle: 'complete' }],
    { browser: true },
  );
});

afterEach(() => {
  cleanup();
  metadata.dispose();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it('opens the Jobs rail PM embed on hire click (no inline artifact expand)', async () => {
  render(
    <metadata.Provider>
      <LeftRail jobsRefresh={0} />
    </metadata.Provider>,
  );
  await waitFor(() => expect(metadata.store.getSnapshot().view.kind).toBe('target'));
  await metadata.observe();
  fireEvent.click(await screen.findByRole('button', { name: /Artifact test job/ }));
  // The row carries its view context, so the Jobs pane can place it.
  expect(openAgentSwarmJob).toHaveBeenCalledWith(expect.objectContaining({
    jobId: 'job_one',
    context: expect.objectContaining({ repo: expect.any(String), session_id: expect.any(String) }),
  }));
  expect(screen.queryByText('Loading artifacts...')).toBeNull();
  expect(screen.queryByText('No artifacts recorded')).toBeNull();
  expect(screen.queryByRole('button', { name: 'Retry', exact: true })).toBeNull();
});
