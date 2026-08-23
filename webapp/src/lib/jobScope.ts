/** Session vs repo-ever views over already-visible jobs. */

export type JobScope = "session" | "repo";

export type ScopedJob = {
  session_id?: string | null;
};

export function jobSessionId(job: ScopedJob): string {
  return String(job.session_id || "").trim();
}

export function jobInActiveSession(job: ScopedJob, activeSessionId: string): boolean {
  const sid = jobSessionId(job);
  const active = (activeSessionId || "").trim();
  return Boolean(sid && active && sid === active);
}

export function filterJobsByScope<T extends ScopedJob>(
  jobs: readonly T[],
  scope: JobScope,
  activeSessionId: string,
): T[] {
  if (scope !== "session") return [...jobs];
  // No active chat yet: keep the repo list rather than going blank.
  if (!(activeSessionId || "").trim()) return [...jobs];
  return jobs.filter((job) => jobInActiveSession(job, activeSessionId));
}

export const JOB_SCOPE_KEY = "marionette.jobScope.v1";

export function loadJobScope(): JobScope {
  try {
    return localStorage.getItem(JOB_SCOPE_KEY) === "repo" ? "repo" : "session";
  } catch {
    return "session";
  }
}

export function saveJobScope(scope: JobScope): void {
  try {
    localStorage.setItem(JOB_SCOPE_KEY, scope);
  } catch {
    /* ignore */
  }
}
