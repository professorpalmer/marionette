import type { Job } from './api';
import type { MetadataSelection } from './jobMetadata';
import { metadataSelectionKey } from './jobMetadata';
import type { LocalRef } from './localJobMetadata';
import { localKey } from './localJobMetadata';

export type NavigationContext = Readonly<{ repo: string; session_id: string; contextEpoch: number }>;
type Destination =
  | { kind: 'pm'; selection: Readonly<MetadataSelection> }
  | { kind: 'native'; localRef: Readonly<LocalRef> }
  | { kind: 'unresolved'; metadataKey?: string };
export type SwarmNavigationTarget = Readonly<Destination & {
  jobId: string;
  context: NavigationContext | null;
  artifactId?: string;
}>;
let pending: SwarmNavigationTarget | null = null;

/** Capture only ownership actually carried by the clicked/observed row. */
export function swarmNavigationTarget(jobId: string, context: NavigationContext | null, job?: Job, artifactId?: string, metadataKey?: string): SwarmNavigationTarget {
  const scope = context ? Object.freeze({ ...context }) : null;
  const base = { jobId: jobId.trim(), context: scope, ...(artifactId?.trim() ? { artifactId: artifactId.trim() } : {}) };
  if (job?.id === base.jobId && scope) {
    if (job.local_ref) return Object.freeze({ ...base, kind: 'native', localRef: Object.freeze({ ...job.local_ref }) });
    if (job.job_ref && (job.source === 'harness' || job.source === 'cli')) return Object.freeze({ ...base, kind: 'pm', selection: Object.freeze({ repo: scope.repo, session_id: scope.session_id, source: job.source, job_ref: Object.freeze({ ...job.job_ref }) }) });
  }
  return Object.freeze({ ...base, kind: 'unresolved', ...(metadataKey ? { metadataKey } : {}) });
}
export function navigationMatches(target: SwarmNavigationTarget, context: NavigationContext | null, job: Job): boolean {
  if (!target.context || !context || target.context.repo !== context.repo || target.context.session_id !== context.session_id || job.id !== target.jobId) return false;
  switch (target.kind) {
    case 'pm': return job.metadata_key === metadataSelectionKey(target.selection);
    case 'native': return !!job.local_ref && localKey(job.local_ref) === localKey(target.localRef);
    case 'unresolved': return target.context.contextEpoch === context.contextEpoch && !!target.metadataKey && job.metadata_key === target.metadataKey;
  }
}
export function queuePendingSwarmNavigation(target: SwarmNavigationTarget): SwarmNavigationTarget {
  const context = target.context ? Object.freeze({ ...target.context }) : null;
  switch (target.kind) {
    case 'pm': pending = Object.freeze({ ...target, context, selection: Object.freeze({ ...target.selection, job_ref: Object.freeze({ ...target.selection.job_ref }) }) }); break;
    case 'native': pending = Object.freeze({ ...target, context, localRef: Object.freeze({ ...target.localRef }) }); break;
    case 'unresolved': pending = Object.freeze({ ...target, context }); break;
  }
  return pending;
}
export function peekPendingSwarmNavigation(): SwarmNavigationTarget | null { return pending; }
/** Compare the actual click object: equal destination fields are still different clicks. */
export function takePendingSwarmNavigation(target: SwarmNavigationTarget): SwarmNavigationTarget | null {
  if (pending !== target) return null;
  pending = null;
  return target;
}

/** Compatibility projections use the same slot; they never grant context authority. */
export function queuePendingSwarmOpenJob(jobId: string, artifactId?: string): void {
  pending = jobId.trim() ? swarmNavigationTarget(jobId, null, undefined, artifactId) : null;
}
export function peekPendingSwarmOpenJob(): string | null { return pending?.jobId ?? null; }
export function peekPendingSwarmOpenArtifact(): string | null { return pending?.artifactId ?? null; }
export function takePendingSwarmOpenJob(): string | null {
  const target = pending;
  // Opening the job does not render its artifact. Leave the atomic target intact.
  if (target && !target.artifactId) takePendingSwarmNavigation(target);
  return target?.jobId ?? null;
}
export function takePendingSwarmOpenArtifact(): string | null {
  const target = pending;
  if (target) takePendingSwarmNavigation(target);
  return target?.artifactId ?? null;
}
export function clearPendingSwarmOpenJob(target: SwarmNavigationTarget | null = pending): void {
  if (target) takePendingSwarmNavigation(target);
}
