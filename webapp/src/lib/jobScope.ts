/** Ownership views over already-visible jobs: session, this repo, or all projects. */

export type JobScope = "session" | "repo" | "all";

export type ScopedJob = {
  id?: string;
  session_id?: string | null;
  cross_project?: boolean;
};

export const JOB_SCOPE_KEY = "marionette.jobScope.v1";
export const JOB_SCOPE_CHANGED_EVENT = "harness-job-scope-changed";

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
  opts?: { includeJobIds?: Iterable<string> },
): T[] {
  const includeIds = new Set(
    [...(opts?.includeJobIds || [])].map((id) => String(id || "").trim()).filter(Boolean),
  );
  let filtered: T[];
  if (scope === "all") {
    filtered = [...jobs];
  } else if (scope === "session") {
    if (!(activeSessionId || "").trim()) {
      filtered = [];
    } else {
      filtered = jobs.filter((job) => jobInActiveSession(job, activeSessionId));
    }
  } else {
    filtered = jobs.filter((job) => !job.cross_project);
  }
  if (includeIds.size === 0) return filtered;
  const kept = new Set(filtered.map((job) => String(job.id || "").trim()).filter(Boolean));
  const extras = jobs.filter((job) => {
    const id = String(job.id || "").trim();
    return Boolean(id && includeIds.has(id) && !kept.has(id));
  });
  return extras.length ? [...filtered, ...extras] : filtered;
}

export function loadJobScope(): JobScope {
  try {
    const raw = localStorage.getItem(JOB_SCOPE_KEY);
    if (raw === "repo" || raw === "all" || raw === "session") return raw;
    return "session";
  } catch {
    return "session";
  }
}

export function saveJobScope(scope: JobScope): void {
  try {
    localStorage.setItem(JOB_SCOPE_KEY, scope);
    window.dispatchEvent(new CustomEvent(JOB_SCOPE_CHANGED_EVENT, { detail: { scope } }));
  } catch {
    /* ignore */
  }
}
