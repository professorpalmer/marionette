/** Classify /api/swarm/live rows as terminal command vs provider swarm.
 *
 * Background run_command / run_command_batch jobs ride the same live feed as
 * swarms (local-job projection) but must never paint Swarm Tracker chrome.
 * job_kind and local-cmd* ids are the seam. role/adapter "command" is
 * corroborating, not sufficient — swarm workers can use adapter=command.
 */

const COMMAND_JOB_KINDS = new Set(["run_command", "run_command_batch"]);

export type CommandJobSignals = {
  id?: string | null;
  job_kind?: string | null;
  role?: string | null;
  adapter?: string | null;
};

function commandId(id: string): boolean {
  return id.startsWith("local-cmd-") || id.startsWith("local-cmdbatch-");
}

/** True for background command / command-batch jobs. */
export function isCommandJob(job: CommandJobSignals): boolean {
  const kind = String(job.job_kind || "").trim().toLowerCase();
  if (COMMAND_JOB_KINDS.has(kind)) return true;
  const id = String(job.id || "").trim().toLowerCase();
  if (commandId(id)) return true;
  return false;
}
