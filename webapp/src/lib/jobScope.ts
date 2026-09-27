/** Ownership views over already-owned Marionette jobs: session, this repo, or all. */

export type JobScope = "session" | "repo" | "all";

export type ScopedJob = {
  id?: string;
  session_id?: string | null;
  cross_project?: boolean;
};

/** v1 stored a bare scope, so one "all" pick stuck across every later session. */
export const JOB_SCOPE_KEY = "marionette.jobScope.v2";
export const JOB_SCOPE_CHANGED_EVENT = "harness-job-scope-changed";

/** A scope pick applies only to the session it was made in. */
export type JobScopeChoice = { scope: JobScope; sessionId: string };

export function jobSessionId(job: ScopedJob): string {
  return String(job.session_id || "").trim();
}

export function jobInActiveSession(job: ScopedJob, activeSessionId: string): boolean {
  const sid = jobSessionId(job);
  const active = (activeSessionId || "").trim();
  return Boolean(sid && active && sid === active);
}

/** Stamped session id is the frontend ownership proof on an already-listed row. */
export function jobOwnedForScope(job: ScopedJob): boolean {
  return Boolean(jobSessionId(job));
}

/** Sibling-store or unstamped rows must not be pinned back into a narrower scope. */
export function jobIsForeignForScope(job: ScopedJob): boolean {
  return Boolean(job.cross_project) || !jobOwnedForScope(job);
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
  const owned = jobs.filter((job) => jobOwnedForScope(job));
  let filtered: T[];
  if (scope === "all") {
    filtered = [...owned];
  } else if (scope === "session") {
    if (!(activeSessionId || "").trim()) {
      filtered = [];
    } else {
      filtered = owned.filter((job) => jobInActiveSession(job, activeSessionId));
    }
  } else {
    filtered = owned.filter((job) => !job.cross_project);
  }
  if (includeIds.size === 0) return filtered;
  const kept = new Set(filtered.map((job) => String(job.id || "").trim()).filter(Boolean));
  const extras = jobs.filter((job) => {
    const id = String(job.id || "").trim();
    return Boolean(id && includeIds.has(id) && !kept.has(id) && !jobIsForeignForScope(job));
  });
  return extras.length ? [...filtered, ...extras] : filtered;
}

export function parseJobScopeChoice(raw: unknown): JobScopeChoice | null {
  if (typeof raw !== "object" || raw === null || !("scope" in raw) || !("sessionId" in raw)) return null;
  const { scope, sessionId } = raw;
  if (scope !== "session" && scope !== "repo" && scope !== "all") return null;
  if (typeof sessionId !== "string" || !sessionId.trim()) return null;
  return { scope, sessionId: sessionId.trim() };
}

/** Every session starts on "session" unless the user picked a scope in it. */
export function jobScopeForSession(choice: JobScopeChoice | null, activeSessionId: string): JobScope {
  const active = (activeSessionId || "").trim();
  return choice && active && choice.sessionId === active ? choice.scope : "session";
}

export function loadJobScopeChoice(): JobScopeChoice | null {
  try {
    return parseJobScopeChoice(JSON.parse(localStorage.getItem(JOB_SCOPE_KEY) || "null"));
  } catch {
    return null;
  }
}

export function loadJobScope(activeSessionId: string): JobScope {
  return jobScopeForSession(loadJobScopeChoice(), activeSessionId);
}

export function saveJobScope(scope: JobScope, activeSessionId: string): void {
  const sessionId = (activeSessionId || "").trim();
  try {
    if (sessionId) localStorage.setItem(JOB_SCOPE_KEY, JSON.stringify({ scope, sessionId }));
    window.dispatchEvent(new CustomEvent(JOB_SCOPE_CHANGED_EVENT, { detail: { scope } }));
  } catch {
    /* ignore */
  }
}
