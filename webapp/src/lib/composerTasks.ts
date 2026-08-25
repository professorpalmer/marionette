import type { Job, Task } from "./api";
import { jobInActiveSession } from "./jobScope";

export type ComposerTaskState = "pending" | "in_progress" | "completed" | "failed" | "degraded";

export type ComposerTask = {
  id: string;
  content: string;
  state: ComposerTaskState;
};

export function taskState(status: unknown): ComposerTaskState {
  const s = String(status || "").trim().toLowerCase();
  if (s.includes("fail") || s.includes("error") || s.includes("dead")) return "failed";
  if (s.includes("degrad")) return "degraded";
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

export function pickTaskSourceJob(jobs: readonly Job[], activeSessionId: string): Job | null {
  const scoped = jobs.filter((job) => jobInActiveSession(job, activeSessionId) && (job.tasks || []).length);
  if (!scoped.length) return null;
  return [...scoped].sort((a, b) => {
    const rank = jobRank(a) - jobRank(b);
    if (rank !== 0) return rank;
    return String(b.updated_at || b.created_at || "").localeCompare(String(a.updated_at || a.created_at || ""));
  })[0];
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
    return {
      id: String(task.id || `${job?.id || "job"}-${i}`),
      content: oneLineLabel(task),
      state,
    };
  });
}

export function taskProgress(tasks: readonly ComposerTask[]): { done: number; total: number } {
  return {
    done: tasks.filter((t) => t.state === "completed" || t.state === "degraded").length,
    total: tasks.length,
  };
}
