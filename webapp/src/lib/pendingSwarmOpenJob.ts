/**
 * Queue a Swarm Tracker deep-link when SwarmPane is not yet mounted.
 *
 * openAgentSwarmJob fires harness-focus-tab then harness-open-swarm-job
 * synchronously. SwarmPane may mount only after the right pane opens / the
 * swarm tab activates, so the event can be missed — stash the job id until
 * SwarmPane consumes it on mount (same idea as App.pendingRightTab).
 */

let pendingOpenJobId: string | null = null;

/** Stash a job id for a late-mounted SwarmPane to consume. */
export function queuePendingSwarmOpenJob(jobId: string): void {
  const id = (jobId || "").trim();
  pendingOpenJobId = id || null;
}

/** Peek without clearing (tests / race guards). */
export function peekPendingSwarmOpenJob(): string | null {
  return pendingOpenJobId;
}

/** Take and clear the pending job id (null when none). */
export function takePendingSwarmOpenJob(): string | null {
  const id = pendingOpenJobId;
  pendingOpenJobId = null;
  return id;
}

/** Test helper: drop any stashed deep-link. */
export function clearPendingSwarmOpenJob(): void {
  pendingOpenJobId = null;
}
