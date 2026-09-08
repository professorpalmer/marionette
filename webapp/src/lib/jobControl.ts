import { isPublicJobRef, jobRefKey } from './publicJobRef';
import type { PublicJobRef } from './publicJobRef';
import type { LocalRef } from './localJobMetadata';
import type { Job } from './api';
import { selectJobRef } from './jobArtifacts';

export type TaskBinding = {
  task_id: string;
  generation: number | null;
  lease_id: string | null;
  owner: string | null;
};
export type CancellationView =
  | { status: 'complete'; limit: 200; bindings: TaskBinding[] }
  | { status: 'partial' | 'unavailable' | 'cursor_expired'; limit: 200 };
type SelectionContext = { repo: string; session_id: string };
export type BoundJobSelection = SelectionContext & {
  version: 2;
  source: 'harness' | 'cli';
  job_ref: PublicJobRef;
  bindings: TaskBinding[];
};
export type JobControlSelection = BoundJobSelection | (SelectionContext & {
  version: 1; source: 'local'; local_incarnation?: string; job_ref: { job_id: string; state_id: null };
});
export type NativeMetadataControlSelection = Extract<JobControlSelection, { source: 'local' }> & { local_incarnation: string };

export function selectNativeMetadataControl(ref: LocalRef, context: SelectionContext): NativeMetadataControlSelection | null {
  if (!/^local-[a-zA-Z0-9_-]+$/.test(ref.job_id) || ref.job_id.length > 256
    || !/^[a-zA-Z0-9_-]{1,128}$/.test(ref.incarnation) || !context.repo || !context.session_id) return null;
  return { version: 1, source: 'local', repo: context.repo, session_id: context.session_id,
    local_incarnation: ref.incarnation, job_ref: { job_id: ref.job_id, state_id: null } };
}
export type CancellationReceipt = {
  job_ref: BoundJobSelection['job_ref'];
  request_id: string;
  bindings: TaskBinding[];
  outcome: 'requested' | 'observed_stop' | 'stale_binding' | 'already_terminal' | 'conflict';
  revision: number;
  cleanup: 'unknown' | 'partial' | 'local_process_exited';
};
export type CancellationRequest = { selection: BoundJobSelection; request_id: string };
export type CancellationResult = { ok: true; receipt: CancellationReceipt };

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
function isBinding(value: unknown): value is TaskBinding {
  return record(value) && typeof value.task_id === 'string' && value.task_id.length > 0
    && (value.generation === null || (typeof value.generation === 'number'
      && Number.isSafeInteger(value.generation) && value.generation >= 0))
    && (value.lease_id === null || typeof value.lease_id === 'string')
    && (value.owner === null || typeof value.owner === 'string');
}
function isBindings(value: unknown): value is TaskBinding[] {
  return Array.isArray(value) && value.length > 0 && value.length <= 200
    && value.every(isBinding) && new Set(value.map(b => b.task_id)).size === value.length;
}
function bindingsKey(bindings: TaskBinding[]): string {
  return JSON.stringify(bindings.map(b => [b.task_id, b.generation, b.lease_id, b.owner])
    .sort((a, b) => String(a[0]).localeCompare(String(b[0]))));
}
function isReceipt(value: unknown): value is CancellationReceipt {
  return record(value) && isPublicJobRef(value.job_ref) && typeof value.request_id === 'string'
    && isBindings(value.bindings) && typeof value.revision === 'number'
    && Number.isSafeInteger(value.revision) && value.revision >= 1
    && (value.outcome === 'requested' || value.outcome === 'observed_stop'
      || value.outcome === 'stale_binding' || value.outcome === 'already_terminal' || value.outcome === 'conflict')
    && (value.cleanup === 'unknown' || value.cleanup === 'partial' || value.cleanup === 'local_process_exited');
}
export function parseCancellationResult(value: unknown, request: CancellationRequest): CancellationResult {
  const s = request.selection;
  if (!record(value) || value.ok !== true || !record(value.selection) || !isPublicJobRef(value.selection.job_ref)
      || value.selection.version !== 2 || value.selection.source !== s.source
      || value.selection.repo !== s.repo || value.selection.session_id !== s.session_id
      || jobRefKey(value.selection.job_ref) !== jobRefKey(s.job_ref)
      || !isBindings(value.selection.bindings) || bindingsKey(value.selection.bindings) !== bindingsKey(s.bindings)
      || value.request_id !== request.request_id || !isReceipt(value.receipt)
      || value.receipt.request_id !== request.request_id || jobRefKey(value.receipt.job_ref) !== jobRefKey(s.job_ref)
      || (value.receipt.outcome !== 'conflict' && bindingsKey(value.receipt.bindings) !== bindingsKey(s.bindings))) {
    throw new Error('Cancellation acknowledgement does not match the selected workers. Refresh the original receipt.');
  }
  return { ok: true, receipt: value.receipt };
}

export function selectJobControl(job: Job, repo: string, sessionId: string): JobControlSelection | null {
  if (job.id.startsWith('local-') && !job.job_ref && !job.cross_project
      && (job.source === 'harness' || job.source === 'local')
      && repo && sessionId && job.session_id === sessionId) {
    return { version: 1, source: 'local', repo, session_id: sessionId,
      job_ref: { job_id: job.id, state_id: null } };
  }
  const selection = selectJobRef(job, repo, sessionId);
  const view = job.cancellation_view;
  if (job.unavailable_fields?.includes('tasks') || !selection || selection.job_ref.version !== 2 || (selection.source !== 'harness' && selection.source !== 'cli')
      || view?.status !== 'complete' || !isBindings(view.bindings)) return null;
  return { ...selection, version: 2, source: selection.source,
    bindings: view.bindings.map(b => ({ ...b })) };
}

export function jobControlKey(job: Job, repo: string, sessionId: string): string {
  return JSON.stringify([1, job.id, job.job_ref?.state_id ?? null, job.source ?? 'harness',
    job.session_id ?? null, job.cwd ?? null, repo, sessionId, ...(job.job_ref?.version === 2 ? [2, job.job_ref.incarnation] : [])]);
}

export function cancellationMessage(receipt: CancellationReceipt): string {
  const cleanup = receipt.cleanup === 'local_process_exited'
    ? 'Local process exit recorded; descendants and remote effects remain unresolved.'
    : receipt.cleanup === 'partial' ? 'Cleanup is partial; remaining effects are unresolved.'
    : 'Cleanup and remote effects are unknown.';
  switch (receipt.outcome) {
    case 'requested': return `Stop requested; awaiting worker acknowledgement. ${cleanup}`;
    case 'observed_stop': return `Local worker stop observed. ${cleanup}`;
    case 'stale_binding': return 'Selected worker generation or lease changed. Successor workers were not stopped.';
    case 'already_terminal': return 'Selected workers were already terminal. This request did not stop them.';
    case 'conflict': return 'Request ID conflicts with a different set of workers. No new stop was requested.';
    default: { const exhaustive: never = receipt.outcome; return exhaustive; }
  }
}
