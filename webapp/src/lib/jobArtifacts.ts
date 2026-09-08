import { jobRefQuery } from './publicJobRef';
import type { PublicJobRef } from './publicJobRef';
import type { Artifact, Job } from './api';
import { getJSON } from './transport';

export type SelectedJobRef = {
  job_ref: PublicJobRef;
  source: string;
  repo: string;
  session_id: string;
};
export type ArtifactLoad =
  | { kind: 'loading' }
  | { kind: 'loaded'; artifacts: Artifact[] }
  | { kind: 'error'; message: string };

type JobArtifactsResult = SelectedJobRef & { artifacts: Artifact[] };

export function selectJobRef(job: Job, repo: string, sessionId: string): SelectedJobRef | null {
  if (job.cross_project || !job.job_ref || job.job_ref.job_id !== job.id || !job.job_ref.state_id
      || !repo || !sessionId || job.session_id !== sessionId
      || (job.source !== 'harness' && job.source !== 'cli')) return null;
  return { job_ref: { ...job.job_ref }, source: job.source, repo, session_id: sessionId };
}

export function jobArtifactKey(selection: SelectedJobRef): string {
  return JSON.stringify([selection.job_ref.job_id, selection.job_ref.state_id,
    selection.source, selection.repo, selection.session_id, ...(selection.job_ref.version === 2 ? [2, selection.job_ref.incarnation] : [])]);
}

export async function fetchJobArtifacts(selection: SelectedJobRef): Promise<Artifact[]> {
  selection = { ...selection, job_ref: { ...selection.job_ref } };
  const query = new URLSearchParams({ ...jobRefQuery(selection.job_ref), source: selection.source,
    repo: selection.repo, session_id: selection.session_id });
  const result = await getJSON<JobArtifactsResult>(`/api/jobs/artifacts/v1?${query}`, {
    sessionId: selection.session_id, repo: selection.repo,
  });
  if (!result || !result.job_ref || jobArtifactKey(result) !== jobArtifactKey(selection)
      || !Array.isArray(result.artifacts)) {
    throw new Error('Artifacts are unavailable for the selected job.');
  }
  return result.artifacts;
}
