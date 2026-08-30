import type { Job } from "../../lib/api";
import type { AgentCommandSession } from "../../lib/agentCommandIndex";
import { isCommandJob, isTrackerJob } from "../../lib/jobClassification";
import { jobInActiveSession } from "../../lib/jobScope";

export type ComposerStatusStackKind = "swarm" | "terminal";
export type ComposerStatusStackState = "running" | "done" | "failed";

export type ComposerStatusStackRow = {
  id: string;
  kind: ComposerStatusStackKind;
  label: string;
  state: ComposerStatusStackState;
  updatedAt: number;
  title: string;
  command?: string;
  output?: string;
};

export type ComposerStatusStackSource = {
  commandSessions: readonly AgentCommandSession[];
  nowMs?: number;
  swarmJobs: readonly Job[];
  /** When set, Term/PM rows must belong to this chat. Unscoped leftovers drop. */
  sessionId?: string;
};

const SWARM_SUCCESS_LINGER_MS = 4_000;
const SWARM_FAILURE_LINGER_MS = 12_000;
const COMMAND_SUCCESS_LINGER_MS = 4_000;
const COMMAND_FAILURE_LINGER_MS = 12_000;

function timestampMs(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value > 1e12 ? value : value * 1000;
  }
  if (typeof value === "string" && value.trim()) {
    const parsed = Date.parse(value);
    return Number.isNaN(parsed) ? null : parsed;
  }
  return null;
}

function overlayExistingRow(
  existing: ComposerStatusStackRow,
  incoming: ComposerStatusStackRow,
  allowRunningOnDone = false,
): ComposerStatusStackRow {
  let state = existing.state;
  let updatedAt = existing.updatedAt;
  if (incoming.state === "failed" && existing.state !== "failed") {
    state = "failed";
    updatedAt = incoming.updatedAt;
  } else if (incoming.state === "done" && existing.state === "running") {
    state = "done";
    updatedAt = incoming.updatedAt;
  } else if (allowRunningOnDone && incoming.state === "running" && existing.state === "done") {
    state = "running";
    updatedAt = incoming.updatedAt;
  }
  return {
    ...existing,
    state,
    updatedAt,
    output: incoming.output || existing.output,
    command: incoming.command || existing.command,
    label: existing.label || incoming.label,
    title: existing.title || incoming.title,
  };
}

function normalizeState(
  status: unknown,
  emptyFallback: ComposerStatusStackState = "running",
): ComposerStatusStackState {
  const s = String(status || "").trim().toLowerCase();
  if (!s) return emptyFallback;
  if (
    s.includes("fail")
    || s.includes("error")
    || s.includes("stall")
    || s.includes("dead")
    || s.includes("interrupt")
    || s.includes("cancel")
    || s.includes("truncat")
    || /time(?:d)?[-_ ]?out/.test(s)
  ) {
    return "failed";
  }
  if (
    s.includes("run")
    || s.includes("progress")
    || s.includes("active")
    || s.includes("pend")
    || s.includes("queue")
  ) {
    return "running";
  }
  if (s.includes("complete") || s.includes("done") || s.includes("success") || s === "ok" || s.includes("finish")) {
    return "done";
  }
  return "running";
}

function oneLinePreview(text: string): string {
  return String(text || "").replace(/\s+/g, " ").trim();
}

function jobAccountingOwned(job: Job): boolean {
  if (job.accounting_owned === true) return true;
  if (job.accounting_owned === false) return false;
  return (job.source || "harness").toLowerCase() !== "cli";
}

function jobUpdatedAt(job: Job): number | null {
  return timestampMs(job.updated_at ?? job.created_at ?? null);
}

function lingerAllows(state: ComposerStatusStackState, updatedAt: number | null, nowMs: number, successMs: number, failureMs: number): boolean {
  if (state !== "running" && updatedAt == null) return false;
  if (state === "done" && updatedAt != null && nowMs - updatedAt > successMs) return false;
  if (state === "failed" && updatedAt != null && nowMs - updatedAt > failureMs) return false;
  return true;
}

/** Accounting-owned provider swarm only. Command jobs return null (reclassify as terminal). */
export function visibleSwarmJob(job: Job, nowMs: number): ComposerStatusStackRow | null {
  if (!jobAccountingOwned(job)) return null;
  if (!isTrackerJob(job)) return null;
  const state = normalizeState(job.status);
  const updatedAt = jobUpdatedAt(job);
  if (!lingerAllows(state, updatedAt, nowMs, SWARM_SUCCESS_LINGER_MS, SWARM_FAILURE_LINGER_MS)) return null;
  const id = String(job.id || "").trim();
  if (!id) return null;
  const label = String(job.goal || job.role || job.id || "").trim() || id;
  return {
    id,
    kind: "swarm",
    label,
    state,
    updatedAt: updatedAt ?? nowMs,
    title: label,
  };
}

/** Live command / command-batch rows from /api/swarm/live, painted as terminal. */
export function visibleCommandJob(job: Job, nowMs: number): ComposerStatusStackRow | null {
  if (!jobAccountingOwned(job)) return null;
  if (!isCommandJob(job)) return null;
  const state = normalizeState(job.status, "done");
  const updatedAt = jobUpdatedAt(job);
  if (!lingerAllows(state, updatedAt, nowMs, COMMAND_SUCCESS_LINGER_MS, COMMAND_FAILURE_LINGER_MS)) return null;
  const id = String(job.id || "").trim();
  if (!id) return null;
  const command = String(job.command_preview || job.goal || "").trim();
  const label = oneLinePreview(command) || String(job.role || job.id || "").trim() || id;
  const receipt = job.terminal_receipt;
  const output = receipt && typeof receipt.summary === "string" ? receipt.summary : undefined;
  return {
    id,
    kind: "terminal",
    label,
    state,
    updatedAt: updatedAt ?? nowMs,
    title: label,
    command: command || label,
    output,
  };
}

function commandSessionOverlay(session: AgentCommandSession): ComposerStatusStackRow | null {
  const id = String(session.id || "").trim();
  const command = String(session.command || "").trim();
  if (!id || !command) return null;
  return {
    id,
    kind: "terminal",
    label: oneLinePreview(command),
    state: session.state || "running",
    updatedAt: session.updatedAt,
    title: oneLinePreview(command),
    command,
    output: session.output,
  };
}

export function buildComposerStatusStackRows(
  source: ComposerStatusStackSource,
): ComposerStatusStackRow[] {
  const nowMs = source.nowMs ?? Date.now();
  const rows: ComposerStatusStackRow[] = [];
  const seen = new Set<string>();
  const sessionId = String(source.sessionId || "").trim();
  const sessionsById = new Map<string, AgentCommandSession>();
  for (const session of source.commandSessions) {
    if (sessionId && session.sessionId !== sessionId) continue;
    const id = String(session.id || "").trim();
    if (id) sessionsById.set(id, session);
  }

  // /api/swarm/live isCommandJob rows are the only Terminal authority.
  // Matching sessions overlay output/state; they never mint a row alone.
  for (const job of source.swarmJobs) {
    if (sessionId && !jobInActiveSession(job, sessionId)) continue;
    const id = String(job.id || "").trim();
    const row = visibleSwarmJob(job, nowMs) ?? visibleCommandJob(job, nowMs);
    if (!row) continue;
    if (id && seen.has(id)) {
      const idx = rows.findIndex((item) => item.id === id);
      if (idx >= 0) rows[idx] = overlayExistingRow(rows[idx], row);
      continue;
    }
    if (row.kind === "terminal" && id) {
      const overlay = sessionsById.get(id);
      const incoming = overlay ? commandSessionOverlay(overlay) : null;
      const blankCommandStatus = !String(job.status ?? "").trim();
      rows.push(incoming ? overlayExistingRow(row, incoming, blankCommandStatus) : row);
    } else {
      rows.push(row);
    }
    if (id) seen.add(id);
  }

  return rows.sort((a, b) => {
    if (a.kind !== b.kind) return a.kind === "swarm" ? -1 : 1;
    if (a.state !== b.state) {
      const order: Record<ComposerStatusStackState, number> = { running: 0, failed: 1, done: 2 };
      return order[a.state] - order[b.state];
    }
    return b.updatedAt - a.updatedAt;
  });
}
