import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import SwarmPane from '../components/SwarmPane';
import type { MetadataTask } from '../lib/jobMetadata';
import type { HistoryLane, HistoryRow, SelectedHistory } from '../lib/selectedMetadataEvidence';
import { expertDetail, expertMetadataFixture, expertSummary } from './metadataExpert.fixtures';
import { selection } from './jobMetadata.fixtures';
import wire from './expertWire.backend.json';

export function identityTask(id: string, status = 'running'): MetadataTask {
  return { id, status, stamp: 'known', revision: 2,
    binding: { task_id: id, generation: 3, lease_id: `lease-${id}`, owner: `owner-${id}` } };
}

// Only public producer fields are varied; every response crosses the real client/parser/store.
export async function identityFixture(options: {
  tasks?: MetadataTask[]; models?: (string | null)[]; lifecycle?: string;
  outcomes?: { task: string | null; code: number; timedOut: boolean }[];
} = {}) {
  const selected = { ...selection(), job_ref: { ...selection().job_ref, version: 2,
    incarnation: '12345678-1234-4234-8234-123456789abc' } };
  const tasks = options.tasks ?? [identityTask('task-a')];
  let models = options.models ?? ['model-a'];
  const lifecycle = options.lifecycle ?? 'running';
  const fixture = await expertMetadataFixture([{ ...expertSummary(selected, 'Identity evidence'), lifecycle,
    task_count: tasks.length }]);
  const lane = (rows: HistoryRow[]): HistoryLane => ({ page: { outcome: 'complete', next_cursor: null,
    scanned: rows.length, captured_count: rows.length, coverage: 'captured', complete_invocation_history: false }, rows });
  fixture.selected.mockImplementation(async () => {
    const detail = expertDetail(selected, fixture.context());
    const attempts = models.map((model, i) => ({ job_ref: selected.job_ref, kind: 'attempt', sequence: i + 1,
      facts: { ...wire[0].detail.history.attempts.rows[0].facts, job_id: selected.job_ref.job_id,
        task_id: tasks[i]?.id ?? 'unmatched-task', attempt_id: `attempt-${i}`, run_id: `run-${i}`, model, adapter: 'openrouter' } }));
    const runs = tasks.map((task, i) => ({ job_ref: selected.job_ref, kind: 'run', sequence: i + 1,
      facts: { ...wire[0].detail.history.runs.rows[0].facts, job_id: selected.job_ref.job_id,
        task_id: task.id, id: `run-${i}`, role: `role-${task.id}`, worker_id: `worker-${task.id}`,
        status: task.status, completed_at: task.status === 'complete' || task.status === 'failed' ? '2026-09-07T19:00:00Z' : null } }));
    const outcomes = (options.outcomes ?? []).map((outcome, i) => ({ job_ref: selected.job_ref, kind: 'outcome', sequence: i + 1,
      facts: { ...wire[0].detail.history.process_outcomes.rows[0].facts, job_id: selected.job_ref.job_id,
        task_id: outcome.task, run_id: `run-${tasks.findIndex(task => task.id === outcome.task)}`,
        attempt_id: `attempt-${tasks.findIndex(task => task.id === outcome.task)}`, observation_id: `outcome-${i}`, returncode: outcome.code, timed_out: outcome.timedOut,
        identity_state: outcome.task === null ? 'unavailable' : 'available' } }));
    const history: SelectedHistory = { kind: 'available', counts: { captured_attempts: attempts.length,
      captured_runs: runs.length, captured_process_outcomes: outcomes.length, captured_observations: 0,
      outcome: 'available', coverage: 'captured', complete_invocation_history: false },
      attempts: lane(attempts), runs: lane(runs), process_outcomes: lane(outcomes), observations: lane([]) };
    return { ...detail, lifecycle, task_count: tasks.length, history,
      tasks: { page: { ...detail.tasks.page, scanned: tasks.length }, rows: tasks } };
  });
  const rendered = render(<fixture.Provider><SwarmPane /></fixture.Provider>);
  const job = await screen.findByRole('button', { name: /^Identity evidence/ });
  if (job.getAttribute('aria-expanded') === 'false') fireEvent.click(job);
  const inspect = screen.getByRole('button', { name: 'Inspect tasks and artifacts' });
  fireEvent.click(inspect);
  await screen.findByRole('region', { name: 'Tasks' });
  await waitFor(() => { if (fixture.store.getSnapshot().working) throw Error('Still reading'); });
  return { ...fixture, job, inspect, models(next: (string | null)[]) { models = next; },
    close() { rendered.unmount(); fixture.dispose(); } };
}

export function workerDetails(id: string, status = 'running') {
  const button = screen.getByRole('button', { name: `${id}: ${status}` });
  const details = document.getElementById(button.getAttribute('aria-controls') ?? '');
  if (!details) throw Error('Missing worker disclosure');
  return { button, details };
}
