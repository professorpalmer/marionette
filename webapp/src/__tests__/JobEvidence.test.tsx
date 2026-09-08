import { evidenceFixture, evidenceHash, evidenceSelection } from './frontend52-evidence.fixtures';
import { expertDetail } from './metadataExpert.fixtures';
import { parseMetadataDetail } from '../lib/jobMetadata';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import SwarmPane from '../components/SwarmPane';
import { navigationFixture } from './navigationProducer.fixtures';
import JobEvidence from '../components/JobEvidence';
import { fetchJobEvidence, type JobEvidenceData, type ConsumptionMetric } from '../lib/jobEvidence';

vi.mock('../lib/jobEvidence', () => ({ fetchJobEvidence: vi.fn() }));
const unknownMetric = {
  total: null, known_subtotal: null, status: 'unknown', known_attempts: 0,
  unknown_attempts: 0, estimated_attempts: 0, conflicting_attempts: 0,
} satisfies ConsumptionMetric;
const emptyConsumption = {
  tokens_in: unknownMetric, tokens_out: unknownMetric, cache_read_tokens: unknownMetric,
  cache_write_tokens: unknownMetric, api_cost_usd: unknownMetric,
  plan_marginal_cost_usd: unknownMetric, api_equivalent_cost_usd: unknownMetric,
};
const fixture: JobEvidenceData = {
  job_ref: { job_id: 'job-a', state_id: 'state-a' }, source: 'harness', status: 'completed',
  links: { request: null, turn: null, action: null, attempts: 'unavailable' },
  attempts: [], attempt_coverage: 'unverified', totals: { tasks: 1, artifacts: 1, attempts: 0 },
  tasks: [{ id: 'task-a', status: 'completed', attempt_count: 2 }],
  artifacts: [{ id: 'art-a', task_id: 'task-a', type: 'gate', presence: 'recorded', check_result: 'failed' }],
  truncated: false, cost: { selected_usd: null, total_attempt_usd: null, recorded_attempts: emptyConsumption, source: 'unavailable' },
  missing: ['No durable request link.'], provenance: 'Public store records.',
};
beforeEach(() => vi.clearAllMocks());
let navigation: Awaited<ReturnType<typeof navigationFixture>> | undefined;
afterEach(() => { navigation?.dispose(); navigation = undefined; });

it('loads lazily and shows recorded failure and unknown cost despite completed status', async () => {
  vi.mocked(fetchJobEvidence).mockResolvedValue(fixture);
  render(<JobEvidence stateId="state-a" jobId="job-a" sessionId="session-a" repo="/repo" source="harness" />);
  expect(fetchJobEvidence).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Evidence' }));
  expect(await screen.findByText('Recorded checks failed: 1')).toBeInTheDocument();
  expect(screen.getByText('Selected delivery cost: unknown')).toBeInTheDocument();
  expect(screen.getByText('No durable request link.')).toBeInTheDocument();
});

it('discards a response after changing the selected session', async () => {
  let resolve: (value: JobEvidenceData) => void = () => undefined;
  vi.mocked(fetchJobEvidence).mockReturnValue(new Promise(r => { resolve = r; }));
  const view = render(<JobEvidence stateId="state-a" jobId="job-a" sessionId="session-a" repo="/repo" source="harness" />);
  fireEvent.click(screen.getByRole('button', { name: 'Evidence' }));
  view.rerender(<JobEvidence stateId="state-a" jobId="job-a" sessionId="session-b" repo="/repo" source="harness" />);
  resolve(fixture);
  await waitFor(() => expect(screen.queryByText('state-a')).not.toBeInTheDocument());
});

it('keeps unavailable reads explicit', async () => {
  vi.mocked(fetchJobEvidence).mockRejectedValue(new Error('private server text'));
  render(<JobEvidence stateId="state-a" jobId="job-a" sessionId="session-a" repo="/repo" source="harness" />);
  fireEvent.click(screen.getByRole('button', { name: 'Evidence' }));
  expect(await screen.findByText(/Evidence could not be read/)).toBeInTheDocument();
  expect(screen.queryByText('private server text')).not.toBeInTheDocument();
});


it("opens bounded inspection with verification identity and hash without a check verdict", async () => {
  const fixture = await evidenceFixture('Evidence entry');
  const detail = expertDetail(evidenceSelection, fixture.context());
  detail.lifecycle = 'complete';
  detail.artifacts.rows[0] = { ...detail.artifacts.rows[0], id: 'verification-evidence', type: 'verification', sha256: evidenceHash };
  fixture.selected.mockResolvedValue(detail);
  try {
    localStorage.clear(); sessionStorage.clear();
    const view = render(<fixture.Provider><SwarmPane /></fixture.Provider>);
    fireEvent.click(await screen.findByRole('button', { name: 'Evidence entry · complete' }));
    expect(fixture.selected).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' }));
    await screen.findByRole('region', { name: 'Selected job inspector' });
    expect(fixture.selected).toHaveBeenCalledWith(evidenceSelection, expect.objectContaining({ task_cursor: null, artifact_cursor: null }));
    fireEvent.click(screen.getByRole('button', { name: 'Artifacts', exact: true }));
    fireEvent.click(screen.getByText('verification / verification-evidence: unknown'));
    expect(screen.getByText(evidenceHash)).toBeVisible();
    expect(screen.getByText('task-1')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Checks', exact: true }));
    expect(screen.getByRole('region', { name: 'Checks' })).toHaveTextContent('Recorded artifacts and completed lifecycle do not establish that checks passed');
    expect(screen.getByRole('region', { name: 'Checks' })).toHaveTextContent('verification / verification-evidence: recorded');
    const cursors = { task_cursor: null, artifact_cursor: null };
    expect(parseMetadataDetail(detail, fixture.context(), evidenceSelection, cursors).artifacts.rows[0].id).toBe('verification-evidence');
    for (const unsupported of [
      { ...detail, artifacts: { ...detail.artifacts, rows: [{ ...detail.artifacts.rows[0], check_result: 'failed' }] } },
      { ...detail, tasks: { ...detail.tasks, rows: [{ ...detail.tasks.rows[0], model: 'gpt-5.3-codex' }] } },
      { ...detail, tasks: { ...detail.tasks, rows: [{ ...detail.tasks.rows[0], tokens: 120000, est_cost_usd: 0.14 }] } },
      { ...detail, display: { kind: 'available', goal_preview: 'Evidence entry', goal_preview_truncated: false, delivery: 'unverified', quality: 'degraded' } },
    ]) expect(() => parseMetadataDetail(unsupported, fixture.context(), evidenceSelection, cursors)).toThrow('invalid_metadata');
    expect(fetchJobEvidence).not.toHaveBeenCalled();
    view.unmount();
  } finally { fixture.dispose(); }
});


it('shows actual attempt references, distinct spend states and incomplete coverage', async () => {
  vi.mocked(fetchJobEvidence).mockResolvedValue({ ...fixture,
    attempts: [{ attempt_id: 'attempt-a', run_id: 'run-a', task_id: 'task-a',
      adapter: 'local', provider: 'provider-a', model: 'model-a', outcome: 'unavailable',
      consumption: { ...emptyConsumption, plan_marginal_cost_usd: { ...unknownMetric, total: 0, known_subtotal: 0, status: 'measured', known_attempts: 1 } } }],
    totals: { tasks: 1, artifacts: 1, attempts: 101 }, truncated: true,
    cost: { ...fixture.cost, recorded_attempts: { ...emptyConsumption,
      api_cost_usd: { ...unknownMetric, known_subtotal: 2, status: 'partial', known_attempts: 1, estimated_attempts: 1, unknown_attempts: 100 },
      plan_marginal_cost_usd: { ...unknownMetric, known_subtotal: 0, status: 'partial', known_attempts: 1, unknown_attempts: 100 } } },
  });
  render(<JobEvidence stateId="state-a" jobId="job-a" sessionId="session-a" repo="/repo" source="harness" />);
  fireEvent.click(screen.getByRole('button', { name: 'Evidence' }));
  expect(await screen.findByRole('region', { name: 'Attempts' })).toHaveTextContent('attempt-a');
  expect(screen.getByRole('region', { name: 'Attempts' })).toHaveTextContent('provider-a / model-a');
  expect(screen.getByText(/1 of 101 recorded attempts/)).toBeInTheDocument();
  expect(screen.getByText(/Recorded plan marginal subtotal: \$0.000000/)).toBeInTheDocument();
  expect(screen.getByText(/Recorded API subtotal: \$2.000000/)).toBeInTheDocument();
  expect(screen.getByText('1 known attempt; 100 unknown; 1 estimated; 0 conflicting.')).toBeInTheDocument();
  expect(screen.getByText('All-attempt spend: unknown; invocation coverage is unverified.')).toBeInTheDocument();
});

it('keeps empty consumption unknown and never presents an empty sum as zero', async () => {
  vi.mocked(fetchJobEvidence).mockResolvedValue(fixture);
  render(<JobEvidence stateId="state-a" jobId="job-a" sessionId="session-a" repo="/repo" source="harness" />);
  fireEvent.click(screen.getByRole('button', { name: 'Evidence' }));
  expect(await screen.findByText('Recorded API subtotal: unknown.')).toBeInTheDocument();
  expect(screen.getByText('Recorded plan marginal subtotal: unknown.')).toBeInTheDocument();
  expect(screen.queryByText(/\$0.000000/)).not.toBeInTheDocument();
});

it('shows conflicting API coverage separately from measured plan zero and equivalent estimates', async () => {
  const consumption = {
    ...emptyConsumption,
    api_cost_usd: { ...unknownMetric, unknown_attempts: 1, conflicting_attempts: 1 },
    plan_marginal_cost_usd: { ...unknownMetric, total: 0, known_subtotal: 0, status: 'measured', known_attempts: 1 },
    api_equivalent_cost_usd: { ...unknownMetric, total: 9, known_subtotal: 9, status: 'estimated', known_attempts: 1, estimated_attempts: 1 },
  } satisfies JobEvidenceData['cost']['recorded_attempts'];
  vi.mocked(fetchJobEvidence).mockResolvedValue({ ...fixture,
    attempts: [{ attempt_id: 'attempt-a', run_id: 'run-a', task_id: 'task-a', adapter: 'local',
      provider: 'provider', model: 'model', outcome: 'unavailable', consumption }],
    totals: { ...fixture.totals, attempts: 1 },
    cost: { ...fixture.cost, selected_usd: 3, recorded_attempts: consumption },
  });
  render(<JobEvidence stateId="state-a" jobId="job-a" sessionId="session-a" repo="/repo" source="harness" />);
  fireEvent.click(screen.getByRole('button', { name: 'Evidence' }));
  expect(await screen.findByText('Selected delivery cost: $3.000000')).toBeInTheDocument();
  expect(screen.getByText('Recorded API subtotal: unknown.')).toBeInTheDocument();
  expect(screen.getByText('0 known attempts; 1 unknown; 0 estimated; 1 conflicting.')).toBeInTheDocument();
  expect(screen.getByText('Recorded plan marginal subtotal: $0.000000 (measured).')).toBeInTheDocument();
  expect(screen.getByText('API-equivalent estimate')).toBeInTheDocument();
  expect(screen.getByText('$9.000000; not spend')).toBeInTheDocument();
  expect(screen.getByText('Outcome unavailable.')).toBeInTheDocument();
});
