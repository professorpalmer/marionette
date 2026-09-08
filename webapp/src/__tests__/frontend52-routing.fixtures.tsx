import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { onTestFinished } from 'vitest';
import SwarmPane from '../components/SwarmPane';
import type { MetadataArtifact, MetadataDetail } from '../lib/jobMetadata';
import type { HistoryLane, HistoryRow } from '../lib/selectedMetadataEvidence';
import { expertDetail, expertMetadataFixture, expertSummary } from './metadataExpert.fixtures';
import { selection } from './jobMetadata.fixtures';
import { identityTask } from './frontend52-identity.fixtures';
import wire from './expertWire.backend.json';
export { workerDetails } from './frontend52-identity.fixtures';

export const referenceHash = 'abcdef0123456789'.repeat(4);
export function routingReference(id = 'route-ref', type = 'ROUTING'): MetadataArtifact {
  return { id, type, task_id: 'task-a', status: null, stamp: 'known', revision: 1,
    sha256: referenceHash, presence: 'recorded', check_result: 'unavailable' };
}
type Attempt = { task: string; model: string; id: string };
type Outcome = { task: string; code: number; timedOut: boolean };
// Public captured records only. No route-policy bodies or current assignments.
export async function capturedRoutingFixture(options: {
  tasks?: string[]; attempts?: Attempt[]; artifacts?: MetadataArtifact[];
  status?: string; width?: number; outcomes?: Outcome[];
} = {}) {
  const selected = { ...selection(), job_ref: { ...selection().job_ref, version: 2,
    incarnation: '12345678-1234-4234-8234-123456789abc' } };
  const status = options.status ?? 'running';
  const tasks = (options.tasks ?? ['task-a', 'task-b']).map(id => identityTask(id, status));
  let attempts = options.attempts ?? [{ task: tasks[0]?.id ?? 'unmatched-task', model: 'captured-model', id: 'attempt-a' }];
  let outcomes = options.outcomes ?? [];
  const artifacts = options.artifacts ?? [];
  const fixture = await expertMetadataFixture([{ ...expertSummary(selected, 'Captured routing evidence'),
    lifecycle: status, task_count: tasks.length, artifact_count: artifacts.length }]);
  const lane = (rows: HistoryRow[]): HistoryLane => ({ page: { outcome: 'complete', next_cursor: null,
    scanned: rows.length, captured_count: rows.length, coverage: 'captured', complete_invocation_history: false }, rows });
  fixture.selected.mockImplementation(async (): Promise<MetadataDetail> => {
    const base = expertDetail(selected, fixture.context());
    const attemptRows = attempts.map((attempt, i) => ({ job_ref: selected.job_ref, kind: 'attempt', sequence: i + 1,
      facts: { ...wire[0].detail.history.attempts.rows[0].facts, job_id: selected.job_ref.job_id,
        task_id: attempt.task, attempt_id: attempt.id, run_id: `run-${attempt.task}`, model: attempt.model,
        adapter: 'openrouter', provider: 'captured-provider' } }));
    const runs = tasks.map((task, i) => ({ job_ref: selected.job_ref, kind: 'run', sequence: i + 1,
      facts: { ...wire[0].detail.history.runs.rows[0].facts, job_id: selected.job_ref.job_id,
        task_id: task.id, id: `run-${task.id}`, role: `role-${task.id}`, worker_id: `worker-${task.id}`, status } }));
    const outcomeRows = outcomes.map((outcome, i) => ({ job_ref: selected.job_ref, kind: 'outcome', sequence: i + 1,
      facts: { ...wire[0].detail.history.process_outcomes.rows[0].facts, job_id: selected.job_ref.job_id,
        task_id: outcome.task, run_id: `run-${outcome.task}`, attempt_id: 'attempt-a', observation_id: `outcome-${i}`,
        returncode: outcome.code, timed_out: outcome.timedOut, identity_state: 'available' } }));
    return { ...base, lifecycle: status, task_count: tasks.length, artifact_count: artifacts.length,
      tasks: { page: { ...base.tasks.page, scanned: tasks.length }, rows: tasks },
      artifacts: { page: { ...base.artifacts.page, scanned: artifacts.length }, rows: artifacts },
      history: { kind: 'available', counts: { captured_attempts: attemptRows.length, captured_runs: runs.length,
        captured_process_outcomes: outcomeRows.length, captured_observations: 0, outcome: 'available', coverage: 'captured', complete_invocation_history: false },
        attempts: lane(attemptRows), runs: lane(runs), process_outcomes: lane(outcomeRows), observations: lane([]) } };
  });
  const rendered = render(<fixture.Provider><div style={{ width: options.width ? `${options.width}px` : undefined }}><SwarmPane /></div></fixture.Provider>);
  onTestFinished(() => { rendered.unmount(); fixture.dispose(); });
  const job = await screen.findByRole('button', { name: /^Captured routing evidence/ });
  if (job.getAttribute('aria-expanded') === 'false') fireEvent.click(job);
  fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' }));
  const inspector = await screen.findByRole('region', { name: 'Selected job inspector' });
  await waitFor(() => { if (fixture.store.getSnapshot().working) throw Error('Still reading'); });
  return { ...fixture, selection: selected, inspector, container: rendered.container,
    attempts(next: Attempt[]) { attempts = next; }, outcomes(next: Outcome[]) { outcomes = next; },
    async refresh() { await act(async () => { await fixture.store.readDetail(); }); } };
}
