import { useEffect } from "react";
import type { ExpertArtifact, ExpertTask } from "./expertMetadata";
import { canonicalExpertSelection, metadataSelectionKey, type MetadataDetail, type MetadataSelection } from "./jobMetadata";
import { currentExpert, useSharedJobMetadata } from "./jobMetadataContext";
import { displayModelId } from "./modelIdentity";
import type { JobMetadataState } from "./useJobMetadata";

/** One worker of a swarm job, as the inline transcript card shows it. */
export type SwarmWorkerRow = {
  taskId: string;
  role: string;
  model: string;
  status: string;
  tokens: number | null;
  lastAction: string;
};

const ACTION_MAX = 120;
const FINISHED = new Set(["complete", "completed", "done", "failed", "error", "cancelled", "canceled", "interrupted", "partial"]);

function oneLine(text: string): string {
  const line = text.split("\n").map((l) => l.trim()).find(Boolean) || "";
  return line.length > ACTION_MAX ? `${line.slice(0, ACTION_MAX - 1)}…` : line;
}

/**
 * The Puppetmaster job behind a transcript swarm id: a job id, the tool
 * call id that started it, or the local id of a synchronous swarm
 * (`local-swarm-<call id>`), which the local tracker maps to its job.
 * Only a job of the store's open view counts: during an owner switch the
 * old owner's rows are still in the state.
 */
export function resolveSwarmSelection(
  state: Pick<JobMetadataState, "view" | "local" | "observations">,
  swarmKey: string,
): MetadataSelection | null {
  if (!swarmKey || state.view.kind !== "view") return null;
  const { repo, session_id: sessionId } = state.view.context;
  const owned = (selection: MetadataSelection) => selection.repo === repo && selection.session_id === sessionId;
  for (const { row } of state.local.observations) {
    const canonical = row.canonical;
    if (!canonical) continue;
    if (row.local_ref.job_id === swarmKey || canonical.dispatch_id === swarmKey || canonical.job_ref.job_id === swarmKey) {
      const selection = canonicalExpertSelection(canonical, repo);
      return owned(selection) ? selection : null;
    }
  }
  const listed = state.observations.find(({ row }) => row.selection.job_ref.job_id === swarmKey && owned(row.selection));
  return listed ? listed.row.selection : null;
}

/**
 * The newest thing the worker did that the job record carries: its
 * failure, else its newest non-routing artifact, else (while it runs) its
 * task. The record has no per-step tool rows for Puppetmaster workers.
 */
export function workerLastAction(task: ExpertTask, status: string, artifacts: readonly ExpertArtifact[]): string {
  const own = artifacts.filter((a) => a.task_id === task.id && a.type.toLowerCase() !== "routing");
  const failure = own.map((a) => a.failure).filter(Boolean).pop();
  if (failure) return oneLine(failure);
  const newest = own[own.length - 1];
  if (newest?.headline) return oneLine(newest.headline);
  return FINISHED.has(status.toLowerCase()) ? "" : oneLine(task.instruction);
}

function taskTokens(task: ExpertTask): number | null {
  const { tokens, tokens_in, tokens_out } = task.usage;
  const total = typeof tokens === "number" ? tokens : (tokens_in ?? 0) + (tokens_out ?? 0);
  return total > 0 ? total : null;
}

export function swarmWorkerRows(
  tasks: readonly ExpertTask[],
  artifacts: readonly ExpertArtifact[],
  detail: MetadataDetail | null,
): SwarmWorkerRow[] {
  return tasks.map((task) => {
    const status = detail?.tasks.rows.find((row) => row.id === task.id)?.status || "unknown";
    return {
      taskId: task.id,
      role: task.role || "worker",
      // The Swarm Tracker's name for the model, so both surfaces agree.
      model: task.model ? displayModelId(task.model) : "",
      status,
      tokens: taskTokens(task),
      lastAction: workerLastAction(task, status, artifacts),
    };
  });
}

export type SwarmWorkers = { jobId: string; rows: SwarmWorkerRow[] };

/**
 * Workers of the swarm a transcript row names, from the shared job
 * metadata store (no legacy job reads). The detail is read into the
 * store's cache without taking the Jobs pane's selection. A running swarm
 * reads it again on the store's detail cadence.
 */
export function useSwarmWorkers(swarmKey: string, running: boolean): SwarmWorkers | null {
  const { store, state } = useSharedJobMetadata();
  const selection = resolveSwarmSelection(state, swarmKey);
  const key = selection ? metadataSelectionKey(selection) : "";
  const cached = !key ? undefined
    : state.detail.kind === "selected" && metadataSelectionKey(state.detail.selection) === key ? state.detail : state.detailCache[key];
  const detail = cached?.observation ?? null;

  // The store reads the detail in its own detail slot: a read from here
  // could take the one read slot from a view or owner switch.
  useEffect(() => {
    if (!selection) return;
    return store.wantDetail(selection, running);
    // The key names the selection; the object is rebuilt each render.
  }, [key, running, store]);

  const expert = key ? currentExpert(state, key) : undefined;
  if (!selection || !expert) return null;
  return { jobId: selection.job_ref.job_id, rows: swarmWorkerRows(expert.tasks, expert.artifacts, detail) };
}
