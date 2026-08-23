import type { Job } from "../../lib/api";
import type { AgentCommandSession } from "../../lib/agentCommandIndex";

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

function normalizeState(status: unknown): ComposerStatusStackState {
  const s = String(status || "").trim().toLowerCase();
  if (
    s.includes("fail")
    || s.includes("error")
    || s.includes("stall")
    || s.includes("dead")
    || s.includes("interrupt")
    || s.includes("cancel")
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

function jobAccountingOwned(job: Job): boolean {
  if (job.accounting_owned === true) return true;
  if (job.accounting_owned === false) return false;
  return (job.source || "harness").toLowerCase() !== "cli";
}

function jobUpdatedAt(job: Job): number | null {
  return timestampMs(job.updated_at ?? job.created_at ?? null);
}

function visibleSwarmJob(job: Job, nowMs: number): ComposerStatusStackRow | null {
  if (!jobAccountingOwned(job)) return null;
  const state = normalizeState(job.status);
  const updatedAt = jobUpdatedAt(job);
  if (state !== "running" && updatedAt == null) return null;
  if (state === "done" && updatedAt != null && nowMs - updatedAt > SWARM_SUCCESS_LINGER_MS) return null;
  if (state === "failed" && updatedAt != null && nowMs - updatedAt > SWARM_FAILURE_LINGER_MS) return null;
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

function visibleCommandSession(
  session: AgentCommandSession,
  nowMs: number,
): ComposerStatusStackRow | null {
  const id = String(session.id || "").trim();
  const command = String(session.command || "").trim();
  if (!id || !command) return null;
  const state = session.state || "running";
  if (state === "done" && nowMs - session.updatedAt > COMMAND_SUCCESS_LINGER_MS) return null;
  if (state === "failed" && nowMs - session.updatedAt > COMMAND_FAILURE_LINGER_MS) return null;
  return {
    id,
    kind: "terminal",
    label: command,
    state,
    updatedAt: session.updatedAt,
    title: command,
    command,
    output: session.output,
  };
}

export function buildComposerStatusStackRows(
  source: ComposerStatusStackSource,
): ComposerStatusStackRow[] {
  const nowMs = source.nowMs ?? Date.now();
  const rows: ComposerStatusStackRow[] = [];

  for (const job of source.swarmJobs) {
    const row = visibleSwarmJob(job, nowMs);
    if (row) rows.push(row);
  }

  for (const session of source.commandSessions) {
    const row = visibleCommandSession(session, nowMs);
    if (row) rows.push(row);
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
