import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import CompactSwarmDashboard from '../components/CompactSwarmDashboard';
import type { ExpertMetadata, ExpertTask } from '../lib/expertMetadata';
import { producerTask } from './nativeExpert.fixtures';
import type { LocalRoute } from '../lib/localJobMetadata';

afterEach(cleanup);

const task = (index: number): ExpertTask => ({
  id: `task-${index}`,
  role: `Worker ${index}`,
  instruction: `Instruction for worker ${index}`,
  instruction_truncated: false,
  adapter: 'codex',
  model: `model-${index}`,
  created_at: null,
  updated_at: null,
  usage: { tokens_in: 10, tokens_out: 5, est_cost_usd: null, estimated: null, cost_provenance: null },
});

const expert = {
  kind: 'available',
  reason: null,
  header: null,
  tasks: [1, 2, 3, 4, 5].map(task),
  artifacts: [1, 2, 3, 4, 5].map(index => ({
    id: `artifact-${index}`, task_id: `task-${index}`, type: 'FINDING', created_by: 'worker', created_at: `2026-09-10T0${index}:00:00Z`,
    headline: `Evidence ${index}`, detail: null, result: 'recorded', failure: null, confidence: null,
    model: null, adapter: null, policy: null, provider: null, role: null, est_cost_usd: null, rejected: [], check_result: 'unavailable' as const,
  })),
  coverage: { tasks: 'complete', artifacts: 'complete' },
  quality: 'unverified',
} satisfies ExpertMetadata;

it('keeps five assigned workers visible, then selects one without exposing instructions by default', () => {
  render(<CompactSwarmDashboard title="Five-worker routing" lifecycle="running" expert={expert}
    workerStatuses={new Map([['task-1', 'running'], ['task-2', 'queued'], ['task-3', 'complete'], ['task-4', 'failed'], ['task-5', 'unknown']])}
    headerModel="header-model" artifactCount={5} usage={75} />);
  const roster = screen.getByRole('group', { name: 'Workers' });
  for (const index of [1, 2, 3, 4, 5]) {
    expect(within(roster).getByText(`Worker ${index}`)).toBeVisible();
    expect(within(roster).getByTitle(`Model: model-${index}`)).toBeVisible();
  }
  expect(within(roster).getByText('running')).toBeVisible();
  expect(within(roster).getByText('queued')).toBeVisible();
  expect(within(roster).getByText('complete')).toBeVisible();
  expect(within(roster).getByText('failed')).toBeVisible();
  expect(within(roster).getByText('unknown')).toBeVisible();
  expect(screen.getByText(/0\/5 routes recorded/)).toBeVisible();
  expect(within(roster).getAllByText('assigned')).toHaveLength(5);
  expect(screen.queryByText('Instruction for worker 1')).toBeNull();

  const selected = within(roster).getByRole('button', { name: /Worker 4/ });
  selected.focus();
  expect(selected).toHaveFocus();
  fireEvent.click(selected);
  const inspector = screen.getByRole('region', { name: 'Selected worker' });
  expect(within(inspector).getByText('Worker 4')).toBeVisible();
  expect(within(inspector).getByText('Evidence 4 · unavailable')).toBeVisible();
  expect(within(inspector).queryByText('Instruction for worker 4')).toBeNull();
  fireEvent.click(within(inspector).getByText('Instruction'));
  expect(within(inspector).getByText('Instruction for worker 4')).toBeVisible();
});

it('shows the recorded route instead of an earlier task assignment', () => {
  const routed: ExpertMetadata = { ...expert, tasks: [task(1)], artifacts: [{ ...expert.artifacts[0],
    type: 'routing', model: 'routed-model', created_by: 'router', policy: 'balanced', detail: 'Required capability matched.' }] };
  render(<CompactSwarmDashboard title="Recorded routing" lifecycle="running" expert={routed} workerStatuses={new Map([['task-1', 'running']])} />);
  expect(within(screen.getByRole('group', { name: 'Workers' })).getByTitle('Model: routed-model')).toHaveTextContent('routed');
  expect(screen.getByText(/1\/1 routes recorded/)).toBeVisible();
  expect(screen.queryByTitle('Model: model-1')).toBeNull();
});

it('keeps native forecasts distinct from an existing assignment', () => {
  const native = { ...producerTask(), model: 'assigned-model', model_kind: 'assigned' as const };
  const route: LocalRoute = { ordinal: 1, task_id: native.task_id, association: 'explicit', model: 'forecast-model',
    model_kind: 'forecast', role: native.role, policy: 'cheap', adapter: 'codex', created_by: 'router',
    detail: 'Projected choice', est_cost_usd: null, truncated: false };
  render(<CompactSwarmDashboard title="Native routing" lifecycle="running" nativeTasks={[native]}
    nativeRoutes={new Map([[native.task_id!, route]])} routeCoverage="complete" workerStatuses={new Map()} />);
  expect(within(screen.getByRole('group', { name: 'Workers' })).getByTitle('Model: assigned-model')).toHaveTextContent('assigned');
  expect(screen.queryByTitle('Model: forecast-model')).toBeNull();
  fireEvent.click(screen.getByText('cheap · router · explicit'));
  expect(screen.getByText('Forecast: forecast-model. Projected choice')).toBeVisible();
});

it('never paints Model unavailable for a running native worker still waiting on a route', () => {
  render(<CompactSwarmDashboard title="Native implement" lifecycle="running" nativeTasks={[producerTask()]}
    workerStatuses={new Map()} />);
  const roster = screen.getByRole('group', { name: 'Workers' });
  expect(within(roster).getByText('implement')).toBeVisible();
  expect(within(roster).queryByText(/implement \(agentic\)/)).toBeNull();
  expect(within(roster).getByText('routing…')).toBeVisible();
  expect(screen.queryByText(/unavailable/i)).toBeNull();
  expect(screen.getByText('routing… · routing in progress')).toBeVisible();
});

it('shows a native forecast model instead of hiding the worker behind unavailable', () => {
  const native = producerTask();
  const route: LocalRoute = { ordinal: 1, task_id: native.task_id, association: 'explicit', model: 'gpt-5.6-luna',
    model_kind: 'forecast', role: 'implement', policy: 'balanced', adapter: 'agentic', created_by: 'router',
    detail: 'Projected choice', est_cost_usd: null, truncated: false };
  render(<CompactSwarmDashboard title="Native forecast" lifecycle="running" nativeTasks={[native]}
    nativeRoutes={new Map([[native.task_id!, route]])} routeCoverage="partial" workerStatuses={new Map()} />);
  expect(within(screen.getByRole('group', { name: 'Workers' })).getByTitle('Model: gpt-5.6-luna')).toHaveTextContent('forecast');
  expect(screen.queryByText(/unavailable/i)).toBeNull();
});

it('uses routing… for an active expert worker without a recorded model', () => {
  const pending: ExpertMetadata = { ...expert, tasks: [{ ...task(1), model: '', adapter: 'codex' }], artifacts: [] };
  render(<CompactSwarmDashboard title="Pending route" lifecycle="running" expert={pending}
    workerStatuses={new Map([['task-1', 'running']])} />);
  expect(screen.getByText('routing…')).toBeVisible();
  expect(screen.queryByText(/unavailable/i)).toBeNull();
});

it('labels partial worker and token coverage without inventing a complete total', () => {
  const partial: ExpertMetadata = { ...expert, tasks: [{ ...task(1), usage: { ...task(1).usage, tokens_out: null } }],
    artifacts: [], coverage: { tasks: 'partial', artifacts: 'partial' } };
  render(<CompactSwarmDashboard title="Partial data" lifecycle="running" expert={partial} workerCount={5}
    workerStatuses={new Map([['task-1', 'complete']])} workerCoverage="partial" />);
  expect(screen.getByText('1 workers loaded · partial coverage')).toBeVisible();
  expect(screen.getByText('1/5')).toBeVisible();
  expect(screen.getByText('10+')).toBeVisible();
  expect(screen.getByText(/0\/1 routes recorded/)).toBeVisible();
});

it('shows the worker verdict as latest evidence when a worker saves its artifacts in one second', () => {
  // A worker saves its run record, findings and verdict with one created_at.
  // The tie kept the first (the run record), not the verdict saved last.
  const at = '2026-10-03T20:19:12Z';
  const artifact = (id: string, type: string, headline: string, detail: string | null = null) => ({
    id, task_id: 'task-1', type, created_by: 'worker', created_at: at, headline, detail, result: 'passed', failure: null,
    confidence: null, model: null, adapter: null, policy: null, provider: null, role: null, est_cost_usd: null,
    rejected: [], check_result: 'passed' as const,
  });
  const saved = { ...expert, tasks: [task(1)], artifacts: [
    artifact('a-run', 'VERIFICATION', 'Worker run: 6 turns, 14 tool calls'),
    artifact('a-finding', 'FINDING', 'Split rm flags bypass the classifier'),
    artifact('a-verdict', 'VERIFICATION', 'Worker verdict PASS', 'Covered the assigned scope.'),
  ] } satisfies ExpertMetadata;
  render(<CompactSwarmDashboard title="One worker" lifecycle="complete" expert={saved}
    workerStatuses={new Map([['task-1', 'complete']])} headerModel="m" artifactCount={3} usage={10} />);
  fireEvent.click(within(screen.getByRole('group', { name: 'Workers' })).getByRole('button', { name: /Worker 1/ }));
  const inspector = screen.getByRole('region', { name: 'Selected worker' });
  expect(within(inspector).getByText('Worker verdict PASS · passed')).toBeVisible();
});
