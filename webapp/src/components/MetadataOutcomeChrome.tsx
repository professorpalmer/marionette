import type { Job } from '../lib/api';

export const failedOutcomeStatuses = new Set(['failed', 'timeout', 'timed_out', 'truncated', 'interrupted']);
export function metadataOutcomeLabel(status: string): string {
  return failedOutcomeStatuses.has(status) && status !== 'interrupted' ? 'failed' : status;
}
export function MetadataOutcomeLabel({ status }: { status: string }) {
  return <span className={failedOutcomeStatuses.has(status) ? 'text-risk' : 'text-muted'}>{metadataOutcomeLabel(status)}</span>;
}
export function MetadataOutcomeCounts({ jobs }: { jobs: Job[] }) {
  const failures = jobs.filter(job => failedOutcomeStatuses.has(job.status)).length;
  const cancelled = jobs.filter(job => job.status === 'cancelled').length;
  return <p className="px-2 text-xs text-muted">{[failures ? `${failures} failed` : '', cancelled ? `${cancelled} cancelled` : ''].filter(Boolean).join(' · ')}</p>;
}
