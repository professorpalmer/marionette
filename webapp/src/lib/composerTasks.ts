import type { Job, Task } from "./api";
import { isWaveCoordinator } from "./jobClassification";
import { jobInActiveSession } from "./jobScope";

export type ComposerTaskState = "pending" | "in_progress" | "completed" | "failed" | "degraded";

export type ComposerTask = {
  id: string;
  content: string;
  state: ComposerTaskState;
  detail?: string;
};

export function taskState(status: unknown): ComposerTaskState {
  const s = String(status || "").trim().toLowerCase();
  if (s.includes("fail") || s.includes("error") || s.includes("dead")) return "failed";
  if (s.includes("timeout") || s.includes("timed_out") || s.includes("cancel") || s.includes("truncat")) {
    return "failed";
  }
  if (s.includes("partial") || s.includes("degrad")) return "degraded";
  if (s.includes("complete") || s.includes("done") || s === "ok" || s.includes("success")) return "completed";
  if (s.includes("run") || s.includes("progress") || s.includes("active")) return "in_progress";
  return "pending";
}

function jobRank(job: Job): number {
  const st = taskState(job.status);
  if (st === "in_progress") return 0;
  if (st === "failed") return 1;
  if (st === "completed") return 2;
  return 3;
}

function composerJobIsLive(job: Job): boolean {
  const st = taskState(job.status);
  if (st === "in_progress" || st === "pending") return true;
  return (job.tasks || []).some((task) => {
    const ts = taskState(task.status);
    return ts === "in_progress" || ts === "pending";
  });
}

function pickRankedJob(pool: readonly Job[]): Job {
  return [...pool].sort((a, b) => {
    const rank = jobRank(a) - jobRank(b);
    if (rank !== 0) return rank;
    return String(b.updated_at || b.created_at || "").localeCompare(String(a.updated_at || a.created_at || ""));
  })[0];
}

export function pickTaskSourceJob(jobs: readonly Job[], activeSessionId: string): Job | null {
  const inSession = jobs.filter((job) => jobInActiveSession(job, activeSessionId));
  const remaining = inSession.filter(
    (job) => (job.tasks || []).length && composerTasksRemainVisible(buildComposerTasks(job)),
  );
  if (!remaining.length) return null;
  const liveRemaining = remaining.filter(composerJobIsLive);
  const liveWaves = liveRemaining.filter((job) => isWaveCoordinator(job));
  if (liveWaves.length) return pickRankedJob(liveWaves);
  if (liveRemaining.length) return pickRankedJob(liveRemaining);
  if (inSession.some((job) => composerJobIsLive(job) && !isWaveCoordinator(job))) {
    return null;
  }
  const waves = remaining.filter((job) => isWaveCoordinator(job));
  return pickRankedJob(waves.length ? waves : remaining);
}

function oneLineLabel(task: Task): string {
  const role = String(task.role || "").replace(/\s+/g, " ").trim();
  const instruction = String(task.instruction || "").replace(/\s+/g, " ").trim();
  if (role && instruction && !instruction.toLowerCase().startsWith(role.toLowerCase())) {
    return `${role} · ${instruction}`;
  }
  return instruction || role || String(task.id || "Task");
}

function artifactLooksFailed(art: { result?: unknown; type?: unknown; failure?: unknown; task_id?: unknown }): boolean {
  const result = String(art.result || "").trim().toLowerCase();
  const kind = String(art.type || "").trim().toLowerCase();
  return !!art.failure
    || result === "failed"
    || result === "blocked"
    || result === "error"
    || result === "degraded"
    || kind === "error";
}

export function buildComposerTasks(job: Job | null): ComposerTask[] {
  const arts = Array.isArray(job?.artifacts) ? job.artifacts as { result?: unknown; type?: unknown; failure?: unknown; task_id?: unknown }[] : [];
  return (job?.tasks || []).map((task: Task, i) => {
    let state = taskState(task.status);
    if (state === "completed") {
      const fail = arts.find((a) => String(a.task_id || "") === String(task.id || "") && artifactLooksFailed(a));
      if (fail) state = "degraded";
    }
    const detail = waveChildDetail(task);
    return {
      id: String(task.id || `${job?.id || "job"}-${i}`),
      content: oneLineLabel(task),
      state,
      ...(detail ? { detail } : {}),
    };
  });
}

export function composerTasksRemainVisible(tasks: readonly ComposerTask[]): boolean {
  return tasks.some((task) => task.state !== "completed");
}

export function taskProgress(tasks: readonly ComposerTask[]): { done: number; total: number } {
  return {
    done: tasks.filter((t) => t.state === "completed" || t.state === "degraded").length,
    total: tasks.length,
  };
}

export function waveProgress(job: Job | null): { completed: number; failed: number; applied: number; total: number } {
  const receiptChildren = job?.terminal_receipt?.children;
  const rows = Array.isArray(receiptChildren) && receiptChildren.length
    ? receiptChildren
    : (job?.tasks || []);
  const total = Number(job?.child_count) || Number(job?.task_count) || rows.length;
  let completed = 0;
  let failed = 0;
  let applied = 0;
  for (const row of rows) {
    const st = taskState(row.status);
    if (st === "completed") completed += 1;
    else if (st === "failed") failed += 1;
    if (row.applied) applied += 1;
  }
  return { completed, failed, applied, total };
}

function waveChildDetail(task: Task): string {
  const stage = String(task.failure_stage || "").trim();
  const reason = String(task.failure_reason || task.error || "").trim();
  if (stage && reason && reason.toLowerCase() !== stage.toLowerCase()) {
    return `${stage}: ${reason}`;
  }
  return reason || stage;
}

export function waveHeaderText(job: Job): string {
  const { completed, failed, applied, total } = waveProgress(job);
  const status = String(job.status || "running").trim().toLowerCase().replace(/_/g, " ");
  let text = `Parallel wave — ${status} ${completed}/${total} completed`;
  if (failed > 0) text += ` · ${failed} failed`;
  if (applied > 0) text += ` · ${applied} patch${applied === 1 ? "" : "es"} applied`;
  if (job.review_required || job.terminal_receipt?.review_required) {
    text += " · review required";
  }
  return text;
}
