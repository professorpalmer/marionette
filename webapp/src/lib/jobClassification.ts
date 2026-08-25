/** Classify /api/swarm/live rows for Swarm Tracker vs terminal chrome.
 *
 * 342 hid run_command / local-cmd-* only. Wave parents still leaked
 * (local-wave-* / role+adapter parallel_wave) beside their hired children.
 * Tracker allowlist: real hires only.
 *
 * Show: run_swarm, run_implement, remote job_*, unnamed non-coordinator rows.
 * Hide: role/adapter command AND parallel_wave; ids local-wave-* and local-cmd-*.
 * Do not show the wave parent alongside its children.
 *
 * isCommandJob stays the terminal-reclassify seam. role/adapter "command" is
 * still not sufficient there — a hire (run_swarm / run_implement / job_*)
 * still paints even when the worker adapter is command.
 */

const COMMAND_JOB_KINDS = new Set(["run_command", "run_command_batch"]);
const HIRE_JOB_KINDS = new Set(["run_swarm", "run_implement"]);
const EXCLUDED_STAMPS = new Set(["command", "command_batch", "parallel_wave"]);

export type CommandJobSignals = {
  id?: string | null;
  job_kind?: string | null;
  role?: string | null;
  adapter?: string | null;
  status?: string | null;
};

function norm(value: unknown): string {
  return String(value || "").trim().toLowerCase();
}

function commandId(id: string): boolean {
  return id.startsWith("local-cmd-") || id.startsWith("local-cmdbatch-");
}

function waveId(id: string): boolean {
  return id.startsWith("local-wave-");
}

/** True for the run_parallel wave parent, never its hired children. */
export function isWaveCoordinator(job: CommandJobSignals): boolean {
  const id = norm(job.id);
  if (waveId(id)) return true;
  return (
    norm(job.job_kind) === "parallel_wave"
    || norm(job.role) === "parallel_wave"
    || norm(job.adapter) === "parallel_wave"
  );
}

/** True for background command / command-batch jobs. */
export function isCommandJob(job: CommandJobSignals): boolean {
  const kind = norm(job.job_kind);
  if (COMMAND_JOB_KINDS.has(kind)) return true;
  const id = norm(job.id);
  if (commandId(id)) return true;
  return false;
}

/** True for run_swarm / run_implement / remote job_* / local-swarm|impl. */
export function isTrackerHire(job: CommandJobSignals): boolean {
  const kind = norm(job.job_kind);
  if (HIRE_JOB_KINDS.has(kind)) return true;
  const id = String(job.id || "").trim();
  const idLower = id.toLowerCase();
  if (id.startsWith("job_")) return true;
  if (idLower.startsWith("local-swarm-") || idLower.startsWith("local-impl-")) {
    return true;
  }
  return false;
}

/**
 * Tracker allowlist. Wave parent never sits beside children. Command-stamped
 * non-hires are excluded; command-adapter hires and unnamed store rows stay.
 */
export function isSwarmTrackerJob(job: CommandJobSignals): boolean {
  if (isWaveCoordinator(job)) return false;
  if (isCommandJob(job)) return false;
  if (isTrackerHire(job)) return true;
  const role = norm(job.role);
  const adapter = norm(job.adapter);
  if (EXCLUDED_STAMPS.has(role) || EXCLUDED_STAMPS.has(adapter)) return false;
  return true;
}

/** Alias used by SwarmPane / composer stack. */
export const isTrackerJob = isSwarmTrackerJob;

export function isRunningJobStatus(status: unknown): boolean {
  const s = String(status || "").toLowerCase();
  return s.includes("run") || s.includes("progress") || s.includes("active");
}

export function countRunningTrackerJobs(jobs: readonly CommandJobSignals[]): number {
  return jobs.filter((j) => isSwarmTrackerJob(j) && isRunningJobStatus(j.status)).length;
}
