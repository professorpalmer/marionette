import { act } from '@testing-library/react';
import wire from './nativeExpert.backend.json';
import captured from './expertWire.backend.json';
import { expertMetadataFixture, expertSummary } from './metadataExpert.fixtures';
import { selection } from './jobMetadata.fixtures';
import { nativeActiveStatuses, nativeAttentionStatuses, parseLocalDetail } from '../lib/localJobMetadata';
import type { LocalTask, LocalRoute } from '../lib/localJobMetadata';
import type { MetadataContext, MetadataSelection } from '../lib/jobMetadata';

export async function nativeExpertFixture(options: { jobId?: string; repo?: string; session?: string; task?: Partial<LocalTask>; routes?: LocalRoute[] } = {}) {
  const selected = { ...selection(), repo: options.repo ?? '/repo', session_id: options.session ?? 'sess-test' };
  const fixture = await expertMetadataFixture([expertSummary(selected, 'PM context')]);
  const local_ref = { ...wire.tasks.local_ref, job_id: options.jobId ?? wire.tasks.local_ref.job_id };
  let revision = wire.tasks.page.revision;
  const task = { ...wire.tasks.rows[0], task_id: `${local_ref.job_id}-w0`, ...options.task };
  const routes = options.routes ?? wire.routing.rows.map(row => ({ ...row, task_id: task.task_id }));
  const summary = () => ({ ...wire.tasks.summary, local_ref, revision, session_id: selected.session_id });
  const original = fixture.request.getMockImplementation();
  if (!original) throw Error('Missing wire responder');
  const response = (lane: string) => {
    const rows = lane === 'tasks' ? [task] : lane === 'routing' ? routes : [];
    return { ...wire.tasks, context: fixture.context(), local_ref, summary: summary(), lane, rows, total: rows.length,
      page: { ...wire.tasks.page, revision, checkpoint: revision, scanned: rows.length } };
  };
  fixture.request.mockImplementation(async (method, path) => {
    const url = new URL(path, 'http://fixture');
    if (url.pathname.endsWith('/view')) {
      const result = await original(method, path);
      if (!result || typeof result !== 'object') throw Error('Missing view');
      return { ...result, local: { available: true, incarnation: local_ref.incarnation, version: 1, lanes: ['active', 'history'], active_statuses: nativeActiveStatuses, attention_statuses: nativeAttentionStatuses } };
    }
    if (url.pathname.includes('/metadata/local')) {
      const lane = url.searchParams.get('lane') ?? 'history';
      if (url.pathname.endsWith('/detail')) return response(lane);
      const active = lane === 'active';
      return { ...response(lane), incarnation: local_ref.incarnation, rows: [summary()],
        coverage: { ...wire.tasks.coverage, membership: active ? 'retained_local_active' : 'retained_local_history', ...(active ? { metadata: 'live_during_traversal' } : {}) },
        page: { ...wire.tasks.page, revision, checkpoint: revision, scanned: 1 } };
    }
    return original(method, path);
  });
  await act(async () => { await fixture.store.readView(); });
  await act(async () => { await fixture.store.advanceLocal('active'); });
  return { ...fixture, local_ref, response, revise(instruction: string) { task.instruction = instruction; revision++; } };
}

export function producerTask(): LocalTask {
  const context: MetadataContext = { ...wire.tasks.context, scope: 'session' };
  const parsed = parseLocalDetail(wire.tasks, context, wire.tasks.local_ref, 'tasks');
  if (parsed.lane !== 'tasks') throw Error('Expected task lane');
  return parsed.rows[0];
}

// Captured public attempt schema, with explicit test model input. No model is inferred from a task or job.
export function capturedModelDetail(selected: MetadataSelection, context: MetadataContext) {
  const detail = captured[0].detail;
  const history = detail.history;
  const bind = (lane: typeof history.attempts | typeof history.runs | typeof history.process_outcomes | typeof history.observations) => ({ ...lane,
    rows: lane.rows.map(row => ({ ...row, job_ref: selected.job_ref, facts: { ...row.facts, job_id: selected.job_ref.job_id },
      ...('completion' in row ? { completion: { ...row.completion, job_ref: selected.job_ref } } : {}) })) });
  return { ...detail, selection: selected, context, cost: { ...detail.cost, job_ref: selected.job_ref }, history: { ...history,
    attempts: { ...bind(history.attempts), rows: history.attempts.rows.map(row => ({ ...row, job_ref: selected.job_ref,
      facts: { ...row.facts, model: 'grok-4-5', job_id: selected.job_ref.job_id } })) },
    runs: bind(history.runs), process_outcomes: bind(history.process_outcomes), observations: bind(history.observations) } };
}
