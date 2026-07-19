import type { SwarmPendingItem, SwarmPendingStatus } from "../TranscriptList";

/** Unique, trimmed, sorted job ids — canonical identity for a lifecycle pill. */
export function normalizeSwarmJobIds(jobIds: readonly string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of jobIds) {
    const id = String(raw || "").trim();
    if (!id || seen.has(id)) continue;
    seen.add(id);
    out.push(id);
  }
  out.sort();
  return out;
}

/** Stable key for a pending row; null when there are no usable job ids. */
export function swarmPendingIdentityKey(
  jobIds: readonly string[],
): string | null {
  const ids = normalizeSwarmJobIds(jobIds);
  return ids.length > 0 ? ids.join("\0") : null;
}

export function swarmPendingStatusOf(
  item: SwarmPendingItem,
): SwarmPendingStatus {
  if (item.status) return item.status;
  if (item.resolved) return "done";
  return "running";
}

export function isSwarmPendingTerminalStatus(
  status: SwarmPendingStatus,
): boolean {
  return status === "done" || status === "failed" || status === "ended";
}

export function isSwarmPendingTerminal(item: SwarmPendingItem): boolean {
  return isSwarmPendingTerminalStatus(swarmPendingStatusOf(item));
}

/** Higher = more advanced; never walk backward from terminal toward running. */
export function swarmPendingStatusRank(status: SwarmPendingStatus): number {
  switch (status) {
    case "failed":
      return 3;
    case "done":
      return 2;
    case "ended":
      return 1;
    default:
      return 0;
  }
}

/**
 * Merge two lifecycle rows that share an identity. Preserves the most advanced
 * terminal status and unions job / terminal id sets. Never resurrects running
 * over a terminal state.
 */
export function mergeSwarmPendingItems(
  prev: SwarmPendingItem,
  next: SwarmPendingItem,
): SwarmPendingItem {
  const prevStatus = swarmPendingStatusOf(prev);
  const nextStatus = swarmPendingStatusOf(next);
  const prevRank = swarmPendingStatusRank(prevStatus);
  const nextRank = swarmPendingStatusRank(nextStatus);
  const status = nextRank > prevRank ? nextStatus : prevStatus;
  const terminal = isSwarmPendingTerminalStatus(status);
  const primary = nextRank > prevRank ? next : prev;
  const secondary = primary === next ? prev : next;
  return {
    kind: "swarm_pending",
    job_ids: normalizeSwarmJobIds([...prev.job_ids, ...next.job_ids]),
    objective: primary.objective || secondary.objective || "",
    status,
    resolved: terminal,
    terminal_job_ids: normalizeSwarmJobIds([
      ...(prev.terminal_job_ids || []),
      ...(next.terminal_job_ids || []),
    ]),
  };
}

/**
 * Apply a replayed swarm_pending onto an existing row. Terminal rows stay
 * terminal; running rows absorb id/objective updates in place.
 */
export function mergeSwarmPendingReplay(
  existing: SwarmPendingItem,
  jobIds: readonly string[],
  objective: string,
): SwarmPendingItem {
  const incoming: SwarmPendingItem = {
    kind: "swarm_pending",
    job_ids: normalizeSwarmJobIds(jobIds),
    objective: objective || "",
    resolved: false,
    status: "running",
    terminal_job_ids: [],
  };
  return mergeSwarmPendingItems(existing, incoming);
}

/**
 * Index of the canonical lifecycle row for these job ids.
 * Objective is only a conservative single-id alias fallback (local-swarm ↔
 * substrate), never a broad collapse across distinct jobs that share a goal.
 */
export function findCanonicalSwarmPendingIndex(
  items: readonly { kind: string }[],
  jobIds: readonly string[],
  objective?: string,
  opts?: { allowObjectiveAlias?: boolean; requireNonTerminalAlias?: boolean },
): number {
  const key = swarmPendingIdentityKey(jobIds);
  let bestIdx = -1;
  let bestRank = -1;

  const consider = (idx: number, item: SwarmPendingItem) => {
    const rank = swarmPendingStatusRank(swarmPendingStatusOf(item));
    // Prefer more advanced status; on ties keep the earlier row (scroll-stable).
    if (bestIdx < 0 || rank > bestRank) {
      bestIdx = idx;
      bestRank = rank;
    }
  };

  if (key) {
    for (let i = 0; i < items.length; i++) {
      const it = items[i];
      if (it.kind !== "swarm_pending") continue;
      const pending = it as SwarmPendingItem;
      if (swarmPendingIdentityKey(pending.job_ids) === key) {
        consider(i, pending);
      }
    }
    if (bestIdx >= 0) return bestIdx;
  }

  const allowAlias = opts?.allowObjectiveAlias !== false;
  const obj = (objective || "").trim();
  const incomingIds = normalizeSwarmJobIds(jobIds);
  if (
    !allowAlias
    || !obj
    || incomingIds.length !== 1
  ) {
    return -1;
  }

  for (let i = 0; i < items.length; i++) {
    const it = items[i];
    if (it.kind !== "swarm_pending") continue;
    const pending = it as SwarmPendingItem;
    if (pending.job_ids.length !== 1) continue;
    if ((pending.objective || "").trim() !== obj) continue;
    if (
      opts?.requireNonTerminalAlias
      && isSwarmPendingTerminal(pending)
    ) {
      continue;
    }
    // Distinct concrete ids with the same goal stay distinct — only alias when
    // at least one side looks like a local-swarm placeholder, or ids already match.
    const existingId = pending.job_ids[0];
    const incomingId = incomingIds[0];
    if (
      existingId !== incomingId
      && !existingId.startsWith("local-swarm-")
      && !incomingId.startsWith("local-swarm-")
    ) {
      continue;
    }
    consider(i, pending);
  }
  return bestIdx;
}
