/**
 * Queue a Swarm Tracker deep-link when SwarmPane is not yet mounted.
 *
 * openAgentSwarmJob fires harness-focus-tab then harness-open-swarm-job
 * synchronously. SwarmPane may mount only after the right pane opens / the
 * swarm tab activates, so the event can be missed — stash the target until
 * SwarmPane consumes it on mount (same idea as App.pendingRightTab).
 */

let pendingOpenJobId: string | null = null;
let pendingOpenArtifactId: string | null = null;

/** Stash a job and optional artifact for a late-mounted SwarmPane to consume. */
export function queuePendingSwarmOpenJob(jobId: string, artifactId?: string): void {
  const id = (jobId || "").trim();
  pendingOpenJobId = id || null;
  pendingOpenArtifactId = id ? (artifactId || "").trim() || null : null;
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

/** Peek the optional artifact target without clearing it. */
export function peekPendingSwarmOpenArtifact(): string | null {
  return pendingOpenArtifactId;
}

/** Take and clear the optional artifact target paired with the pending job. */
export function takePendingSwarmOpenArtifact(): string | null {
  const id = pendingOpenArtifactId;
  pendingOpenArtifactId = null;
  return id;
}

/** Test helper: drop any stashed deep-link. */
export function clearPendingSwarmOpenJob(): void {
  pendingOpenJobId = null;
  pendingOpenArtifactId = null;
}
