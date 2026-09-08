import { expertDetail, expertMetadataFixture, expertSummary } from './metadataExpert.fixtures';
import { context } from './jobMetadata.fixtures';
import type { MetadataSelection } from '../lib/jobMetadata';
import type { HistoryLane, SelectedHistory } from '../lib/selectedMetadataEvidence';

export async function terminalWorkerMetadataFixture() {
  const selection: MetadataSelection = { repo: context.repo, session_id: context.session_id, source: 'harness',
    job_ref: { job_id: 'job_terminal_worker', state_id: 'store-A', version: 2, incarnation: '12345678-1234-4234-8234-123456789abc' } };
  const fixture = await expertMetadataFixture([{ ...expertSummary(selection, 'Inspect completed worker'), lifecycle: 'complete' }]);
  const empty: HistoryLane = { page: { outcome: 'complete', next_cursor: null, scanned: 0, captured_count: 0,
    coverage: 'captured', complete_invocation_history: false }, rows: [] };
  // Run facts are the bounded public history fields, not current task presentation.
  const history: SelectedHistory = { kind: 'available', counts: { captured_attempts: 0, captured_runs: 2,
    captured_process_outcomes: 0, captured_observations: 0, outcome: 'available', coverage: 'captured', complete_invocation_history: false },
    attempts: empty, observations: empty, process_outcomes: empty,
    runs: { page: { ...empty.page, scanned: 2, captured_count: 2 }, rows: [
      { job_ref: selection.job_ref, kind: 'run', sequence: 1, facts: { id: 'run-terminal', task_id: 'task-terminal', role: 'completed-worker',
        worker_id: 'worker-terminal', status: 'completed', started_at: '2026-08-16T16:00:00+00:00', completed_at: '2026-08-16T17:00:00+00:00' } },
      { job_ref: selection.job_ref, kind: 'run', sequence: 2, facts: { id: 'run-unrelated', task_id: 'task-terminal-other', role: 'unrelated-worker',
        worker_id: 'worker-unrelated', status: 'completed', started_at: '2026-08-15T11:00:00+00:00', completed_at: '2026-08-15T12:00:00+00:00' } },
    ] } };
  fixture.selected.mockImplementation(async selected => {
    const detail = expertDetail(selected, fixture.context());
    return { ...detail, lifecycle: 'complete', history, task_count: 1,
      tasks: { page: detail.tasks.page, rows: [{ id: 'task-terminal', status: 'complete', stamp: 'known', revision: 2, binding: null }] } };
  });
  return fixture;
}
